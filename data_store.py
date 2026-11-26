"""
SQLite 数据层 —— 历史回测数据 + 实时策略数据 统一存储

表设计:
  daily_bar     日线行情 (回测核心数据, 主键 symbol+date)
  news          新闻/资讯 (策略运行的情绪面数据)
  strategy_run  策略运行记录 (每次回测/选股的参数与绩效)
  pick_record   选股记录 (每次选出的票, 用于跟踪胜率)
  download_log  下载日志 (数据覆盖范围管理)
"""
import os
import time
import sqlite3
import random
import threading
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "stock.db")
CSV_DIR = os.path.join(BASE_DIR, "cache")

os.makedirs(DATA_DIR, exist_ok=True)


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_conn()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS daily_bar (
        symbol            TEXT NOT NULL,
        date              TEXT NOT NULL,
        open REAL, high REAL, low REAL, close REAL,
        volume REAL, amount REAL,
        outstanding_share REAL, turnover REAL,
        PRIMARY KEY (symbol, date)
    );
    CREATE INDEX IF NOT EXISTS idx_bar_date ON daily_bar(date);

    CREATE TABLE IF NOT EXISTS news (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT, source TEXT, symbol TEXT,
        title TEXT, content TEXT, sentiment REAL,
        title_hash TEXT,         -- 标题hash, 用于多源去重
        source_time TEXT,        -- 各源自身发布时间, 用于保留最早来源
        link TEXT                -- 原文链接
    );
    CREATE INDEX IF NOT EXISTS idx_news_date ON news(date);

    CREATE TABLE IF NOT EXISTS strategy_run (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_time TEXT, kind TEXT,          -- kind: backtest / pick
        strategy TEXT, params TEXT,        -- JSON
        metrics TEXT                       -- JSON 绩效
    );

    CREATE TABLE IF NOT EXISTS pick_record (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER, symbol TEXT, pick_date TEXT,
        close REAL, ret_20 REAL, cap_yi REAL, turnover_pct REAL,
        FOREIGN KEY(run_id) REFERENCES strategy_run(id)
    );

    CREATE TABLE IF NOT EXISTS download_log (
        symbol TEXT PRIMARY KEY,
        status TEXT, rows INTEGER,
        first_date TEXT, last_date TEXT, updated_at TEXT
    );

    CREATE TABLE IF NOT EXISTS backtest_cache (
        cache_key TEXT PRIMARY KEY,
        strategy TEXT, params TEXT,
        fingerprint TEXT,             -- 数据指纹(股票数+日期范围), 数据更新后自动失效
        payload TEXT,                 -- JSON: metrics + curve + calendar
        created_at TEXT
    );

    -- ===== 回测结果 (按策略+参数指纹保存, 参数不变时页面直接回显) =====
    CREATE TABLE IF NOT EXISTS backtest_result (
        strategy  TEXT NOT NULL,
        param_sig TEXT NOT NULL,      -- 参数签名 (含初始资金/区间/成本/策略参数)
        params    TEXT,               -- JSON: 参数明细
        fingerprint TEXT,             -- 运行时的数据指纹
        payload   TEXT,               -- JSON: curve/trades/fills/holdings/performance
        created_at TEXT,
        PRIMARY KEY (strategy, param_sig)
    );

    -- ===== 数据覆盖索引: 每个标的已同步到哪个区间 (只补缺口) =====
    CREATE TABLE IF NOT EXISTS data_coverage (
        dataset    TEXT NOT NULL,     -- daily_bar / fundamental / realtime
        symbol     TEXT NOT NULL,
        start_date TEXT,
        end_date   TEXT,
        rows       INTEGER DEFAULT 0,
        updated_at TEXT,
        PRIMARY KEY (dataset, symbol)
    );

    -- ===== 财务数据 (季报) =====
    CREATE TABLE IF NOT EXISTS fundamental (
        symbol       TEXT NOT NULL,
        report_date  TEXT NOT NULL,   -- 报告期 20250930
        ann_date     TEXT,            -- 公告日
        eps          REAL,
        bps          REAL,            -- 每股净资产
        revenue      REAL,            -- 营业收入
        net_profit   REAL,            -- 归母净利润
        revenue_yoy  REAL,            -- 营收同比 %
        profit_yoy   REAL,            -- 净利同比 %
        roe          REAL,            -- 净资产收益率 %
        gross_margin REAL,            -- 销售毛利率 %
        debt_ratio   REAL,            -- 资产负债率 %
        goodwill     REAL,            -- 商誉
        goodwill_ratio REAL,          -- 商誉/净资产 %
        pe_ttm       REAL,
        pb           REAL,
        total_mv     REAL,            -- 总市值 (元)
        industry     TEXT,
        updated_at   TEXT,
        PRIMARY KEY (symbol, report_date)
    );
    CREATE INDEX IF NOT EXISTS idx_fund_report ON fundamental(report_date);

    -- ===== 实时行情快照 (模拟交易用) =====
    CREATE TABLE IF NOT EXISTS realtime_quote (
        symbol    TEXT NOT NULL,
        ts        TEXT NOT NULL,      -- YYYY-MM-DD HH:MM:SS
        price     REAL, pct REAL,
        open REAL, high REAL, low REAL, pre_close REAL,
        volume    REAL, amount REAL, turnover REAL,
        PRIMARY KEY (symbol, ts)
    );
    CREATE INDEX IF NOT EXISTS idx_rt_symbol ON realtime_quote(symbol, ts);

    -- ===== 策略驱动的数据任务队列 =====
    CREATE TABLE IF NOT EXISTS data_task (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        dataset   TEXT,               -- daily_bar / fundamental / realtime
        params    TEXT,               -- JSON: symbols/start/end
        status    TEXT,               -- pending / running / done / failed
        progress  TEXT,
        message   TEXT,
        created_at TEXT,
        finished_at TEXT
    );
    """)
    # daily_bar 兼容升级: 加 asset_type 列 (stock/etf)
    try:
        conn.execute("ALTER TABLE daily_bar ADD COLUMN asset_type TEXT DEFAULT 'stock'")
    except Exception:
        pass  # 列已存在
    # news 兼容升级: 加 title_hash + source_time 列 (多源去重)
    for col, default in [("title_hash", ""), ("source_time", "")]:
        try:
            conn.execute(f"ALTER TABLE news ADD COLUMN {col} TEXT DEFAULT '{default}'")
        except Exception:
            pass
    conn.execute("CREATE INDEX IF NOT EXISTS idx_news_hash ON news(title_hash)")
    # fundamental 兼容升级: 加 bps 列
    try:
        conn.execute("ALTER TABLE fundamental ADD COLUMN bps REAL")
    except Exception:
        pass
    # fundamental 兼容升级: 不良标的识别字段 (每股经营现金流/营业利润/ST标记)
    for col, typ in (("ocf_per_share", "REAL"), ("operating_profit", "REAL"),
                     ("is_st", "INTEGER")):
        try:
            conn.execute(f"ALTER TABLE fundamental ADD COLUMN {col} {typ}")
        except Exception:
            pass
    # ===== 分红送配记录 (长期不分红识别) =====
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dividend (
            symbol       TEXT NOT NULL,
            report_date  TEXT NOT NULL,   -- 报告期 20241231 (财年口径)
            cash_div_ps  REAL,            -- 每股现金分红 (元)
            ann_date     TEXT,            -- 公告日 (预案/实施最早公告)
            updated_at   TEXT,
            PRIMARY KEY (symbol, report_date)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_div_ann ON dividend(ann_date)")
    # 应用配置 KV 表 (代理 / 新闻源开关 等)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key   TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT
        )
    """)
    # 升级: 旧库补 link 列
    try:
        conn.execute("ALTER TABLE news ADD COLUMN link TEXT")
    except Exception:
        pass

    # ===== 华泰柏瑞 ETF 增强数据 (htbr_source 数据源) =====
    # 单位统一: 金额一律「元」, 份额一律「份」; 取数时 unit=3(万元/万份) 后 ×1e4
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS etf_meta (
        symbol      TEXT PRIMARY KEY,   -- sh510300
        secu_code   TEXT,               -- 510300
        inner_code  TEXT,               -- 基金内码 (fund-daily-quote.fundobject 回填)
        fund_name   TEXT,
        updated_at  TEXT
    );

    CREATE TABLE IF NOT EXISTS etf_quote_ext (
        symbol          TEXT NOT NULL,
        date            TEXT NOT NULL,
        turnover_value  REAL,           -- 成交额(元)
        turnover_volume REAL,           -- 成交量(股)
        turnover_rate   REAL,           -- 换手率(%)
        avg_price       REAL,
        change_pct      REAL,
        range_pct       REAL,
        discount_ratio  REAL,           -- 贴水率(%)
        premium_ratio   REAL,           -- 溢价率(%)
        is_suspend      INTEGER,
        updated_at      TEXT,
        PRIMARY KEY (symbol, date)
    );

    CREATE TABLE IF NOT EXISTS etf_capital (
        symbol          TEXT NOT NULL,
        date            TEXT NOT NULL,
        unit_nv         REAL,           -- 单位净值(元)
        close_price     REAL,
        exchange_shares REAL,           -- 场内份额(份)
        market_scale    REAL,           -- 规模(元)
        net_sub_shares  REAL,           -- 净申购份额(份)
        finance_net_buy REAL,           -- 融资净买入(元)
        finance_balance REAL,           -- 融资余额(元)
        near3_netbuy    REAL,
        near5_netbuy    REAL,
        near20_netbuy   REAL,
        near60_netbuy   REAL,
        updated_at      TEXT,
        PRIMARY KEY (symbol, date)
    );

    CREATE TABLE IF NOT EXISTS etf_margin (
        symbol              TEXT NOT NULL,
        date                TEXT NOT NULL,
        finance_buy         REAL,       -- 融资买入额(元)
        finance_refund      REAL,       -- 融资偿还额(元)
        finance_net_buy     REAL,       -- 融资净买入额(元)
        security_sell_vol   REAL,       -- 融券卖出量
        security_refund_vol REAL,       -- 融券偿还量
        security_net_sell_vol REAL,     -- 融券净卖出量
        finance_balance     REAL,       -- 期末融资余额(元)
        security_balance    REAL,       -- 期末融券余额
        security_volume     REAL,       -- 期末融券余量
        finance_pct         REAL,       -- 融资余额占流通市值(%)
        security_pct        REAL,       -- 融券余额占流通市值(%)
        updated_at          TEXT,
        PRIMARY KEY (symbol, date)
    );

    CREATE TABLE IF NOT EXISTS etf_share_daily (
        symbol     TEXT NOT NULL,
        date       TEXT NOT NULL,
        shares     REAL,           -- 场内份额(份), 上交所官方 (深市暂无)
        nav        REAL,           -- 单位净值(元), 东财
        updated_at TEXT,
        PRIMARY KEY (symbol, date)
    );

    CREATE TABLE IF NOT EXISTS etf_market_flow (
        date       TEXT NOT NULL,
        category   TEXT NOT NULL,       -- all/stock/bond/commodity/monetary/qdii
        buy_value  REAL,
        sell_value REAL,
        net_value  REAL,
        etf_count  INTEGER,
        updated_at TEXT,
        PRIMARY KEY (date, category)
    );

    CREATE INDEX IF NOT EXISTS idx_quote_ext_date  ON etf_quote_ext(date);
    CREATE INDEX IF NOT EXISTS idx_capital_date    ON etf_capital(date);
    CREATE INDEX IF NOT EXISTS idx_share_date      ON etf_share_daily(date);
    CREATE INDEX IF NOT EXISTS idx_margin_date     ON etf_margin(date);

    CREATE TABLE IF NOT EXISTS etf_valuation (
        symbol        TEXT NOT NULL,
        report_date   TEXT NOT NULL,
        total_mv      REAL,             -- 持仓总市值(亿元, 原始口径)
        negotiable_mv REAL,
        pe_ttm        REAL,
        pb            REAL,
        dividend_ratio REAL,
        updated_at    TEXT,
        PRIMARY KEY (symbol, report_date)
    );

    -- ETF 分红记录 (后复权用): 新浪 fund_etf_dividend_sina 的「日期 + 累计分红」
    CREATE TABLE IF NOT EXISTS etf_dividend (
        symbol      TEXT NOT NULL,
        date        TEXT NOT NULL,      -- 除息日 YYYY-MM-DD
        cum_div     REAL,               -- 累计分红(元/份, 自成立以来)
        updated_at  TEXT,
        PRIMARY KEY (symbol, date)
    );
    -- 分红同步进度: 无分红的 ETF 也要留痕, 否则每天重复请求
    CREATE TABLE IF NOT EXISTS etf_dividend_sync (
        symbol      TEXT PRIMARY KEY,
        events      INTEGER,
        updated_at  TEXT
    );

    -- ETF 名称 (选品池按名称识别「跨境/港股」)
    CREATE TABLE IF NOT EXISTS etf_name (
        symbol      TEXT PRIMARY KEY,
        name        TEXT,
        updated_at  TEXT
    );
    """)
    conn.commit()
    conn.close()


# ==================== 应用配置 (代理 / 新闻源) ====================

def get_setting(key: str, default=None) -> str | None:
    conn = get_conn()
    row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row[0] if row else default


def set_setting(key: str, value: str) -> None:
    from datetime import datetime
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO app_settings (key, value, updated_at) VALUES (?,?,?)",
        (key, value, datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    conn.close()


def get_all_settings() -> dict:
    conn = get_conn()
    rows = conn.execute("SELECT key, value FROM app_settings").fetchall()
    conn.close()
    return dict(rows)


# ==================== 行情写入/读取 ====================

def upsert_bars(symbol: str, df: pd.DataFrame, asset_type: str = "stock"):
    """批量写入日线 (INSERT OR REPLACE)"""
    if df is None or df.empty:
        return 0
    df = df.copy()
    df["symbol"] = symbol
    df["asset_type"] = asset_type
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    cols = ["symbol", "date", "open", "high", "low", "close",
            "volume", "amount", "outstanding_share", "turnover", "asset_type"]
    for c in cols[2:]:
        if c not in df.columns:
            df[c] = np.nan
    rows = df[cols].replace({np.nan: None}).values.tolist()
    conn = get_conn()
    conn.executemany(
        f"INSERT OR REPLACE INTO daily_bar ({','.join(cols)}) "
        f"VALUES ({','.join(['?'] * len(cols))})", rows)
    conn.commit()
    n = len(rows)
    conn.close()
    log_download(symbol, "ok", n, rows[0][1], rows[-1][1])
    return n


def load_bars(symbol: str) -> pd.DataFrame | None:
    conn = get_conn()
    df = pd.read_sql(
        "SELECT date,open,high,low,close,volume,amount,outstanding_share,turnover "
        f"FROM daily_bar WHERE symbol='{symbol}' ORDER BY date", conn)
    conn.close()
    if df.empty:
        return None
    df["date"] = pd.to_datetime(df["date"])
    return df


def list_symbols() -> list[str]:
    conn = get_conn()
    out = [r[0] for r in conn.execute("SELECT DISTINCT symbol FROM daily_bar").fetchall()]
    conn.close()
    return out


_STATS_CACHE = {"ts": 0.0, "data": None}


def last_coverage_date(min_symbols: int = 1000) -> str | None:
    """全市场真正有数据的最后交易日

    MAX(date) 会被个别标的 (如指数或刚补了单根K线的股票) 抬高。
    回测终点/补全起点必须用「当日至少 min_symbols 只标的有K线」的日期,
    否则最后一天大量持仓取不到行情, 净值会假性塌陷。
    """
    conn = get_conn()
    row = conn.execute(
        "SELECT date FROM daily_bar GROUP BY date HAVING COUNT(*) >= ? "
        "ORDER BY date DESC LIMIT 1", (min_symbols,)).fetchone()
    conn.close()
    return row[0] if row else None


def db_stats(ttl: float = 30.0) -> dict:
    """库统计。带短 TTL 缓存: COUNT(*) 在 350 万行上每次 rerun 都扫一遍会很慢"""
    import time
    now = time.time()
    if _STATS_CACHE["data"] is not None and now - _STATS_CACHE["ts"] < ttl:
        return dict(_STATS_CACHE["data"])
    conn = get_conn()
    q = lambda sql: conn.execute(sql).fetchone()[0]
    stats = {
        "symbols": q("SELECT COUNT(DISTINCT symbol) FROM daily_bar"),
        "bars": q("SELECT COUNT(*) FROM daily_bar"),
        "first_date": q("SELECT MIN(date) FROM daily_bar") or "-",
        "last_date": q("SELECT MAX(date) FROM daily_bar") or "-",
        "runs": q("SELECT COUNT(*) FROM strategy_run"),
        "picks": q("SELECT COUNT(*) FROM pick_record"),
        "news": q("SELECT COUNT(*) FROM news"),
    }
    conn.close()
    # 全市场覆盖到的最后一天 (回测终点/增量补全起点都用它)
    stats["coverage_last_date"] = last_coverage_date() or stats["last_date"]
    _STATS_CACHE["ts"] = now
    _STATS_CACHE["data"] = stats
    return dict(stats)


def log_download(symbol, status, rows, first_date, last_date):
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO download_log VALUES (?,?,?,?,?,?)",
        (symbol, status, rows, first_date, last_date, datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    conn.close()


# ==================== 数据指纹 (缓存失效依据) ====================

_FP_CACHE = {"value": None, "ts": 0.0}


def data_fingerprint() -> str:
    """数据指纹: 股票数+K线总数+最早/最新日期。数据一变, 回测/因子缓存自动失效

    必须包含 bars 总数: 只补某一天的数据时 last_date 可能不变 (MAX(date) 早被抬高),
    仅靠日期会让因子缓存沿用旧行情。

    进程级短 TTL: Streamlit 每次交互都 rerun, 而 db_stats 在 350 万行上 COUNT(*)
    约需 0.3~0.4s; 数据写入会调 invalidate_stats() 主动清缓存, 平时 15s 内直接命中。
    """
    import time
    now = time.time()
    if _FP_CACHE["value"] is not None and now - _FP_CACHE["ts"] < 15.0:
        return _FP_CACHE["value"]
    s = db_stats()
    # 分红事件数影响 ETF 后复权因子, 名称影响跨境/港股分类, 都必须纳入指纹
    conn = get_conn()
    try:
        n_div = conn.execute("SELECT COUNT(*) FROM etf_dividend").fetchone()[0]
        n_name = conn.execute("SELECT COUNT(*) FROM etf_name").fetchone()[0]
    except sqlite3.OperationalError:
        n_div = n_name = 0
    conn.close()
    fp = (f"{s['symbols']}_{s['bars']}_{s['first_date']}_{s['last_date']}"
          f"_d{n_div}_n{n_name}")
    _FP_CACHE["value"] = fp
    _FP_CACHE["ts"] = now
    return fp


def invalidate_stats():
    """写数据之后调用, 让下一次 db_stats() 重新统计"""
    _STATS_CACHE["data"] = None
    _STATS_CACHE["ts"] = 0.0
    _FP_CACHE["value"] = None
    _FP_CACHE["ts"] = 0.0


# ==================== 回测结果缓存 (SQLite 持久化) ====================

def get_backtest_cache(cache_key: str) -> dict | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT payload FROM backtest_cache WHERE cache_key=?", (cache_key,)).fetchone()
    conn.close()
    if not row:
        return None
    import json as _json
    try:
        return _json.loads(row[0])
    except Exception:
        return None


def save_backtest_cache(cache_key: str, strategy: str, params: str,
                        fingerprint: str, payload: dict):
    import json as _json
    conn = get_conn()
    # 只保留当前数据指纹的缓存, 旧指纹缓存自动清理
    conn.execute("DELETE FROM backtest_cache WHERE fingerprint != ?", (fingerprint,))
    conn.execute(
        "INSERT OR REPLACE INTO backtest_cache "
        "(cache_key, strategy, params, fingerprint, payload, created_at) VALUES (?,?,?,?,?,?)",
        (cache_key, strategy, params, fingerprint,
         _json.dumps(payload, ensure_ascii=False, default=str),
         datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    conn.close()


# ==================== 回测结果 (参数不变时长期回显) ====================

def get_backtest_result(strategy: str, param_sig: str) -> dict | None:
    """按 (策略, 参数签名) 取最近一次回测结果 (含成交/持仓明细)"""
    return _query_backtest_result(
        "WHERE strategy=? AND param_sig=?", (strategy, param_sig))


def get_latest_backtest_result(strategy: str) -> dict | None:
    """取该策略最近一次回测结果 (任意参数), 用于参数变更后先回显旧结果"""
    return _query_backtest_result(
        "WHERE strategy=? ORDER BY created_at DESC LIMIT 1", (strategy,))


def _query_backtest_result(where: str, args: tuple) -> dict | None:
    import json as _json
    conn = get_conn()
    row = conn.execute(
        "SELECT payload, params, fingerprint, created_at, param_sig "
        f"FROM backtest_result {where}", args).fetchone()
    conn.close()
    if not row:
        return None
    try:
        payload = _json.loads(row[0])
    except Exception:
        return None
    try:
        params = _json.loads(row[1]) if row[1] else {}
    except Exception:
        params = {}
    return {"payload": payload, "params": params,
            "fingerprint": row[2] or "", "created_at": row[3] or "",
            "param_sig": row[4] or ""}


def save_backtest_result(strategy: str, param_sig: str, params: dict,
                         fingerprint: str, payload: dict):
    """保存回测结果; 同参数重复回测覆盖, 每个策略只保留最近 10 组参数"""
    import json as _json
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO backtest_result "
        "(strategy, param_sig, params, fingerprint, payload, created_at) VALUES (?,?,?,?,?,?)",
        (strategy, param_sig, _json.dumps(params, ensure_ascii=False, default=str),
         fingerprint, _json.dumps(payload, ensure_ascii=False, default=str),
         datetime.now().isoformat(timespec="seconds")))
    conn.execute(
        "DELETE FROM backtest_result WHERE strategy=? AND param_sig NOT IN "
        "(SELECT param_sig FROM backtest_result WHERE strategy=? "
        " ORDER BY created_at DESC LIMIT 10)", (strategy, strategy))
    conn.commit()
    conn.close()


# ==================== 下载 (akshare → SQLite) ====================

def get_main_board_codes(limit: int = None) -> list[tuple[str, str]]:
    """股票列表。limit=None 时返回沪深全部A股 (主板+创业板+科创板), 排除ST"""
    import akshare as ak
    all_stocks = ak.stock_info_a_code_name()
    mb = all_stocks[
        all_stocks["code"].str.startswith(("60", "00", "30", "688")) &
        ~all_stocks["name"].str.contains("ST|退", na=False)
    ].reset_index(drop=True)
    if limit:
        random.seed(42)
        mb = mb.sample(min(limit, len(mb)))
    return list(zip(mb["code"], mb["name"]))


_ETF_CACHE = {"ts": 0.0, "data": None}
_ETF_CACHE_TTL = 3600.0     # 清单缓存 1 小时, 避免页面/worker 反复请求


def _to_etf_symbol(code) -> str | None:
    """代码归一化为 sh510300 / sz159915, 非场内 ETF 返回 None"""
    s = str(code or "").strip().lower()
    if s[:2] in ("sh", "sz"):
        s = s[2:]
    if len(s) != 6 or not s.isdigit():
        return None
    if not s.startswith(("51", "56", "58", "15")):
        return None
    return ("sh" if s.startswith(("51", "56", "58")) else "sz") + s


def _etf_codes_em() -> list[tuple[str, str]]:
    """东财 ETF 快照 (首选源)"""
    import akshare as ak
    df = ak.fund_etf_spot_em()
    out = []
    for _, r in df.iterrows():
        sym = _to_etf_symbol(r.get("代码", ""))
        if sym:
            out.append((sym, str(r.get("名称", ""))))
    return out


def _etf_codes_sina() -> list[tuple[str, str]]:
    """新浪 ETF 分类行情 (备用源): 东财 push2 限流时仍可用"""
    import akshare as ak
    df = ak.fund_etf_category_sina(symbol="ETF基金")
    out = []
    for _, r in df.iterrows():
        sym = _to_etf_symbol(r.get("代码", ""))
        if sym:
            out.append((sym, str(r.get("名称", ""))))
    return out


def _etf_codes_from_db() -> list[tuple[str, str]]:
    """兜底: 库内已存在的 ETF (两个网络源都失败时至少保证增量补全不断档)"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT DISTINCT symbol FROM daily_bar WHERE asset_type='etf' ORDER BY symbol").fetchall()
    conn.close()
    return [(r[0], "") for r in rows]


def get_etf_codes(use_cache: bool = True) -> list[tuple[str, str]]:
    """全部场内 ETF 列表 [(sh510300, 名称)]

    东财 fund_etf_spot_em 实测会抛 Push2RateLimitError, 因此:
      1) 优先东财快照; 2) 失败退新浪分类行情; 3) 再失败退库内已知 ETF
    成功结果缓存 1 小时, 避免限流时反复重试。
    """
    now = time.time()
    if use_cache and _ETF_CACHE["data"] and now - _ETF_CACHE["ts"] < _ETF_CACHE_TTL:
        return list(_ETF_CACHE["data"])
    out = []
    for fetcher in (_etf_codes_em, _etf_codes_sina):
        try:
            out = fetcher()
        except Exception:
            out = []
        if out:
            break
    if not out:
        out = _etf_codes_from_db()
    if out:
        _ETF_CACHE.update(ts=now, data=out)
        return list(out)
    return list(_ETF_CACHE["data"] or [])


def etf_stats() -> dict:
    """ETF 数据覆盖概览 (数据管理页展示)"""
    conn = get_conn()
    row = conn.execute(
        "SELECT COUNT(DISTINCT symbol), COUNT(*), MIN(date), MAX(date) "
        "FROM daily_bar WHERE asset_type='etf'").fetchone()
    conn.close()
    return {"symbols": row[0] or 0, "bars": row[1] or 0,
            "first_date": row[2] or "-", "last_date": row[3] or "-"}


# ==================== ETF 名称 / 分红 (选品分类 + 后复权) ====================

def sync_etf_names(use_cache: bool = True) -> int:
    """同步 ETF 名称到库 (选品池按名称识别「跨境/港股」), 返回写入条数"""
    from datetime import datetime
    pairs = [(s, n) for s, n in get_etf_codes(use_cache=use_cache) if s and n]
    if not pairs:
        return 0
    ts = datetime.now().isoformat(timespec="seconds")
    conn = get_conn()
    conn.executemany(
        "INSERT OR REPLACE INTO etf_name (symbol, name, updated_at) VALUES (?,?,?)",
        [(s, n, ts) for s, n in pairs])
    conn.commit()
    conn.close()
    return len(pairs)


def etf_name_map() -> dict[str, str]:
    """{sh513100: '国泰纳斯达克100ETF'} — 缺失时返回空字典 (调用方需容错)"""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT symbol, name FROM etf_name WHERE name IS NOT NULL AND name != ''").fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    return {r[0]: r[1] for r in rows}


def sync_etf_dividends(symbols: list[str] | None = None, max_age_days: int = 30,
                       workers: int = 6, force: bool = False,
                       progress_cb=None) -> dict:
    """同步 ETF 分红记录 (后复权的除息事件), 返回 {synced, events, skipped}

    新浪 fund_etf_dividend_sina 给「日期 + 累计分红」; 无分红的 ETF 也写入同步表,
    避免每轮重复请求。max_age_days 内的记录视为新鲜, 不再重拉。
    """
    import akshare as ak
    from datetime import datetime
    if symbols is None:
        symbols = [s for s, _ in get_etf_codes()]
    symbols = [s for s in dict.fromkeys(symbols) if s]
    if not symbols:
        return {"synced": 0, "events": 0, "skipped": 0}

    fresh = {}
    if not force:
        cutoff = (datetime.now() - timedelta(days=max_age_days)).isoformat(timespec="seconds")
        conn = get_conn()
        try:
            fresh = {r[0] for r in conn.execute(
                "SELECT symbol FROM etf_dividend_sync WHERE updated_at >= ?", (cutoff,))}
        except sqlite3.OperationalError:
            fresh = set()
        conn.close()
    todo = [s for s in symbols if s not in fresh]
    if not todo:
        return {"synced": 0, "events": 0, "skipped": len(symbols)}

    def _one(sym):
        try:
            df = ak.fund_etf_dividend_sina(symbol=sym)
        except Exception:
            return sym, None
        if df is None or df.empty:
            return sym, []
        out = []
        for _, r in df.iterrows():
            try:
                out.append((pd.Timestamp(r["日期"]).strftime("%Y-%m-%d"),
                            float(r["累计分红"])))
            except Exception:
                continue
        return sym, out

    ts = datetime.now().isoformat(timespec="seconds")
    rows, sync_rows = [], []
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_one, s): s for s in todo}
        for fut in as_completed(futs):
            sym, evs = fut.result()
            done += 1
            if evs is None:            # 请求失败: 不留痕, 下轮重试
                if progress_cb:
                    progress_cb(done, len(todo), sym)
                continue
            rows.extend([(sym, d, v, ts) for d, v in evs])
            sync_rows.append((sym, len(evs), ts))
            if progress_cb:
                progress_cb(done, len(todo), sym)
    conn = get_conn()
    if rows:
        conn.executemany(
            "INSERT OR REPLACE INTO etf_dividend (symbol, date, cum_div, updated_at) "
            "VALUES (?,?,?,?)", rows)
    if sync_rows:
        conn.executemany(
            "INSERT OR REPLACE INTO etf_dividend_sync (symbol, events, updated_at) "
            "VALUES (?,?,?)", sync_rows)
    conn.commit()
    conn.close()
    invalidate_stats()
    return {"synced": len(sync_rows), "events": len(rows),
            "skipped": len(symbols) - len(todo)}


def etf_dividend_map() -> dict[str, list[tuple[str, float]]]:
    """{sh510500: [(date, cum_div), ...]} — 后复权用, 无记录返回空字典"""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT symbol, date, cum_div FROM etf_dividend "
            "WHERE cum_div IS NOT NULL ORDER BY symbol, date").fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    out: dict[str, list] = {}
    for sym, d, v in rows:
        out.setdefault(sym, []).append((str(d), float(v)))
    return out


def _upsert_rows(table: str, cols: list[str], rows: list[dict]) -> int:
    """通用批量写入 (INSERT OR REPLACE); rows 的键与 cols 同名, 缺省写 NULL"""
    if not rows:
        return 0
    now = datetime.now().isoformat(timespec="seconds")
    payload = []
    for r in rows:
        payload.append([r.get(c) for c in cols[:-1]] + [now])
    conn = get_conn()
    conn.executemany(
        f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) "
        f"VALUES ({','.join(['?'] * len(cols))})", payload)
    conn.commit()
    n = len(payload)
    conn.close()
    return n


def upsert_etf_meta(symbol: str, secu_code: str, inner_code: str | None,
                    fund_name: str = "") -> None:
    """记录 ETF 代码映射 (symbol ↔ 6位代码 ↔ 基金内码)"""
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO etf_meta (symbol, secu_code, inner_code, fund_name, updated_at) "
        "VALUES (?,?,?,?,?)",
        (symbol, secu_code, inner_code, fund_name,
         datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    conn.close()


def etf_meta_map() -> dict:
    """{symbol: inner_code} (仅包含已解析出内码的标的)"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT symbol, inner_code FROM etf_meta WHERE inner_code IS NOT NULL").fetchall()
    conn.close()
    return {r[0]: r[1] for r in rows}


def upsert_etf_quote_ext(symbol: str, rows: list[dict]) -> int:
    cols = ["symbol", "date", "turnover_value", "turnover_volume", "turnover_rate",
            "avg_price", "change_pct", "range_pct", "discount_ratio", "premium_ratio",
            "is_suspend", "updated_at"]
    for r in rows:
        r["symbol"] = symbol
    return _upsert_rows("etf_quote_ext", cols, rows)


def upsert_etf_capital(symbol: str, rows: list[dict]) -> int:
    cols = ["symbol", "date", "unit_nv", "close_price", "exchange_shares",
            "market_scale", "net_sub_shares", "finance_net_buy", "finance_balance",
            "near3_netbuy", "near5_netbuy", "near20_netbuy", "near60_netbuy", "updated_at"]
    for r in rows:
        r["symbol"] = symbol
    return _upsert_rows("etf_capital", cols, rows)


def upsert_etf_margin(symbol: str, rows: list[dict]) -> int:
    cols = ["symbol", "date", "finance_buy", "finance_refund", "finance_net_buy",
            "security_sell_vol", "security_refund_vol", "security_net_sell_vol",
            "finance_balance", "security_balance", "security_volume",
            "finance_pct", "security_pct", "updated_at"]
    for r in rows:
        r["symbol"] = symbol
    return _upsert_rows("etf_margin", cols, rows)


def upsert_etf_share_daily(rows: list[dict]) -> int:
    """写入官方份额 / 东财净值

    份额与净值来自两个接口, 同一天会分两次写入, 因此按 COALESCE 合并,
    不能用 INSERT OR REPLACE (会互相把对方的列清成 NULL)。
    """
    if not rows:
        return 0
    now = datetime.now().isoformat(timespec="seconds")
    payload = [(r.get("symbol"), r.get("date"), r.get("shares"), r.get("nav"), now)
               for r in rows if r.get("symbol") and r.get("date")]
    if not payload:
        return 0
    conn = get_conn()
    conn.executemany(
        "INSERT INTO etf_share_daily (symbol, date, shares, nav, updated_at) "
        "VALUES (?,?,?,?,?) ON CONFLICT(symbol, date) DO UPDATE SET "
        "shares=COALESCE(excluded.shares, shares), "
        "nav=COALESCE(excluded.nav, nav), "
        "updated_at=excluded.updated_at", payload)
    conn.commit()
    n = len(payload)
    conn.close()
    return n


def etf_share_max_date() -> str | None:
    """已有份额数据的最后日期 (回补缺口判断)"""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT MAX(date) FROM etf_share_daily WHERE shares IS NOT NULL").fetchone()
    except sqlite3.OperationalError:
        row = (None,)
    conn.close()
    return row[0] if row else None


def etf_share_dates() -> set:
    """已有份额数据的交易日集合"""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT DISTINCT date FROM etf_share_daily WHERE shares IS NOT NULL").fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    return {r[0] for r in rows}


def etf_share_symbols() -> list[str]:
    """已有份额数据的标的 (净值回补用)"""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT DISTINCT symbol FROM etf_share_daily WHERE shares IS NOT NULL "
            "ORDER BY symbol").fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    return [r[0] for r in rows]


def upsert_etf_market_flow(rows: list[dict]) -> int:
    cols = ["date", "category", "buy_value", "sell_value", "net_value",
            "etf_count", "updated_at"]
    return _upsert_rows("etf_market_flow", cols, rows)


def upsert_etf_valuation(symbol: str, rows: list[dict]) -> int:
    cols = ["symbol", "report_date", "total_mv", "negotiable_mv", "pe_ttm", "pb",
            "dividend_ratio", "updated_at"]
    for r in rows:
        r["symbol"] = symbol
    return _upsert_rows("etf_valuation", cols, rows)


def etf_ext_stats() -> dict:
    """ETF 增强数据覆盖 (数据管理页展示)"""
    conn = get_conn()
    q = lambda s: conn.execute(s).fetchone()
    quote_n, quote_sym, quote_last = q(
        "SELECT COUNT(*), COUNT(DISTINCT symbol), MAX(date) FROM etf_quote_ext")
    cap_n, cap_sym, cap_last = q(
        "SELECT COUNT(*), COUNT(DISTINCT symbol), MAX(date) FROM etf_capital")
    mg_n, mg_last = q("SELECT COUNT(*), MAX(date) FROM etf_margin")
    fl_n, fl_last = q("SELECT COUNT(*), MAX(date) FROM etf_market_flow")
    sh_n, sh_sym, sh_last = q(
        "SELECT COUNT(*), COUNT(DISTINCT symbol), MAX(date) FROM etf_share_daily "
        "WHERE shares IS NOT NULL")
    nav_n, nav_last = q(
        "SELECT COUNT(*), MAX(date) FROM etf_share_daily WHERE nav IS NOT NULL")
    meta_n = q("SELECT COUNT(*) FROM etf_meta")[0]
    conn.close()
    return {"meta_symbols": meta_n or 0,
            "quote_bars": quote_n or 0, "quote_symbols": quote_sym or 0,
            "quote_last": quote_last or "-",
            "capital_bars": cap_n or 0, "capital_symbols": cap_sym or 0,
            "capital_last": cap_last or "-",
            "margin_bars": mg_n or 0, "margin_last": mg_last or "-",
            "flow_bars": fl_n or 0, "flow_last": fl_last or "-",
            "share_bars": sh_n or 0, "share_symbols": sh_sym or 0,
            "share_last": sh_last or "-",
            "nav_bars": nav_n or 0, "nav_last": nav_last or "-"}


def _has_year(table: str, symbol: str, year: int, min_rows: int = 200) -> bool:
    """该标的是否已有某年的完整年度数据 (回补缺口判断)

    规则: 行数 >= min_rows 视为完整; 当年数据从年初附近开始也视为完整,
    避免只同步了近几日就把整年跳过。
    """
    b, e = f"{year}-01-01", f"{year}-12-31"
    conn = get_conn()
    n, first = conn.execute(
        f"SELECT COUNT(*), MIN(date) FROM {table} WHERE symbol=? AND date BETWEEN ? AND ?",
        (symbol, b, e)).fetchone()
    conn.close()
    if (n or 0) >= min_rows:
        return True
    return bool(year == datetime.now().year and first and str(first)[5:10] <= "02-15")


def has_etf_quote_year(symbol: str, year: int) -> bool:
    return _has_year("etf_quote_ext", symbol, year)


def has_etf_capital_year(symbol: str, year: int) -> bool:
    return _has_year("etf_capital", symbol, year)


def _apply_official_flow(conn, flow: dict, window: int = 5) -> None:
    """用交易所官方份额覆盖资金流因子: 近 5 个交易日净申购 / 当前份额 × 100"""
    try:
        rows = conn.execute(
            "SELECT symbol, date, shares FROM etf_share_daily "
            "WHERE shares IS NOT NULL ORDER BY symbol, date").fetchall()
    except sqlite3.OperationalError:
        return
    hist = {}
    for sym, d, sh in rows:
        h = hist.setdefault(sym, [])
        if len(h) >= window:
            base = h[-window]
            if base and sh and sh > 0:
                flow.setdefault(d, {})[sym] = (float(sh) - float(base)) / float(sh) * 100.0
        h.append(sh)


def _apply_official_premium(conn, premium: dict) -> None:
    """官方份额 + 东财净值 → 本地算溢价率, 仅补华泰柏瑞缺失的日期

    单位净值口径与 IOPV 有差异, 且 QDII 净值滞后 T-1/T-2,
    因此只做兜底, 不覆盖已有数据。
    """
    try:
        rows = conn.execute(
            "SELECT s.symbol, s.date, b.close, s.nav FROM etf_share_daily s "
            "JOIN daily_bar b ON b.symbol = s.symbol AND b.date = s.date "
            "WHERE s.nav > 0 AND b.close > 0").fetchall()
    except sqlite3.OperationalError:
        return
    for sym, d, close, nav in rows:
        day = premium.setdefault(d, {})
        if sym not in day:
            day[sym] = (float(close) / float(nav) - 1.0) * 100.0


def load_etf_factor_maps() -> dict:
    """ETF 增强因子 (按日期索引, 供回测 as-of 查询)

    返回:
      premium: {date: {symbol: 溢价率%}}      — 华泰柏瑞实测值, 缺失时用「收盘价/净值」兜底
      flow:    {date: {symbol: 净申购强度%}}  — 近5日净申购 / 规模 × 100
      market:  {date: {"stock": 亿元, "all": 亿元}} — 全市场 ETF 当日净流入
    """
    conn = get_conn()
    premium, flow, market = {}, {}, {}
    try:
        for sym, d, v in conn.execute(
                "SELECT symbol, date, premium_ratio FROM etf_quote_ext "
                "WHERE premium_ratio IS NOT NULL"):
            premium.setdefault(d, {})[sym] = float(v)
        for sym, d, sub, scale in conn.execute(
                "SELECT symbol, date, near5_netbuy, market_scale FROM etf_capital "
                "WHERE near5_netbuy IS NOT NULL AND market_scale > 0"):
            flow.setdefault(d, {})[sym] = float(sub) / float(scale) * 100.0
        # 交易所官方份额覆盖面更广, 覆盖华泰柏瑞的净申购因子
        _apply_official_flow(conn, flow)
        _apply_official_premium(conn, premium)
        for d, cat, v in conn.execute(
                "SELECT date, category, net_value FROM etf_market_flow "
                "WHERE category IN ('all','stock') AND net_value IS NOT NULL"):
            market.setdefault(d, {})[cat] = float(v) / 1e8
    except sqlite3.OperationalError:
        # 旧库未建增强表: 返回空因子, 策略保持原有行为
        pass
    finally:
        conn.close()
    return {"premium": premium, "flow": flow, "market": market}


def _norm_day(d) -> str:
    """任意日期写法 -> YYYY-MM-DD"""
    s = str(d).strip().replace("-", "").replace("/", "")[:8]
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return str(d)


def download_etf_one(symbol: str, start: str, end: str) -> tuple[str, int]:
    """下载单只 ETF 日线并入库, 返回 (symbol, rows)
    注意: fund_etf_hist_sina 只接受 symbol, 返回全部历史, 需本地按区间裁剪
    """
    import akshare as ak
    try:
        df = ak.fund_etf_hist_sina(symbol=symbol)
        if df is None or df.empty:
            return symbol, 0
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])
        # 新浪 ETF 日线只有 volume (股), 没有成交额: 用 收盘价 × 成交量 折算,
        # 否则 ETF 策略的「近20日日均成交额」流动性门槛会因 amount 全空而全部被剔除
        if "amount" not in df.columns:
            df["amount"] = df["close"] * df["volume"]
        s = pd.Timestamp(_norm_day(start))
        e = pd.Timestamp(_norm_day(end))
        df = df[(df["date"] >= s) & (df["date"] <= e)]
        if df.empty:
            return symbol, 0
        n = upsert_bars(symbol, df, asset_type="etf")
        return symbol, n
    except Exception:
        return symbol, 0


def _ak_stock_daily(symbol: str, start: str, end: str, adjust: str = "qfq"):
    """个股日线 (兼容 akshare 改名)

    akshare 1.20.46 起 stock_zh_a_daily 更名为 stock_zh_a_daily_sina,
    旧名直接抛 AttributeError; 曾被 except 吞掉, 表现为"下载 5029 只 / 0 行"。
    """
    import akshare as ak
    fn = getattr(ak, "stock_zh_a_daily_sina", None) or getattr(ak, "stock_zh_a_daily", None)
    return fn(symbol=symbol, start_date=start, end_date=end, adjust=adjust)


_DL_ERR = {"n": 0}


def _note_dl_error(symbol: str, exc: Exception) -> None:
    """下载异常不能静默: 只记前 3 条, 避免全市场失败时刷屏"""
    import logging
    _DL_ERR["n"] += 1
    if _DL_ERR["n"] <= 3:
        logging.getLogger(__name__).warning("日线下载失败 %s: %s", symbol, exc)


def download_one(code: str, start: str, end: str) -> tuple[str, int]:
    """下载单只股票日线并入库, 返回 (symbol, rows)"""
    sina = f"sh{code}" if code.startswith("6") else f"sz{code}"
    s = _norm_day(start).replace("-", "")
    e = _norm_day(end).replace("-", "")
    try:
        df = _ak_stock_daily(sina, s, e)
        if df is None or df.empty:
            return sina, 0
        n = upsert_bars(sina, df)
        return sina, n
    except Exception as exc:
        _note_dl_error(sina, exc)
        return sina, 0


def download_batch(codes: list[str], start: str, end: str,
                   workers: int = 8, progress_cb=None, fetch_fn=None) -> dict:
    """并发下载多只, progress_cb(done, total, symbol)。fetch_fn 默认股票接口, ETF 传 download_etf_one"""
    total = len(codes)
    done, ok_rows = 0, 0
    if fetch_fn is None:
        fetch_fn = lambda c: download_one(c[2:] if c.startswith(("sh", "sz")) else c, start, end)
    def _run(c):
        try:
            return fetch_fn(c)
        except Exception:
            return c, 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_run, c): c for c in codes}
        for fut in as_completed(futs):
            symbol, n = fut.result()
            done += 1
            ok_rows += n
            if progress_cb:
                progress_cb(done, total, symbol, n)
    return {"done": done, "ok_rows": ok_rows}


# ==================== CSV 导入 (本地缓存迁移) ====================

def import_csv_cache() -> int:
    """把本地 cache/*.csv 导入 SQLite, 返回导入股票数"""
    if not os.path.isdir(CSV_DIR):
        return 0
    count = 0
    for fname in os.listdir(CSV_DIR):
        if not fname.endswith(".csv"):
            continue
        symbol = fname.replace(".csv", "")
        try:
            df = pd.read_csv(os.path.join(CSV_DIR, fname))
            df["date"] = pd.to_datetime(df["date"])
            upsert_bars(symbol, df)
            count += 1
        except Exception:
            pass
    return count


def update_all_bars(start: str, end: str, workers: int = 8, progress_cb=None) -> dict:
    """增量更新库内所有股票的K线 (从 last_date+1 到 end)"""
    symbols = list_symbols()
    if not symbols:
        return {"done": 0, "rows": 0}

    def _update_one(symbol):
        # 找该股票最后日期和资产类型, 从次日开始下载
        conn = get_conn()
        row = conn.execute(
            "SELECT MAX(date), MAX(asset_type) FROM daily_bar WHERE symbol=?", (symbol,)).fetchone()
        conn.close()
        last = row[0] if row and row[0] else start
        atype = row[1] if row and row[1] else "stock"
        import datetime as _dt
        s = (pd.Timestamp(last) + pd.Timedelta(days=1)).strftime("%Y%m%d")
        if pd.Timestamp(s) > pd.Timestamp(end):
            return symbol, 0
        try:
            if atype == "etf":
                # fund_etf_hist_sina 不支持 start_date/end_date, 统一走 download_etf_one
                # (内部拉全历史后本地裁剪, 金额由 收盘×成交量 补算)
                _, n = download_etf_one(symbol, s, end)
                return symbol, n
            df = _ak_stock_daily(symbol, s, end)
            if df is None or df.empty:
                return symbol, 0
            n = upsert_bars(symbol, df, asset_type=atype)
            return symbol, n
        except Exception as exc:
            _note_dl_error(symbol, exc)
            return symbol, 0

    done, total_rows = 0, 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_update_one, sym): sym for sym in symbols}
        for fut in as_completed(futs):
            sym, n = fut.result()
            done += 1
            total_rows += n
            if progress_cb:
                progress_cb(done, len(symbols), sym, n)
    return {"done": done, "rows": total_rows}


def load_all_bars() -> dict[str, pd.DataFrame]:
    """统一入口: SQLite 优先, 空则回退 CSV (并顺手导入)

    一次全表查询 + groupby 拆分, 避免对 5000 只标的发 5000 条 SQL。
    """
    init_db()
    conn = get_conn()
    df = pd.read_sql(
        "SELECT symbol,date,open,high,low,close,volume,amount,outstanding_share,turnover,asset_type "
        "FROM daily_bar ORDER BY symbol,date", conn)
    conn.close()

    if df.empty and os.path.isdir(CSV_DIR):
        import_csv_cache()
        conn = get_conn()
        df = pd.read_sql(
            "SELECT symbol,date,open,high,low,close,volume,amount,outstanding_share,turnover,asset_type "
            "FROM daily_bar ORDER BY symbol,date", conn)
        conn.close()

    if df.empty:
        return {}

    df["date"] = pd.to_datetime(df["date"])
    # 老数据兼容: 早期 ETF 由新浪源写入, amount 为空 (新浪只有 volume)。
    # 用 收盘×成交量 回填, 否则 ETF 流动性门槛会把全部老 ETF 剔除。
    etf_mask = df["asset_type"].fillna("") == "etf"
    miss = etf_mask & df["amount"].isna()
    if miss.any():
        df.loc[miss, "amount"] = df.loc[miss, "close"] * df.loc[miss, "volume"]
    df = df.drop(columns=["asset_type"])
    out = {}
    for sym, g in df.groupby("symbol", sort=False):
        out[sym] = g.drop(columns=["symbol"]).reset_index(drop=True)
    return out


# ==================== 因子缓存 (磁盘, 避免每次重启重算 80 秒) ====================
FEAT_CACHE_PATH = os.path.join(DATA_DIR, "feat_cache.pkl")
FEAT_CACHE_VERSION = 6      # 因子口径变更时 +1 (v3: ETF 份额折算复权; v4: max_dd_250 改单层rolling,
                            # v5: ETF 改后复权 (叠加分红除息事件); v6: 新增 ret_120/ret_250)

# 进程级 feat 常驻 memo: feat_cache.pkl 在 NAS 上 1.1GB / 6476 标的, 冷读约 157s。
# 若每次都重读磁盘, 用户首次点进计算页会卡 2~3 分钟。memo 让同进程内同指纹只加载一次,
# 配合 streamlit_app 启动时的后台预热线程, 把 157s 挪到开机阶段而非用户点击时。
# 锁 + Event: 并发调用只触发一次磁盘读, 其余线程 wait, 避免惊群 (thundering herd)。
# loading_fp 放在 dict 里: 函数内若直接给模块级变量赋值会触发 Python 局部变量绑定,
# 导致同函数内更早的读取抛 UnboundLocalError。dict 内的键赋值只是变更, 不触发该问题。
_feat_memo = {"fingerprint": None, "feat": None, "loading_fp": None}
_feat_memo_lock = threading.Lock()
_feat_memo_event = threading.Event()
_feat_memo_event.set()          # set = 当前无加载进行中 (memo 可用或为空)


def load_feat_cached(fingerprint: str = None) -> dict:
    """行情 + 因子。优先级: 进程 memo → 磁盘缓存 (指纹一致) → 重算并写回。

    全市场 350 万行: load_all_bars 约 30s + compute_factors 约 55s, 不能每次都算。
    feat_cache.pkl 在 NAS 上 1.1GB, 冷读约 157s, 所以进程内常驻 memo 是关键。
    """
    import pickle
    fingerprint = fingerprint or data_fingerprint()

    # 快路径: memo 已命中
    with _feat_memo_lock:
        if _feat_memo["fingerprint"] == fingerprint and _feat_memo["feat"] is not None:
            return _feat_memo["feat"]
        # 已有其他线程正在加载同一指纹 → 等它完成, 直接复用结果
        wait_for_other = (_feat_memo["loading_fp"] == fingerprint)

    if wait_for_other:
        _feat_memo_event.wait()          # 阻塞到加载完成 set()
        with _feat_memo_lock:
            if _feat_memo["fingerprint"] == fingerprint and _feat_memo["feat"] is not None:
                return _feat_memo["feat"]
        # 极端情况 (加载抛错 / 指纹又变): 落到下面自己加载

    # 抢到加载权
    with _feat_memo_lock:
        if _feat_memo["fingerprint"] == fingerprint and _feat_memo["feat"] is not None:
            return _feat_memo["feat"]
        _feat_memo["loading_fp"] = fingerprint
        _feat_memo_event.clear()

    feat = None
    try:
        p = FEAT_CACHE_PATH
        if os.path.exists(p):
            try:
                with open(p, "rb") as f:
                    d = pickle.load(f)
                if (d.get("fingerprint") == fingerprint and d.get("feat")
                        and d.get("version") == FEAT_CACHE_VERSION):
                    feat = d["feat"]
            except Exception:
                pass
        if feat is None:
            from backtest_engine import compute_factors   # 延迟导入, 避免循环依赖
            raw = load_all_bars()
            # ETF 后复权: 分红除息事件一并交给因子层 (缺失时只做份额折算复权)
            feat = compute_factors(raw, div_map=etf_dividend_map()) if raw else {}
            try:
                tmp = p + ".tmp"
                with open(tmp, "wb") as f:
                    pickle.dump({"fingerprint": fingerprint, "version": FEAT_CACHE_VERSION,
                                 "feat": feat}, f, protocol=5)
                os.replace(tmp, p)
            except Exception:
                pass
    finally:
        with _feat_memo_lock:
            _feat_memo["fingerprint"] = fingerprint
            _feat_memo["feat"] = feat
            _feat_memo["loading_fp"] = None
            _feat_memo_event.set()
    return feat


# ==================== 策略运行记录 ====================

import json

def save_run(kind: str, strategy: str, params: dict, metrics: dict) -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO strategy_run (run_time, kind, strategy, params, metrics) VALUES (?,?,?,?,?)",
        (datetime.now().isoformat(timespec="seconds"), kind, strategy,
         json.dumps(params, ensure_ascii=False, default=str),
         json.dumps(metrics, ensure_ascii=False, default=str)))
    conn.commit()
    run_id = cur.lastrowid
    conn.close()
    return run_id


def save_picks(run_id: int, picks: pd.DataFrame):
    if picks is None or picks.empty:
        return
    conn = get_conn()
    rows = [(run_id, r["sina_code"], r["date"], r["close"],
             r.get("ret_20"), r.get("cap_yi"), r.get("turnover_pct"))
            for _, r in picks.iterrows()]
    conn.executemany(
        "INSERT INTO pick_record (run_id,symbol,pick_date,close,ret_20,cap_yi,turnover_pct) "
        "VALUES (?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


def get_recent_picks(limit: int = 50) -> pd.DataFrame:
    """最近选股记录 (供投资组合页展示模拟持仓与历史)"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT pr.symbol, pr.pick_date, pr.close, pr.ret_20, pr.cap_yi, "
        "pr.turnover_pct, sr.strategy, sr.run_time "
        "FROM pick_record pr JOIN strategy_run sr ON sr.id = pr.run_id "
        "ORDER BY pr.pick_date DESC, sr.run_time DESC LIMIT ?",
        (limit,)).fetchall()
    conn.close()
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows, columns=[
        "symbol", "pick_date", "close", "ret_20", "cap_yi",
        "turnover_pct", "strategy", "run_time"])


def save_news(items: list[dict]):
    """items: [{date, source, symbol, title, content, sentiment, source_time}]
    多源去重: 按 title_hash, 保留 source_time 最早的来源 (避免垃圾转载)
    """
    if not items:
        return
    import hashlib
    conn = get_conn()

    # 第一步: 所有条目算 hash, 找出重复 hash 里 source_time 最早的那条
    best = {}   # hash -> item
    for it in items:
        title = (it.get("title") or "").strip()
        if not title:
            continue
        h = hashlib.md5(title.encode("utf-8")).hexdigest()
        it["title_hash"] = h
        st = it.get("source_time") or it.get("date") or "9999-99-99"
        it["source_time"] = st
        prev = best.get(h)
        if prev is None or st < prev.get("source_time", "9999-99-99"):
            best[h] = it

    # 第二步: 跟库内已有数据比较, 仍保留最早来源
    for h, it in best.items():
        row = conn.execute(
            "SELECT id, source_time FROM news WHERE title_hash=?", (h,)).fetchone()
        if row:
            if it["source_time"] < (row[1] or "9999-99-99"):
                conn.execute(
                    "UPDATE news SET source=?, symbol=?, title=?, content=?, date=?, source_time=?, link=? WHERE id=?",
                    (it["source"], it.get("symbol"), it.get("title"),
                     it.get("content"), it.get("date"), it["source_time"],
                     it.get("link"), row[0]))
        else:
            conn.execute(
                "INSERT INTO news (date,source,symbol,title,content,sentiment,title_hash,source_time,link) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (it.get("date"), it["source"], it.get("symbol"),
                 it.get("title"), it.get("content"), it.get("sentiment"),
                 h, it["source_time"], it.get("link")))
    conn.commit()
    conn.close()


def get_news_detail(news_id: int) -> dict | None:
    """读取单条新闻详情"""
    conn = get_conn()
    row = conn.execute(
        "SELECT id, date, source, symbol, title, content, source_time, link FROM news WHERE id=?",
        (news_id,)).fetchone()
    conn.close()
    if not row:
        return None
    cols = ["id", "date", "source", "symbol", "title", "content", "source_time", "link"]
    return dict(zip(cols, row))


# ==================== 数据覆盖索引 (增量补全的核心) ====================

def coverage_get(dataset: str, symbol: str) -> tuple[str, str] | None:
    """返回 (start_date, end_date) 已覆盖区间, 无记录返回 None"""
    conn = get_conn()
    row = conn.execute(
        "SELECT start_date, end_date FROM data_coverage WHERE dataset=? AND symbol=?",
        (dataset, symbol)).fetchone()
    conn.close()
    return (row[0], row[1]) if row else None


def coverage_set(dataset: str, symbol: str, start: str, end: str, rows: int = 0):
    conn = get_conn()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        "SELECT start_date, end_date, rows FROM data_coverage WHERE dataset=? AND symbol=?",
        (dataset, symbol)).fetchone()
    if cur:
        s = min(filter(None, [cur[0], start])) if start else cur[0]
        e = max(filter(None, [cur[1], end])) if end else cur[1]
        conn.execute(
            "UPDATE data_coverage SET start_date=?, end_date=?, rows=rows+?, updated_at=? "
            "WHERE dataset=? AND symbol=?",
            (s, e, rows, now, dataset, symbol))
    else:
        conn.execute(
            "INSERT INTO data_coverage (dataset,symbol,start_date,end_date,rows,updated_at) "
            "VALUES (?,?,?,?,?,?)", (dataset, symbol, start, end, rows, now))
    conn.commit()
    conn.close()


def coverage_stats(dataset: str = "daily_bar") -> dict:
    conn = get_conn()
    row = conn.execute(
        "SELECT COUNT(*), MIN(start_date), MAX(end_date), SUM(rows) "
        "FROM data_coverage WHERE dataset=?", (dataset,)).fetchone()
    conn.close()
    return {"symbols": row[0] or 0, "start": row[1], "end": row[2], "rows": row[3] or 0}


def get_bar_range(symbol: str) -> tuple[str, str] | None:
    """该标的日线在库内的实际区间"""
    conn = get_conn()
    row = conn.execute(
        "SELECT MIN(date), MAX(date) FROM daily_bar WHERE symbol=?", (symbol,)).fetchone()
    conn.close()
    return (row[0], row[1]) if row and row[0] else None


def get_latest_closes(symbols: list[str]) -> dict[str, tuple[str, float]]:
    """批量取多只股票最新收盘价: {symbol: (date, close)}"""
    if not symbols:
        return {}
    conn = get_conn()
    placeholders = ",".join("?" * len(symbols))
    rows = conn.execute(
        f"SELECT b.symbol, b.date, b.close FROM daily_bar b "
        f"INNER JOIN (SELECT symbol, MAX(date) md FROM daily_bar "
        f"WHERE symbol IN ({placeholders}) GROUP BY symbol) m "
        f"ON b.symbol = m.symbol AND b.date = m.md",
        symbols).fetchall()
    conn.close()
    return {r[0]: (r[1], r[2]) for r in rows}


def get_all_bar_ranges() -> dict:
    """{symbol: (min_date, max_date)} 一次查询全部, 避免 N 次往返"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT symbol, MIN(date), MAX(date) FROM daily_bar GROUP BY symbol").fetchall()
    conn.close()
    return {r[0]: (r[1], r[2]) for r in rows}


def get_all_coverage(dataset: str = "daily_bar") -> dict:
    """{symbol: (start_date, end_date)} 覆盖索引, 用于规划缺口 (不重复下载)"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT symbol, start_date, end_date FROM data_coverage WHERE dataset=?",
        (dataset,)).fetchall()
    conn.close()
    return {r[0]: (r[1], r[2]) for r in rows if r[1] and r[2]}


def latest_bar_map() -> dict:
    """{symbol: (最新收盘价, 股本)} 一次查询, 用于本地估算估值"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT b.symbol, b.close, b.outstanding_share FROM daily_bar b "
        "JOIN (SELECT symbol, MAX(date) d FROM daily_bar GROUP BY symbol) t "
        "ON b.symbol=t.symbol AND b.date=t.d").fetchall()
    conn.close()
    return {r[0]: (r[1], r[2]) for r in rows}


# ==================== 财务数据 ====================

def upsert_fundamentals(items: list[dict]) -> int:
    """批量写入财务指标, items 需含 symbol + report_date"""
    if not items:
        return 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_conn()
    n = 0
    for it in items:
        if not it.get("symbol") or not it.get("report_date"):
            continue
        conn.execute(
            "INSERT OR REPLACE INTO fundamental "
            "(symbol,report_date,ann_date,eps,bps,revenue,net_profit,revenue_yoy,profit_yoy,"
            " roe,gross_margin,debt_ratio,goodwill,goodwill_ratio,pe_ttm,pb,total_mv,industry,"
            " ocf_per_share,operating_profit,is_st,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (it["symbol"], it["report_date"], it.get("ann_date"), it.get("eps"), it.get("bps"),
             it.get("revenue"), it.get("net_profit"), it.get("revenue_yoy"), it.get("profit_yoy"),
             it.get("roe"), it.get("gross_margin"), it.get("debt_ratio"), it.get("goodwill"),
             it.get("goodwill_ratio"), it.get("pe_ttm"), it.get("pb"), it.get("total_mv"),
             it.get("industry"), it.get("ocf_per_share"), it.get("operating_profit"),
             it.get("is_st"), now))
        n += 1
    conn.commit()
    conn.close()
    return n


def load_fundamentals(asof: str = None) -> dict:
    """读取每只股票最新一期财务数据 {symbol: {...}}
    asof: 截止公告日 (回测时避免用未来数据)
    """
    conn = get_conn()
    if asof:
        rows = conn.execute(
            "SELECT f.* FROM fundamental f "
            "JOIN (SELECT symbol, MAX(report_date) rd FROM fundamental "
            "      WHERE ann_date IS NULL OR ann_date <= ? GROUP BY symbol) t "
            "ON f.symbol=t.symbol AND f.report_date=t.rd", (asof,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT f.* FROM fundamental f "
            "JOIN (SELECT symbol, MAX(report_date) rd FROM fundamental GROUP BY symbol) t "
            "ON f.symbol=t.symbol AND f.report_date=t.rd").fetchall()
    cols = [d[0] for d in conn.execute("SELECT * FROM fundamental LIMIT 0").description]
    conn.close()
    return {r[0]: dict(zip(cols, r)) for r in rows}


def fundamental_stats() -> dict:
    conn = get_conn()
    row = conn.execute(
        "SELECT COUNT(DISTINCT symbol), COUNT(*), MAX(report_date) FROM fundamental").fetchone()
    conn.close()
    return {"symbols": row[0] or 0, "rows": row[1] or 0, "latest_report": row[2]}


def cleanup_fundamentals() -> int:
    """删除非沪深A股的脏记录 (北交所/新三板/B股等)"""
    conn = get_conn()
    cur = conn.execute(
        "DELETE FROM fundamental WHERE symbol NOT LIKE 'sh6%' "
        "AND symbol NOT LIKE 'sz0%' AND symbol NOT LIKE 'sz3%'")
    n = cur.rowcount
    conn.commit()
    conn.close()
    return n


def update_valuation(items: list[dict]) -> int:
    """把 PE/PB/总市值 快照更新到每只股票最新一期财报行上"""
    if not items:
        return 0
    conn = get_conn()
    n = 0
    for it in items:
        cur = conn.execute(
            "UPDATE fundamental SET pe_ttm=?, pb=?, total_mv=? "
            "WHERE symbol=? AND report_date=(SELECT MAX(report_date) FROM fundamental WHERE symbol=?)",
            (it.get("pe_ttm"), it.get("pb"), it.get("total_mv"), it["symbol"], it["symbol"]))
        n += cur.rowcount
    conn.commit()
    conn.close()
    return n


def upsert_dividends(items: list[dict]) -> int:
    """批量写入分红送配记录, items 需含 symbol + report_date (仅现金分红>0)"""
    if not items:
        return 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_conn()
    n = 0
    for it in items:
        if not it.get("symbol") or not it.get("report_date"):
            continue
        # 已有记录取更早公告日 (预案公告 -> 实施), 分红额取较大值
        old = conn.execute(
            "SELECT cash_div_ps, ann_date FROM dividend WHERE symbol=? AND report_date=?",
            (it["symbol"], it["report_date"])).fetchone()
        div = it.get("cash_div_ps")
        if old and old[0] is not None and div is not None:
            div = max(old[0], div)
        ann = it.get("ann_date")
        if old and old[1] and ann:
            ann = min(old[1], ann)
        elif old and old[1]:
            ann = old[1]
        conn.execute(
            "INSERT OR REPLACE INTO dividend (symbol,report_date,cash_div_ps,ann_date,updated_at) "
            "VALUES (?,?,?,?,?)",
            (it["symbol"], it["report_date"], div, ann, now))
        n += 1
    conn.commit()
    conn.close()
    return n


def load_dividend_years(asof: str = None) -> dict:
    """读取 {symbol: set(财年 int)} — asof 公告日之前已公告过现金分红的记录

    回测防未来函数: ann_date > asof 的分红记录不计入
    """
    conn = get_conn()
    if asof:
        rows = conn.execute(
            "SELECT symbol, report_date FROM dividend "
            "WHERE ann_date IS NULL OR ann_date <= ?", (asof,)).fetchall()
    else:
        rows = conn.execute("SELECT symbol, report_date FROM dividend").fetchall()
    conn.close()
    out: dict = {}
    for sym, rd in rows:
        try:
            out.setdefault(sym, set()).add(int(str(rd)[:4]))
        except (ValueError, TypeError):
            continue
    return out


# ==================== 实时行情 ====================

def save_realtime(items: list[dict]) -> int:
    if not items:
        return 0
    conn = get_conn()
    conn.executemany(
        "INSERT OR REPLACE INTO realtime_quote "
        "(symbol,ts,price,pct,open,high,low,pre_close,volume,amount,turnover) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(it["symbol"], it["ts"], it.get("price"), it.get("pct"), it.get("open"),
          it.get("high"), it.get("low"), it.get("pre_close"), it.get("volume"),
          it.get("amount"), it.get("turnover")) for it in items if it.get("symbol") and it.get("ts")])
    conn.commit()
    conn.close()
    return len(items)


def load_realtime_latest(symbols: list[str] = None) -> dict:
    """每只标的的最新一条快照"""
    conn = get_conn()
    q = ("SELECT r.* FROM realtime_quote r JOIN "
         "(SELECT symbol, MAX(ts) ts FROM realtime_quote GROUP BY symbol) t "
         "ON r.symbol=t.symbol AND r.ts=t.ts")
    rows = conn.execute(q).fetchall()
    cols = [d[0] for d in conn.execute("SELECT * FROM realtime_quote LIMIT 0").description]
    conn.close()
    out = {r[0]: dict(zip(cols, r)) for r in rows}
    if symbols:
        out = {k: v for k, v in out.items() if k in set(symbols)}
    return out


# ==================== 任务队列 ====================

def task_create(dataset: str, params: dict, message: str = "") -> int:
    import json
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO data_task (dataset,params,status,progress,message,created_at) "
        "VALUES (?,?,?,?,?,?)",
        (dataset, json.dumps(params, ensure_ascii=False), "pending", "0%", message,
         datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    return tid


def task_update(tid: int, status: str, progress: str = "", message: str = ""):
    conn = get_conn()
    fin = datetime.now().strftime("%Y-%m-%d %H:%M:%S") if status in ("done", "failed") else None
    conn.execute(
        "UPDATE data_task SET status=?, progress=?, message=?, finished_at=COALESCE(?, finished_at) "
        "WHERE id=?", (status, progress, message, fin, tid))
    conn.commit()
    conn.close()


def task_list(limit: int = 20, status: str = None) -> list[dict]:
    conn = get_conn()
    if status:
        rows = conn.execute(
            "SELECT * FROM data_task WHERE status=? ORDER BY id DESC LIMIT ?",
            (status, limit)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM data_task ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    cols = [d[0] for d in conn.execute("SELECT * FROM data_task LIMIT 0").description]
    conn.close()
    return [dict(zip(cols, r)) for r in rows]


def task_next_pending() -> dict | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM data_task WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
    if not row:
        conn.close()
        return None
    cols = [d[0] for d in conn.execute("SELECT * FROM data_task LIMIT 0").description]
    conn.close()
    return dict(zip(cols, row))
