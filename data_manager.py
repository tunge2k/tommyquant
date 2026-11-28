"""
策略驱动的数据管理器 (Data Manager)
=====================================
参考 wondertrader 的数据分层思路:
  1. 存储层与读取层分离  —— SQLite 落盘, 上层只关心 DataFrame
  2. 覆盖索引 (coverage)  —— 记录每个标的已同步区间, 只补缺口, 不重复下载
  3. 交易日历驱动调度     —— 只在交易日/交易时段做对应的事
  4. 盘后增量回补         —— 收盘后只补当日一根K线
  5. 实时订阅落库         —— 交易时段轮询快照, 支撑模拟交易
  6. 策略声明数据需求     —— 策略需要什么数据, 由策略自己声明, 系统按需补全
"""
import os
import time
from datetime import datetime, timedelta

import pandas as pd

import data_store

_CAL = None   # 交易日历缓存


# ==================== 交易日历 ====================

def trading_days() -> list[str]:
    """A股交易日历 (YYYY-MM-DD). 拉取失败时退化为"工作日"策略"""
    global _CAL
    if _CAL is None:
        try:
            import akshare as ak
            df = ak.tool_trade_date_hist_sina()
            _CAL = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d").tolist()
        except Exception:
            _CAL = []
    return _CAL


def is_trading_day(date_str: str) -> bool:
    d = datetime.strptime(date_str, "%Y-%m-%d")
    if d.weekday() >= 5:
        return False
    cal = trading_days()
    return date_str in cal if cal else True


def last_trading_day(ref: datetime = None) -> str:
    """最近一个交易日 (含当天)"""
    ref = ref or datetime.now()
    for i in range(10):
        d = (ref - timedelta(days=i)).strftime("%Y-%m-%d")
        if is_trading_day(d):
            return d
    return ref.strftime("%Y-%m-%d")


def is_trading_time(now: datetime = None) -> bool:
    """是否处于可采集实时行情的时段 (9:15-15:05)"""
    now = now or datetime.now()
    if not is_trading_day(now.strftime("%Y-%m-%d")):
        return False
    hm = now.hour * 100 + now.minute
    return 915 <= hm <= 1505


def prev_trading_day(date_str: str) -> str:
    d = datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=1)
    return last_trading_day(d)


# ==================== 策略数据需求声明 ====================

STRATEGY_REQUIREMENTS = {
    "小市值轮动·戴维斯双击": {
        "datasets": ["daily_bar", "fundamental"],
        "lookback_days": 250,
        "realtime": False,
        "note": "需要日线 + 季报财务 (成长/估值/财务健康)",
    },
    "动量+均线+小市值": {"datasets": ["daily_bar"], "lookback_days": 250},
    "小市值+高换手": {"datasets": ["daily_bar"], "lookback_days": 250},
    "短周期突破+量能": {"datasets": ["daily_bar"], "lookback_days": 120},
    "随机对照": {"datasets": ["daily_bar"], "lookback_days": 120},
    "ETF策略": {"datasets": ["daily_bar"], "lookback_days": 250,
               "note": "ETF 池按动量排名, 无财务/分红依赖"},
    "ETF调仓策略": {"datasets": ["daily_bar", "etf_index_val"], "lookback_days": 250,
                 "note": "PE/PB/百分位取自蛋卷「跟踪指数」估值, 无映射的 ETF 不参与该指标排序"},
}


def requirements_for(strategy: str) -> dict:
    return STRATEGY_REQUIREMENTS.get(strategy, {"datasets": ["daily_bar"], "lookback_days": 250})


# ==================== 缺口计算 ====================

def _norm_day(d) -> str:
    """任意日期写法 -> YYYY-MM-DD (DB 内统一格式)"""
    s = str(d).strip().replace("-", "").replace("/", "")[:8]
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return str(d)


def _missing_ranges(have: tuple | None, start: str, end: str) -> list[tuple[str, str]]:
    """根据已有区间算需要补的区间"""
    if have is None or not have[0]:
        return [(start, end)]
    hs, he = have
    gaps = []
    if start < hs:
        gaps.append((start, (datetime.strptime(hs, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")))
    if end > he:
        gaps.append(((datetime.strptime(he, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d"), end))
    return gaps


def backfill_coverage() -> int:
    """用库内已有K线区间回填覆盖索引
    升级兼容: 老库有 daily_bar 但没有 data_coverage, 不做这步会被当成"全部缺失"而重复全量下载
    """
    ranges = data_store.get_all_bar_ranges()
    cov = data_store.get_all_coverage("daily_bar")
    n = 0
    for sym, (s, e) in ranges.items():
        if not s or not e:
            continue
        cur = cov.get(sym)
        # 无覆盖索引, 或已有索引没涵盖库内真实区间 → 合并扩展
        if cur is None or cur[0] > s or cur[1] < e:
            data_store.coverage_set("daily_bar", sym, s, e, 0)
            n += 1
    return n


def plan_bars(symbols: list[str], start: str, end: str) -> dict:
    """规划下载任务: {(start,end): [symbol,...]}, 已完整的标的自动跳过"""
    start, end = _norm_day(start), _norm_day(end)
    ranges = data_store.get_all_coverage("daily_bar")
    bars = data_store.get_all_bar_ranges()
    plan = {}
    skipped = 0
    for s in symbols:
        # 已有K线: 上市前的区间不可能有数据, 不要把 [start, 首根K线] 当成缺口反复下载
        actual = bars.get(s)
        s_start = start
        if actual and actual[0] and actual[0] > start:
            s_start = actual[0]
        gaps = _missing_ranges(ranges.get(s), s_start, end)
        if not gaps:
            skipped += 1
            continue
        for g in gaps:
            plan.setdefault(g, []).append(s)
    return {"plan": plan, "skipped": skipped, "total": len(symbols)}


# ==================== 日线补全 ====================

def ensure_bars(symbols: list[str], start: str, end: str,
                progress_cb=None, workers: int = 8) -> dict:
    """
    只补缺失区间, 不重复下载。
    progress_cb(done, total, symbol, rows)
    """
    p = plan_bars(symbols, start, end)
    plan = p["plan"]
    if not plan:
        return {"downloaded": 0, "rows": 0, "skipped": p["skipped"]}

    total = sum(len(v) for v in plan.values())
    done, rows = 0, 0
    for (gs, ge), syms in plan.items():
        codes = [s[2:] if s.startswith(("sh", "sz")) else s for s in syms]
        etf = [c for c in codes if c.startswith(("51", "56", "58", "15"))]
        stocks = [c for c in codes if c not in etf]

        def _cb(d, t, symbol, n, _ge=ge):
            nonlocal done, rows
            done += 1
            rows += n
            if n > 0:
                data_store.coverage_set("daily_bar", symbol, gs, _ge, n)
            if progress_cb:
                progress_cb(done, total, symbol, n)

        if stocks:
            data_store.download_batch(stocks, gs, ge, workers=workers, progress_cb=_cb)
        for sym in [s for s in syms if s[2:] in etf]:
            _, n = data_store.download_etf_one(sym, gs, ge)
            done += 1
            rows += n
            if n > 0:
                data_store.coverage_set("daily_bar", sym, gs, ge, n)
            if progress_cb:
                progress_cb(done, total, sym, n)
        time.sleep(0.2)

    return {"downloaded": done, "rows": rows, "skipped": p["skipped"]}


def ensure_etf_universe(start: str = "20200101", end: str = None,
                        progress_cb=None) -> dict:
    """ETF 清单同步 + 自动补数

    拉取最新场内 ETF 清单 (东财→新浪→库内兜底), 对清单内全部标的一次性
    ensure_bars: 新上市的补全历史, 已有的只补最后日期之后的缺口。
    盘后每天调用一次, 保证 ETF 策略候选池始终是最新的。
    """
    etfs = data_store.get_etf_codes()
    symbols = [s for s, _ in etfs]
    if not symbols:
        return {"error": "ETF 清单获取失败", "universe": 0}
    # 名称 (跨境/港股分类) + 分红 (后复权除息事件): 供选品与因子层使用
    n_name = data_store.sync_etf_names(use_cache=False)
    div_stat = data_store.sync_etf_dividends(symbols)
    end = end or last_trading_day()
    r = ensure_bars(symbols, start, end, progress_cb=progress_cb)
    return {"universe": len(symbols), "names": n_name, "dividends": div_stat, **r}


# ==================== ETF 增强数据 (华泰柏瑞数据源) ====================

def _fnum(v):
    """字符串/数字 -> float; 空值或非法返回 None (不写 0)"""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _etf_pool(top_n: int = 60, lookback: int = 90, min_amount_yi: float = 0.5) -> list[str]:
    """ETF 池 = 近 30 日选股记录中的 ETF ∪ 近 60 日动量前 top_n

    动量用「近 20 个交易日涨幅」, 流动性用「近 20 日日均成交额(亿)」。
    """
    end = last_trading_day()
    begin = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=lookback)).strftime("%Y-%m-%d")
    conn = data_store.get_conn()
    rows = conn.execute(
        """
        WITH t AS (
            SELECT symbol, close, amount,
                   ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC) rn
            FROM daily_bar
            WHERE asset_type='etf' AND date >= ? AND close > 0
        )
        SELECT symbol,
               MAX(CASE WHEN rn = 1  THEN close END)  AS c_now,
               MAX(CASE WHEN rn = 21 THEN close END)  AS c_20,
               AVG(CASE WHEN rn <= 20 THEN amount END) AS amt_20
        FROM t GROUP BY symbol
        """, (begin,)).fetchall()
    picks = conn.execute(
        "SELECT DISTINCT symbol FROM pick_record WHERE pick_date >= ?", (begin,)).fetchall()
    conn.close()

    ranked = []
    for sym, c_now, c_20, amt in rows:
        if not c_now or not c_20 or not amt:
            continue
        if amt < min_amount_yi * 1e8:
            continue
        ranked.append((c_now / c_20 - 1.0, sym))
    ranked.sort(reverse=True)
    out = [s for _, s in ranked[:top_n]]
    for (sym,) in picks:
        if sym and sym.startswith(("sh51", "sh56", "sh58", "sz15")) and sym not in out:
            out.append(sym)
    return out


def _norm_quote_rows(recs: list[dict]) -> list[dict]:
    """fund-daily-quote → etf_quote_ext（成交额万元→元, 成交量亿股→股）"""
    out = []
    for r in recs:
        day = _norm_day(r.get("tradingday"))
        if not day:
            continue
        amt = _fnum(r.get("turnovervalue"))
        vol = _fnum(r.get("turnovervolume"))
        out.append({
            "date": day,
            "turnover_value": amt * 1e4 if amt is not None else None,
            "turnover_volume": vol * 1e8 if vol is not None else None,
            "turnover_rate": _fnum(r.get("turnoverrate")),
            "avg_price": _fnum(r.get("avgprice")),
            "change_pct": _fnum(r.get("changepct")),
            "range_pct": _fnum(r.get("rangepct")),
            "discount_ratio": _fnum(r.get("discountratio")),
            "premium_ratio": _fnum(r.get("premiumrate")),
            "is_suspend": 1 if "未停牌" not in str(r.get("ifsuspend") or "未停牌") else 0,
        })
    return out


def _norm_capital_rows(recs: list[dict]) -> list[dict]:
    """etf-capital-report(unit=3: 万元/万份) → etf_capital（统一元/份）"""
    out = []
    for r in recs:
        day = _norm_day(r.get("tradingday"))
        if not day:
            continue
        def yi(key):
            v = _fnum(r.get(key))
            return v * 1e4 if v is not None else None
        out.append({
            "date": day,
            "unit_nv": _fnum(r.get("unitnv")),
            "close_price": _fnum(r.get("closepricelist")),
            "exchange_shares": yi("exchangeshares"),
            "market_scale": yi("marketscale"),
            "net_sub_shares": yi("netsubscriptionshares"),
            "finance_net_buy": yi("financenetbuyvalue"),
            "finance_balance": yi("financebalancevalue"),
            "near3_netbuy": yi("near3daynetbuy"),
            "near5_netbuy": yi("near5daynetbuy"),
            "near20_netbuy": yi("near20daynetbuy"),
            "near60_netbuy": yi("near60daynetbuy"),
        })
    return out


def _norm_margin_rows(recs: list[dict]) -> list[dict]:
    """etf-margin(unit=3: 万元) → etf_margin（金额元, 数量原样）"""
    out = []
    for r in recs:
        day = _norm_day(r.get("tradingday"))
        if not day:
            continue
        def wan(key):
            v = _fnum(r.get(key))
            return v * 1e4 if v is not None else None
        out.append({
            "date": day,
            "finance_buy": wan("financebuyvalue"),
            "finance_refund": wan("financerefundvalue"),
            "finance_net_buy": wan("financenetbuyvalue"),
            "security_sell_vol": _fnum(r.get("securitysellvolume")),
            "security_refund_vol": _fnum(r.get("securityrefundvolume")),
            "security_net_sell_vol": _fnum(r.get("securitynetsellvolume")),
            "finance_balance": wan("financevalue"),
            "security_balance": _fnum(r.get("securityvalue")),
            "security_volume": _fnum(r.get("securityvolume")),
            "finance_pct": _fnum(r.get("negotiablemvoffinance")),
            "security_pct": _fnum(r.get("negotiablemvofsecurity")),
        })
    return out


def _norm_flow_rows(recs: list[dict]) -> list[dict]:
    """etf-capital-flow-stats(unit=1) → etf_market_flow（全市场 + 5 分类）"""
    cats = {"alletf": "all", "stocketf": "stock", "bondetf": "bond",
            "commodityetf": "commodity", "monetaryetf": "monetary", "qdiietf": "qdii"}
    out = []
    for r in recs:
        day = _norm_day(r.get("enddate"))
        if not day:
            continue
        for prefix, cat in cats.items():
            out.append({
                "date": day, "category": cat,
                "buy_value": _fnum(r.get(f"{prefix}totalbuyvalue")),
                "sell_value": _fnum(r.get(f"{prefix}totalsellvalue")),
                "net_value": _fnum(r.get(f"{prefix}netbuyvalue")),
                "etf_count": int(_fnum(r.get(f"{prefix}num")) or 0),
            })
    return out


def ensure_etf_extras(top_n: int = 60, days: int = 5, with_margin: bool = True,
                      with_valuation: bool = True, progress_cb=None,
                      dry_run: bool = False) -> dict:
    """盘后同步 ETF 增强数据（华泰柏瑞数据源）

    池子 = 近 30 日选股记录里的 ETF ∪ 近 90 日动量前 top_n。
    依次拉取: 全市场资金流(1次) → 日行情/资金报表/两融(每只3次) → 持仓估值(每月1次)。
    任何单标的失败只跳过, 不中断整批; 配额耗尽立即终止。
    """
    import htbr_source as hs

    ok, reason = hs.available()
    if not ok:
        return {"skipped": reason, "symbols": 0, "rows": 0}
    pool = _etf_pool(top_n=top_n)
    if not pool:
        return {"skipped": "ETF 池为空（缺少行情数据）", "symbols": 0, "rows": 0}

    end = last_trading_day()
    begin = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=days * 2)).strftime("%Y-%m-%d")
    compact = lambda d: d.replace("-", "")
    plan_calls = 1 + len(pool) * (2 + int(with_margin)) + len(pool) * int(with_valuation)
    stats = {"symbols": len(pool), "rows": 0, "calls_planned": plan_calls,
             "calls_used": 0, "failed": [], "day": end}
    if dry_run:
        stats["pool"] = pool
        return stats

    def _log(msg):
        if progress_cb:
            progress_cb(msg)

    # 1) 全市场 ETF 资金流（一次拿全市场）
    try:
        recs = hs.market_flow(compact(begin), compact(end))
        stats["rows"] += data_store.upsert_etf_market_flow(_norm_flow_rows(recs))
    except hs.HtbrQuotaExceeded:
        stats["aborted"] = "配额耗尽"
        return stats
    except Exception as e:
        stats["failed"].append(f"market_flow: {type(e).__name__}")

    # 2) 逐只标的
    inner_map = data_store.etf_meta_map()
    for i, sym in enumerate(pool, 1):
        secu = sym[2:] if sym.startswith(("sh", "sz")) else sym
        try:
            quotes = hs.daily_quote(secu, begin, end)
            if quotes:
                stats["rows"] += data_store.upsert_etf_quote_ext(sym, _norm_quote_rows(quotes))
                if sym not in inner_map:
                    inner_map[sym] = str(quotes[0].get("fundobject") or "") or None
                    data_store.upsert_etf_meta(sym, secu, inner_map[sym],
                                              str(quotes[0].get("fundname") or ""))

            inner = inner_map.get(sym) or hs.resolve_inner_code(secu)
            if not inner:
                stats["failed"].append(f"{sym}: 无基金内码")
                continue
            inner_map[sym] = inner

            stats["rows"] += data_store.upsert_etf_capital(
                sym, _norm_capital_rows(hs.capital_report(inner, begin, end)))

            if with_margin:
                stats["rows"] += data_store.upsert_etf_margin(
                    sym, _norm_margin_rows(hs.margin(inner, begin, end)))

            if with_valuation:
                # 估值按报告期更新, 每月取一次即可
                vkey = f"htbr_val_{sym}"
                last = data_store.get_setting(vkey, "")
                month = end[:7]
                if last != month:
                    vrecs = hs.valuation(inner, "", end)
                    if vrecs:
                        rows = []
                        for r in vrecs:
                            rd = _norm_day(r.get("reportdate"))
                            if not rd:
                                continue
                            rows.append({"report_date": rd,
                                         "total_mv": _fnum(r.get("totalmv")),
                                         "negotiable_mv": _fnum(r.get("negotiablemv")),
                                         "pe_ttm": _fnum(r.get("pettm")),
                                         "pb": _fnum(r.get("pb")),
                                         "dividend_ratio": _fnum(r.get("dividendratio"))})
                        stats["rows"] += data_store.upsert_etf_valuation(sym, rows)
                    data_store.set_setting(vkey, month)
        except hs.HtbrQuotaExceeded:
            stats["aborted"] = "配额耗尽"
            break
        except Exception as e:
            stats["failed"].append(f"{sym}: {type(e).__name__}: {str(e)[:80]}")
            continue
        finally:
            stats["calls_used"] = hs.quota_used()
        _log(f"[{i}/{len(pool)}] {sym} 累计 {stats['rows']} 行")
    return stats


# ==================== ETF 跟踪指数估值 (蛋卷 PE/PB) ====================

# ETF 名称 -> 蛋卷指数 code 的别名规则 (关键词长度降序匹配)
# 仅用于「核心段完全相等」的匹配, 例如 券商ETF -> 证券公司; 不匹配 创业板软件ETF。
_INDEX_ALIASES = [
    ("恒生中国企业", "HKHSCEI"), ("恒生国企", "HKHSCEI"), ("国企指数", "HKHSCEI"),
    ("恒生科技", "HKHSTECH"), ("恒生", "HKHSI"),
    ("纳斯达克100", "NDX"), ("纳斯达克", "NDX"), ("纳指100", "NDX"), ("纳指", "NDX"),
    ("德国DAX", "GDAXI"), ("德国", "GDAXI"),
    ("中概互联网", "CSIH30533"), ("中概互联", "CSIH30533"), ("中国互联", "CSIH11136"),
    ("证券公司", "SZ399975"), ("券商", "SZ399975"), ("证券", "SZ399975"),
    ("中证银行", "SZ399986"), ("银行", "SZ399986"),
    ("中证医疗", "SZ399989"), ("医疗", "SZ399989"),
    ("中证白酒", "SZ399997"), ("白酒", "SZ399997"),
    ("食品饮料", "SZ399396"), ("国证食品", "SZ399396"),
    ("中证传媒", "SZ399971"), ("传媒", "SZ399971"),
    ("煤炭", "SZ399998"), ("房地产", "SZ399393"), ("地产", "SZ399393"),
    ("军工", "SZ399967"), ("国防", "SZ399967"),
    ("环保", "SH000827"), ("主要消费", "SH000932"),
    ("全指信息", "SH000993"), ("信息技术", "SH000993"),
    ("全指可选", "SH000989"), ("可选消费", "SH000989"),
    ("全指医药", "SH000991"), ("医药100", "SH000978"),
    ("创业板", "SZ399006"), ("科创50", "SH000688"), ("科创板50", "SH000688"),
    ("红利低波", "CSIH30269"), ("红利", "SH000922"),
    ("中证1000", "SH000852"), ("中证500", "SH000905"),
    ("沪深300", "SH000300"), ("上证50", "SH000016"),
    ("上证180", "SH000010"), ("中证100", "SH000903"),
    ("深证100", "SZ399330"), ("深证成指", "SZ399001"),
    ("新能源车", "SZ399417"), ("5G通讯", "CSI931079"), ("5G通信", "CSI931079"),
    ("科技龙头", "CSI931087"), ("央视50", "SZ399550"),
    ("MSCI中国", "CSI716567"), ("300价值", "SH000919"),
    ("养老产业", "SZ399812"), ("国证A指", "SZ399317"),
    ("基本面50", "SH000925"), ("基本面60", "SZ399701"), ("基本面120", "SZ399702"),
]


def _etf_core_name(name: str) -> str:
    """ETF 名称截到最后一个 ETF 为止: '红利低波ETF华泰柏瑞' -> '红利低波ETF'"""
    s = str(name or "")
    i = s.rfind("ETF")
    return s[:i + 3] if i >= 0 else s


def match_track_index(name: str, index_names: dict[str, str]):
    """按 ETF 名称匹配蛋卷跟踪指数, 返回 (index_code, index_name) 或 None

    要求「核心段完全相等」(如 沪深300ETF), 避免 创业板软件ETF 被误判为 创业板。
    """
    core = _etf_core_name(name)
    if not core:
        return None
    for code, nm in index_names.items():
        if core == f"{nm}ETF":
            return code, nm
    for kw, code in _INDEX_ALIASES:
        if core == f"{kw}ETF":
            return code, index_names.get(code, kw)
    return None


def ensure_etf_index_valuation(pool: list[str] | None = None,
                               progress_cb=None, force: bool = False,
                               max_age_days: int = 6) -> dict:
    """同步 ETF 跟踪指数估值 (蛋卷 PE/PB 周频全历史)

    1. 拉蛋卷指数清单, 按 ETF 名称生成/刷新映射 (etf_index_map);
    2. 每个映射到的指数拉一次 PE 与 PB 全历史;
    3. 按 symbol 展开写入 etf_index_val。

    没有指数映射的 ETF 直接跳过 (不写假数据), 估值类排序指标里它不参与排序。
    覆盖率不足时策略会自动回退动量, 见 etf_rebalance._resolve_metric。
    """
    import danjuan_source as dj
    names = data_store.etf_name_map()
    if pool:
        symbols = [s for s in dict.fromkeys(pool) if s in names]
    else:
        symbols = sorted(names)
    if not symbols:
        return {"skipped": "无 ETF 名称数据", "symbols": 0, "mapped": 0, "rows": 0}

    iv = data_store.etf_index_val_stats()
    if not force and iv["symbols"] > 0 and iv["last_date"] != "-":
        try:
            age = (datetime.now() - datetime.strptime(iv["last_date"][:10], "%Y-%m-%d")).days
        except ValueError:
            age = max_age_days + 1
        if age < max_age_days:
            return {"skipped": f"指数估值 {age} 天前同步过", **iv}

    def _log(msg):
        if progress_cb:
            progress_cb(msg)

    try:
        idx_list = dj.fetch_index_list()
    except Exception as e:
        return {"error": f"指数清单获取失败: {type(e).__name__}: {e}"}
    index_names = {it["index_code"]: it["name"] for it in idx_list}

    existing = data_store.etf_index_map_all()
    map_rows = []
    for sym in symbols:
        hit = match_track_index(names.get(sym, ""), index_names)
        if not hit:
            continue
        code, nm = hit
        old = existing.get(sym) or {}
        if old.get("index_code") == code and not force:
            continue
        map_rows.append({"symbol": sym, "index_code": code,
                         "index_name": nm, "source": "danjuan"})
    if map_rows:
        data_store.upsert_etf_index_map(map_rows)
        _log(f"指数映射更新 {len(map_rows)} 只")

    mapping = data_store.etf_index_map_all()
    mapped = {s: mapping[s] for s in symbols if s in mapping}
    if not mapped:
        return {"symbols": len(symbols), "mapped": 0, "indices": 0, "rows": 0,
                "failed": [], "note": "没有任何 ETF 匹配到蛋卷指数"}

    codes = sorted({m["index_code"] for m in mapped.values()})
    hist, failed = {}, []
    for i, code in enumerate(codes, 1):
        try:
            pe = dj.fetch_history(code, "pe")
            pb = dj.fetch_history(code, "pb")
            hist[code] = {"pe": dict(pe), "pb": dict(pb)}
            _log(f"[{i}/{len(codes)}] 指数 {code}: PE {len(pe)} 点 / PB {len(pb)} 点")
        except Exception as e:
            failed.append(f"{code}: {type(e).__name__}")
            _log(f"[{i}/{len(codes)}] 指数 {code} 失败: {type(e).__name__}")
        time.sleep(0.15)

    rows_total, done = 0, 0
    for sym, m in mapped.items():
        code = m["index_code"]
        h = hist.get(code)
        if not h:
            continue
        dates = sorted(set(h["pe"]) | set(h["pb"]))
        rows = [{"date": d, "pe": h["pe"].get(d), "pb": h["pb"].get(d), "index_code": code}
                for d in dates]
        if rows:
            rows_total += data_store.upsert_etf_index_val(sym, rows)
        done += 1
        _log(f"[{done}/{len(mapped)}] {sym} {m.get('index_name') or code} +{len(rows)} 行")

    return {"symbols": len(symbols), "mapped": len(mapped), "indices": len(hist),
            "rows": rows_total, "failed": failed[:20]}


def backfill_etf_market_flow(start: str = "2020-01-01", end: str | None = None) -> dict:
    """回补全市场 ETF 资金流历史 (按自然年切窗, 约 1 次/年)

    该接口不依赖基金内码, 单次返回一年数据, 是成本最低的历史回补项。
    """
    import htbr_source as hs
    ok, reason = hs.available()
    if not ok:
        return {"skipped": reason, "rows": 0}
    end = end or last_trading_day()
    y0, y1 = int(start[:4]), int(end[:4])
    rows, years = 0, []
    for y in range(y0, y1 + 1):
        b = max(start, f"{y}-01-01").replace("-", "")
        e = min(end, f"{y}-12-31").replace("-", "")
        try:
            recs = hs.market_flow(b, e)
        except hs.HtbrQuotaExceeded:
            return {"rows": rows, "years": years, "aborted": "配额耗尽"}
        except Exception as ex:
            years.append(f"{y}:{type(ex).__name__}")
            continue
        rows += data_store.upsert_etf_market_flow(_norm_flow_rows(recs))
        years.append(y)
    return {"rows": rows, "years": years}


def _db_trade_dates(start: str, end: str) -> list[str]:
    """库内已有行情的交易日 (回补份额时用它当交易日历, 不依赖外部日历)"""
    conn = data_store.get_conn()
    rows = conn.execute(
        "SELECT DISTINCT date FROM daily_bar WHERE date BETWEEN ? AND ? ORDER BY date",
        (start, end)).fetchall()
    conn.close()
    return [r[0] for r in rows]


def ensure_etf_shares(day: str | None = None) -> dict:
    """盘后同步当日沪市 ETF 份额 (交易所官方, 1 次调用/日, 无配额)"""
    import exchange_source as es
    if not es.env_enabled():
        return {"skipped": "官方源已关闭"}
    day = day or last_trading_day()
    if day in data_store.etf_share_dates():
        return {"skipped": "已有数据", "date": day}
    rows = es.fetch_sse_shares(day)
    return {"date": day, "rows": data_store.upsert_etf_share_daily(rows)}


def backfill_etf_shares(start: str = "2020-01-01", end: str | None = None,
                        max_days: int = 250, progress_cb=None) -> dict:
    """回补沪市 ETF 份额历史 (上交所官方, 任意历史日可回溯)

    每个交易日 1 次调用、单次返回当日全市场, 因此 2020 年至今约 1500 次调用;
    按已覆盖日期判断缺口, 未完成部分下次运行会继续。
    """
    import exchange_source as es
    if not es.env_enabled():
        return {"skipped": "官方源已关闭"}
    end = end or last_trading_day()
    have = data_store.etf_share_dates()
    dates = [d for d in _db_trade_dates(start, end) if d not in have]
    stats = {"days": 0, "rows": 0, "failed": [], "remaining": len(dates), "aborted": None}
    for i, d in enumerate(dates[:max_days], 1):
        try:
            rows = es.fetch_sse_shares(d)
        except es.SourceError as ex:
            stats["failed"].append(f"{d}: {ex}")
            if len(stats["failed"]) >= 5:
                stats["aborted"] = "连续失败, 已停止"
                break
            continue
        stats["rows"] += data_store.upsert_etf_share_daily(rows)
        stats["days"] += 1
        stats["remaining"] = len(dates) - i
        if progress_cb:
            progress_cb(i, min(max_days, len(dates)), f"{d} {len(rows)} 只")
    return stats


def backfill_etf_nav(symbols: list[str] | None = None, start: str = "2020-01-01",
                     end: str | None = None, max_symbols: int = 60,
                     progress_cb=None) -> dict:
    """回补 ETF 单位净值历史 (东财), 供本地计算溢价率

    只处理还没有净值数据的标的; 默认范围为已有份额数据的沪市 ETF。
    """
    import exchange_source as es
    end = end or last_trading_day()
    pool = symbols or [s for s in data_store.etf_share_symbols() if s.startswith("sh")]
    conn = data_store.get_conn()
    have = {r[0] for r in conn.execute(
        "SELECT DISTINCT symbol FROM etf_share_daily WHERE nav IS NOT NULL")}
    conn.close()
    todo_all = [s for s in pool if s not in have]
    todo = todo_all[:max_symbols]
    stats = {"symbols": 0, "rows": 0, "failed": [], "remaining": len(todo_all) - len(todo)}
    for i, sym in enumerate(todo, 1):
        try:
            rows = es.fetch_nav_history(sym[2:] if sym.startswith("sh") else sym, start, end)
        except es.SourceError as ex:
            stats["failed"].append(f"{sym}: {ex}")
            continue
        data_store.upsert_etf_share_daily(
            [{"symbol": sym, "date": r["date"], "nav": r["nav"]} for r in rows])
        stats["rows"] += len(rows)
        stats["symbols"] += 1
        if progress_cb:
            progress_cb(i, len(todo), f"{sym} {len(rows)} 天")
    return stats


def _traded_etf_symbols() -> list[str]:
    """从历史 ETF 回测结果中提取「实际交易过的 ETF」, 用于定向回补溢价率

    回补当前动量池之外的标的没有意义: 回测会买的往往是历史强势品种。
    """
    import re
    conn = data_store.get_conn()
    rows = conn.execute(
        "SELECT payload FROM backtest_result WHERE strategy LIKE '%ETF%'").fetchall()
    conn.close()
    pat = re.compile(r'"(sh5\d{5}|sh58\d{4}|sz15\d{4}|sz16\d{4})"')
    out = set()
    for (payload,) in rows:
        out.update(pat.findall(payload or ""))
    return sorted(out)


def backfill_etf_factors(start_year: int = 2022, top_n: int = 60,
                         max_calls: int = 380, with_capital: bool = True,
                         symbols: list[str] | None = None,
                         progress_cb=None) -> dict:
    """回补 ETF 的溢价率与资金流历史 (按年切窗, 供回测使用)

    symbols 为空时 = 当前动量池 ∪ 历史 ETF 回测交易过的标的 (后者才是回测真正会买的);
    每只标的每年 2 次调用 (行情 + 资金报表, with_capital=False 时只回补行情/溢价),
    受 max_calls 保护, 未完成部分下次运行会从缺口处继续 (按年判断覆盖情况)。
    """
    import htbr_source as hs
    ok, reason = hs.available()
    if not ok:
        return {"skipped": reason, "symbols": 0}
    if symbols:
        pool = list(dict.fromkeys(symbols))
    else:
        pool = _etf_pool(top_n=top_n)
        pool = list(dict.fromkeys(pool + _traded_etf_symbols()))
    end = last_trading_day()
    years = list(range(int(start_year), int(end[:4]) + 1))
    inner_map = data_store.etf_meta_map()
    stats = {"symbols": 0, "rows": 0, "calls": 0, "done": 0, "skipped": 0,
             "failed": [], "aborted": None}
    calls0 = hs.quota_used()

    for i, sym in enumerate(pool, 1):
        secu = sym[2:] if sym.startswith(("sh", "sz")) else sym
        for y in years:
            if hs.quota_used() - calls0 >= max_calls:
                stats["aborted"] = f"达到本次上限 {max_calls} 次"
                return stats
            b = f"{y}-01-01"
            e = min(end, f"{y}-12-31")
            # 已有该年数据则跳过 (按行情增强表首末日期粗判)
            have = data_store.has_etf_quote_year(sym, y)
            try:
                if not have:
                    quotes = hs.daily_quote(secu, b, e)
                    stats["rows"] += data_store.upsert_etf_quote_ext(
                        sym, _norm_quote_rows(quotes))
                    if quotes and sym not in inner_map:
                        inner_map[sym] = str(quotes[0].get("fundobject") or "") or None
                        data_store.upsert_etf_meta(sym, secu, inner_map[sym],
                                                  str(quotes[0].get("fundname") or ""))
                    stats["done"] += 1
                else:
                    stats["skipped"] += 1
                inner = inner_map.get(sym)
                if with_capital and inner and not data_store.has_etf_capital_year(sym, y):
                    stats["rows"] += data_store.upsert_etf_capital(
                        sym, _norm_capital_rows(hs.capital_report(inner, b, e)))
            except hs.HtbrQuotaExceeded:
                stats["aborted"] = "配额耗尽"
                return stats
            except Exception as ex:
                stats["failed"].append(f"{sym}/{y}: {type(ex).__name__}")
        stats["symbols"] += 1
        stats["calls"] = hs.quota_used() - calls0
        if progress_cb:
            progress_cb(f"[{i}/{len(pool)}] {sym} 累计 {stats['rows']} 行")
    stats["calls"] = hs.quota_used() - calls0
    return stats


# ==================== 财务数据 ====================

def _recent_quarters(n: int = 4) -> list[str]:
    """最近 n 个已过报告期 (YYYYMMDD), 从新到旧"""
    out = []
    y = datetime.now().year
    for year in (y, y - 1):
        for md in ("1231", "0930", "0630", "0331"):
            d = f"{year}{md}"
            if datetime.strptime(d, "%Y%m%d") <= datetime.now():
                out.append(d)
    return out[:n]


def _col(row, names, default=None):
    for n in names:
        if n in row and pd.notna(row[n]):
            return row[n]
    return default


def _norm_ann(v):
    """公告日期归一化为 YYYY-MM-DD, 便于与回测日期做字符串比较"""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip()[:10]
    return _norm_day(s) if s else None


def _to_float(v):
    try:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
        return float(str(v).replace(",", "").replace("%", ""))
    except Exception:
        return None


def ensure_fundamentals(quarters: int = 4, progress_cb=None) -> dict:
    """
    批量拉取全市场季报 + 估值快照 (akshare 批量接口, 每季度只需 3 次请求)
    数据: 营收/净利及同比、ROE、毛利率、资产负债率、所处行业、PE/PB/总市值
    附加: 每股经营现金流、营业利润 (不良标的识别), ST 标记, 分红送配记录
    """
    import akshare as ak

    # 清理历史脏数据 (北交所/新三板/B股)
    try:
        data_store.cleanup_fundamentals()
    except Exception:
        pass

    total, quarters_ok = 0, 0
    for qi, q in enumerate(_recent_quarters(quarters)):
        try:
            yj = ak.stock_yjbb_em(date=q)
        except Exception:
            continue
        if yj is None or yj.empty:
            continue
        zc = None
        try:
            zc = ak.stock_zcfz_em(date=q)
        except Exception:
            pass
        # 利润表: 营业利润 (识别利润依赖营业外/变卖资产)
        op_map = {}
        try:
            lr = ak.stock_lrb_em(date=q)
            if lr is not None and not lr.empty:
                for _, r in lr.iterrows():
                    code = str(_col(r, ["股票代码", "代码"], ""))
                    op_map[code] = _to_float(_col(r, ["营业利润"]))
        except Exception:
            pass

        debt_map = {}
        if zc is not None and not zc.empty:
            for _, r in zc.iterrows():
                code = str(_col(r, ["股票代码", "代码"], ""))
                debt_map[code] = _to_float(_col(r, ["资产负债率"]))

        items = []
        for _, r in yj.iterrows():
            code = str(_col(r, ["股票代码", "代码"], ""))
            # 只要沪深A股 (排除北交所/新三板/B股等, 避免脏 symbol)
            if not code or not code.startswith(("60", "68", "00", "30")):
                continue
            symbol = f"sh{code}" if code.startswith("6") else f"sz{code}"
            name = str(_col(r, ["股票简称", "简称"], "") or "")
            items.append({
                "symbol": symbol,
                "report_date": q,
                "ann_date": _norm_ann(_col(r, ["最新公告日期", "公告日期"])),
                "eps": _to_float(_col(r, ["每股收益"])),
                "bps": _to_float(_col(r, ["每股净资产"])),
                "revenue": _to_float(_col(r, ["营业总收入-营业总收入", "营业收入-营业收入", "营业收入"])),
                "net_profit": _to_float(_col(r, ["净利润-净利润", "净利润"])),
                "revenue_yoy": _to_float(_col(r, ["营业总收入-同比增长", "营业收入-同比增长"])),
                "profit_yoy": _to_float(_col(r, ["净利润-同比增长"])),
                "roe": _to_float(_col(r, ["净资产收益率"])),
                "gross_margin": _to_float(_col(r, ["销售毛利率"])),
                "debt_ratio": debt_map.get(code),
                "goodwill": None,
                "goodwill_ratio": None,
                "industry": _col(r, ["所处行业", "行业"]),
                # --- 不良标的识别字段 ---
                "ocf_per_share": _to_float(_col(r, ["每股经营现金流量"])),
                "operating_profit": op_map.get(code),
                # ST/*ST/退市整理: 以最新简称标记 (监管风险/财务造假高发池)
                "is_st": 1 if ("ST" in name.upper() or name.endswith("退")) else 0,
            })
        n = data_store.upsert_fundamentals(items)
        total += n
        quarters_ok += 1
        data_store.coverage_set("fundamental", f"Q{q}", q, q, n)
        if progress_cb:
            progress_cb(qi + 1, quarters, q, n)

    # 估值快照 (PE/PB/总市值): 覆盖到每只股票最新一期
    df, src = _spot_df()
    updated = 0
    if df is not None:
        vals = [{"symbol": it["symbol"], "pe_ttm": it["pe_ttm"],
                 "pb": it["pb"], "total_mv": it["total_mv"]}
                for it in parse_spot(df, src) if it.get("pe_ttm") is not None]
        if vals:
            updated = data_store.update_valuation(vals)
    # 快照不可用 (如东财接口被限流) → 用本地收盘价 + 季报 EPS/BPS 估算
    if updated == 0:
        updated = local_valuation()

    # 分红送配记录 (长期不分红识别)
    try:
        div_res = ensure_dividends()
    except Exception:
        div_res = {"rows": 0}

    return {"rows": total, "quarters": quarters_ok, "valuation": updated,
            "dividends": div_res.get("rows", 0)}


def _dividend_periods(years: int = 6) -> list[str]:
    """最近 years 个完整财年的年报期 + 近两年中报期 (中期分红口径)

    覆盖回测所需的分红历史; 无记录的报告期接口返回空表, 无副作用
    """
    y = datetime.now().year
    periods = [f"{yy}1231" for yy in range(y - years, y)]
    periods += [f"{y - 1}0630", f"{y}0630"]
    return sorted(set(periods))


def ensure_dividends(progress_cb=None) -> dict:
    """
    批量拉取全市场分红送配 (东财分红派息接口, 每期 1 次请求)
    仅保留现金分红 > 0 的记录, 用于「长期不分红」标的剔除
    """
    import akshare as ak

    total = 0
    periods = _dividend_periods(6)
    for pi, q in enumerate(periods):
        try:
            df = ak.stock_fhps_em(date=q)
        except Exception:
            continue
        if df is None or df.empty:
            continue
        items = []
        for _, r in df.iterrows():
            code = str(_col(r, ["代码", "股票代码"], ""))
            if not code or not code.startswith(("60", "68", "00", "30")):
                continue
            # 现金分红-现金分红比例: 每10股派X元 → 每股 X/10
            div10 = _to_float(_col(r, ["现金分红-现金分红比例"]))
            if div10 is None or div10 <= 0:
                continue  # 送转股/无现金分红方案不计入
            symbol = f"sh{code}" if code.startswith("6") else f"sz{code}"
            ann = _norm_ann(_col(r, ["预案公告日"])) or _norm_ann(_col(r, ["最新公告日期"]))
            items.append({
                "symbol": symbol,
                "report_date": q,
                "cash_div_ps": div10 / 10.0,
                "ann_date": ann,
            })
        total += data_store.upsert_dividends(items)
        if progress_cb:
            progress_cb(pi + 1, len(periods), q, total)
    return {"rows": total, "periods": periods}


def local_valuation() -> int:
    """本地估算 PE/PB/总市值: 最新收盘价 + 季报 EPS(年化)/每股净资产
    用途: 东财快照接口不可用时的兜底, 保证"低估值"因子仍可计算
    """
    bars = data_store.latest_bar_map()
    funds = data_store.load_fundamentals()
    if not bars or not funds:
        return 0
    vals = []
    for sym, f in funds.items():
        close, shares = bars.get(sym, (None, None))
        if not close or close <= 0:
            continue
        eps = f.get("eps")
        bps = f.get("bps")
        pe = None
        if eps and eps > 0:
            rd = str(f.get("report_date") or "")
            frac = {"0331": 0.25, "0630": 0.5, "0930": 0.75, "1231": 1.0}.get(rd[4:8], 1.0)
            eps_ttm = eps / frac
            if eps_ttm > 0:
                pe = round(close / eps_ttm, 2)
        pb = round(close / bps, 2) if bps and bps > 0 else None
        mv = close * shares if shares else None
        vals.append({"symbol": sym, "pe_ttm": pe, "pb": pb, "total_mv": mv})
    return data_store.update_valuation(vals)


# ==================== 实时行情 ====================

def _spot_df(retries: int = 3):
    """全市场快照 (东财优先, 失败退化为新浪), 返回 (df, source)"""
    import akshare as ak
    for i in range(retries):
        try:
            df = ak.stock_zh_a_spot_em()
            if df is not None and not df.empty:
                return df, "em"
        except Exception:
            time.sleep(2 + i * 2)
    try:
        df = ak.stock_zh_a_spot()
        if df is not None and not df.empty:
            return df, "sina"
    except Exception:
        pass
    return None, None


def parse_spot(df, src: str) -> list[dict]:
    """把不同来源的快照统一成同一结构"""
    out = []
    for _, r in df.iterrows():
        if src == "em":
            code = str(_col(r, ["代码", "股票代码"], ""))
            if not code:
                continue
            symbol = f"sh{code}" if code.startswith("6") else f"sz{code}"
            out.append({
                "symbol": symbol,
                "price": _to_float(_col(r, ["最新价"])),
                "pct": _to_float(_col(r, ["涨跌幅"])),
                "open": _to_float(_col(r, ["今开"])),
                "high": _to_float(_col(r, ["最高"])),
                "low": _to_float(_col(r, ["最低"])),
                "pre_close": _to_float(_col(r, ["昨收"])),
                "volume": _to_float(_col(r, ["成交量"])),
                "amount": _to_float(_col(r, ["成交额"])),
                "turnover": _to_float(_col(r, ["换手率"])),
                "pe_ttm": _to_float(_col(r, ["市盈率-动态", "市盈率"])),
                "pb": _to_float(_col(r, ["市净率"])),
                "total_mv": _to_float(_col(r, ["总市值"])),
            })
        else:  # sina: 代码形如 sh600000
            symbol = str(_col(r, ["代码"], ""))
            if not symbol.startswith(("sh", "sz")):
                continue
            out.append({
                "symbol": symbol,
                "price": _to_float(_col(r, ["最新价"])),
                "pct": _to_float(_col(r, ["涨跌幅"])),
                "open": _to_float(_col(r, ["今开"])),
                "high": _to_float(_col(r, ["最高"])),
                "low": _to_float(_col(r, ["最低"])),
                "pre_close": _to_float(_col(r, ["昨收"])),
                "volume": _to_float(_col(r, ["成交量"])),
                "amount": _to_float(_col(r, ["成交额"])),
                "turnover": None,
                "pe_ttm": None, "pb": None, "total_mv": None,
            })
    return out


def _sina_realtime_batch(symbols: list[str]) -> list[dict]:
    """新浪 hq.sinajs.cn 直连批量报价 (0.1~0.3 秒, 不受东财限流影响).

    symbols: ["sh600000", "sz000001", ...]
    """
    import urllib.request
    out = []
    for i in range(0, len(symbols), 200):
        chunk = symbols[i:i + 200]
        url = "https://hq.sinajs.cn/list=" + ",".join(chunk)
        req = urllib.request.Request(url, headers={
            "Referer": "https://finance.sina.com.cn",
            "User-Agent": "Mozilla/5.0"})
        try:
            raw = urllib.request.urlopen(req, timeout=10).read().decode("gbk", errors="replace")
        except Exception:
            continue
        for line in raw.strip().split("\n"):
            if '="' not in line:
                continue
            head, body = line.split('="', 1)
            sym = head.split("str_")[-1].strip()
            f = body.rstrip('";').split(",")
            if len(f) < 10:
                continue
            price = _to_float(f[3])
            pre_close = _to_float(f[2])
            if not price or price <= 0:
                continue
            pct = ((price / pre_close - 1) * 100) if pre_close else 0.0
            out.append({
                "symbol": sym, "price": price, "pct": pct,
                "open": _to_float(f[1]), "high": _to_float(f[4]),
                "low": _to_float(f[5]), "pre_close": pre_close,
                "volume": _to_float(f[8]), "amount": _to_float(f[9]),
                "turnover": None,
            })
    return out


def ensure_realtime(symbols: list[str] = None) -> int:
    """抓取实时快照并落库.

    - 指定 symbols (<=2000): 走新浪直连, 快且不触发东财限流
    - 未指定: 抓全市场快照 (东财优先, 失败退化为新浪)
    """
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if symbols:
        rows = _sina_realtime_batch(list(symbols))
        if rows:
            items = [dict(r, ts=ts) for r in rows]
            return data_store.save_realtime(items)
        # 新浪失败再退回全市场快照

    df, src = _spot_df()
    if df is None:
        return 0
    want = set(symbols) if symbols else None
    items = []
    for it in parse_spot(df, src):
        if want and it["symbol"] not in want:
            continue
        items.append({
            "symbol": it["symbol"], "ts": ts,
            "price": it["price"], "pct": it["pct"],
            "open": it["open"], "high": it["high"], "low": it["low"],
            "pre_close": it["pre_close"], "volume": it["volume"],
            "amount": it["amount"], "turnover": it["turnover"],
        })
    return data_store.save_realtime(items)


# ==================== 全球主要市场指数 (首页展示) ====================
# 目标指数清单: (名称, 匹配关键字, 国旗图标, 地区分组)
_GLOBAL_TARGETS = [
    # A股
    ("上证指数", "上证指数", "🇨🇳", "中国"),
    ("深证成指", "深证成指", "🇨🇳", "中国"),
    ("创业板指", "创业板指", "🇨🇳", "中国"),
    ("沪深300", "沪深300", "🇨🇳", "中国"),
    # 美股
    ("道琼斯", "道琼斯", "🇺🇸", "美洲"),
    ("纳斯达克", "纳斯达克", "🇺🇸", "美洲"),
    ("标普500", "标普500", "🇺🇸", "美洲"),
    # 欧洲
    ("德国DAX", "DAX", "🇩🇪", "欧洲"),
    ("英国富时100", "富时", "🇬🇧", "欧洲"),
    ("法国CAC40", "CAC", "🇫🇷", "欧洲"),
    # 亚太
    ("日经225", "日经", "🇯🇵", "亚太"),
    ("韩国KOSPI", "韩国", "🇰🇷", "亚太"),
    ("澳洲标普200", "澳大利亚", "🇦🇺", "亚太"),
    # 港股
    ("恒生指数", "恒生", "🇭🇰", "港股"),
    ("恒生科技", "恒生科技", "🇭🇰", "港股"),
    # 商品
    ("COMEX黄金", "黄金", "🥇", "商品"),
    ("WTI原油", "原油", "🛢️", "商品"),
    # 汇率
    ("美元/人民币", "美元", "💱", "汇率"),
    # 加密货币
    ("比特币", "比特币", "₿", "加密"),
]
_GLOBAL_CACHE = {"ts": 0.0, "data": None}


def _ak_call(fn_names, **kwargs):
    """按候选名依次尝试调用 akshare 函数, 兼容不同版本接口重命名"""
    import akshare as ak
    for name in fn_names:
        fn = getattr(ak, name, None)
        if fn is None:
            continue
        try:
            return fn(**kwargs)
        except Exception:
            continue
    return None


def _try_zh_index_em() -> dict:
    """A股指数快照: 新浪源可靠, 东财源易被限流"""
    out = {}
    # 新浪源 (容器内基本稳定)
    df = _ak_call(["stock_zh_index_spot_sina", "index_zh_spot_sina"])
    if df is not None:
        try:
            for _, r in df.iterrows():
                name = str(r.get("名称", ""))
                price = _to_float(r.get("最新价"))
                pct = _to_float(r.get("涨跌幅"))
                if price and price > 0:
                    out[name] = {"price": price, "pct": pct}
        except Exception:
            pass
    return out


def _try_global_index_em() -> dict:
    """欧/日/韩/澳主要指数: 东财全球指数快照 (一次请求覆盖全部)"""
    out = {}
    df = _ak_call(["index_global_spot_em", "index_global_spot_sina"])
    if df is None or df.empty:
        return out
    # 目标名称 -> 匹配关键字 (按顺序取第一个命中)
    want = [("德国DAX", ("DAX",)), ("英国富时100", ("富时100", "FTSE")),
            ("法国CAC40", ("CAC",)), ("日经225", ("日经",)),
            ("韩国KOSPI", ("韩国",)), ("澳洲标普200", ("澳大利亚", "澳洲")),
            ("恒生指数", ("恒生指数",))]
    try:
        for _, r in df.iterrows():
            nm = str(r.get("名称", ""))
            price = _to_float(r.get("最新价"))
            pct = _to_float(r.get("涨跌幅"))
            if not price or price <= 0:
                continue
            for cn, kws in want:
                if cn in out:
                    continue
                if any(k in nm for k in kws):
                    out[cn] = {"price": price, "pct": pct}
    except Exception:
        pass
    return out


def _try_btc() -> dict:
    """比特币现价: OKX -> Binance -> Coinbase 依次兜底"""
    import json
    import urllib.request
    out = {}
    sources = (
        ("https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT", "okx"),
        ("https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT", "binance"),
        ("https://api.coinbase.com/v2/prices/BTC-USD/spot", "coinbase"),
    )
    for url, kind in sources:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            j = json.loads(urllib.request.urlopen(req, timeout=6).read().decode("utf-8"))
            if kind == "okx" and j.get("data"):
                d = j["data"][0]
                price = float(d["last"])
                pct = (price / float(d["open24h"]) - 1) * 100
            elif kind == "binance":
                price = float(j["lastPrice"])
                pct = float(j["priceChangePercent"])
            elif kind == "coinbase":
                price = float(j["data"]["amount"])
                pct = 0.0
            else:
                continue
            if price > 0:
                out["比特币"] = {"price": price, "pct": pct}
                return out
        except Exception:
            continue
    return out


def _try_us_index() -> dict:
    """美股指数: 新浪日频 (取最近交易日, 稳定不触发 push2 限流)"""
    out = {}
    mapping = {"道琼斯": ".DJI", "标普500": ".INX", "纳斯达克": ".IXIC"}
    for cn_name, sym in mapping.items():
        df = _ak_call(["index_us_stock_sina", "index_us_daily_sina"], symbol=sym)
        if df is None or df.empty:
            continue
        try:
            col = "close" if "close" in df.columns else "收盘"
            close = float(df[col].iloc[-1])
            prev = float(df[col].iloc[-2]) if len(df) >= 2 else close
            if close > 0 and prev > 0:
                out[cn_name] = {"price": close,
                                "pct": (close / prev - 1) * 100}
        except Exception:
            continue
    return out


def _try_gold_oil() -> dict:
    """全球大宗商品实时行情 (新浪外盘直连, 约 0.1 秒; 失败才退化为东财)"""
    import urllib.request
    out = {}
    # 新浪外盘期货: hf_GC=纽约黄金(COMEX), hf_CL=纽约原油(WTI)
    # 字段: [0]当前价 [6]行情时间 [7]昨收 [13]名称
    mapping = {"hf_GC": "COMEX黄金", "hf_CL": "WTI原油"}
    try:
        url = "https://hq.sinajs.cn/list=" + ",".join(mapping)
        req = urllib.request.Request(url, headers={
            "Referer": "https://finance.sina.com.cn",
            "User-Agent": "Mozilla/5.0"})
        raw = urllib.request.urlopen(req, timeout=8).read().decode("gbk", errors="replace")
        for line in raw.strip().split("\n"):
            if '="' not in line:
                continue
            code = line.split("str_")[-1].split("=")[0].strip()
            cn_name = mapping.get(code)
            if not cn_name:
                continue
            f = line.split('="', 1)[1].rstrip('";').split(",")
            if len(f) < 8:
                continue
            price = _to_float(f[0])
            pre_close = _to_float(f[7])
            if price and price > 0:
                out[cn_name] = {
                    "price": price,
                    "pct": (price / pre_close - 1) * 100 if pre_close else 0.0,
                }
    except Exception:
        pass

    if out:
        return out

    # 兜底: 东财全球期货 (较慢, 620+ 品种)
    import akshare as ak
    try:
        df = ak.futures_global_realtime_em()
        if df is not None and not df.empty:
            for _, r in df.iterrows():
                nm = str(r.get("名称", ""))
                p = _to_float(r.get("最新价"))
                pct = _to_float(r.get("涨跌幅"))
                if not p:
                    continue
                if "黄金" in nm and out.get("COMEX黄金") is None:
                    out["COMEX黄金"] = {"price": p, "pct": pct}
                elif ("原油" in nm or "WTI" in nm) and out.get("WTI原油") is None:
                    out["WTI原油"] = {"price": p, "pct": pct}
    except Exception:
        pass
    return out


def _try_fx() -> dict:
    """外汇即期报价 (新浪 fx_spot_quote)"""
    import akshare as ak
    out = {}
    try:
        df = ak.fx_spot_quote()
        if df is not None and not df.empty:
            for _, r in df.iterrows():
                pair = str(r.get("货币对", ""))
                bid = _to_float(r.get("买报价"))
                ask = _to_float(r.get("卖报价"))
                price = (bid + ask) / 2 if (bid and ask) else (bid or ask)
                if not price:
                    continue
                # 匹配常见货币对
                if pair.startswith("USDCNY") or pair.startswith("USD/CNY") or "美元人民币" in pair:
                    out["美元/人民币"] = {"price": price, "pct": 0}
                elif pair.startswith("EURUSD") or pair.startswith("EUR/USD"):
                    out["欧元/美元"] = {"price": price, "pct": 0}
                elif pair.startswith("USDJPY") or pair.startswith("USD/JPY"):
                    out["美元/日元"] = {"price": price, "pct": 0}
                elif pair.startswith("GBPUSD") or pair.startswith("GBP/USD"):
                    out["英镑/美元"] = {"price": price, "pct": 0}
    except Exception:
        pass
    return out


def _fetch_global_markets() -> list[dict]:
    """从各数据源并发拉取全球市场数据 (串行约 10s, 并发约 2s)"""
    from concurrent.futures import ThreadPoolExecutor
    pools = {}
    fns = (_try_zh_index_em, _try_global_index_em, _try_us_index,
           _try_gold_oil, _try_fx, _try_btc)
    with ThreadPoolExecutor(max_workers=len(fns)) as ex:
        for fut in [ex.submit(f) for f in fns]:
            try:
                pools.update(fut.result(timeout=30))
            except Exception:
                continue

    out = []
    for name, kw, flag, group in _GLOBAL_TARGETS:
        hit = None
        for k, v in pools.items():
            if kw in k:
                hit = v
                break
        if hit is not None:
            pct = hit.get("pct") or 0.0
            out.append({
                "name": name, "flag": flag, "group": group,
                "price": hit.get("price"),
                "pct": pct,
                "trend": "up" if pct > 0 else ("down" if pct < 0 else "flat"),
            })
    return out


def refresh_global_markets() -> list[dict]:
    """强制刷新全球市场数据并写入内存 + SQLite 缓存 (供 Worker 定时调用)"""
    import json
    # 先取上一版数据, 用于给无涨跌幅的品种 (如汇率) 计算 tick 变动
    prev_map = {}
    try:
        import data_store
        raw = data_store.get_setting("global_markets_cache")
        if raw:
            for it in json.loads(raw).get("data", []):
                if it.get("price"):
                    prev_map[it.get("name")] = float(it["price"])
    except Exception:
        pass
    for it in _GLOBAL_CACHE["data"] or []:
        if it.get("price"):
            prev_map.setdefault(it.get("name"), float(it["price"]))

    out = _fetch_global_markets()
    for it in out:
        if not it.get("pct") and it.get("price"):
            prev = prev_map.get(it.get("name"))
            if prev and prev > 0:
                it["pct"] = (float(it["price"]) / prev - 1) * 100
                it["trend"] = ("up" if it["pct"] > 0
                               else ("down" if it["pct"] < 0 else "flat"))

    now = time.time()
    _GLOBAL_CACHE["ts"] = now
    _GLOBAL_CACHE["data"] = out
    try:
        import data_store
        payload = json.dumps({"ts": now, "data": out}, ensure_ascii=False)
        data_store.set_setting("global_markets_cache", payload)
    except Exception:
        pass
    return out


_BENCH_CACHE = {"ts": 0.0, "df": None}


def get_benchmark_curve(symbol: str = "sh000300", max_age: float = 86400.0) -> pd.DataFrame:
    """基准指数日线 (date, close). SQLite 缓存 24 小时, 过期用新浪回补.

    akshare 接口名在两个大版本间不同, 这里按候选名依次尝试:
    1.20+ 用 index_zh_daily_sina, 1.16 用 stock_zh_index_daily.
    """
    import json
    now = time.time()
    if _BENCH_CACHE["df"] is not None and now - _BENCH_CACHE["ts"] < 300:
        return _BENCH_CACHE["df"]

    key = "benchmark_" + symbol
    cached = None
    try:
        raw = data_store.get_setting(key)
        if raw:
            obj = json.loads(raw)
            if now - float(obj.get("ts", 0)) < max_age and obj.get("rows"):
                cached = pd.DataFrame(obj["rows"], columns=["date", "close"])
                cached["date"] = pd.to_datetime(cached["date"])
    except Exception:
        cached = None

    if cached is not None and len(cached):
        _BENCH_CACHE.update(ts=now, df=cached)
        return cached

    df = pd.DataFrame(columns=["date", "close"])
    try:
        import akshare as ak
        for fn in ("index_zh_daily_sina", "stock_zh_index_daily"):
            f = getattr(ak, fn, None)
            if f is None:
                continue
            try:
                raw_df = f(symbol=symbol)
            except Exception:
                continue
            if raw_df is None or len(raw_df) == 0:
                continue
            cols = {str(c).lower(): c for c in raw_df.columns}
            if "date" not in cols or "close" not in cols:
                continue
            df = pd.DataFrame({
                "date": pd.to_datetime(raw_df[cols["date"]]),
                "close": pd.to_numeric(raw_df[cols["close"]], errors="coerce"),
            }).dropna()
            if len(df):
                break
    except Exception:
        pass

    if len(df) == 0:
        return cached if cached is not None else df

    try:
        payload = json.dumps({
            "ts": now,
            "rows": [[str(r.date)[:10], float(r.close)] for r in df.itertuples()],
        }, ensure_ascii=False)
        data_store.set_setting(key, payload)
    except Exception:
        pass
    _BENCH_CACHE.update(ts=now, df=df)
    return df


def get_global_markets(ttl: float = 600) -> list[dict]:
    """全球主要市场指数 (内存 + SQLite 双层缓存, 页面调用永远秒开).

    数据由 Worker 定时调用 refresh_global_markets() 刷新 (交易时段 30 秒一次).
    缓存过期也返回最后一版数据, 由页面展示"更新于 xx"提示, 避免整块空白.
    """
    import json
    now = time.time()

    # 1. 内存缓存 (同进程, 最快)
    if _GLOBAL_CACHE["data"] is not None and now - _GLOBAL_CACHE["ts"] < ttl:
        return _GLOBAL_CACHE["data"]

    # 2. SQLite 缓存 (跨进程, Worker 写入)
    try:
        import data_store
        raw = data_store.get_setting("global_markets_cache")
        if raw:
            payload = json.loads(raw)
            data = payload.get("data", [])
            if data:
                _GLOBAL_CACHE["ts"] = payload.get("ts", 0)
                _GLOBAL_CACHE["data"] = data
                return data
    except Exception:
        pass

    # 内存里的旧数据优先于空白
    if _GLOBAL_CACHE["data"]:
        return _GLOBAL_CACHE["data"]
    return []


def global_markets_updated_at() -> float:
    """全球市场缓存最后一次刷新时间戳 (epoch 秒), 0 表示从未刷新"""
    if _GLOBAL_CACHE["ts"]:
        return _GLOBAL_CACHE["ts"]
    try:
        import json
        import data_store
        raw = data_store.get_setting("global_markets_cache")
        if raw:
            return json.loads(raw).get("ts", 0)
    except Exception:
        pass
    return 0.0


# ==================== 策略触发: 按需补全 ====================

def ensure_for_strategy(strategy: str, symbols: list[str],
                        start: str, end: str, progress_cb=None) -> dict:
    """策略运行前调用: 只下载该策略声明需要、且库里缺失的数据"""
    req = requirements_for(strategy)
    res = {"strategy": strategy, "datasets": {}, "ok": True}
    if "daily_bar" in req["datasets"]:
        r = ensure_bars(symbols, start, end, progress_cb=progress_cb)
        res["datasets"]["daily_bar"] = r
    if "fundamental" in req["datasets"]:
        st = data_store.fundamental_stats()
        if st["symbols"] < len(symbols) * 0.5:
            res["datasets"]["fundamental"] = ensure_fundamentals(quarters=2)
        else:
            res["datasets"]["fundamental"] = {"cached": True, **st}
    res["ok"] = all(isinstance(v, dict) for v in res["datasets"].values())
    return res


# ==================== 盘后自动补全 ====================

def after_close_sync(progress_cb=None, fundamentals: bool = False) -> dict:
    """盘后增量补全: 从全市场覆盖日的次日起, 补到「已收盘的最后一个交易日」

    两个坑 (都曾导致漏数据):
      1. 起点不能用 MAX(date) —— 指数/个别标的会把日期抬高, 于是中间缺口永远补不到
      2. 终点不能直接用「今天」 —— 盘中/重启时当天还没收盘, 拉不到K线,
         次日又因为起点已经超过缺口而永久跳过
    """
    symbols = data_store.list_symbols()
    if not symbols:
        return {"error": "库内无标的"}
    today = last_trading_day()
    # 只有过了收盘采集时点 (默认 15:35), 当天才算有完整K线; 盘中/重启时终点退到前一交易日
    close_hm = int(os.environ.get("CLOSE_HOUR", "15")) * 100 + int(os.environ.get("CLOSE_MINUTE", "35"))
    now_hm = datetime.now().hour * 100 + datetime.now().minute
    end = today if now_hm > close_hm else prev_trading_day(today)
    last = data_store.db_stats().get("coverage_last_date")
    if last and last >= end:
        return {"bars": {"downloaded": 0, "skipped": len(symbols)}, "trade_date": end,
                "note": "已是最新"}
    start = today
    if last:
        start = (datetime.strptime(last, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    r = ensure_bars(symbols, start, end, progress_cb=progress_cb)
    out = {"bars": r, "trade_date": end}
    if fundamentals:
        out["fundamental"] = ensure_fundamentals(quarters=1)
    return out
