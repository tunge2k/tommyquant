"""
抓涨停策略回测引擎 —— 可独立调用的核心逻辑
供 Streamlit 应用和 CLI 共用
"""
import pandas as pd
import numpy as np
import os
import random
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import price_limits

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")


# ==================== 回测缓存 (键/序列化, UI 与 Worker 共用) ====================
def bt_cache_key(strategy_key, initial, hold, top, lu_pct, start_d, end_d,
                 fingerprint, cost_str="") -> tuple:
    """返回 (可读参数串, sha1 缓存键)。参数或数据指纹变化 → 缓存自动失效"""
    import hashlib
    key_raw = f"v18|{strategy_key}|{initial}|{hold}|{top}|{lu_pct}|{start_d}|{end_d}|{cost_str}"
    return key_raw, hashlib.sha1((key_raw + "|" + fingerprint).encode()).hexdigest()


def _py_scalar(v):
    """numpy 标量 -> Python 原生类型 (否则 json 会退化成字符串)。
    递归处理 dict / list, 使 cost_summary 等嵌套结构也能正确序列化。"""
    if isinstance(v, dict):
        return {k: _py_scalar(val) for k, val in v.items()}
    if isinstance(v, (list, tuple)):
        return [_py_scalar(x) for x in v]
    return v.item() if hasattr(v, "item") and not isinstance(v, (str, bytes)) else v


def _num(v):
    """历史缓存里可能是 "1000000" 这样的字符串, 读回时还原成数字"""
    if isinstance(v, str):
        try:
            f = float(v)
            return int(f) if v.lstrip("-").isdigit() else f
        except ValueError:
            return v
    return v


def serialize_result(r) -> dict:
    curve = r["curve"].copy()
    curve["date"] = curve["date"].astype(str)
    return {
        "curve": curve.to_dict(orient="records"),
        "trades": [_py_scalar(t) for t in (r.get("trades") or [])],
        "fills": [_py_scalar(f) for f in (r.get("fills") or [])],
        "holdings_log": [_py_scalar(h) for h in (r.get("holdings_log") or [])],
        "performance": {k: _py_scalar(v) for k, v in (r.get("performance") or {}).items()},
        "selection_dates": [str(d)[:10] for d in r.get("selection_dates", [])],
        "trade_dates": [str(d)[:10] for d in r.get("trade_dates", [])],
        "lu_dates": [str(d)[:10] for d in r.get("lu_dates", [])],
        "all_trade_days": [str(d)[:10] for d in r.get("all_trade_days", [])],
        "fill_blocks": r.get("fill_blocks") or {},
    }


def deserialize_result(d) -> dict:
    curve = pd.DataFrame(d["curve"])
    curve["date"] = pd.to_datetime(curve["date"])
    return {
        "curve": curve,
        "trades": d.get("trades") or [],
        "fills": d.get("fills") or [],
        "holdings_log": d.get("holdings_log") or [],
        "performance": {k: _num(v) for k, v in (d.get("performance") or {}).items()},
        "selection_dates": d.get("selection_dates", []),
        "trade_dates": d.get("trade_dates", []),
        "lu_dates": d.get("lu_dates", []),
        "all_trade_days": d.get("all_trade_days", []),
        "fill_blocks": d.get("fill_blocks") or {},
    }


def sina_code(code: str) -> str:
    """股票代码 -> 新浪格式"""
    return f"sh{code}" if code.startswith("6") else f"sz{code}"


def load_cache() -> dict:
    """加载所有缓存的日线数据"""
    raw = {}
    if not os.path.isdir(CACHE_DIR):
        return raw
    for fname in os.listdir(CACHE_DIR):
        if not fname.endswith(".csv"):
            continue
        try:
            df = pd.read_csv(os.path.join(CACHE_DIR, fname))
            df["date"] = pd.to_datetime(df["date"])
            raw[fname.replace(".csv", "")] = df.sort_values("date").reset_index(drop=True)
        except Exception:
            pass
    return raw


def _adjust_fund_splits(df: pd.DataFrame) -> pd.DataFrame:
    """ETF/LOF 份额折算复权 (新浪源为不复权价)

    ETF 大比例分红或 1拆N / N合1 时价格会出现 -60%~-80% 的跳变,
    不复权直接回测会把这些折算当成真实暴跌 (例如 561980 2026-06-26 4.147→0.842)。
    A股/ETF 单日涨跌幅上限最多 20%, 因此把单日 |涨跌幅| > 25% 视为份额折算,
    用当日比例因子回溯调整之前的价格 (价格×因子, 成交量÷因子, 成交额不变)。
    """
    if df is None or len(df) < 2 or "close" not in df.columns:
        return df
    close = pd.to_numeric(df["close"], errors="coerce").to_numpy(dtype=float)
    ratio = close[1:] / close[:-1]
    bad = ~np.isfinite(ratio) | (ratio <= 0)
    flag = (~bad) & ((ratio > 1.25) | (ratio < 0.80))
    if not flag.any():
        return df
    fac = np.ones(len(close), dtype=float)
    for i in np.where(flag)[0]:
        fac[:i + 1] *= ratio[i]
    df = df.copy()
    for c in ("open", "high", "low", "close"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce") * fac
    if "volume" in df.columns:
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce") / fac
    return df


def _adjust_fund_dividends(df: pd.DataFrame, events) -> pd.DataFrame:
    """ETF 分红后复权 (除息日价差抹平, 与份额折算复权叠加)

    新浪 ETF 日线是不复权价, 除息日会凭空掉一个「分红额」的缺口, 回测会把它当成
    真实下跌 (误触发补仓/止损)。这里按后复权口径构造乘法因子 (交易所除权因子):
        f(t) = Π_{除息日 ≤ t} 前收 / (前收 - 每份分红)
        P_hfq(t) = P_raw(t) × f(t)
    后复权只放大除息日之后的价格, 历史K线不随新分红变化 → 增量下载不会产生跳空。

    events: [(date_str, 累计分红), ...] (升序, 来自 data_store.etf_dividend_map)
    """
    if df is None or len(df) < 2 or not events or "close" not in df.columns:
        return df
    df = df.copy()
    close = pd.to_numeric(df["close"], errors="coerce").to_numpy(dtype=float)
    dates = pd.to_datetime(df["date"]).to_numpy()
    evs = []
    prev_cum = 0.0
    for ds, cum in events:
        try:
            evs.append((np.datetime64(pd.Timestamp(ds)), float(cum) - prev_cum))
            prev_cum = float(cum)
        except Exception:
            continue
    if not evs:
        return df
    fac = np.ones(len(df), dtype=float)
    cur, j = 1.0, 0
    for i in range(len(df)):
        while j < len(evs) and evs[j][0] <= dates[i]:
            pay = evs[j][1]
            j += 1
            if pay > 1e-9 and i > 0 and np.isfinite(close[i - 1]) and close[i - 1] > pay:
                cur *= close[i - 1] / (close[i - 1] - pay)
        fac[i] = cur
    for c in ("open", "high", "low", "close"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce") * fac
    return df


def add_factors(df: pd.DataFrame, code: str = None, div_events=None) -> pd.DataFrame | None:
    """为单只股票计算技术面因子

    code 提供时按板块口径标记涨停 (主板 10% / 创业板·科创板 20% / 北交所 30%),
    不再对所有标的写死 9.5%。ETF 额外做「份额折算 + 分红」后复权 (div_events)。
    """
    if df is None or len(df) < 120:
        return None
    # 场内基金先做份额折算复权, 再叠加分红后复权, 否则折算/除息日会被当成暴跌
    if code and price_limits.is_etf(code):
        df = _adjust_fund_splits(df)
        if div_events:
            df = _adjust_fund_dividends(df, div_events)
    df = df.copy()
    for c in ["open", "close", "high", "low", "volume", "turnover", "outstanding_share"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    df["pct"] = df["close"].pct_change() * 100
    df["ma5"] = df["close"].rolling(5).mean()
    df["ma10"] = df["close"].rolling(10).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma60"] = df["close"].rolling(60).mean()
    df["ret_5"] = df["close"].pct_change(5) * 100
    df["ret_20"] = df["close"].pct_change(20) * 100
    df["ret_60"] = df["close"].pct_change(60) * 100
    df["turnover_pct"] = df["turnover"] * 100
    df["cap_yi"] = df["close"] * df["outstanding_share"] / 1e8
    df["above_ma20"] = (df["close"] > df["ma20"]).astype(int)
    df["above_ma60"] = (df["close"] > df["ma60"]).astype(int)
    # MA60 20 日斜率 (中长期趋势强度): >0 表示 MA60 持续上行
    df["ma60_slope"] = (df["ma60"] - df["ma60"].shift(20)) / df["ma60"].shift(20)
    # 近 250 日高点回撤 (负值, 如 -0.25 表示当前价较近250日高点回撤 25%)
    # 用单层 rolling(250) 而非双层, 使因子在 250 个交易日后即生效
    # (原双层 rolling 需 500 天, 导致次新 ETF 的回撤过滤长期空转)
    _roll_max = df["close"].rolling(250).max()
    df["max_dd_250"] = (df["close"] - _roll_max) / _roll_max
    lu_threshold = price_limits.board_limit_pct(code) * 100 - 0.5 if code else 9.5
    df["limit_up"] = (df["pct"] >= lu_threshold).astype(int)
    df["vol_ma5"] = df["volume"].rolling(5).mean()
    df["vol_expand"] = (df["volume"] > df["vol_ma5"] * 1.5).astype(int)
    # 成交额均值: ETF 策略的流动性门槛 (ETF 无市值/换手数据, 用日均成交额代替)
    if "amount" in df.columns:
        df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
        df["amount_ma20"] = df["amount"].rolling(20).mean()
    else:
        df["amount_ma20"] = np.nan

    return df


def compute_factors(raw: dict, div_map: dict = None) -> dict:
    """批量计算因子

    div_map: {symbol: [(除息日, 累计分红), ...]} — ETF 后复权用; 不传时尝试从库读取
    (无库/无记录则退化为只做份额折算复权)。
    """
    if div_map is None:
        try:
            import data_store
            div_map = data_store.etf_dividend_map()
        except Exception:
            div_map = {}
    feat = {}
    for code, df in raw.items():
        f = add_factors(df, code=code, div_events=div_map.get(code))
        if f is not None:
            feat[code] = f
    return feat


# ==================== ETF 选品区域分类 (跨境 / 港股 / 内地) ====================
# ETF 没有行业/成分股元数据, 只能按名称识别投资区域。港股系优先判定 (中概互联
# 同时含港股与美股, 统一归入港股系剔除), 其余含海外指数关键词的才算跨境。
ETF_HK_KEYWORDS = ("恒生", "港股", "香港", "H股", "国企", "中概", "港科",
                   "港中小", "中华港股", "港股通")
ETF_OVERSEAS_KEYWORDS = ("纳指", "纳斯达克", "标普", "道琼斯", "道指", "美国",
                         "美股", "日经", "东证", "日本", "德国", "法国", "英国",
                         "欧洲", "东南亚", "越南", "印度", "沙特", "巴西",
                         "亚太", "新兴市场", "全球", "海外")

_ETF_NAME_CACHE: dict = {"ts": 0.0, "map": {}}


def classify_etf_region(name: str) -> str:
    """按名称判断 ETF 投资区域: hk / overseas / domestic / unknown"""
    s = str(name or "")
    if not s:
        return "unknown"
    if any(k in s for k in ETF_HK_KEYWORDS):
        return "hk"
    # 「标普A股红利」「MSCI中国A股」这类是境内指数, 名称里带海外指数商的字眼
    if "A股" in s or "中国" in s:
        return "domestic"
    if any(k in s for k in ETF_OVERSEAS_KEYWORDS):
        return "overseas"
    return "domestic"


def is_overseas_etf(name: str) -> bool:
    """跨境(外盘)ETF 且非港股系"""
    return classify_etf_region(name) == "overseas"


def _etf_name_map(ttl: float = 600.0) -> dict:
    """ETF 名称映射 (库内 etf_name), 10 分钟进程内缓存; 无库时返回空字典"""
    import time
    now = time.time()
    if _ETF_NAME_CACHE["map"] and now - _ETF_NAME_CACHE["ts"] < ttl:
        return _ETF_NAME_CACHE["map"]
    try:
        import data_store
        m = data_store.etf_name_map()
    except Exception:
        m = {}
    if m:
        _ETF_NAME_CACHE["map"] = m
        _ETF_NAME_CACHE["ts"] = now
    return m


def get_selection_dates(feat: dict) -> set:
    """获取所有周频选股日（每周一或该周第一个交易日）"""
    if not feat:
        return set()
    sample = list(feat.values())[0].copy()
    sample["wk"] = sample["date"].dt.strftime("%Y-%W")
    return set(sample.drop_duplicates("wk", keep="first")["date"].tolist())


class BarIndex:
    """按日取截面索引 + 列式日频矩阵 (借鉴 bt 的数据布局)

    双轨存储:
      1) 按标的保留原始日期数组/列数组, row/last_row/prev_close 用 searchsorted
      2) 按全市场交易日日历预构建列式矩阵 (D, N), D=交易日数, N=标的数;
         每日 snapshot 退化为取一行 numpy 视图, 比逐日 searchsorted 全标的快 1000x
    两种布局共存, 互不影响; 新增列式矩阵的额外内存: 每列 float32 约 D*N*4 字节,
    5000 只 × 900 天 ≈ 18 MB/列, 常见列 (< 20) 总内存 < 400 MB, 可接受。
    """

    def __init__(self, feat: dict):
        self._dates = {}
        self._cols = {}
        arrs = []
        for sc, df in feat.items():
            d = df["date"].to_numpy(dtype="datetime64[ns]")
            self._dates[sc] = d
            self._cols[sc] = {c: df[c].to_numpy() for c in df.columns if c != "date"}
            arrs.append(d)
        if arrs:
            self.all_dates = pd.DatetimeIndex(np.unique(np.concatenate(arrs))).sort_values()
        else:
            self.all_dates = pd.DatetimeIndex([])
            self._dt_idx = {}
            self._codes = []
            self._code_idx = {}
            self._matrix_cols = set()
            self._matrix = {}
            self._has_matrix = np.zeros((0, 0), dtype=bool)
            return

        # 列式矩阵预构建: 取所有标的共有列, 对齐到统一交易日历
        codes = list(feat.keys())
        self._codes = codes
        self._code_idx = {c: j for j, c in enumerate(codes)}
        dates_arr = self.all_dates.to_numpy(dtype="datetime64[ns]")
        self._dt_idx = {d: i for i, d in enumerate(dates_arr)}
        D, N = len(self.all_dates), len(codes)

        # 交集列 (所有 feat 中都出现的列); 原始列数组仍保留完整列
        common = None
        for sc in codes:
            cs = set(self._cols[sc].keys())
            common = cs if common is None else (common & cs)
        common = common or set()
        self._matrix_cols = common

        self._matrix = {}
        self._has_matrix = np.zeros((D, N), dtype=bool)
        for c in common:
            self._matrix[c] = np.full((D, N), np.nan, dtype=np.float32)

        for j, sc in enumerate(codes):
            d = self._dates[sc]
            if len(d) == 0:
                continue
            pos = np.searchsorted(dates_arr, d)
            hit = (pos < D) & (dates_arr[pos] == d)
            src = pos[hit]
            if src.size == 0:
                continue
            self._has_matrix[src, j] = True
            for c in common:
                self._matrix[c][src, j] = self._cols[sc][c][hit].astype(np.float32)

        # --- 静态过滤器预构建 (N 长度数组, 与 codes 顺序一致) ---
        # 创业板/科创板 (北交所 8/4 开头的数据层已排除)
        pure_arr = np.array([
            c[2:] if isinstance(c, str) and c[:2] in ("sh", "sz") else str(c)
            for c in codes
        ])
        self._gem_arr = np.array([p.startswith("30") or p.startswith("688") for p in pure_arr])
        # 场内基金 (ETF/LOF): 股票策略池需显式排除, ETF 策略池只保留它们
        self._etf_arr = np.array([price_limits.is_etf(c) for c in codes])
        # 跨境(外盘)ETF 且非港股系: 按库内 ETF 名称识别 (无名称数据时全 False)
        _names = _etf_name_map()
        self._etf_overseas_arr = np.array([
            bool(price_limits.is_etf(c)) and is_overseas_etf(_names.get(c, ""))
            for c in codes])
        # 每只标的上市首日 (datetime64[ns], 无数据用 NaT)
        self._first_dates = np.empty(N, dtype="datetime64[ns]")
        self._first_dates.fill(np.datetime64("NaT"))
        for j, sc in enumerate(codes):
            arr = self._dates.get(sc)
            if arr is not None and len(arr) > 0:
                self._first_dates[j] = arr[0]

    @property
    def codes(self):
        """列式矩阵的标的列表 (固定顺序, 与 _matrix 的列一致)"""
        return self._codes

    def row(self, code, date):
        """取单只标的某一天的记录 (持仓估值/卖出用, 不必建全市场截面)"""
        arr = self._dates.get(code)
        if arr is None or len(arr) == 0:
            return None
        d = np.datetime64(pd.Timestamp(date).to_datetime64())
        i = np.searchsorted(arr, d)
        if i < len(arr) and arr[i] == d:
            return {c: v[i] for c, v in self._cols[code].items()}
        return None

    def last_row(self, code, date):
        """取某只标的在 date 当天或之前最近的一条记录 (停牌/缺行情时估值用)

        停牌股当日没有K线, 用最近收盘价标记市值; 若该股在 date 之前完全没有数据
        (例如数据缺口在早期), 返回 None 由调用方决定回退值。
        """
        arr = self._dates.get(code)
        if arr is None or len(arr) == 0:
            return None
        d = np.datetime64(pd.Timestamp(date).to_datetime64())
        i = int(np.searchsorted(arr, d, side="right")) - 1
        if i < 0:
            return None
        return {c: v[i] for c, v in self._cols[code].items()}

    def prev_close(self, code, date):
        """取 date 之前最近一根K线的收盘价 (涨跌停价基准); 无历史返回 None"""
        arr = self._dates.get(code)
        if arr is None or len(arr) == 0:
            return None
        d = np.datetime64(pd.Timestamp(date).to_datetime64())
        i = int(np.searchsorted(arr, d, side="left")) - 1
        if i < 0:
            return None
        return float(self._cols[code]["close"][i])

    def snapshot(self, date):
        """取某一天的截面

        返回 CrossSection 视图 (列式矩阵 O(N) 行切片, 0.03ms/天, 比旧 dict-per-code 快 1500x),
        接口与 dict 兼容 (items/keys/__getitem__/__len__), 策略可零改动直接跑。
        真正需要高性能的策略可以用 CrossSection.col(name) 直接拿 numpy 列做向量化过滤。
        """
        if self._matrix and self._dt_idx:
            date_ts = pd.Timestamp(date)
            row_idx = self._dt_idx.get(date_ts)
            if row_idx is not None:
                return CrossSection(
                    row_idx=row_idx,
                    col_mask=np.flatnonzero(self._has_matrix[row_idx]),
                    codes=np.array(self._codes, dtype=object),
                    matrix=self._matrix,
                    cols=self._matrix_cols,
                    bar_index=self,
                )
        # 回退路径 (date 不在交易日历, 理论上不会出现)
        d = np.datetime64(pd.Timestamp(date).to_datetime64())
        out = {}
        for sc, arr in self._dates.items():
            i = np.searchsorted(arr, d)
            if i < len(arr) and arr[i] == d:
                out[sc] = {c: v[i] for c, v in self._cols[sc].items()}
        return out


class CrossSection:
    """某一天的全市场截面 (列式矩阵零拷贝视图)

    模拟 dict 接口 (策略里用 snap.items()/snap[code] 均可用), 同时提供 col(name)
    直接取 numpy 列做向量化过滤。避免了 Python 为每只标的建 dict 的巨大开销。
    bar_index 引用让 CrossSection 可以访问 BarIndex 预构建的静态过滤器数组。
    """
    __slots__ = ("_i", "_jmask", "_codes", "_matrix", "_cols", "_len", "_bar_index")

    def __init__(self, row_idx, col_mask, codes, matrix, cols, bar_index=None):
        self._i = row_idx          # 交易日索引 (矩阵的行)
        self._jmask = col_mask     # 当日有行情的标的索引数组 (矩阵的列)
        self._codes = codes        # 所有标的 (np.array[str])
        self._matrix = matrix      # {col: np.ndarray[D,N]}
        self._cols = cols          # set[str] 共有列名
        self._len = len(col_mask)
        self._bar_index = bar_index  # BarIndex 引用, 用于访问预计算过滤器

    # --- dict 模拟接口 (让现有策略零改动直接跑) ---
    def __len__(self):
        return self._len

    def __contains__(self, code):
        return code in self._codes[self._jmask]

    def __iter__(self):
        for j in self._jmask:
            yield self._codes[j]

    def keys(self):
        return [self._codes[j] for j in self._jmask]

    def values(self):
        for j in self._jmask:
            yield self._row_dict(j)

    def items(self):
        for j in self._jmask:
            yield self._codes[j], self._row_dict(j)

    def __getitem__(self, code):
        pos = self._codes[self._jmask]
        idx = np.flatnonzero(pos == code)
        if idx.size == 0:
            raise KeyError(code)
        return self._row_dict(self._jmask[idx[0]])

    def get(self, code, default=None):
        try:
            return self[code]
        except KeyError:
            return default

    def _row_dict(self, j):
        """为某列构造一条 dict (仅在兼容旧逻辑时用, 尽量少走这个)"""
        return {c: float(self._matrix[c][self._i, j]) for c in self._cols}

    # --- 高性能接口 (新策略应优先用) ---
    @property
    def codes(self):
        """当日有行情的标的列表"""
        return [self._codes[j] for j in self._jmask]

    def col(self, name):
        """取某列在当日有行情标的上的 numpy 数组 (shape = self._len)
        列不存在返回全 NaN 数组 (长度与 codes 对齐, 保持接口稳定)"""
        if name in self._matrix:
            return self._matrix[name][self._i, self._jmask]
        return np.full(self._len, np.nan, dtype=np.float32)

    def static_filter(self, name):
        """取 BarIndex 预构建的静态过滤器数组, 在当日有行情标的上的值
        name: 'gem' (创业板/科创板) / 'first_date' (上市首日 datetime64)
        返回 shape=(self._len,) 的 numpy 数组"""
        bi = self._bar_index
        if bi is None:
            return None
        if name == "gem":
            return bi._gem_arr[self._jmask]
        if name == "etf":
            return bi._etf_arr[self._jmask]
        if name == "etf_overseas":
            return bi._etf_overseas_arr[self._jmask]
        if name == "first_date":
            return bi._first_dates[self._jmask]
        return None


_BAR_INDEX_CACHE = {}


def build_date_index(feat: dict) -> BarIndex:
    """构建日期索引 (同一份 feat 只构一次, 多个策略共用)"""
    key = id(feat)
    hit = _BAR_INDEX_CACHE.get(key)
    if hit is not None and hit[0] is feat:
        return hit[1]
    idx = BarIndex(feat)
    _BAR_INDEX_CACHE.clear()          # 只保留最新一份, 避免内存堆积
    _BAR_INDEX_CACHE[key] = (feat, idx)
    return idx


# ==================== 选股策略函数 ====================
# 每个策略: (snapshot_dict, **params) -> list[sina_codes]

def select_momentum(snap: dict, top_n: int = 5, ret_20_min: float = 5.0,
                    cap_max: float = 200.0, turn_min: float = 1.0, turn_max: float = 15.0,
                    require_ma20: bool = True) -> list:
    """动量 + 均线确认 + 小市值"""
    candidates = []
    for code, row in snap.items():
        if np.isnan(row.get("ret_20", np.nan)) or row["ret_20"] < ret_20_min:
            continue
        if require_ma20 and row.get("above_ma20", 0) != 1:
            continue
        mc = row.get("cap_yi", np.nan)
        if np.isnan(mc) or mc <= 0 or mc > cap_max:
            continue
        to = row.get("turnover_pct", 0)
        if to < turn_min or to > turn_max:
            continue
        candidates.append((code, row["ret_20"]))
    candidates.sort(key=lambda x: x[1], reverse=True)
    return [c[0] for c in candidates[:top_n]]


def select_chip(snap: dict, top_n: int = 5, cap_max: float = 100.0,
                turn_min: float = 3.0) -> list:
    """纯小市值 + 高换手"""
    candidates = []
    for code, row in snap.items():
        mc = row.get("cap_yi", np.nan)
        if np.isnan(mc) or mc <= 0 or mc > cap_max:
            continue
        to = row.get("turnover_pct", 0)
        if to < turn_min:
            continue
        candidates.append((code, 100 - mc + to * 5))
    candidates.sort(key=lambda x: x[1], reverse=True)
    return [c[0] for c in candidates[:top_n]]


def select_breakout(snap: dict, top_n: int = 5, ret_5_min: float = 3.0,
                    vol_expand: bool = True, cap_max: float = 150.0) -> list:
    """短周期突破 + 量能放大"""
    candidates = []
    for code, row in snap.items():
        if np.isnan(row.get("ret_5", np.nan)) or row["ret_5"] < ret_5_min:
            continue
        if vol_expand and row.get("vol_expand", 0) != 1:
            continue
        mc = row.get("cap_yi", np.nan)
        if np.isnan(mc) or mc <= 0 or mc > cap_max:
            continue
        candidates.append((code, row["ret_5"]))
    candidates.sort(key=lambda x: x[1], reverse=True)
    return [c[0] for c in candidates[:top_n]]


def select_random(snap: dict, top_n: int = 5, seed: int = 42) -> list:
    """随机对照"""
    codes = list(snap.keys())
    rng = random.Random(seed)
    return rng.sample(codes, min(top_n, len(codes)))


def _est_pe_pb(close, f: dict) -> tuple:
    """用当日收盘价 + 当时可见的季报 EPS/BPS 估算 PE/PB (point-in-time)
    避免依赖固定的估值快照, 回测任何日期都能得到当时的估值水平
    """
    if not f or not close or close <= 0:
        return None, None
    eps, bps = f.get("eps"), f.get("bps")
    pe = pb = None
    if eps and eps > 0:
        frac = {"0331": 0.25, "0630": 0.5, "0930": 0.75, "1231": 1.0}.get(
            str(f.get("report_date") or "")[4:8], 1.0)
        eps_ttm = eps / frac
        if eps_ttm > 0:
            pe = close / eps_ttm
    if bps and bps > 0:
        pb = close / bps
    return pe, pb


def select_smallcap_rotation(snap: dict, top_n: int = 3, cap_max: float = 30.0,
                             ret_20_min: float = 5.0, ret_60_min: float = 0.0,
                             require_ma20: bool = True, require_ma60: bool = True,
                             turn_min: float = 3.0, turn_max: float = 20.0,
                             volume_ratio_min: float = 0.8,
                             profit_yoy_min: float = 20.0, revenue_yoy_min: float = 10.0,
                             max_debt_ratio: float = 70.0,
                             max_goodwill_ratio: float = 30.0) -> list:
    """
    小市值轮动 · 戴维斯双击
    硬条件:
      1. 小市值: cap_max 亿以下 (弹性)
      2. 高成长: 净利同比 > 20% 且营收同比 > 10% (戴维斯第一击: EPS 提升)
      3. 低估值: 0 < PE < 所属行业中位PE (第二击: PE 扩张空间)
      4. 财务健康: 资产负债率 < 70%, 商誉/净资产 < 30%
      5. 行业竞争力: ROE 高于行业中位
      6. 趋势确认: ret_20 > 5%, 站上 MA20/MA60, 换手 3-20%, 不缩量
    若该日期没有可用的财务数据 (历史早期), 自动退化为纯动量选股。
    """
    candidates = []
    for code, row in snap.items():
        mc = row.get("cap_yi", np.nan)
        ret20 = row.get("ret_20", np.nan)
        ret60 = row.get("ret_60", np.nan)
        to = row.get("turnover_pct", 0)
        vratio = row.get("vol_ma5", 0)
        vol = row.get("volume", 0)

        # --- 行情/趋势条件 ---
        if np.isnan(mc) or mc <= 0 or mc > cap_max:
            continue
        if np.isnan(ret20) or ret20 < ret_20_min:
            continue
        if ret_60_min and (np.isnan(ret60) or ret60 < ret_60_min):
            continue
        if require_ma20 and row.get("above_ma20", 0) != 1:
            continue
        if require_ma60 and row.get("above_ma60", 0) != 1:
            continue
        if to < turn_min or to > turn_max:
            continue
        if vratio > 0 and vol > 0 and (vol / vratio) < volume_ratio_min:
            continue

        # --- 基本面条件 (有数据时生效) ---
        f = row.get("_f")
        fmed = row.get("_fmed") or {}
        if f:
            py = f.get("profit_yoy")
            ry = f.get("revenue_yoy")
            pe = f.get("pe_ttm")
            if not pe:
                pe, _ = _est_pe_pb(row.get("close"), f)
            roe = f.get("roe")
            debt = f.get("debt_ratio")
            gwr = f.get("goodwill_ratio")
            ind = f.get("industry") or "其他"
            med_pe, med_roe = fmed.get(ind, (None, None))

            if py is not None and py < profit_yoy_min:
                continue
            if ry is not None and ry < revenue_yoy_min:
                continue
            if pe is not None and (pe <= 0 or (med_pe and pe > med_pe)):
                continue
            if debt is not None and debt > max_debt_ratio:
                continue
            if gwr is not None and gwr > max_goodwill_ratio:
                continue
            if roe is not None and med_roe is not None and roe < med_roe:
                continue

        # --- 综合评分 ---
        score = ret20 * 0.35 + (ret60 if not np.isnan(ret60) else 0) * 0.15 \
                + (cap_max - mc) / cap_max * 20 * 0.10 + to * 0.10
        if f:
            py = f.get("profit_yoy") or 0
            roe = f.get("roe") or 0
            pe = f.get("pe_ttm")
            if not pe:
                pe, _ = _est_pe_pb(row.get("close"), f)
            pe = pe or 0
            score += min(py, 200) * 0.15 + min(roe, 30) * 0.10
            if pe > 0:
                score += (50.0 / pe) * 5
        candidates.append((code, score))

    candidates.sort(key=lambda x: x[1], reverse=True)
    return [c[0] for c in candidates[:top_n]]


# ==================== 财务数据注入 (避免未来函数) ====================
_FUNDA_CACHE = {}


def fundamental_asof(date_str) -> dict:
    """取 date_str 当日可见的财务数据 (ann_date <= date), 按季度缓存"""
    key = str(date_str)[:7]
    if key in _FUNDA_CACHE:
        return _FUNDA_CACHE[key]
    try:
        import data_store
        m = data_store.load_fundamentals(asof=str(date_str)[:10])
    except Exception:
        m = {}
    # 行业估值/ROE 中位数
    buckets = {}
    for sym, r in m.items():
        buckets.setdefault(r.get("industry") or "其他", []).append(
            (r.get("pe_ttm"), r.get("roe")))
    med = {}
    for ind, lst in buckets.items():
        pes = [p for p, _ in lst if p and p > 0]
        roes = [x for _, x in lst if x is not None]
        med[ind] = (float(np.median(pes)) if pes else None,
                    float(np.median(roes)) if roes else None)
    out = {"map": m, "median": med}
    _FUNDA_CACHE[key] = out
    return out


_DIV_CACHE = {}


def dividend_years_asof(date_str=None) -> dict:
    """取 date_str 公告日之前已公告现金分红的 {symbol: set(财年 int)}, 按月缓存

    date_str 为空 → 全量 (今日选股等最新截面场景)
    """
    key = str(date_str)[:7] if date_str else "latest"
    if key in _DIV_CACHE:
        return _DIV_CACHE[key]
    try:
        import data_store
        m = data_store.load_dividend_years(asof=str(date_str)[:10] if date_str else None)
    except Exception:
        m = {}
    _DIV_CACHE[key] = m
    return m


def _filter_bad_fundamentals(f: dict, report_date=None, exclude_st=True,
                             exclude_ocf_diverge=True,
                             exclude_nonop_profit=True) -> str | None:
    """不良标的识别 (财务造假/虚假盈利): ST / 现金流背离 / 营业外依赖

    返回剔除原因 (None=通过)。无财务数据时放行, 与既有策略退化约定一致:
      1. ST/*ST/退市整理  — 监管风险与财务造假高发池
      2. 现金流背离 (纸面利润) — EPS>0 但累计经营现金流为负
         (Q1 单季累计现金流为负较常见, 豁免; 半年报/三季报/年报仍为负 → 利润无现金支撑)
      3. 虚假盈利 (营业外依赖) — 净利润>0 而营业利润<=0 (利润全靠变卖资产/补助),
         或营业外贡献过半利润 (>50%)
    长期不分红检查与财务行解耦, 由调用方直接用 code 查分红历史 (见 _filter_market_cap_universe)
    """
    if not f:
        return None
    if exclude_st and f.get("is_st"):
        return "ST/退市风险"
    if exclude_ocf_diverge:
        eps, ocf = f.get("eps"), f.get("ocf_per_share")
        rd = str(report_date or f.get("report_date") or "")[4:8]
        if eps and eps > 0 and ocf is not None and ocf < 0 and rd != "0331":
            return "现金流背离(利润无现金支撑)"
    if exclude_nonop_profit:
        npf, op = f.get("net_profit"), f.get("operating_profit")
        if npf and npf > 0 and op is not None:
            if op <= 0:
                return "营业外盈利(主业亏损靠变卖资产)"
            if (npf - op) / npf > 0.5:
                return "营业外盈利(过半利润非主业)"
    return None



# ==================== 市值策略 (自定义类: 支持75%资金等比例 + 动态再平衡) ====================

# MA30 缓存模块级缓存, 同一份 feat + lookback 只算一次 (避免参数寻优时每个组合都重算)
# 仿照 static_meta / build_date_index 的 id(feat) 缓存模式
_MA30_CACHE = {}


def build_ma30_cache(feat: dict, lookback: int = 5) -> dict:
    """预计算每只标的的周线收盘序列与 MA30 前缀和 (只算一次, 避免逐日 resample)

    返回 {code: {wk_idx, prefix, ma30, dates, closes}}:
      wk_idx  周五为界的周标签 (含最后一周, 可能是未走完的周)
      prefix  周收盘价前缀和, prefix[i] = sum(closes[:i])
      ma30    已走完各周的 MA30 (rolling(30).mean())
      dates / closes  原始日线日期与收盘价, 用于把「未走完的这一周」替换为当日收盘

    逐日调用 resample 在 5000 只 × 900 天规模下要跑几百万次, 是回测的主要瓶颈;
    这里把与日期无关的部分全部预计算, 单日查询降到 O(log n)。
    """
    lb = int(lookback)
    cache_key = (id(feat), lb)
    hit = _MA30_CACHE.get(cache_key)
    if hit is not None and hit[0] is feat:
        return hit[1]

    cache = {}
    if feat:
        for code, df in feat.items():
            if "date" not in df.columns:
                continue
            close_col = "close" if "close" in df.columns else "adj_close"
            if close_col not in df.columns:
                continue
            open_col = "open" if "open" in df.columns else None
            cols = ["date", close_col] + ([open_col] if open_col else [])
            sub = df[cols].dropna(subset=[close_col])
            if len(sub) < 60:
                continue
            sub = sub.sort_values("date")
            dts = pd.to_datetime(sub["date"])
            vals_arr = sub[close_col].to_numpy(dtype="float64")
            wk = pd.Series(vals_arr, index=dts).resample("W-FRI").last().dropna()
            # 周开盘价 = 该周首个交易日的开盘价, 用于判断「周阳线」(收盘 > 开盘)
            if open_col:
                wk_open = pd.Series(sub[open_col].to_numpy(dtype="float64"), index=dts) \
                    .resample("W-FRI").first()
                wk_open = wk_open.reindex(wk.index)
                open_vals = wk_open.to_numpy(dtype="float64")
            else:
                open_vals = np.full(len(wk), np.nan)
            need = 30 + lb
            if len(wk) < need:
                continue   # 周数不足 → 不参与 MA30 判断 (与原逻辑一致: 放行)
            vals = wk.to_numpy(dtype="float64")
            s = pd.Series(vals)
            cache[code] = {
                "wk_idx": wk.index.values,
                "prefix": np.concatenate(([0.0], np.cumsum(vals))),
                "ma30": s.rolling(30).mean().to_numpy(),
                "ma60": s.rolling(60).mean().to_numpy(),
                "ma120": s.rolling(120).mean().to_numpy(),
                "w_open": open_vals,
                "w_close": vals,
                "dates": dts.to_numpy(dtype="datetime64[ns]"),
                "closes": vals_arr,
                "lb": lb,
            }

    # 全局缓存只保留最新一份, 避免内存堆积
    if len(_MA30_CACHE) >= 3:
        _MA30_CACHE.clear()
    _MA30_CACHE[cache_key] = (feat, cache)
    return cache


def ma30_slope_up(cache: dict, code: str, date, lookback: int = None) -> bool | None:
    """按日期查周线 MA30 是否向上; None = 数据不足, 不做判断

    与逐日 resample 口径完全一致: 已走完的周用该周最后收盘价; 当前这一周
    尚未走完, 用当日收盘价代替该周收盘 (即 MA30(t) 随当日价格逐日变化)。
    """
    item = cache.get(code) if cache else None
    if item is None or date is None:
        return None
    d = pd.Timestamp(date).to_datetime64()
    lb = int(item["lb"] if lookback is None else lookback)
    prefix = item["prefix"]
    nwk = len(item["wk_idx"])
    # 周标签是「该周周五」, 当日所在周标签可能晚于当日 → 用第一个 >= 当日的标签
    i = int(np.searchsorted(item["wk_idx"], d, side="left"))
    beyond = i >= nwk
    if beyond:                       # 当日之后该股已无行情 → 用最后一个完整周
        i = nwk - 1
    if i < 29 + lb:
        return None
    cur_sum = prefix[i] - prefix[i - 29]   # 前 29 个完整周的收盘之和
    if beyond:
        cur = (cur_sum + (prefix[i + 1] - prefix[i])) / 30.0
    else:
        di = int(np.searchsorted(item["dates"], d, side="right")) - 1
        if di < 0:
            return None
        close_now = item["closes"][di]
        if not np.isfinite(close_now):
            return None
        cur = (cur_sum + close_now) / 30.0  # 未走完的一周用当日收盘
    past = item["ma30"][i - lb]
    if not np.isfinite(past):
        return None
    return bool(cur > past)


def ma30_rising_weeks(cache: dict, code: str, date, weeks: int) -> bool | None:
    """周K线 MA30 是否连续 weeks 周上升 (建仓趋势确认)

    判定 MA30(t) > MA30(t-1) > ... > MA30(t-weeks), 其中 t 为当前周
    (未走完的一周用当日收盘补齐, 与 ma30_slope_up 口径一致)。
    None = 周线数据不足, 不做判断; weeks<=0 = 不启用该条件。
    """
    weeks = int(weeks or 0)
    if weeks <= 0:
        return None
    item = cache.get(code) if cache else None
    if item is None or date is None:
        return None
    d = pd.Timestamp(date).to_datetime64()
    prefix = item["prefix"]
    nwk = len(item["wk_idx"])
    i = int(np.searchsorted(item["wk_idx"], d, side="left"))
    beyond = i >= nwk
    if beyond:
        i = nwk - 1
    # 需要 ma30[i-1] ... ma30[i-weeks] 均有效 (索引 >= 29), 且当周补齐数据可用
    if i < 29 + weeks:
        return None
    cur_sum = prefix[i] - prefix[i - 29]
    if beyond:
        cur = (cur_sum + (prefix[min(i + 1, len(prefix) - 1)] - prefix[i])) / 30.0
    else:
        di = int(np.searchsorted(item["dates"], d, side="right")) - 1
        if di < 0:
            return None
        close_now = item["closes"][di]
        if not np.isfinite(close_now):
            return None
        cur = (cur_sum + close_now) / 30.0

    ma30 = item["ma30"]
    prev = ma30[i - 1]
    if not (np.isfinite(cur) and np.isfinite(prev)):
        return None
    if cur <= prev:                      # 当周 MA30 未上升
        return False
    for k in range(1, weeks):            # 之前各周同样逐周上升
        a, b = ma30[i - k], ma30[i - k - 1]
        if not (np.isfinite(a) and np.isfinite(b)):
            return None
        if a <= b:
            return False
    return True


def weekly_bull_weeks(cache: dict, code: str, date, weeks: int = 2) -> bool | None:
    """最近 weeks 个「已走完的周」是否都是阳线 (周收盘 > 周开盘)

    用于建仓确认: 周 MA30 由降转升后, 还需连续阳线证明上涨趋势真实。
    None = 数据不足/缺开盘价, 不做判断 (放行); weeks<=0 = 不启用。
    """
    weeks = int(weeks or 0)
    if weeks <= 0:
        return None
    item = cache.get(code) if cache else None
    if item is None or date is None:
        return None
    w_open = item.get("w_open")
    w_close = item.get("w_close")
    if w_open is None or w_close is None:
        return None
    d = pd.Timestamp(date).to_datetime64()
    nwk = len(item["wk_idx"])
    i = int(np.searchsorted(item["wk_idx"], d, side="left"))
    if i >= nwk:
        i = nwk                      # 当日之后无行情 → 全部周都已走完
    if i < weeks:
        return None
    for k in range(1, weeks + 1):
        o, c = w_open[i - k], w_close[i - k]
        if not (np.isfinite(o) and np.isfinite(c)):
            return None
        if c <= o:
            return False
    return True


def weekly_ma30_premium(cache: dict, code: str, date) -> float | None:
    """最新价相对周线 MA30 的溢价率 (最新价/MA30 - 1); None = 数据不足

    用于「不追高」: 价格已远离 MA30 时不建仓, 避免买在阶段高点。
    """
    item = cache.get(code) if cache else None
    if item is None or date is None:
        return None
    d = pd.Timestamp(date).to_datetime64()
    prefix = item["prefix"]
    nwk = len(item["wk_idx"])
    i = int(np.searchsorted(item["wk_idx"], d, side="left"))
    beyond = i >= nwk
    if beyond:
        i = nwk - 1
    if i < 29:
        return None
    cur_sum = prefix[i] - prefix[i - 29]
    if beyond:
        cur = (cur_sum + (prefix[min(i + 1, len(prefix) - 1)] - prefix[i])) / 30.0
        di = len(item["closes"]) - 1
    else:
        di = int(np.searchsorted(item["dates"], d, side="right")) - 1
        if di < 0:
            return None
        close_now = item["closes"][di]
        if not np.isfinite(close_now):
            return None
        cur = (cur_sum + close_now) / 30.0
    last = item["closes"][di]
    if not (np.isfinite(cur) and np.isfinite(last)) or cur <= 0:
        return None
    return float(last / cur - 1.0)


def weekly_long_trend_ok(cache: dict, code: str, date) -> bool | None:
    """长期趋势确认: 价格站上最长可用周均线(优先 MA120, 不足降级 MA60),
    且周 MA30 在长期均线之上 (多头排列雏形)

    用于建仓确认: 只在「长期向上」的趋势中建仓, 过滤下跌趋势中的死猫反弹。
    返回 None = 周线不足 60 周 (连 MA60 都没有), 数据不足放行;
         True  = 价格 > 长均线 且 MA30 > 长均线;
         False = 价格在长均线下方 / MA30 在长均线下方 (长期趋势向下, 不建仓)。
    """
    item = cache.get(code) if cache else None
    if item is None or date is None:
        return None
    wk_idx = item.get("wk_idx")
    w_close = item.get("w_close")
    ma30 = item.get("ma30")
    ma60 = item.get("ma60")
    ma120 = item.get("ma120")
    if wk_idx is None or w_close is None or ma60 is None:
        return None
    d = pd.Timestamp(date).to_datetime64()
    nwk = len(wk_idx)
    i = int(np.searchsorted(wk_idx, d, side="left"))
    if i >= nwk:
        i = nwk - 1
    if i < 0:
        return None
    # 最长可用周均线: 优先 MA120, 其次 MA60
    long_ma = None
    if ma120 is not None and i < len(ma120) and np.isfinite(ma120[i]):
        long_ma = float(ma120[i])
    elif i < len(ma60) and np.isfinite(ma60[i]):
        long_ma = float(ma60[i])
    if long_ma is None or long_ma <= 0:
        return None                       # 不足 60 周, 放行
    px = float(w_close[i])
    if not np.isfinite(px):
        return None
    above_long = px > long_ma
    # 短期均线在长期均线之上 = 多头排列雏形 (趋势向上的佐证)
    ma30_ok = True
    if ma30 is not None and i < len(ma30) and np.isfinite(ma30[i]):
        ma30_ok = float(ma30[i]) > long_ma
    return bool(above_long and ma30_ok)


_STATIC_META_CACHE = {}

# 全市场过滤器预计算缓存, 键 (id(feat), ma30_lb):
#   gem_arr[N]: bool, True=创业板/科创板, 一次性构建
#   first_date_years_arr[N]: float, 每只标的上市首日距今的年数 (以某个基准日计算)
#   ma30_up_mat[D,N]: bool, 每日每只标的 MA30 是否向上 (None=缺周线不足)
# 构建一次, 逐日过滤退化为取一行掩码, 无 Python 循环
_FILTER_PRE_CACHE = {}


def static_meta(feat: dict) -> dict:
    """每只标的的静态属性: (六位代码, 上市首日)

    逐日全市场过滤时, 原实现每次都执行 feat[code]["date"].iloc[0] 取上市日,
    单次约 40 µs, 5000 只 × 900 天约占过滤耗时的 44%。这里按 feat 缓存一次。
    """
    if not feat:
        return {}
    key = id(feat)
    hit = _STATIC_META_CACHE.get(key)
    if hit is not None and hit[0] is feat:
        return hit[1]
    meta = {}
    for code, df in feat.items():
        pure = code[2:] if code.startswith(("sh", "sz")) else code
        try:
            first = df["date"].iloc[0]
        except Exception:
            continue
        meta[code] = (pure, first)
    _STATIC_META_CACHE.clear()          # 只留最新一份, 避免内存堆积
    _STATIC_META_CACHE[key] = (feat, meta)
    return meta


def _filter_market_cap_universe(snap, feat: dict = None, date=None,
                                cap_min: float = 15.0, cap_max: float = 200.0,
                                min_listed_years: float = 3.0,
                                exclude_st: bool = True,
                                exclude_ocf_diverge: bool = True,
                                exclude_nonop_profit: bool = True,
                                exclude_no_dividend: bool = True,
                                dividend_years: int = 3,
                                f_map: dict = None, div_map: dict = None,
                                ma30_up_only: bool = True,
                                ma30_lookback: int = 5,
                                ma30_entry_weeks: int = 0,
                                ma30_cache: dict = None,
                                pool_type: str = "stock",
                                etf_mom_days: int = 60,
                                etf_min_amount: float = 0.5,
                                etf_min_history: int = 120,
                                premium_map: dict = None,
                                flow_map: dict = None,
                                premium_cap: float = 0.0,
                                flow_min: float | None = None,
                                market_ok: bool | None = None,
                                max_dd_threshold: float = 0.30,
                                require_ma60_up: bool = True,
                                ret20_min: float = 0.0,
                                ret60_min: float = 0.0,
                                overseas_only: bool = False) -> list:
    """
    标的池过滤 (股票池 / ETF 池共用入口, pool_type 分流):
    - stock: 市值区间 + 上市年限 + 财务/分红剔除 + 周K MA30 上升, 按市值升序
      (显式排除 ETF/LOF: 场内基金 outstanding_share 缺失, cap_yi=0 会混进小市值池)
    - etf: 无财务/分红/市值过滤, 按「近 N 日涨幅」降序排名,
      并叠加 20 日均成交额流动性门槛 + 最少K线长度 + 周K MA30 趋势确认
      + 2026-11-20 新增: 最大回撤 < 30% / MA60 上行 / 多周期动量非负

    CrossSection 输入走向量化快路径 (numpy 掩码); 普通 dict 输入走回退路径。
    """
    if pool_type == "etf":
        return _filter_etf_universe(
            snap, feat=feat, date=date, mom_days=etf_mom_days,
            min_amount=etf_min_amount, min_history=etf_min_history,
            ma30_up_only=ma30_up_only, ma30_lookback=ma30_lookback,
            ma30_entry_weeks=ma30_entry_weeks, ma30_cache=ma30_cache,
            premium_map=premium_map, flow_map=flow_map,
            premium_cap=premium_cap, flow_min=flow_min, market_ok=market_ok,
            max_dd_threshold=max_dd_threshold, require_ma60_up=require_ma60_up,
            ret20_min=ret20_min, ret60_min=ret60_min,
            overseas_only=overseas_only)

    # --- CrossSection 向量化快路径 ---
    if isinstance(snap, CrossSection):
        return _filter_crosssection_vec(
            snap, feat, date, cap_min, cap_max, min_listed_years,
            exclude_st, exclude_ocf_diverge, exclude_nonop_profit,
            exclude_no_dividend, dividend_years, f_map, div_map,
            ma30_up_only, ma30_lookback, ma30_entry_weeks, ma30_cache)

    # --- dict 输入: 原逐标的逻辑 (回退) ---
    any_fund_filter = exclude_st or exclude_ocf_diverge or exclude_nonop_profit
    if exclude_no_dividend and div_map is None:
        div_map = dividend_years_asof(str(date)[:10] if date is not None else None)
    if exclude_no_dividend and not div_map:
        exclude_no_dividend = False
    sel_year = pd.Timestamp(date).year if date is not None else None

    meta = static_meta(feat)
    date_ts = pd.Timestamp(date) if date is not None else None

    candidates = []
    for code, row in snap.items():
        pure, first_date = meta.get(code, (None, None))
        if pure is None:
            pure = code[2:] if code.startswith(("sh", "sz")) else code
        if pure.startswith(("30", "688")):
            continue
        # 股票池显式排除场内基金 (ETF/LOF 无市值数据, 不能进小市值排序)
        if price_limits.is_etf(code):
            continue
        mc = row.get("cap_yi", np.nan)
        if np.isnan(mc) or mc <= 0 or mc < cap_min or mc > cap_max:
            continue
        if first_date is not None and date_ts is not None:
            if (date_ts - first_date).days / 365.25 < min_listed_years:
                continue
        elif feat and code in feat:
            if len(feat[code]) < int(min_listed_years * 250):
                continue
        if any_fund_filter:
            f = (f_map or {}).get(code) or row.get("_f")
            reason = _filter_bad_fundamentals(
                f, exclude_st=exclude_st, exclude_ocf_diverge=exclude_ocf_diverge,
                exclude_nonop_profit=exclude_nonop_profit)
            if reason:
                continue
        if exclude_no_dividend:
            ys = div_map.get(code, ())
            y = sel_year or datetime.now().year
            if not any((y - k) in range(0, int(dividend_years) + 1) for k in ys):
                continue
        if ma30_up_only and ma30_cache is not None and date is not None:
            if ma30_slope_up(ma30_cache, code, date) is False:
                continue
            if ma30_entry_weeks and ma30_rising_weeks(
                    ma30_cache, code, date, ma30_entry_weeks) is False:
                continue
        elif ma30_up_only and feat and code in feat:
            df = feat[code]
            close_col = "close" if "close" in df.columns else "adj_close"
            sub = df[["date", close_col]].dropna().copy()
            sub["date"] = pd.to_datetime(sub["date"])
            if date is not None:
                sub = sub[sub["date"] <= pd.Timestamp(date)]
            if len(sub) >= 5:
                wk = sub.sort_values("date").set_index("date")[close_col] \
                         .resample("W-FRI").last().dropna().values
                if len(wk) >= 30 + ma30_lookback:
                    ma30_now = wk[-30:].mean()
                    ma30_past = wk[-30 - ma30_lookback:-ma30_lookback].mean()
                    if ma30_now <= ma30_past:
                        continue
                if ma30_entry_weeks and len(wk) >= 30 + ma30_entry_weeks:
                    # 连续 N 周周K MA30 逐周上升 (建仓趋势确认)
                    seq = [wk[len(wk) - 30 - k:len(wk) - k].mean()
                           for k in range(0, ma30_entry_weeks + 1)]
                    if any(seq[k] <= seq[k + 1] for k in range(ma30_entry_weeks)):
                        continue
        candidates.append((code, mc))
    candidates.sort(key=lambda x: x[1])
    return [c[0] for c in candidates]


def _filter_crosssection_vec(cs: "CrossSection", feat, date,
                              cap_min, cap_max, min_listed_years,
                              exclude_st, exclude_ocf_diverge, exclude_nonop_profit,
                              exclude_no_dividend, dividend_years, f_map, div_map,
                              ma30_up_only, ma30_lookback, ma30_entry_weeks,
                              ma30_cache) -> list:
    """CrossSection 输入的向量化过滤版本 (直接 numpy 掩码, 零 dict 构造)

    预构建静态过滤器 (gem, first_dates) + MA30 日频布尔矩阵, 逐日过滤退化为
    一行掩码运算 (numpy 一次), 5000 × 900 天从 90ms/day 降到 1ms/day。
    """
    codes = cs._codes[cs._jmask]  # np.array[str]
    N = len(codes)
    if N == 0:
        return []

    # --- 市值范围 (numpy 掩码, 零 Python 循环) ---
    cap = cs.col("cap_yi")
    mask = np.isfinite(cap) & (cap > 0) & (cap >= cap_min) & (cap <= cap_max)

    # --- 创业板/科创板 (BarIndex 预构建) ---
    gem = cs.static_filter("gem")
    if gem is not None:
        mask &= ~gem

    # --- 场内基金 (ETF/LOF) 不进股票池 ---
    etf = cs.static_filter("etf")
    if etf is not None:
        mask &= ~etf

    # --- 上市年限 (BarIndex 预构建 first_dates, 直接 numpy 比较) ---
    if mask.any():
        first_dates = cs.static_filter("first_date")
        date_ts = pd.Timestamp(date) if date is not None else None
        if first_dates is not None and date_ts is not None:
            dt = np.datetime64(date_ts.to_datetime64())
            age_days = (dt - first_dates).astype("timedelta64[D]").astype(float)
            mask &= ~((age_days < min_listed_years * 365.25) & np.isfinite(age_days))

    # --- MA30 上升 (预构建日频矩阵, 取一行掩码) ---
    if ma30_up_only and ma30_cache is not None and date is not None and mask.any():
        ma30_mat = _get_ma30_up_matrix(cs._bar_index, ma30_cache, ma30_lookback,
                                       ma30_entry_weeks)
        if ma30_mat is not None:
            row_idx = cs._i
            if 0 <= row_idx < ma30_mat.shape[0]:
                mask &= ma30_mat[row_idx, cs._jmask]

    # --- 按市值从小到大排序 ---
    if not mask.any():
        return []
    sort_cap = np.where(mask, cap, np.inf)
    order = np.argsort(sort_cap, kind="stable")
    picked = order[mask[order]]
    return [codes[j] for j in picked]


def _filter_etf_universe(snap, feat: dict = None, date=None,
                         mom_days: int = 60, min_amount: float = 0.5,
                         min_history: int = 120,
                         ma30_up_only: bool = True, ma30_lookback: int = 5,
                         ma30_entry_weeks: int = 0, ma30_cache: dict = None,
                         premium_map: dict = None, flow_map: dict = None,
                         premium_cap: float = 0.0,
                         flow_min: float | None = None,
                         market_ok: bool | None = None,
                         max_dd_threshold: float = 0.30,
                         require_ma60_up: bool = True,
                         ret20_min: float = 0.0,
                         ret60_min: float = 0.0,
                         overseas_only: bool = False) -> list:
    """ETF 池筛选: 中长期向上趋势 + 低回撤 + 动量排名 (近 N 日涨幅降序)

    新增 (2026-11-20) 优质标的过滤:
      9.  历史最大回撤: 近 250 日最大回撤 < max_dd_threshold (默认 30%)
      10. 中长期趋势: 收盘价站上 MA60 且 MA60 斜率 > 0 (持续上行)
      11. 多周期动量: ret_20 >= ret20_min 且 ret_60 >= ret60_min (默认都 >= 0)
      叠加在原有的流动性/数据长度/MA30/溢价/资金流过滤之上。

    与股票池的差异 (ETF 没有财务/分红/市值/换手/ST 概念):
      1. 只保留场内基金 (price_limits.is_etf)
      2. 流动性: 近 20 日日均成交额 >= min_amount 亿元 (ETF 无市值, 用成交额代替)
      3. 数据长度: 上市/有行情 >= min_history 根K线 (新 ETF 数据不足不参与排序)
      4. 趋势: 周K MA30 向上 (+ 可选连续 N 周上升的建仓确认), 复用股票池的预计算矩阵
      5. 按近 mom_days 日涨幅从大到小排序 (20 或 60 日)
      6. 溢价过滤: 当日溢价率 > premium_cap(%) 剔除 (QDII 溢价回落是主要亏损源)
      7. 资金流: 近 5 日净申购/规模 < flow_min(%) 剔除
      8. 市场级风控: market_ok=False (全市场 ETF 资金持续净流出) 时返回空池 → 触发清仓

    说明: 溢价/资金流数据缺失 (未同步或历史不可得) 时视为「无信号」, 不剔除标的,
    避免接入初期把整个池子误删; market_ok=None 同样视为不拦截。
    """
    if market_ok is False:
        return []
    mom_col = f"ret_{int(mom_days)}"
    if isinstance(snap, CrossSection):
        codes = snap._codes[snap._jmask]
        if len(codes) == 0:
            return []
        etf = snap.static_filter("etf")
        if etf is None:
            return []
        mask = etf.copy()

        # 选品范围: 只做跨境(外盘)ETF, 并剔除港股系 (恒生/港股通/H股/中概等)
        if overseas_only:
            ovs = snap.static_filter("etf_overseas")
            if ovs is not None and ovs.any():
                mask &= ovs

        amt = snap.col("amount_ma20")
        mask &= np.isfinite(amt) & (amt >= float(min_amount) * 1e8)

        ret = snap.col(mom_col)
        if not np.isfinite(ret).any():      # 兼容旧因子缓存
            ret = snap.col("ret_60")
        mask &= np.isfinite(ret)

        # 溢价 / 资金流 (仅在有数据的标的上生效)
        if premium_cap and premium_cap > 0 and premium_map:
            pv = np.array([premium_map.get(c, np.nan) for c in codes], dtype=float)
            mask &= ~(np.isfinite(pv) & (pv > float(premium_cap)))
        if flow_min is not None and flow_map:
            fv = np.array([flow_map.get(c, np.nan) for c in codes], dtype=float)
            mask &= ~(np.isfinite(fv) & (fv < float(flow_min)))

        date_ts = pd.Timestamp(date) if date is not None else None
        first_dates = snap.static_filter("first_date")
        if first_dates is not None and date_ts is not None:
            dt = np.datetime64(date_ts.to_datetime64())
            age_days = (dt - first_dates).astype("timedelta64[D]").astype(float)
            # 交易日数量约等于日历天数 × 250/365
            approx_bars = age_days * 250.0 / 365.0
            mask &= ~((approx_bars < int(min_history)) & np.isfinite(approx_bars))

        if ma30_up_only and ma30_cache is not None and date is not None and mask.any():
            ma30_mat = _get_ma30_up_matrix(snap._bar_index, ma30_cache,
                                           ma30_lookback, ma30_entry_weeks)
            if ma30_mat is not None and 0 <= snap._i < ma30_mat.shape[0]:
                mask &= ma30_mat[snap._i, snap._jmask]

        # 中长期趋势: 站上 MA60 且 MA60 斜率 > 0 (因子缺失时不拦截, 避免次新ETF误杀)
        if require_ma60_up:
            above60 = snap.col("above_ma60")
            slope = snap.col("ma60_slope")
            above_ok = (above60 == 1) | ~np.isfinite(above60)
            slope_ok = (np.isfinite(slope) & (slope > 0)) | ~np.isfinite(slope)
            mask &= above_ok & slope_ok

        # 近 250 日最大回撤 < 阈值 (max_dd_250 为负值, 如 -0.25; 缺失不拦截)
        if max_dd_threshold is not None:
            mdd = snap.col("max_dd_250")
            mask &= (np.isfinite(mdd) & (mdd > -float(max_dd_threshold))) | ~np.isfinite(mdd)

        # 多周期动量: ret_20 / ret_60 不低于各自阈值 (缺失不拦截)
        r20 = snap.col("ret_20")
        r60 = snap.col("ret_60")
        mask &= (np.isfinite(r20) & (r20 >= float(ret20_min))) | ~np.isfinite(r20)
        mask &= (np.isfinite(r60) & (r60 >= float(ret60_min))) | ~np.isfinite(r60)

        if not mask.any():
            return []
        sort_ret = np.where(mask, ret, -np.inf)
        order = np.argsort(-sort_ret, kind="stable")
        picked = [codes[j] for j in order[mask[order]]]
        # 建仓确认 (连续阳线 + 不追高): 仅作用于新标的入选, 不影响存量持仓调仓
        if ma30_cache is not None and date is not None:
            picked = [c for c in picked if _entry_confirm_ok(ma30_cache, c, date)]
        return picked

    # --- dict 回退路径 ---
    date_ts = pd.Timestamp(date) if date is not None else None
    names = _etf_name_map() if overseas_only else {}
    out = []
    for code, row in snap.items():
        if not price_limits.is_etf(code):
            continue
        if overseas_only and names and not is_overseas_etf(names.get(code, "")):
            continue
        amt = row.get("amount_ma20", np.nan)
        if not np.isfinite(amt) or amt < float(min_amount) * 1e8:
            continue
        r = row.get(mom_col, np.nan)
        if not np.isfinite(r):
            r = row.get("ret_60", np.nan)
        if not np.isfinite(r):
            continue
        if premium_cap and premium_cap > 0 and premium_map:
            pv = premium_map.get(code)
            if pv is not None and float(pv) > float(premium_cap):
                continue
        if flow_min is not None and flow_map:
            fv = flow_map.get(code)
            if fv is not None and float(fv) < float(flow_min):
                continue
        if feat and code in feat and date_ts is not None:
            df = feat[code]
            if int((df["date"] <= date_ts).sum()) < int(min_history):
                continue
        if ma30_up_only and ma30_cache is not None and date is not None:
            if ma30_slope_up(ma30_cache, code, date) is False:
                continue
            if ma30_entry_weeks and ma30_rising_weeks(
                    ma30_cache, code, date, ma30_entry_weeks) is False:
                continue
            # 建仓确认: 连续阳线 + 不追高
            if not _entry_confirm_ok(ma30_cache, code, date):
                continue
        # 中长期趋势: 站上 MA60 且 MA60 斜率 > 0 (因子缺失时不拦截)
        if require_ma60_up:
            above = row.get("above_ma60")
            if above is not None and above != 1:
                continue
            slope = row.get("ma60_slope", np.nan)
            if np.isfinite(slope) and slope <= 0:
                continue
        # 近 250 日最大回撤 < 阈值 (缺失不拦截)
        if max_dd_threshold is not None:
            mdd = row.get("max_dd_250", np.nan)
            if np.isfinite(mdd) and mdd <= -float(max_dd_threshold):
                continue
        # 多周期动量 (缺失不拦截)
        r20 = row.get("ret_20", np.nan)
        r60 = row.get("ret_60", np.nan)
        if np.isfinite(r20) and r20 < float(ret20_min):
            continue
        if np.isfinite(r60) and r60 < float(ret60_min):
            continue
        out.append((code, float(r)))
    out.sort(key=lambda x: x[1], reverse=True)
    return [c for c, _ in out]


def _get_ma30_up_matrix(bar_index, ma30_cache, lookback,
                        weeks: int = 0) -> np.ndarray | None:
    """预构建 [D, N] 布尔矩阵, 记录每日每只标的 MA30 是否向上

    严格对齐 ma30_slope_up 的逐日口径 (含未走完一周用当日收盘补齐、beyond 边界等),
    构建一次 ~1s (5000×900), 之后逐日过滤只是取一行布尔数组, 无 Python 循环。
    weeks>0 时额外要求 MA30 连续 weeks 周逐周上升 (建仓趋势确认)。
    缓存挂在 bar_index 实例上, 生命周期与 bar_index 一致。
    """
    if bar_index is None:
        return None
    weeks = int(weeks or 0)
    cache_attr = f"_ma30_up_mat_{lookback}_{weeks}"
    mat = getattr(bar_index, cache_attr, None)
    if mat is not None:
        return mat

    dates_arr = bar_index.all_dates.to_numpy(dtype="datetime64[ns]")
    D = len(dates_arr)
    codes = bar_index._codes
    N = len(codes)
    mat = np.zeros((D, N), dtype=bool)

    for j, code in enumerate(codes):
        item = ma30_cache.get(code) if ma30_cache else None
        if item is None:
            continue
        lb = int(item["lb"] if lookback is None else lookback)
        prefix = item["prefix"]
        wk_idx = item["wk_idx"]
        closes = item["closes"]
        dates_ns = item["dates"]
        nwk = len(wk_idx)
        ma30 = item["ma30"]

        # --- 逐日查找 ---
        wi = np.searchsorted(wk_idx, dates_arr, side="left")  # 第一个 wk_idx >= date
        beyond = wi >= nwk
        wi_eff = np.where(beyond, nwk - 1, wi)

        # 有效日期: 有足够的历史 (i >= 29 + max(lookback, 连续上升周数))
        valid = wi_eff >= (29 + max(lb, weeks))

        if not valid.any():
            continue

        # cur_sum = prefix[i] - prefix[i-29]  (周 i-29..i-1 总和, 29 个完整周)
        wi_29 = np.maximum(wi_eff - 29, 0)
        cur_sum = prefix[wi_eff] - prefix[wi_29]
        # 当 wi_eff - 29 < 0 时 prefix[0] 是 0, prefix[wi_eff] - 0 不对 —— 但这种情况 valid=False 已排除

        # 当前周收盘: beyond 时用完整周收盘, 否则用当日收盘补齐
        di = np.searchsorted(dates_ns, dates_arr, side="right") - 1
        di = np.clip(di, 0, len(closes) - 1)
        close_now = closes[di].astype(np.float64)

        # beyond=True: wk_idx[wi+1] 是下一周的周五, prefix[wi+1] - prefix[wi] 是当前完整周收盘
        wi_next = np.minimum(wi_eff + 1, len(prefix) - 1)
        cur = np.where(
            beyond,
            (cur_sum + (prefix[wi_next] - prefix[wi_eff])) / 30.0,
            (cur_sum + close_now) / 30.0,
        )

        # past: ma30[i - lb], 需要 valid 范围内
        past_idx = wi_eff - lb
        past = np.full(D, np.nan)
        in_range = past_idx >= 0
        past[in_range] = ma30[past_idx[in_range]]

        mask_j = valid & np.isfinite(cur) & np.isfinite(past) & (cur > past)

        # --- 连续 weeks 周逐周上升: MA30(t) > MA30(t-1) > ... > MA30(t-weeks) ---
        if weeks > 0:
            def _lookup(offset):
                idx = wi_eff - offset
                arr = np.full(D, np.nan)
                ok = idx >= 0
                arr[ok] = ma30[idx[ok]]
                return arr

            prev = _lookup(1)
            mask_j &= np.isfinite(prev) & (cur > prev)
            for k in range(1, weeks):
                a, b = _lookup(k), _lookup(k + 1)
                mask_j &= np.isfinite(a) & np.isfinite(b) & (a > b)

        mat[:, j] = mask_j

    setattr(bar_index, cache_attr, mat)
    return mat


def select_market_cap(snap: dict, top_n: int = 3,
                      cap_min: float = 15.0, cap_max: float = 200.0,
                      min_listed_years: float = 3.0,
                      exclude_st: bool = True,
                      exclude_ocf_diverge: bool = True,
                      exclude_nonop_profit: bool = True,
                      exclude_no_dividend: bool = True,
                      dividend_years: int = 3) -> list:
    """函数式市值选股 (给今日选股/快速回测用)
    财务上下文: 读行内 _f (由 get_today_picks 注入); 分红上下文按截面日期加载
    """
    # 截面日期: 函数式路径读行内 _sel_date (FunctionStrategyAdapter 注入),
    # 今日选股路径读真实 date 列; 都没有 → 最新数据兜底
    snap_date = None
    for row in snap.values():
        d = row.get("_sel_date") or row.get("date")
        if d is not None:
            snap_date = pd.Timestamp(d)
            break
    pool = _filter_market_cap_universe(
        snap, cap_min=cap_min, cap_max=cap_max,
        min_listed_years=min_listed_years,
        exclude_st=exclude_st, exclude_ocf_diverge=exclude_ocf_diverge,
        exclude_nonop_profit=exclude_nonop_profit,
        exclude_no_dividend=exclude_no_dividend,
        dividend_years=dividend_years,
        div_map=dividend_years_asof(str(snap_date)[:10] if snap_date is not None else None),
        f_map=None, date=snap_date)
    return pool[:top_n]


def select_etf(snap: dict, top_n: int = 5,
               mom_days: int = 60, min_amount: float = 0.5,
               min_history: int = 120,
               max_dd_threshold: float = 0.30,
               require_ma60_up: bool = True,
               ret20_min: float = 0.0, ret60_min: float = 0.0,
               overseas_only: bool = True) -> list:
    """函数式 ETF 选股 (给今日选股用)

    复用 _filter_etf_universe 的全部过滤: 流动性 / 数据长度 / 周K MA30 /
    溢价 / 资金流 / 最大回撤 / MA60上行 / 多周期动量, 再按近 mom_days 日涨幅降序。
    注意: 今日选股路径 ma30_cache 为 None, 周K MA30 过滤自动跳过 (不拦截)。
    """
    snap_date = None
    for row in snap.values():
        d = row.get("_sel_date") or row.get("date")
        if d is not None:
            snap_date = pd.Timestamp(d)
            break
    pool = _filter_etf_universe(
        snap, date=snap_date, mom_days=mom_days,
        min_amount=min_amount, min_history=min_history,
        max_dd_threshold=max_dd_threshold,
        require_ma60_up=require_ma60_up,
        ret20_min=ret20_min, ret60_min=ret60_min,
        overseas_only=overseas_only)
    return pool[:top_n]


STRATEGIES = {
    "小市值轮动·戴维斯双击": select_smallcap_rotation,
    "动量+均线+小市值": select_momentum,
    "市值策略": select_market_cap,   # 函数式 (给今日选股/快速回测)
    "ETF策略": select_etf,           # 函数式 (给今日选股)
}


# ==================== 事件驱动回测引擎 ====================

class _Event:
    """事件基类"""
    type = "base"


class MarketBarEvent(_Event):
    """每日开盘行情事件 (含全市场截面 + 持仓标的行情)"""
    type = "bar"

    def __init__(self, date, snapshot: dict, bars: "BarIndex"):
        self.date = date
        self.snapshot = snapshot   # {code: {col:val}} 全市场截面
        self.bars = bars


class SignalEvent(_Event):
    """策略产生的买卖信号 (T日收盘后产生, T+1开盘执行)

    扩展: 自定义策略可以通过 rebalance_orders 直接指定具体数量的订单,
    用于再平衡、动态仓位调整等场景。
    """
    type = "signal"

    def __init__(self, date, buys: list = None, sells: list = None,
                 rebalance_orders: list = None, sell_reasons: dict = None):
        self.date = date                    # 信号产生日
        self.buys = buys or []              # [code, ...] 待买入 (引擎计算数量)
        self.sells = sells or []            # [code, ...] 待卖出 (引擎清仓)
        self.rebalance_orders = rebalance_orders or []  # [OrderEvent, ...] 策略指定数量
        self.sell_reasons = sell_reasons or {}          # {code: 卖出原因}, 供成交流水展示


class OrderEvent(_Event):
    """订单事件"""
    type = "order"

    def __init__(self, date, code, direction, quantity):
        self.date = date
        self.code = code
        self.direction = direction   # "buy" / "sell"
        self.quantity = quantity


class FillEvent(_Event):
    """成交事件 (含真实成本)"""
    type = "fill"

    def __init__(self, date, code, direction, quantity, fill_price,
                 commission, stamp_tax, transfer_fee, slippage_cost,
                 impact_cost, reason=""):
        self.date = date
        self.code = code
        self.direction = direction
        self.quantity = quantity
        self.fill_price = fill_price
        self.commission = commission
        self.stamp_tax = stamp_tax
        self.transfer_fee = transfer_fee
        self.slippage_cost = slippage_cost
        self.impact_cost = impact_cost
        self.reason = reason

    @property
    def total_cost(self):
        return (self.commission + self.stamp_tax + self.transfer_fee
                + self.slippage_cost + self.impact_cost)


class CostModel:
    """A 股真实交易成本模型

    佣金: 股票万1.154 (最低1元), 场内ETF/LOF 万1 (最低0.2元), 双向
    印花税: 0.05%, 仅卖出
    过户费: 0.001%, 双向
    滑点: 0.1% (单边)
    冲击成本: 订单金额/当日成交额 > 5% 时触发
    """

    COMMISSION_RATE = 0.0001154     # 股票 万1.154
    COMMISSION_MIN = 1.0
    ETF_COMMISSION_RATE = 0.0001    # ETF/LOF 万1
    ETF_COMMISSION_MIN = 0.2
    STAMP_TAX_RATE = 0.0005         # 仅卖出
    TRANSFER_FEE_RATE = 0.00001
    SLIPPAGE_RATE = 0.001
    IMPACT_THRESHOLD = 0.05     # 占当日成交额 5%
    MAX_IMPACT_RATE = 0.02      # 冲击成本上限: 不超过交易金额的 2% (防止异常成交额数据导致天价成本)

    def __init__(self, commission_rate=None, commission_min=None,
                 etf_commission_rate=None, etf_commission_min=None,
                 stamp_tax_rate=None, transfer_fee_rate=None,
                 slippage_rate=None):
        if commission_rate is not None:
            self.COMMISSION_RATE = float(commission_rate)
        if commission_min is not None:
            self.COMMISSION_MIN = float(commission_min)
        if etf_commission_rate is not None:
            self.ETF_COMMISSION_RATE = float(etf_commission_rate)
        if etf_commission_min is not None:
            self.ETF_COMMISSION_MIN = float(etf_commission_min)
        if stamp_tax_rate is not None:
            self.STAMP_TAX_RATE = float(stamp_tax_rate)
        if transfer_fee_rate is not None:
            self.TRANSFER_FEE_RATE = float(transfer_fee_rate)
        if slippage_rate is not None:
            self.SLIPPAGE_RATE = float(slippage_rate)

    def commission_of(self, code, amount):
        """佣金 (双边): 股票与场内基金费率/最低收费分开"""
        if price_limits.is_etf(code):
            return max(amount * self.ETF_COMMISSION_RATE, self.ETF_COMMISSION_MIN)
        return max(amount * self.COMMISSION_RATE, self.COMMISSION_MIN)

    def calc_fill(self, code, direction, quantity, ref_price, day_amount=0.0):
        """计算成交价 + 各项成本

        direction: 'buy' 加价滑点, 'sell' 减价滑点
        day_amount: 当日该股票成交额 (用于冲击成本)
        返回 (fill_price, commission, stamp_tax, transfer_fee, slippage_cost, impact_cost)
        """
        if direction == "buy":
            fill_price = ref_price * (1 + self.SLIPPAGE_RATE)
        else:
            fill_price = ref_price * (1 - self.SLIPPAGE_RATE)

        amount = fill_price * quantity
        slippage_cost = abs(fill_price - ref_price) * quantity

        # 佣金 (双向, 股票最低1元 / ETF最低0.2元)
        commission = self.commission_of(code, amount)

        # 印花税 (仅卖出, ETF/LOF 免征)
        stamp_tax = (amount * self.STAMP_TAX_RATE
                     if direction == "sell" and not price_limits.is_etf(code) else 0.0)

        # 过户费 (双向)
        transfer_fee = amount * self.TRANSFER_FEE_RATE

        # 冲击成本:
        # - ETF/LOF 流动性极好, 散户量级订单冲击可忽略, 跳过
        # - 股票: 订单金额/当日成交额 > 5% 时触发, 但上限为交易金额的 2%
        #   (防止数据源成交额异常偏低时产生天价冲击成本, 导致仓位被强制缩量)
        impact_cost = 0.0
        if day_amount > 0 and not price_limits.is_etf(code):
            ratio = amount / day_amount
            if ratio > self.IMPACT_THRESHOLD:
                impact_cost = amount * (ratio ** 1.5)
                impact_cost = min(impact_cost, amount * self.MAX_IMPACT_RATE)

        return (fill_price, commission, stamp_tax, transfer_fee,
                slippage_cost, impact_cost)


class Position:
    """持仓"""
    __slots__ = ("code", "entry_date", "entry_price", "quantity",
                 "entry_commission", "entry_cost_total")

    def __init__(self, code, entry_date, entry_price, quantity,
                 entry_commission, entry_cost_total):
        self.code = code
        self.entry_date = entry_date
        self.entry_price = entry_price
        self.quantity = quantity
        self.entry_commission = entry_commission
        self.entry_cost_total = entry_cost_total


class Portfolio:
    """账户与持仓管理"""

    def __init__(self, initial_capital: float):
        self.cash = initial_capital
        self.initial_capital = initial_capital
        self.positions: dict[str, Position] = {}
        self.trades = []         # 完成的配对交易 (buy→sell)
        self.fills = []          # 逐笔成交流水 (每笔买/卖独立记录)
        # 资金实时校验计数: 买入被现金约束拦截 / 账户现金触底次数
        # 正常情况下两者都应为 0 (先卖后买 + 下单前按可用资金定量)
        self.rejected_buys = 0
        self.cash_floor_hits = 0
        self.cost_summary = {
            "commission": 0.0, "stamp_tax": 0.0, "transfer_fee": 0.0,
            "slippage": 0.0, "impact": 0.0,
        }

    @property
    def holdings_count(self):
        return len(self.positions)

    def apply_fill(self, fill: FillEvent):
        """处理成交, 更新持仓与资金 (支持部分卖出)"""
        amount = fill.fill_price * fill.quantity
        realized_pnl = 0.0   # 本次卖出实现的盈亏 (买入为0)
        cash_before = self.cash

        if fill.direction == "buy":
            total_cost = amount + fill.total_cost
            # 资金实时校验: T+1 开盘实际成交价可能高于下单时估算价,
            # 导致实际扣款超出可用资金。按「含费单位成本」收缩到可用现金内的
            # 最大整手数, 并按数量等比例缩放各项费用, 保证现金永不为负。
            if total_cost > self.cash + 1e-9:
                orig_qty = max(float(fill.quantity), 1.0)
                unit_cost = total_cost / orig_qty
                max_qty = int(max(0.0, self.cash) / unit_cost) if unit_cost > 0 else 0
                max_qty = (max_qty // 100) * 100
                while max_qty >= 100:
                    scale = max_qty / orig_qty
                    if (fill.fill_price * max_qty + fill.total_cost * scale
                            <= self.cash + 1e-9):
                        break
                    max_qty -= 100
                if max_qty < 100:
                    # 可用资金不足一手 → 放弃本笔, 不留负现金
                    self.rejected_buys += 1
                    return False
                scale = max_qty / orig_qty
                fill.commission *= scale
                fill.stamp_tax *= scale
                fill.transfer_fee *= scale
                fill.slippage_cost *= scale
                fill.impact_cost *= scale
                fill.quantity = max_qty
                amount = fill.fill_price * fill.quantity
                total_cost = amount + fill.total_cost
            self.cash -= total_cost

            # 如果已有部分持仓 (部分卖出后再买回), 合并成本
            existing = self.positions.get(fill.code)
            if existing is not None:
                new_qty = existing.quantity + fill.quantity
                # entry_cost_total 只累计交易成本, 不含买入金额 (否则卖出时会把本金当成本扣掉)
                new_entry_cost = existing.entry_cost_total + fill.total_cost
                new_entry_commission = existing.entry_commission + fill.commission
                new_entry_price = (existing.entry_price * existing.quantity +
                                   fill.fill_price * fill.quantity) / new_qty
                self.positions[fill.code] = Position(
                    code=fill.code, entry_date=fill.date,
                    entry_price=new_entry_price, quantity=new_qty,
                    entry_commission=new_entry_commission,
                    entry_cost_total=new_entry_cost)
            else:
                self.positions[fill.code] = Position(
                    code=fill.code, entry_date=fill.date,
                    entry_price=fill.fill_price, quantity=fill.quantity,
                    entry_commission=fill.commission,
                    entry_cost_total=fill.total_cost)
        else:  # sell
            pos = self.positions.get(fill.code)
            if pos is None:
                return
            proceeds = amount - fill.total_cost
            self.cash += proceeds

            if fill.quantity >= pos.quantity:
                # 全仓卖出
                self.positions.pop(fill.code)
                realized_pnl = ((fill.fill_price - pos.entry_price) * pos.quantity
                                - fill.total_cost - pos.entry_cost_total)
                self.trades.append({
                    "code": fill.code,
                    "buy_date": pd.Timestamp(pos.entry_date).normalize() + pd.Timedelta(hours=9, minutes=30),
                    "sell_date": pd.Timestamp(fill.date).normalize() + pd.Timedelta(hours=9, minutes=30),
                    "buy_price": pos.entry_price,
                    "sell_price": fill.fill_price,
                    "buy_qty": pos.quantity,
                    "sell_qty": pos.quantity,
                    "shares": pos.quantity,
                    "pnl": realized_pnl,
                    "days": (fill.date - pos.entry_date).days,
                    "reason": fill.reason,
                    "cost": fill.total_cost + pos.entry_cost_total,
                })
            else:
                # 部分卖出: 只减少持仓
                realized_pnl = ((fill.fill_price - pos.entry_price) * fill.quantity
                                - fill.total_cost)
                pos.quantity -= fill.quantity
                self.trades.append({
                    "code": fill.code,
                    "buy_date": pd.Timestamp(pos.entry_date).normalize() + pd.Timedelta(hours=9, minutes=30),
                    "sell_date": pd.Timestamp(fill.date).normalize() + pd.Timedelta(hours=9, minutes=30),
                    "buy_price": pos.entry_price,
                    "sell_price": fill.fill_price,
                    "buy_qty": pos.quantity + fill.quantity,
                    "sell_qty": fill.quantity,
                    "shares": fill.quantity,
                    "pnl": realized_pnl,
                    "days": (fill.date - pos.entry_date).days,
                    "reason": fill.reason,
                    "cost": fill.total_cost,
                })

        # 资金兜底校验: 浮点误差外若仍出现负现金, 说明成交口径异常。
        # 记录并归零, 保证「账户余额始终非负」的硬约束。
        if self.cash < -1e-6:
            self.cash_floor_hits += 1
            self.cash = 0.0

        self.cost_summary["commission"] += fill.commission
        self.cost_summary["stamp_tax"] += fill.stamp_tax
        self.cost_summary["transfer_fee"] += fill.transfer_fee
        self.cost_summary["slippage"] += fill.slippage_cost
        self.cost_summary["impact"] += fill.impact_cost

        # 逐笔成交流水 (每笔买/卖独立记录, 含成交后账户余额与实现盈亏)
        # phase/time: 先卖 (09:30) 后买 (09:31), 同一交易日不得同时点双向交易
        self.fills.append({
            "date": fill.date,
            "time": "09:30:00" if fill.direction == "sell" else "09:31:00",
            "seq": len(self.fills) + 1,
            "code": fill.code,
            "direction": fill.direction,
            "price": round(fill.fill_price, 3),
            "qty": fill.quantity,
            "amount": round(amount, 2),
            "cost": round(fill.total_cost, 2),
            "cash_before": round(cash_before, 2),
            "cash_after": round(self.cash, 2),
            "pnl": round(realized_pnl, 2),
            "reason": fill.reason or "",
        })
        return True


    def total_value(self, date, bars) -> float:
        """按当日收盘价估值

        当日无K线 (停牌/数据缺口) 时退化为最近可用收盘价, 不能用 0 估值:
        否则回测最后一天缺行情会让全部持仓瞬间归零, 净值出现假性塌陷。
        """
        pv = self.cash
        for code, pos in self.positions.items():
            r = bars.row(code, date)
            if r is None:
                r = bars.last_row(code, date)
            if r is not None:
                pv += r["close"] * pos.quantity
            else:
                pv += pos.entry_price * pos.quantity
        return pv


class BaseStrategy:
    """策略基类

    子类实现 on_bar 产生买卖信号。
    函数式 select(snapshot) 策略可用 FunctionStrategyAdapter 包装。
    """

    name = "base"
    params: dict = {}

    def on_init(self, context: dict) -> None:
        pass

    def on_bar(self, event: MarketBarEvent) -> SignalEvent | None:
        """接收每日行情, 返回信号 (买入列表 + 卖出列表)"""
        return None

    def on_trade(self, fill: FillEvent) -> None:
        """成交回报回调"""
        pass

    def on_finish(self) -> None:
        pass


class FunctionStrategyAdapter(BaseStrategy):
    """把函数式 select(snapshot, **params) 包装成 BaseStrategy"""

    def __init__(self, select_fn, name="adaptive", **params):
        self.select_fn = select_fn
        self.name = name
        self.params = params
        self._pending_sells = []   # 上一选股日标记的卖出 (T+1 执行)

    def on_bar(self, event: MarketBarEvent) -> SignalEvent | None:
        date = event.date
        snap = event.snapshot
        buys = []
        sells = list(self._pending_sells)
        self._pending_sells = []

        # 选股日: 用当日截面选股
        if snap and len(snap) >= self.params.get("top_n", 5):
            # 注入财务上下文 (避免未来函数); _sel_date 供需要 as-of 的选股函数读取
            # (事件引擎 snapshot 行不含 date 字段, 见 BarIndex._cols)
            _ctx = fundamental_asof(date)
            for _sc, _row in snap.items():
                if _ctx["map"]:
                    _row["_f"] = _ctx["map"].get(_sc)
                    _row["_fmed"] = _ctx["median"]
                _row["_sel_date"] = date
            try:
                selected = self.select_fn(snap, **self.params) or []
            except Exception:
                selected = []
            buys = [c for c in selected if c in snap][:self.params.get("top_n", 5)]
            # 现有持仓中不在新选股池的 → 标记卖出 (周度换仓)
            # (卖出在 ExecutionEngine 中根据持仓与 buys 决定)
        return SignalEvent(date, buys, sells) if (buys or sells) else None


# 价格触发再平衡: 单一标的相对"上次建仓/调仓价"涨跌达到该幅度 → 调回目标权重
# (仓位占比 75% 时, 价格变动 4% 仅带来约 1% 的权重变化, 单靠权重阈值会长期不调仓)
PRICE_REBAL_THRESHOLD = 0.04

# 建仓趋势确认: 周 MA30 上升之外, 还要求最近 N 个完整周为阳线 (收盘>开盘)
MA30_BULL_WEEKS = 2
# 不追高: 最新价相对周 MA30 的溢价上限 (10%); None/0 = 关闭
MA30_MAX_PREMIUM = 0.10


def _entry_confirm_ok(ma30_cache: dict, code: str, date,
                      bull_weeks: int = None, max_premium: float = None) -> bool:
    """建仓确认的后三道门槛: 连续 N 周阳线 + 不追高 + 长期趋势向上

    与「周K MA30 连续上升」共同构成建仓确认, 用于过滤假突破/死猫反弹导致的
    "建仓后数日即被 MA30 走弱清仓"的草率建仓。仅拦买入, 不影响存量持仓。
    数据不足时一律放行 (与 MA30 选股过滤的放行口径一致)。
    """
    bw = MA30_BULL_WEEKS if bull_weeks is None else int(bull_weeks)
    mp = MA30_MAX_PREMIUM if max_premium is None else float(max_premium)
    if weekly_bull_weeks(ma30_cache, code, date, bw) is False:
        return False
    # 长期趋势向上: 价格站上最长可用周均线(MA120→MA60), 且周MA30在长均线之上
    # 过滤下跌趋势中的短期反弹 (如 2023 年中的 510050 空头排列)
    if weekly_long_trend_ok(ma30_cache, code, date) is False:
        return False
    if mp:
        prem = weekly_ma30_premium(ma30_cache, code, date)
        if prem is not None and prem > mp:
            return False
    return True


class MarketCapStrategy(BaseStrategy):
    """
    市值策略: 15-200亿市值主板, 等比例买入总金额的75%, 动态再平衡

    交易规则:
      1. 选股: 市值 15-200亿主板 (排除创业板/科创板), 上市 ≥3年, 按市值从小到大选 top_n
      2. 不良标的剔除 (需财务数据, 无数据自动放行):
         - ST/*ST/退市整理 (监管/造假高发)
         - 现金流背离: 盈利但经营现金流为负 (纸面利润, 造假预警)
         - 虚假盈利: 主业亏损靠变卖资产/营业外撑利润, 或营业外贡献过半
         - 长期不分红: 近 dividend_years 个完整财年无现金分红
      3. 建仓: 总账户市值 * weight_fraction (默认75%), 等比例分给 top_n 只
      4. 再平衡 (每日检查, 只调整自身偏离的标的):
         a) 标的市值(价格)相对建仓基准变化 ≥ cap_change_threshold (默认4%)
            → 下跌补仓 / 上涨减仓, 回到单只目标市值
         b) 单只持仓市值相对目标仓位偏离 ≥ rebalance_threshold (默认5%) → 补仓/减仓
         单只目标市值 = 账户总市值 × weight_fraction / 持仓数 (各标的相同目标)
         买单受可用现金约束 (卖单回款先计入), 不会透支
      5. 交易数量对100取整 (A股标准)
      6. 标的池 (universe): 指定后跳过市值/财务选股, 只在该池内建仓/调仓
         (单标的验证用, 如 universe=["sz159509"] + top_n=1)
      7. 调仓方式 rebalance_mode:
         - "deviation": 上条 4 的偏离度再平衡, 每日检查, 换手高
         - "topk_drop" (借 Qlib Topk-Drop): 持仓跌出候选前 top_n+rank_buffer 名才换出,
           空槽位才补新标的; 不做偏离度再平衡, 显著降低换手与成本
           rank_swap=False (界面默认) 时关闭「排名跌出换股」, 只有止损清出空槽才补位;
           此时股票仍走偏离度再平衡 (低买高卖 ±4% / ≥5% 权重)
         - 指定标的池 (universe) 时没有排名概念, topk_drop 自动退化为偏离度再平衡
      8. 标的池类型 pool_type:
         - "stock": 上条 1-3 的股票口径 (市值区间 + 财务/分红/ST 剔除 + 市值升序)
         - "etf": 场内 ETF 口径 (无财务/分红/市值概念), 按近 etf_mom_days 日涨幅降序排名,
           叠加近 20 日日均成交额 >= etf_min_amount 亿元 的流动性门槛、最少
           etf_min_history 根K线的数据长度门槛; 建仓/调仓/撮合/成本与股票版完全一致
         - ETF 风控因子 (数据来自华泰柏瑞增强表, 缺失时自动不拦截):
           溢价率 > etf_premium_cap(%) 剔除; 近5日净申购/规模 < etf_flow_min(%) 剔除;
           etf_mkt_flow_guard=True 时全市场股票型 ETF 资金持续净流出 → 清仓
    """

    name = "市值策略"

    def __init__(self, top_n=3, cap_min=15.0, cap_max=200.0,
                 weight_fraction=0.75, cap_change_threshold=0.04,
                 rebalance_threshold=0.05, min_listed_years=3.0,
                 exclude_st=True, exclude_ocf_diverge=True,
                 exclude_nonop_profit=True, exclude_no_dividend=True,
                 dividend_years=3, ma30_lookback=5, universe=None,
                 ma30_exit=True, ma30_entry_weeks=1,
                 rebalance_mode="deviation", rank_buffer=2,
                 rank_swap=True,
                 check_freq="daily",
                 rank_check_freq=None,
                 pool_type="stock", etf_mom_days=60,
                 etf_min_amount=0.2, etf_min_history=60,
                 etf_premium_cap=1.0, etf_flow_min=None,
                 etf_mkt_flow_guard=False, etf_mkt_flow_days=5,
                 etf_abs_mom_days=60, etf_abs_mom_min=3.0,
                 etf_max_dd_threshold=0.30, etf_require_ma60_up=True,
                 etf_ret20_min=0.0, etf_ret60_min=0.0,
                 etf_overseas_only=True):
        self.top_n = int(top_n)
        # 标的池类型: stock=股票(市值+财务筛选) / etf=ETF(动量排名+流动性门槛)
        # 两类共用同一套建仓/调仓/撮合逻辑, 只有选品规则不同
        self.pool_type = str(pool_type)
        self.etf_mom_days = int(etf_mom_days)          # 动量窗口: 20 / 60 日涨幅
        self.etf_min_amount = float(etf_min_amount)    # 近20日日均成交额下限 (亿元)
        self.etf_min_history = int(etf_min_history)    # 最少K线根数 (次新ETF数据不足)
        # ETF 风控因子 (数据来自华泰柏瑞增强表, 缺失时自动不拦截):
        # 溢价率上限(%): 超过则剔除, 0=关闭; QDII 高溢价回落是主要亏损源
        self.etf_premium_cap = float(etf_premium_cap or 0.0)
        # 近5日净申购/规模(%) 下限: 低于则剔除, None=关闭 (负值=允许小幅流出)
        self.etf_flow_min = None if etf_flow_min is None else float(etf_flow_min)
        # 市场级风控: 全市场股票型 ETF 资金持续净流出时不开新仓并清仓
        self.etf_mkt_flow_guard = bool(etf_mkt_flow_guard)
        self.etf_mkt_flow_days = int(etf_mkt_flow_days)
        # 绝对动量过滤 (ETF): 近 N 日涨幅 < 阈值 → 建仓剔除 / 持仓清仓
        # 阈值为 0 或 None 时关闭 (与界面语义一致)
        self.etf_abs_mom_days = int(etf_abs_mom_days or 60)
        self.etf_abs_mom_min = (None if etf_abs_mom_min is None
                                else float(etf_abs_mom_min))
        # 优质标的过滤 (2026-11-20): 历史最大回撤 / MA60 趋势 / 多周期动量
        self.etf_max_dd_threshold = float(etf_max_dd_threshold)
        self.etf_require_ma60_up = bool(etf_require_ma60_up)
        self.etf_ret20_min = float(etf_ret20_min)
        self.etf_ret60_min = float(etf_ret60_min)
        # 选品范围: 只做跨境(外盘)ETF 并剔除港股系 (恒生/港股通/H股/中概等)
        self.etf_overseas_only = bool(etf_overseas_only)
        self._etf_ext = None        # {premium/flow/market} 按日索引的因子字典
        self._mkt_ok = None         # {date: 市场资金流是否健康}
        # 标的池: 指定后跳过市值/财务选股, 只在这些标的内建仓与调仓 (单标的验证用)
        self.universe = [str(c) for c in universe] if universe else None
        self.cap_min = float(cap_min)
        self.cap_max = float(cap_max)
        self.weight_fraction = float(weight_fraction)
        self.cap_change_threshold = float(cap_change_threshold)
        self.rebalance_threshold = float(rebalance_threshold)
        self.min_listed_years = float(min_listed_years)
        self.exclude_st = bool(exclude_st)
        self.exclude_ocf_diverge = bool(exclude_ocf_diverge)
        self.exclude_nonop_profit = bool(exclude_nonop_profit)
        self.exclude_no_dividend = bool(exclude_no_dividend)
        self.dividend_years = int(dividend_years)
        self.ma30_lookback = int(ma30_lookback)  # 周K线 MA30 斜率判断回看周数
        self.ma30_exit = bool(ma30_exit)         # 周K线 MA30 拐头向下是否清仓换出
        # 建仓趋势确认: 周K MA30 需连续 N 周上升 (0=关闭), 避免刚建仓就遇 MA30 走弱
        self.ma30_entry_weeks = int(ma30_entry_weeks)
        # 调仓方式: deviation=偏离度再平衡 (旧逻辑); topk_drop=排名缓冲 (借 Qlib Topk-Drop)
        self.rebalance_mode = str(rebalance_mode)
        self.rank_buffer = int(rank_buffer)      # topk_drop: 排名跌出 top_n+rank_buffer 才换出
        # 排名跌出是否换股 (2026-11-24): 默认 True 保持直调兼容;
        # 界面默认 False → 只有持仓数 < top_n (止损清出空槽) 才补位, 不做排名驱动的换股,
        # 避免频繁换股; 关闭时股票仍走第 3 步偏离度调仓 (低买高卖 ±4% / ≥5% 权重)。
        self.rank_swap = bool(rank_swap)
        # 排名检查频率: daily=每日; weekly=每周最后一个交易日
        # (ETF 60日动量排名日间噪声大, 日频检查会带来无谓换手与成本)
        self.check_freq = str(check_freq)
        # 换股/补位的排名复核周期 (2026-11-25 用户需求: 调仓保留, 换股拉长到1年或可关闭)。
        # None → 回退到 check_freq (保持股票策略与旧配置行为不变);
        # 可选 daily/weekly/monthly/quarterly/yearly/off。
        # off=关闭换股: 初始建仓后不再因排名补入新标的, 止损清出的仓位留现金,
        # 仅保留持仓内部的偏离度调仓 (低买高卖)。
        self.rank_check_freq = rank_check_freq
        _allowed = {"daily", "weekly", "monthly", "quarterly", "yearly", "off"}
        _eff = (rank_check_freq or check_freq)
        self._eff_rank_freq = _eff if _eff in _allowed else "weekly"
        self._check_days = None

        # 运行时状态 (由 on_init / on_bar 维护)
        self.portfolio = None
        self.bars = None
        self.feat = None
        self._entry_cap_ratios = {}   # {code: 建仓当日的 cap_yi}
        self._entry_prices = {}       # {code: 建仓/上次调仓当日价格} (无市值数据时用价格判断偏离)
        self._target_weights = {}     # {code: 目标权重, 初始等比例 1/top_n}
        self._last_ma30_check = {}    # {code: 上次 MA30 检查日期}
        self._ma30_cache = None       # {code: (周结束日, MA30, MA30.shift)} 预计算, 首次使用时构建

    @property
    def n_slots(self) -> int:
        """持仓槽位数: 指定标的池时=池内标的数, 否则=top_n"""
        return len(self.universe) if self.universe else self.top_n

    def _ensure_ma30_cache(self) -> dict:
        if self._ma30_cache is None:
            self._ma30_cache = build_ma30_cache(self.feat, self.ma30_lookback)
        return self._ma30_cache

    def _ensure_etf_ext(self) -> dict:
        """按需加载 ETF 增强因子 (溢价/资金流/全市场资金流), 只读库一次"""
        if self._etf_ext is None:
            from data_store import load_etf_factor_maps
            self._etf_ext = load_etf_factor_maps()
            if self.etf_mkt_flow_guard:
                self._mkt_ok = {}
                vals = []
                for d in sorted(self._etf_ext["market"]):
                    v = self._etf_ext["market"][d].get("stock")
                    if v is None:
                        v = self._etf_ext["market"][d].get("all")
                    vals.append(v if v is not None else np.nan)
                    win = [x for x in vals[-self.etf_mkt_flow_days:] if np.isfinite(x)]
                    need = min(2, self.etf_mkt_flow_days)
                    self._mkt_ok[d] = (sum(win) >= 0) if len(win) >= need else True
        return self._etf_ext

    def _selection_kwargs(self, date):
        """选股过滤参数: 财务/分红上下文按 as-of 加载 (避免未来函数)"""
        if self.pool_type == "etf":
            # ETF 不依赖财务/分红/市值: 只给动量/流动性/数据长度 + MA30 上下文
            kw = dict(pool_type="etf", etf_mom_days=self.etf_mom_days,
                      etf_min_amount=self.etf_min_amount,
                      etf_min_history=self.etf_min_history,
                      ma30_up_only=True, ma30_lookback=self.ma30_lookback,
                      ma30_entry_weeks=self.ma30_entry_weeks,
                      ma30_cache=self._ensure_ma30_cache(), date=date,
                      max_dd_threshold=self.etf_max_dd_threshold,
                      require_ma60_up=self.etf_require_ma60_up,
                      ret20_min=self.etf_ret20_min,
                      ret60_min=self.etf_ret60_min,
                      overseas_only=self.etf_overseas_only)
            # 溢价/资金流/市场风控: 仅有增强数据时才参与, 否则保持原行为
            if (self.etf_premium_cap or self.etf_flow_min is not None
                    or self.etf_mkt_flow_guard):
                ext = self._ensure_etf_ext()
                d = str(pd.Timestamp(date))[:10] if date is not None else None
                kw.update(premium_cap=self.etf_premium_cap,
                          premium_map=ext["premium"].get(d),
                          flow_min=self.etf_flow_min,
                          flow_map=ext["flow"].get(d))
                if self.etf_mkt_flow_guard and self._mkt_ok is not None:
                    kw["market_ok"] = self._mkt_ok.get(d, True)
            return kw
        f_map = None
        if self.exclude_st or self.exclude_ocf_diverge or self.exclude_nonop_profit:
            f_map = fundamental_asof(date)["map"]
        div_map = None
        if self.exclude_no_dividend:
            div_map = dividend_years_asof(str(date)[:10])
        return dict(cap_min=self.cap_min, cap_max=self.cap_max,
                    min_listed_years=self.min_listed_years,
                    exclude_st=self.exclude_st,
                    exclude_ocf_diverge=self.exclude_ocf_diverge,
                    exclude_nonop_profit=self.exclude_nonop_profit,
                    exclude_no_dividend=self.exclude_no_dividend,
                    dividend_years=self.dividend_years,
                    ma30_up_only=True,
                    ma30_lookback=self.ma30_lookback,
                    ma30_entry_weeks=self.ma30_entry_weeks,
                    ma30_cache=self._ensure_ma30_cache(),
                    f_map=f_map, div_map=div_map, date=date)

    def on_init(self, context: dict) -> None:
        self.portfolio = context.get("portfolio")
        self.bars = context.get("bars")
        self.feat = context.get("feat")
        # 排名/换股复核日: daily=每个交易日; off=永不; 其余取各周期最后一个交易日
        self._check_days = self._compute_check_days()

    def _compute_check_days(self) -> set | None:
        if self.bars is None:
            return None
        freq = self._eff_rank_freq
        if freq in ("daily", "off"):
            return None
        dates = pd.DatetimeIndex(self.bars.all_dates)
        if freq == "weekly":
            groups = dates.strftime("%Y-%W")
        elif freq == "monthly":
            groups = dates.strftime("%Y-%m")
        elif freq == "quarterly":
            groups = dates.to_period("Q").astype(str)
        elif freq == "yearly":
            groups = dates.strftime("%Y")
        else:
            groups = dates.strftime("%Y-%W")
        return {pd.Timestamp(d) for d in
                pd.Series(dates).groupby(groups.to_numpy()).max().tolist()}

    def _is_check_day(self, date) -> bool:
        """排名/换股检查日: daily=每个交易日; off=永不; 其余=对应周期最后一个交易日"""
        if self._eff_rank_freq == "daily":
            return True
        if self._eff_rank_freq == "off":
            return False
        if not self._check_days:
            return True
        return pd.Timestamp(date) in self._check_days

    def _allow_rank_action(self, date, has_positions: bool) -> bool:
        """是否允许在当日进行排名驱动的换股/补位 (买入新标的)。
        - 空仓 (含初始建仓): 始终允许, 否则策略无法建仓;
        - 已有持仓: 仅在复核日允许; off 模式下永不补入新标的 (止损清出的仓位留现金)。
        调仓 (持仓内部低买高卖) 由价格/权重偏离触发, 与换股周期无关, 不受此限制。"""
        if not has_positions:
            return True
        if self._eff_rank_freq == "off":
            return False
        return self._is_check_day(date)

    def _current_cap(self, code, date) -> float:
        """取标的当日市值 (亿)"""
        if self.bars is None:
            return np.nan
        row = self.bars.row(code, date)
        if row is None:
            return np.nan
        return row.get("cap_yi", np.nan)

    def _current_price(self, code, date) -> float:
        if self.bars is None:
            return np.nan
        row = self.bars.row(code, date)
        if row is None:
            # 当日无K线 (停牌/数据缺口): 退化为最近可用收盘价, 与 Portfolio.total_value 口径一致
            row = self.bars.last_row(code, date)
        if row is None:
            return np.nan
        return row.get("close", row.get("open", np.nan))

    def _round_lot(self, shares: float) -> int:
        """A股100股取整, 至少100股"""
        qty = int(shares // 100) * 100
        return max(qty, 0)

    def _weekly_closes(self, code, date=None) -> np.ndarray | None:
        """把 feat[code] 的日线 close 重采样到周线 (取该周最后一个交易日的收盘价)"""
        if self.feat is None or code not in self.feat:
            return None
        df = self.feat[code]
        close_col = "close" if "close" in df.columns else "adj_close"
        sub = df[["date", close_col]].dropna().copy()
        sub["date"] = pd.to_datetime(sub["date"])
        if date is not None:
            sub = sub[sub["date"] <= pd.Timestamp(date)]
        if len(sub) < 5:
            return None
        sub = sub.sort_values("date").set_index("date")
        # resample 到周五为界的周线, 取该周最后一个交易日的收盘价
        wk = sub[close_col].resample("W-FRI").last().dropna()
        return wk.values if len(wk) >= 30 else None

    def _ma30_is_decreasing(self, code, date) -> bool:
        """
        判断标的 周K线 MA30 是否在减小 (趋势走弱):
        取 周线 MA30(t) 与 MA30(t - ma30_lookback) 对比,
        若 MA30(t) <= MA30(t - lookback) → True (在减小)
        数据不足 → False (不做判断)
        """
        if self._ma30_cache is not None or self.feat:
            up = ma30_slope_up(self._ensure_ma30_cache(), code, date)
            if up is not None:
                return not up
        closes = self._weekly_closes(code, date)
        if closes is None:
            return False
        need = 30 + self.ma30_lookback
        if len(closes) < need:
            return False
        ma30_now = closes[-30:].mean()
        ma30_past = closes[-need:-self.ma30_lookback].mean()
        return ma30_now <= ma30_past

    def _ma30_entry_ok(self, code, date) -> bool:
        """建仓趋势确认: 周K MA30 连续 N 周上升 + 最近 M 周阳线 + 不追高

        数据不足时不拦截 (与 MA30 选股过滤的放行口径一致)。
        """
        weeks = int(self.ma30_entry_weeks)
        cache = self._ensure_ma30_cache()
        if weeks > 0:
            up = ma30_rising_weeks(cache, code, date, weeks)
            if up is False:
                return False
        return _entry_confirm_ok(cache, code, date)

    def _abs_mom_enabled(self) -> bool:
        """绝对动量过滤是否启用 (ETF 专用, 阈值 0/None = 关闭)"""
        return (self.pool_type == "etf" and self.etf_abs_mom_min is not None
                and abs(self.etf_abs_mom_min) > 1e-12)

    def _abs_mom_ok(self, code, date) -> bool:
        """近 N 日涨幅是否不低于绝对动量阈值 (数据缺失时放行)"""
        if not self._abs_mom_enabled() or self.bars is None:
            return True
        row = self.bars.row(code, date)
        if row is None:
            return True
        r = row.get(f"ret_{self.etf_abs_mom_days}")
        if r is None or not np.isfinite(r):
            return True
        return float(r) >= self.etf_abs_mom_min

    def _build_rebalance(self, date, targets: dict, extra_cash: float = 0.0) -> list:
        """
        根据 target 市值目标生成再平衡订单 (先卖后买 + 可用现金约束)
        targets: {code: 目标市值金额}
        extra_cash: 本日另行走 sells 通道的卖单回款估算 (Topk-Drop 换股时用)
        返回: [OrderEvent] — 全部走 rebalance_orders (T+1 开盘执行)
        """
        sells, buy_requests = [], []
        # 可用现金: 当前现金 + 全部卖单回款估算 (略扣手续费), 不允许透支
        # 先结算所有卖单, 再按可用现金生成买单 (与 targets 的插入顺序无关)
        avail = max(0.0, self.portfolio.cash) + max(0.0, float(extra_cash))
        for code, target_mv in targets.items():
            pos = self.portfolio.positions.get(code)
            cur_price = self._current_price(code, date)
            if cur_price is None or np.isnan(cur_price) or cur_price <= 0:
                continue

            cur_mv = cur_price * pos.quantity if pos is not None else 0.0
            diff = target_mv - cur_mv
            # 最小交易额过滤: 既不足 100 股, 又不足 max(3000元, 持仓0.5%) → 视为噪音, 不交易
            min_trade_mv = max(3000.0, cur_mv * 0.005)
            if abs(diff) < cur_price * 100 or abs(diff) < min_trade_mv:
                continue

            if diff < 0:
                if pos is None:
                    continue
                qty = min(self._round_lot(abs(diff) / cur_price), pos.quantity)
                if qty >= 100:
                    order = OrderEvent(date, code, "sell", qty)
                    order.reason = "mc_rebalance"
                    sells.append(order)
                    avail += qty * cur_price * 0.998   # 卖单回款 (扣费后)
            else:
                qty = self._round_lot(diff / cur_price)
                buy_requests.append((code, qty, cur_price, pos is None))

        buys = []
        for code, qty, cur_price, is_new in buy_requests:
            # 现金上限: 不足则按可用现金能买的整手数部分补仓
            qty = min(qty, self._round_lot(avail / (cur_price * 1.0015)))
            if qty >= 100:
                order = OrderEvent(date, code, "buy", qty)
                order.reason = "mc_new_entry" if is_new else "mc_rebalance"
                # 附带目标市值: T+1 开盘撮合时按实际成交价重算股数,
                # 避免「信号日收盘价 → 次日开盘价」跳空导致实际买入金额偏离目标 (建仓超配)
                order.target_mv = float(targets[code])
                buys.append(order)
                avail -= qty * cur_price * 1.0015  # 含费用
        return sells + buys

    def on_bar(self, event: MarketBarEvent) -> SignalEvent | None:
        date = event.date
        snap = event.snapshot
        portfolio = self.portfolio

        total_value = portfolio.total_value(date, self.bars)

        buys = []
        sells = []
        sell_reasons = {}
        rebalance_orders = []

        # =========================================
        # 1. 换股逻辑: 持仓中某只 MA30 减小 → 卖出
        # =========================================
        if portfolio.positions:
            for code in list(portfolio.positions.keys()):
                if self.ma30_exit and self._ma30_is_decreasing(code, date):
                    sells.append(code)
                    sell_reasons[code] = "ma30_exit"
                    self._entry_cap_ratios.pop(code, None)
                    self._entry_prices.pop(code, None)
                    self._target_weights.pop(code, None)
                elif self._abs_mom_enabled() and not self._abs_mom_ok(code, date):
                    # 绝对动量转弱: 强制清仓 (与 MA30 走弱同级)
                    sells.append(code)
                    sell_reasons[code] = "abs_mom_exit"
                    self._entry_cap_ratios.pop(code, None)
                    self._entry_prices.pop(code, None)
                    self._target_weights.pop(code, None)

        # =========================================
        # 2. 空仓或换股后仓位不足 → 建仓 (MA30 上升过滤)
        # =========================================
        sell_codes = set(sells)
        remaining = [c for c in portfolio.positions.keys() if c not in sell_codes]
        need_fill = self.n_slots - len(remaining)
        # 是否允许当日买入新标的 (换股/补位): 初始空仓始终允许; 已有持仓仅复核日允许;
        # off 模式下不补入新标的。调仓 (持仓内部低买高卖) 不受影响。
        rank_ok = self._allow_rank_action(date, bool(remaining))

        # =========================================
        # 2a. Topk-Drop 换股 (借 Qlib 思路):
        #     持仓只有跌出候选前 top_n+rank_buffer 名才卖出;
        #     空槽位才补最靠前的候选; 不做偏离度再平衡
        #     → 换手从"每天全额重排"降到"只在排名变化时换股"
        #     补充 (2026-11-22): 清仓当日即用当日排名补位, 不等到下一个检查日,
        #     保证「持仓数」始终被填满 (有合格候选时不会出现 0 仓)
        # =========================================
        if self.rebalance_mode == "topk_drop" and snap and not self.universe:
            ranked = None
            if self._is_check_day(date):
                ranked = _filter_market_cap_universe(
                    snap, feat=self.feat, **self._selection_kwargs(date))
                rank_map = {c: i + 1 for i, c in enumerate(ranked)}
                keep_limit = self.n_slots + self.rank_buffer
                # 持仓跌出候选前 top_n+rank_buffer 名即换出 (2026-11-23: ETF 也启用)。
                # 此前 ETF 只靠周K MA30 走弱退出, 实测出现「排名已跌出标的池仍死扛 85 天」,
                # 是 2026 年两笔大额亏损的主要来源。
                # (2026-11-24) rank_swap=False 时关闭排名换股: 只有止损清出空槽才补位,
                #   不再因排名跌出而换股, 避免频繁换股; 接受排名跌出但未止损时的持有风险。
                if self.rank_swap:
                    for code in list(remaining):
                        r = rank_map.get(code)
                        if r is None or r > keep_limit:
                            sells.append(code)
                            sell_reasons[code] = "rank_drop"
                            remaining.remove(code)
                            sell_codes.add(code)
                            self._entry_cap_ratios.pop(code, None)
                            self._entry_prices.pop(code, None)
                            self._target_weights.pop(code, None)

            # 换出后重新计算空槽位 (上面 need_fill 是换出前的值)
            need_fill = self.n_slots - len(remaining)
            if need_fill > 0 and rank_ok:
                if ranked is None:
                    # 非检查日出现空槽 (清仓后): 用当日排名即时补位
                    ranked = _filter_market_cap_universe(
                        snap, feat=self.feat, **self._selection_kwargs(date))
                candidates = [c for c in ranked
                              if c not in portfolio.positions and c not in sell_codes
                              and self._abs_mom_ok(c, date)][:need_fill]
                if candidates:
                    # 按实际持仓数(remaining+candidates)均分目标仓位,
                    # 避免候选不足 n_slots 时每只只分到 1/n_slots 导致总仓位不足
                    _actual_n = len(remaining) + len(candidates)
                    target_each = total_value * self.weight_fraction / max(_actual_n, 1)
                    # 换出回款当日可用于买入 (卖单先于买单执行)
                    proceeds = 0.0
                    for code in sell_codes:
                        pos = portfolio.positions.get(code)
                        px = self._current_price(code, date)
                        if pos is not None and px and not np.isnan(px):
                            proceeds += px * pos.quantity * 0.998
                    rebalance_orders = self._build_rebalance(
                        date, {c: target_each for c in candidates}, extra_cash=proceeds)
                    for code in candidates:
                        self._target_weights[code] = 1.0 / max(_actual_n, 1)
                        cap = self._current_cap(code, date)
                        if not np.isnan(cap):
                            self._entry_cap_ratios[code] = cap
                        px = self._current_price(code, date)
                        if px and not np.isnan(px):
                            self._entry_prices[code] = px
            need_fill = 0  # 本分支已处理建仓, 跳过下面的偏离度建仓

        if need_fill > 0 and snap and len(snap) >= need_fill:
            if self.universe:
                # 固定标的池 (单标的验证): 跳过市值/财务选股, 只要求当日有行情且 MA30 未走弱
                selected = []
                for code in self.universe:
                    if code in portfolio.positions or code in sell_codes:
                        continue
                    px = self._current_price(code, date)
                    if px is None or np.isnan(px):
                        continue
                    if self.ma30_exit and self._ma30_is_decreasing(code, date):
                        continue
                    if not self._ma30_entry_ok(code, date):
                        continue
                    if not self._abs_mom_ok(code, date):
                        continue
                    selected.append(code)
            elif rank_ok:
                # 选股池: MA30 上升 + 市值/财务过滤 (已由 _filter_market_cap_universe 内置)
                selected = _filter_market_cap_universe(
                    snap, feat=self.feat, **self._selection_kwargs(date))
                # 排除已持有 + 已卖出的
                selected = [c for c in selected
                            if c not in portfolio.positions and c not in sell_codes
                            and self._abs_mom_ok(c, date)]
            else:
                # 非复核日 / 关闭换股: 不补入新标的, 空槽留现金 (调仓仍在存量持仓内进行)
                selected = []
            candidates = selected[:need_fill]

            # 对 sell_codes 也清理 _target_weights (可能已在上一步清)
            for code in sells:
                self._target_weights.pop(code, None)

            # 计算: 卖出后回笼资金 + 现有持仓市值 → 均分
            # 等比例目标: 总持有标的 (remaining + candidates) 均分 weight_fraction * total_value
            held_plus_new = remaining + candidates
            # 只有真正要新建仓时才重算等权; 否则 (无候选可补) 不要每天对存量持仓做精确对齐,
            # 否则会产生每天几百股的碎单 (偏离度调仓由第 3 步的阈值逻辑负责)
            if held_plus_new and candidates:
                # 换标的时的资金闭环: 先卖出 sell_codes 回笼资金,
                # 再用「现金 + 卖出回款」按等权目标买入 remaining + candidates。
                # 卖单由 sells 通道排队, 引擎按「卖出阶段 → 买入阶段」执行。
                proceeds = 0.0
                for code in sell_codes:
                    pos = portfolio.positions.get(code)
                    px = self._current_price(code, date)
                    if pos is not None and px and not np.isnan(px):
                        proceeds += px * pos.quantity * 0.998

                # 同时维护 _target_weights (等比例)
                for code in held_plus_new:
                    self._target_weights[code] = 1.0 / len(held_plus_new)
                    if code not in remaining:
                        # 新建仓, 记录基准市值/价格
                        cap = self._current_cap(code, date)
                        if not np.isnan(cap):
                            self._entry_cap_ratios[code] = cap
                        px = self._current_price(code, date)
                        if px and not np.isnan(px):
                            self._entry_prices[code] = px

                # 统一走 rebalance_orders 精确控制数量 (含卖出回款)
                target_each = total_value * self.weight_fraction / len(held_plus_new)
                targets = {code: target_each for code in held_plus_new}
                rebalance_orders = self._build_rebalance(
                    date, targets, extra_cash=proceeds)
                buys = []  # 已由 rebalance_orders 处理

        # =========================================
        # 3. 动态调仓 (等权再平衡 + 仓位维持):
        #    a) 总持仓市值 < weight_fraction * 总资产 且差额超阈值 → 买入补足
        #    b) 多标的时, 个股市值偏离等权目标超阈值 → 卖高买低, 拉平仓位
        #    手续费感知: 预计交易金额 < 往返手续费则不调 (避免摩擦损耗)
        # =========================================
        held_codes = [c for c in portfolio.positions.keys() if c in self._target_weights]
        held_codes = [c for c in held_codes if c not in sell_codes]

        _deviation_rebal = ((self.rebalance_mode != "topk_drop")
                            or bool(self.universe)
                            or self.pool_type == "etf"
                            or not self.rank_swap)
        if _deviation_rebal and held_codes and not rebalance_orders:
            # 当前总持仓市值
            cur_total_mv = 0.0
            code_mv = {}
            for code in held_codes:
                pos = portfolio.positions.get(code)
                px = self._current_price(code, date)
                if pos is None or not px or np.isnan(px):
                    continue
                mv = px * pos.quantity
                code_mv[code] = (px, mv)
                cur_total_mv += mv

            if cur_total_mv > 0:
                n = len(code_mv)
                # 目标总持仓 = 总资产 × 仓位比例
                target_total = total_value * self.weight_fraction
                # 等权目标 = 目标总持仓 / 持仓数
                target_each = target_total / max(n, 1)

                # 阈值: 统一使用「持仓偏离阈值」(默认 5%), 不再混用市值变化阈值
                mv_thresh = max(0.0, float(self.rebalance_threshold))
                # 往返手续费率估算 (佣金+印花税+过户费, 约 0.25%)
                fee_rate = 0.0025

                targets = {}
                for code, (px, cur_mv) in code_mv.items():
                    dev = cur_mv / target_each - 1.0 if target_each > 0 else 0.0
                    # 价格触发: 相对上次建仓/调仓价涨跌超 4% → 视为需要调回目标权重
                    base_px = self._entry_prices.get(code)
                    dev_px = (px / base_px - 1.0) if (base_px and base_px > 0) else 0.0
                    # 触发条件:
                    #   股票: 权重偏离超阈值 或 价格涨跌超 4% (严格执行低买高卖)
                    #   ETF : 下跌 ≥4% 补仓, 或权重偏离 ≥5% 再平衡;
                    #         上涨未达权重阈值时不因 +4% 削赢 (2026-11-23 确认)
                    if self.pool_type == "etf":
                        if not (dev_px <= -PRICE_REBAL_THRESHOLD
                                or abs(dev) >= mv_thresh):
                            continue
                    elif abs(dev) < mv_thresh and abs(dev_px) < PRICE_REBAL_THRESHOLD:
                        continue
                    # 手续费感知: 调整金额不足以覆盖往返手续费则不调
                    adj_amount = abs(cur_mv - target_each)
                    if adj_amount < target_each * fee_rate:
                        continue
                    # 100 股整数倍约束: 调整量不足 100 股则不调
                    if adj_amount < px * 100:
                        continue
                    targets[code] = target_each

                # 总仓位不足: 以「个体调仓后的预计总市值」判断是否仍低于阈值。
                # 避免单标的偏离时连带触发其他处于阈值内的标的 (测试: 单标的偏离不得连带调仓)。
                proj_total = cur_total_mv
                for code, tgt in targets.items():
                    proj_total += (tgt - code_mv[code][1])
                total_dev = proj_total / target_total - 1.0 if target_total > 0 else 0.0
                if total_dev < -mv_thresh and targets:
                    # 差额仅分摊给已触发调仓的标的, 不再新增处于阈值内的标的
                    shortfall = (target_total - proj_total) / len(targets)
                    for code in list(targets.keys()):
                        px, cur_mv = code_mv[code]
                        tgt = min(targets[code] + shortfall, target_each * (1 + mv_thresh))
                        if (tgt - cur_mv) >= px * 100:
                            targets[code] = tgt

                if targets:
                    rebalance_orders = self._build_rebalance(date, targets)

        if buys or sells or rebalance_orders:
            return SignalEvent(date, buys=buys, sells=sells,
                               rebalance_orders=rebalance_orders,
                               sell_reasons=sell_reasons)
        return None

    def on_trade(self, fill: FillEvent) -> None:
        """成交后以实际成交价重置调仓基准
        (信号用 T 日收盘价, 撮合在 T+1 开盘, 两者差额会反复触发再平衡)
        """
        code = fill.code
        if fill.direction == "sell" and code not in self.portfolio.positions:
            # 已清仓 (MA30 走弱换出): 基准一并清除, 重新建仓时再记录
            self._entry_cap_ratios.pop(code, None)
            self._entry_prices.pop(code, None)
            return
        px = fill.fill_price
        if px and not np.isnan(px):
            self._entry_prices[code] = px
        cap = self._current_cap(code, fill.date)
        if not np.isnan(cap):
            self._entry_cap_ratios[code] = cap


# ==================== 涨跌停撮合约束 ====================

_LIMIT_RULE_DEFAULTS = {"enforce": True, "strict_open": True, "use_st": True}


def normalize_limit_rules(limit_rules: dict | None) -> dict:
    """撮合规则开关: enforce=是否启用, strict_open=开盘触板即拦, use_st=按ST识别5%"""
    rules = dict(_LIMIT_RULE_DEFAULTS)
    if isinstance(limit_rules, dict):
        rules.update(limit_rules)
    return rules


def _is_st_symbol(code, date) -> bool:
    """按公告日取当日可见的 ST 标记; 无财务数据时按非 ST 处理"""
    try:
        f = fundamental_asof(str(date)[:10])["map"].get(code)
        return bool(f and f.get("is_st"))
    except Exception:
        return False


def _order_block_reason(bars, order, date, rules: dict) -> str | None:
    """T+1 开盘撮合被涨跌停拦截的原因; None 表示可成交"""
    if not rules.get("enforce", True):
        return None
    row = bars.row(order.code, date)
    if row is None:
        return None
    is_st = _is_st_symbol(order.code, date) if rules.get("use_st", True) else False
    return price_limits.fill_block_reason(
        row,
        bars.prev_close(order.code, date),
        order.code,
        date,
        order.direction,
        is_st=is_st,
        strict_open=bool(rules.get("strict_open", True)),
    )


class EventDrivenBacktest:
    """事件驱动回测主引擎

    时序: T日收盘后选股 → T+1开盘成交 (无前视偏差)
    成本: 佣金/印花税/过户费/滑点/冲击成本
    """

    def __init__(self, feat: dict, strategy: BaseStrategy,
                 initial_capital: float = 1_000_000, top_n: int = 5,
                 hold_days: int = 5, limit_up_pct: float = 9.5,
                 start_date=None, end_date=None, cost_params: dict = None,
                 limit_rules: dict = None):
        self.feat = feat
        self.strategy = strategy
        self.initial_capital = initial_capital
        self.top_n = top_n
        self.hold_days = hold_days
        self.limit_up_pct = limit_up_pct
        self.limit_rules = normalize_limit_rules(limit_rules)
        self.bars = build_date_index(feat)
        self.all_dates = self.bars.all_dates
        self.sel_dates = get_selection_dates(feat)
        self.cost_model = CostModel(**(cost_params or {}))
        self.portfolio = Portfolio(initial_capital)
        self._date_idx = {pd.Timestamp(d): i for i, d in enumerate(self.all_dates)}

        # 时间区间
        if start_date:
            sd = pd.Timestamp(start_date)
            self.all_dates = self.all_dates[self.all_dates >= sd]
        if end_date:
            ed = pd.Timestamp(end_date)
            self.all_dates = self.all_dates[self.all_dates <= ed]
        # 选股日也限定到回测区间 (日历展示用)
        date_set = set(self.all_dates.tolist())
        self.sel_dates = {d for d in self.sel_dates if d in date_set}

        # 待执行订单 (T日产生, T+1开盘撮合)
        self._pending_orders: list[OrderEvent] = []
        # 跌停/停牌拦住卖不掉的订单: 顺延到下一可卖日, 不静默丢弃
        self._deferred_sells: dict[str, OrderEvent] = {}
        self.fill_blocks = {
            "buy_limit_up": 0, "buy_suspended": 0,
            "sell_limit_down": 0, "sell_suspended": 0,
        }
        self.curve_rows = []
        self.holdings_log = []   # 每日持仓明细: [{date, code, qty, price, mv, cost}]
        self.trade_dates = set()
        self.lu_dates = set()

    def run(self) -> dict:
        # 识别策略类型: 自定义策略类 (非 FunctionStrategyAdapter) → 每日检查
        is_custom = not isinstance(self.strategy, FunctionStrategyAdapter)

        # 初始化策略上下文 (自定义策略类需要 portfolio/bars/feat)
        self.strategy.on_init({
            "portfolio": self.portfolio,
            "bars": self.bars,
            "feat": self.feat,
        })

        for i, current_date in enumerate(self.all_dates):
            # === 1. 撮合上一日产生的订单 (T+1 开盘) ===
            self._execute_orders(current_date)

            # === 2. 生成当日截面并发布行情事件 ===
            # 自定义策略类: 每日都传 snapshot (用于每日再平衡检查)
            # 函数式策略: 仅选股日传 snapshot
            if is_custom:
                snapshot = self.bars.snapshot(current_date)
            elif current_date in self.sel_dates:
                snapshot = self.bars.snapshot(current_date)
            else:
                snapshot = None

            event = MarketBarEvent(current_date, snapshot, self.bars)
            signal = self.strategy.on_bar(event)

            # === 3. 自定义策略类: 跳过固定持有期/涨停退出 (由策略自己控制) ===
            if not is_custom:
                self._check_exits(current_date, signal)

            # === 4. 把信号转为待执行订单 (T+1 执行) ===
            # (a) 策略直接指定的 rebalance_orders → 全部排队
            if signal and signal.rebalance_orders:
                self._pending_orders.extend(signal.rebalance_orders)

            # (b) 标准 buys/sells → 引擎按默认逻辑计算数量
            if signal and (signal.buys or signal.sells):
                # 卖出旧持仓 + 买入新股
                sell_reasons = getattr(signal, "sell_reasons", None) or {}
                for code in list(self.portfolio.positions.keys()):
                    if code in signal.sells:
                        order = OrderEvent(
                            current_date, code, "sell",
                            self.portfolio.positions[code].quantity)
                        order.reason = sell_reasons.get(code, "rebalance")
                        self._pending_orders.append(order)
                per = self.portfolio.cash / max(self.top_n, 1)
                # 买入预算必须包含当日卖出回款 (T+1 先卖后买), 否则换标的时
                # 只能用剩余现金建仓, 造成仓位不足或异常缩量。
                if signal.sells:
                    est_proceeds = 0.0
                    for _c in signal.sells:
                        _pos = self.portfolio.positions.get(_c)
                        if _pos is None:
                            continue
                        _row = self.bars.row(_c, current_date)
                        _px = _row["close"] if _row is not None else None
                        if _px and _px > 0:
                            est_proceeds += _pos.quantity * _px * 0.998
                    per = (self.portfolio.cash + est_proceeds) / max(self.top_n, 1)
                for code in signal.buys:
                    if code in self.portfolio.positions:
                        continue  # 已持有, 不动
                    row = self.bars.row(code, current_date)
                    if row is None:
                        continue
                    ref_px = row.get("open", row["close"])
                    if not ref_px or ref_px <= 0:
                        continue
                    qty = int(per / ref_px / 100) * 100
                    if qty < 100:
                        continue   # 资金不足一手 → 不下单, 避免强行买入
                    self._pending_orders.append(OrderEvent(
                        current_date, code, "buy", qty))


            # === 5. 记录净值 + 每日持仓明细 ===
            pv = self.portfolio.total_value(current_date, self.bars)
            self.curve_rows.append({
                "date": current_date, "value": pv,
                "cash": self.portfolio.cash,
                "holdings": self.portfolio.holdings_count,
            })
            # 每日持仓快照 (仅记录有持仓的日期)
            if self.portfolio.positions:
                for code, pos in self.portfolio.positions.items():
                    row = self.bars.row(code, current_date)
                    if row is None:
                        row = self.bars.last_row(code, current_date)
                    px = row["close"] if row is not None else pos.entry_price
                    mv = px * pos.quantity
                    self.holdings_log.append({
                        "date": current_date,
                        "code": code,
                        "qty": pos.quantity,
                        "cost_price": round(pos.entry_price, 3),
                        "close": round(px, 3),
                        "market_value": round(mv, 2),
                        "pnl": round((px - pos.entry_price) * pos.quantity, 2),
                    })

        # 期末: 用最后一日收盘价更新最后一条净值
        if self.curve_rows:
            last = self.curve_rows[-1]
            self.curve_rows[-1]["value"] = self.portfolio.total_value(
                last["date"], self.bars)

        curve_df = pd.DataFrame(self.curve_rows)
        perf = calc_performance(curve_df, self.portfolio.trades,
                                self.initial_capital)
        perf["cost_summary"] = dict(self.portfolio.cost_summary)
        return {
            "curve": curve_df,
            "trades": self.portfolio.trades,
            "performance": perf,
            "selection_dates": sorted(self.sel_dates),
            "trade_dates": sorted(self.trade_dates),
            "lu_dates": sorted(self.lu_dates),
            "all_trade_days": self.all_dates.tolist(),
            "holdings_log": self.holdings_log,
            "fills": self.portfolio.fills,
            "fill_blocks": dict(self.fill_blocks),
            # 资金实时校验结果: 正常应为 0 (无拒单 / 无负现金兜底)
            "rejected_buys": self.portfolio.rejected_buys,
            "cash_floor_hits": self.portfolio.cash_floor_hits,
        }

    def _do_fill(self, order, date, row):
        """按当日开盘价成交一笔订单"""
        ref_px = row.get("open", row["close"])
        if not ref_px or ref_px <= 0:
            return False
        day_amount = row.get("amount", 0.0) or 0.0
        fp, comm, stamp, trans, slip, impact = self.cost_model.calc_fill(
            order.code, order.direction, order.quantity, ref_px, day_amount)
        fill = FillEvent(date, order.code, order.direction, order.quantity,
                         fp, comm, stamp, trans, slip, impact,
                         reason=getattr(order, "reason", "rebalance"))
        if not self.portfolio.apply_fill(fill):
            return False
        self.strategy.on_trade(fill)
        return True

    def _execute_orders(self, date):
        """T+1 开盘撮合待执行订单

        涨跌停约束:
        - 买入遇涨停 → 放弃本笔 (不追高), 计入 buy_limit_up
        - 卖出遇跌停/停牌 → 顺延到下一可卖日, 计入 sell_limit_down / sell_suspended
        """
        if not self._pending_orders and not self._deferred_sells:
            return
        enforce = bool(self.limit_rules.get("enforce", True))
        if not enforce:
            self._deferred_sells.clear()
        executed = False
        # 碎单过滤门槛: 避免因微小偏离反复买卖 (默认 1 万元本金 → 1000 元, 100 万 → 3000 元)
        _dust = max(500.0, min(3000.0, self.initial_capital * 0.003))

        # 1) 先重试此前被跌停/停牌拦住的卖单
        for code, order in list(self._deferred_sells.items()):
            pos = self.portfolio.positions.get(code)
            if pos is None:
                self._deferred_sells.pop(code, None)
                continue
            order.quantity = pos.quantity
            row = self.bars.row(code, date)
            if row is None:
                self.fill_blocks["sell_suspended"] += 1
                continue
            block = _order_block_reason(self.bars, order, date, self.limit_rules)
            if block:
                self.fill_blocks[f"sell_{block}"] += 1
                continue
            if self._do_fill(order, date, row):
                self._deferred_sells.pop(code, None)
                executed = True

        # 2) 拆分为「卖出阶段」与「买入阶段」: 严格先卖后买
        #    ETF/股票调仓换标的时, 卖出回笼的资金必须先入账, 再用于买入;
        #    同一交易日不允许先买后卖 (否则可用资金不足会透支)。
        pending = self._pending_orders
        sell_orders = [o for o in pending if o.direction == "sell"]
        buy_orders = [o for o in pending if o.direction == "buy"]

        # 2a) 卖出阶段 (09:30): 回笼资金
        for order in sell_orders:
            if order.code in self._deferred_sells:
                continue  # 同一标的已有更早的顺延卖单, 以它为准
            row = self.bars.row(order.code, date)
            if row is None:
                if enforce and order.code in self.portfolio.positions:
                    self._deferred_sells[order.code] = order
                    self.fill_blocks["sell_suspended"] += 1
                continue
            block = _order_block_reason(self.bars, order, date, self.limit_rules)
            if block:
                self._deferred_sells[order.code] = order
                self.fill_blocks[f"sell_{block}"] += 1
                continue
            _pos = self.portfolio.positions.get(order.code)
            _px = row.get("open", row["close"]) or 0
            if _pos is not None and order.quantity * _px < _dust and order.quantity < _pos.quantity:
                continue  # 碎单不卖 (清仓单不受限制)
            if self._do_fill(order, date, row):
                executed = True

        # 2b) 买入阶段 (09:31): 只使用卖出后已到账的现金
        for order in buy_orders:
            row = self.bars.row(order.code, date)
            if row is None:
                self.fill_blocks["buy_suspended"] += 1
                continue
            block = _order_block_reason(self.bars, order, date, self.limit_rules)
            if block:
                self.fill_blocks[f"buy_{block}"] += 1
                continue
            _px = row.get("open", row["close"]) or 0
            # 按 T+1 实际成交价重算股数: 信号用 T 日收盘价算目标, 撮合却在 T+1 开盘,
            # 跳空会让实际买入金额偏离目标市值 (建仓超配 → 首次下跌触发变成减仓).
            # 这里用「目标市值 / 开盘价」反算整手数; 现金不足仍由 apply_fill 收缩。
            _tgt = getattr(order, "target_mv", 0.0)
            if _tgt and _px > 0:
                order.quantity = max(int(_tgt / _px // 100) * 100, 0)
            if order.quantity * _px < _dust:
                continue  # 碎单不买
            if self._do_fill(order, date, row):
                executed = True

        if executed:
            self.trade_dates.add(date)
        self._pending_orders = []

    def _check_exits(self, date, signal):
        """检查持有到期 / 涨停, 产生卖出订单 (T+1 执行)"""
        cur_idx = self._date_idx.get(pd.Timestamp(date))
        for code in list(self.portfolio.positions.keys()):
            pos = self.portfolio.positions[code]
            row = self.bars.row(code, date)
            if row is None:
                continue
            entry_idx = self._date_idx.get(pd.Timestamp(pos.entry_date))
            if entry_idx is not None and cur_idx is not None:
                held = cur_idx - entry_idx + 1
            else:
                held = (date - pos.entry_date).days
            sell = False
            reason = ""
            if held >= self.hold_days:
                sell = True
                reason = "hold_expire"
            elif row.get("limit_up", 0) == 1 and held >= 1:
                sell = True
                reason = "limit_up"
                self.lu_dates.add(date)
            if sell:
                order = OrderEvent(date, code, "sell", pos.quantity)
                order.reason = reason
                self._pending_orders.append(order)


# ==================== 回测引擎 (兼容入口) ====================

def run_backtest(feat: dict, select_fn, strategy_params: dict = None,
                 initial_capital: float = 1_000_000, top_n: int = 5,
                 hold_days: int = 5, limit_up_pct: float = 9.5,
                 start_date=None, end_date=None, cost_params: dict = None,
                 use_strategy_class: bool = False,
                 limit_rules: dict = None) -> dict:
    """
    完整资金曲线回测

    Args:
        select_fn: 选股函数 或 BaseStrategy 实例 (use_strategy_class=True 时)
        use_strategy_class: True 时 select_fn 是 BaseStrategy 实例, 直接使用

    Returns:
        dict with: curve_df, performance dict, trades list,
                  selection_dates (list), trade_dates (list), lu_dates (list)
    """
    if strategy_params is None:
        strategy_params = {}
    strategy_params.setdefault("top_n", top_n)
    # 持仓数可能与 run_backtest 形参不同 (UI 通过 strategy_params 传入), 以策略参数为准
    try:
        top_n = int(strategy_params.get("top_n", top_n))
    except (TypeError, ValueError):
        pass

    # 日期区间合法性检查 (交易天数不足直接返回空)
    bars = build_date_index(feat)
    all_trade_days = list(bars.all_dates)
    if start_date is not None:
        sd = pd.Timestamp(start_date)
        all_trade_days = [d for d in all_trade_days if d >= sd]
    if end_date is not None:
        ed = pd.Timestamp(end_date)
        all_trade_days = [d for d in all_trade_days if d <= ed]

    if len(all_trade_days) < 20:
        return {"curve": pd.DataFrame(), "trades": [], "performance": {},
                "selection_dates": [], "trade_dates": [], "lu_dates": [],
                "all_trade_days": all_trade_days}

    # 构造策略实例
    if use_strategy_class and isinstance(select_fn, BaseStrategy):
        strategy = select_fn
    else:
        # 函数式策略 → BaseStrategy 适配器
        strategy = FunctionStrategyAdapter(
            select_fn, name=getattr(select_fn, "__name__", "adaptive"),
            **strategy_params)

    # 事件驱动引擎 (T+1 开盘撮合 + A 股真实成本模型)
    engine = EventDrivenBacktest(
        feat, strategy,
        initial_capital=initial_capital,
        top_n=top_n,
        hold_days=hold_days,
        limit_up_pct=limit_up_pct,
        start_date=start_date,
        end_date=end_date,
        cost_params=cost_params,
        limit_rules=limit_rules,
    )
    return engine.run()


def calc_performance(curve_df: pd.DataFrame, trades: list, initial: float) -> dict:
    """计算完整绩效指标"""
    if len(curve_df) < 2:
        return {}

    init = initial
    final = curve_df["value"].iloc[-1]
    days = (curve_df["date"].iloc[-1] - curve_df["date"].iloc[0]).days
    years = max(days / 365.25, 0.001)

    total_ret = (final / init - 1) * 100
    cagr = ((final / init) ** (1 / years) - 1) * 100

    # 最大回撤
    curve_df = curve_df.copy()
    curve_df["peak"] = curve_df["value"].cummax()
    curve_df["dd"] = (curve_df["value"] - curve_df["peak"]) / curve_df["peak"] * 100
    mdd = curve_df["dd"].min()

    # 夏普
    curve_df["ret"] = curve_df["value"].pct_change().dropna()
    daily_std = curve_df["ret"].std()
    sharpe = (curve_df["ret"].mean() / daily_std) * np.sqrt(252) if daily_std > 0 else 0

    # 日胜率
    wr = (curve_df["ret"] > 0).sum() / len(curve_df["ret"]) * 100 if len(curve_df) > 1 else 0

    # 涨停命中率
    if trades:
        hit_lu = sum(1 for t in trades if t["reason"] == "limit_up")
        hit_rate = hit_lu / len(trades) * 100
    else:
        hit_rate = 0

    # 盈亏比
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    avg_win = np.mean([t["pnl"] for t in wins]) if wins else 0
    avg_loss = abs(np.mean([t["pnl"] for t in losses])) if losses else 1
    profit_factor = avg_win / avg_loss if avg_loss > 0 else 999

    # 最大连亏
    df_ret = curve_df["ret"].dropna()
    loss_streak = 0
    max_streak = 0
    for val in df_ret:
        if val < 0:
            loss_streak += 1
            max_streak = max(max_streak, loss_streak)
        else:
            loss_streak = 0

    # 索提诺比率 (下行波动率)
    neg_ret = df_ret[df_ret < 0]
    downside_std = neg_ret.std() if len(neg_ret) > 1 else 0
    sortino = (df_ret.mean() / downside_std) * np.sqrt(252) if downside_std > 0 else 0

    # 卡尔玛比率 (CAGR / |最大回撤|)
    calmar = cagr / abs(mdd) if mdd < 0 else (999 if cagr > 0 else 0)

    # 最大回撤持续天数 (水下最长连续交易日数)
    underwater = curve_df["dd"] < 0
    dd_dur = 0
    max_dd_dur = 0
    for u in underwater:
        if u:
            dd_dur += 1
            max_dd_dur = max(max_dd_dur, dd_dur)
        else:
            dd_dur = 0

    # 换手率 & 成本占比 (基于成交明细)
    total_buy = 0.0
    total_sell = 0.0
    total_cost = 0.0
    for t in trades:
        shares = t.get("shares", 0) or 0
        total_buy += (t.get("buy_price", 0) or 0) * shares
        total_sell += (t.get("sell_price", 0) or 0) * shares
        total_cost += t.get("cost", 0) or 0
    total_notional = total_buy + total_sell
    avg_pv = curve_df["value"].mean() if len(curve_df) else 1
    n_days = max(len(curve_df), 1)
    # 日均换手率 = 日均成交金额 / 平均账户市值
    daily_turnover = (total_notional / n_days) / avg_pv * 100 if avg_pv > 0 else 0
    # 成本占比 = 总交易成本 / 总成交金额
    cost_ratio = (total_cost / total_notional * 100) if total_notional > 0 else 0

    return {
        "initial_capital": init,
        "final_value": final,
        "total_return": total_ret,
        "cagr": cagr,
        "max_drawdown": mdd,
        "sharpe": sharpe,
        "sortino": sortino,
        "calmar": calmar,
        "win_rate": wr,
        "hit_rate": hit_rate,
        "trades_count": len(trades),
        "wins_count": len(wins),
        "losses_count": len(losses),
        "profit_factor": profit_factor,
        "max_loss_streak": max_streak,
        "max_dd_duration": max_dd_dur,
        "daily_turnover": daily_turnover,
        "cost_ratio": cost_ratio,
        "total_cost": total_cost,
        "days": days,
        "years": years,
    }


# ==================== 今日选股 ====================

def get_today_picks(feat: dict, select_fn, strategy_params: dict = None, top_n: int = 10) -> pd.DataFrame:
    """获取今日（或最新一个交易日）的选股"""
    if strategy_params is None:
        strategy_params = {}
    strategy_params["top_n"] = 50  # 多取一些

    if not feat:
        return pd.DataFrame()

    # 取所有股票最新日期 (数据已按日期升序, 直接看最后一行)
    latest_date = max(df["date"].iloc[-1] for df in feat.values() if len(df))
    latest_snap = {}
    for sc, df in feat.items():
        if len(df) and df["date"].iloc[-1] == latest_date:
            latest_snap[sc] = df.iloc[-1].to_dict()

    # 注入财务上下文 (今日选股用最新可见财报)
    _ctx = fundamental_asof(latest_date)
    if _ctx["map"]:
        for _sc, _row in latest_snap.items():
            _row["_f"] = _ctx["map"].get(_sc)
            _row["_fmed"] = _ctx["median"]

    selected = select_fn(latest_snap, **strategy_params)

    # 构造结果
    rows = []
    for sc in selected[:top_n]:
        row = latest_snap.get(sc)
        if row is None:
            continue
        code = sc[2:]
        f = row.get("_f") or {}
        rows.append({
            "code": code,
            "sina_code": sc,
            "date": latest_date.strftime("%Y-%m-%d"),
            "close": row["close"],
            "ret_5": row.get("ret_5", 0),
            "ret_20": row.get("ret_20", 0),
            "turnover_pct": row.get("turnover_pct", 0),
            "cap_yi": row.get("cap_yi", 0),
            "above_ma20": row.get("above_ma20", 0),
            "vol_expand": row.get("vol_expand", 0),
            "industry": f.get("industry") or "-",
            "pe_ttm": f.get("pe_ttm") or _est_pe_pb(row.get("close"), f)[0],
            "profit_yoy": f.get("profit_yoy"),
            "roe": f.get("roe"),
            "debt_ratio": f.get("debt_ratio"),
        })

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("ret_20", ascending=False)
    return df
