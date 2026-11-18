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
    key_raw = f"v15|{strategy_key}|{initial}|{hold}|{top}|{lu_pct}|{start_d}|{end_d}|{cost_str}"
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
        "trades": r["trades"],
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
        "trades": d["trades"],
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


def add_factors(df: pd.DataFrame, code: str = None) -> pd.DataFrame | None:
    """为单只股票计算技术面因子

    code 提供时按板块口径标记涨停 (主板 10% / 创业板·科创板 20% / 北交所 30%),
    不再对所有标的写死 9.5%。
    """
    if df is None or len(df) < 120:
        return None
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
    lu_threshold = price_limits.board_limit_pct(code) * 100 - 0.5 if code else 9.5
    df["limit_up"] = (df["pct"] >= lu_threshold).astype(int)
    df["vol_ma5"] = df["volume"].rolling(5).mean()
    df["vol_expand"] = (df["volume"] > df["vol_ma5"] * 1.5).astype(int)

    return df


def compute_factors(raw: dict) -> dict:
    """批量计算因子"""
    feat = {}
    for code, df in raw.items():
        f = add_factors(df, code=code)
        if f is not None:
            feat[code] = f
    return feat


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
            sub = df[["date", close_col]].dropna()
            if len(sub) < 60:
                continue
            sub = sub.sort_values("date")
            dts = pd.to_datetime(sub["date"])
            vals_arr = sub[close_col].to_numpy(dtype="float64")
            wk = pd.Series(vals_arr, index=dts).resample("W-FRI").last().dropna()
            need = 30 + lb
            if len(wk) < need:
                continue   # 周数不足 → 不参与 MA30 判断 (与原逻辑一致: 放行)
            vals = wk.to_numpy(dtype="float64")
            cache[code] = {
                "wk_idx": wk.index.values,
                "prefix": np.concatenate(([0.0], np.cumsum(vals))),
                "ma30": pd.Series(vals).rolling(30).mean().to_numpy(),
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
                                ma30_cache: dict = None) -> list:
    """
    市值策略股票池过滤:
    - CrossSection 输入走向量化快路径 (numpy 掩码, 绕开逐标的 dict 构造, 1000x 快)
    - 普通 dict 输入保留原逐标的逻辑 (回退路径, 兼容函数式选股等)
    - 排除创业板 (30) / 科创板 (688) / 北交所 (8/4) — 北交所已由数据层排除
    - 市值 [cap_min, cap_max] 亿之间
    - 上市 >= min_listed_years 年 / 周K线 MA30 上升 / 不良标的剔除
    - 按市值从小到大排列
    """
    # --- CrossSection 向量化快路径 ---
    if isinstance(snap, CrossSection):
        return _filter_crosssection_vec(
            snap, feat, date, cap_min, cap_max, min_listed_years,
            exclude_st, exclude_ocf_diverge, exclude_nonop_profit,
            exclude_no_dividend, dividend_years, f_map, div_map,
            ma30_up_only, ma30_lookback, ma30_cache)

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
        candidates.append((code, mc))
    candidates.sort(key=lambda x: x[1])
    return [c[0] for c in candidates]


def _filter_crosssection_vec(cs: "CrossSection", feat, date,
                              cap_min, cap_max, min_listed_years,
                              exclude_st, exclude_ocf_diverge, exclude_nonop_profit,
                              exclude_no_dividend, dividend_years, f_map, div_map,
                              ma30_up_only, ma30_lookback, ma30_cache) -> list:
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
        ma30_mat = _get_ma30_up_matrix(cs._bar_index, ma30_cache, ma30_lookback)
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


def _get_ma30_up_matrix(bar_index, ma30_cache, lookback) -> np.ndarray | None:
    """预构建 [D, N] 布尔矩阵, 记录每日每只标的 MA30 是否向上

    严格对齐 ma30_slope_up 的逐日口径 (含未走完一周用当日收盘补齐、beyond 边界等),
    构建一次 ~1s (5000×900), 之后逐日过滤只是取一行布尔数组, 无 Python 循环。
    缓存挂在 bar_index 实例上, 生命周期与 bar_index 一致。
    """
    if bar_index is None:
        return None
    cache_attr = f"_ma30_up_mat_{lookback}"
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

        # --- 逐日查找 ---
        wi = np.searchsorted(wk_idx, dates_arr, side="left")  # 第一个 wk_idx >= date
        beyond = wi >= nwk
        wi_eff = np.where(beyond, nwk - 1, wi)

        # 有效日期: 有足够的历史 (i >= 29 + lb)
        valid = wi_eff >= (29 + lb)

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
        past[in_range] = item["ma30"][past_idx[in_range]]

        mask_j = valid & np.isfinite(cur) & np.isfinite(past) & (cur > past)
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


STRATEGIES = {
    "小市值轮动·戴维斯双击": select_smallcap_rotation,
    "动量+均线+小市值": select_momentum,
    "市值策略": select_market_cap,   # 函数式 (给今日选股/快速回测)
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

        # 冲击成本
        impact_cost = 0.0
        if day_amount > 0:
            ratio = amount / day_amount
            if ratio > self.IMPACT_THRESHOLD:
                impact_cost = amount * (ratio ** 1.5)

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

        if fill.direction == "buy":
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

        self.cost_summary["commission"] += fill.commission
        self.cost_summary["stamp_tax"] += fill.stamp_tax
        self.cost_summary["transfer_fee"] += fill.transfer_fee
        self.cost_summary["slippage"] += fill.slippage_cost
        self.cost_summary["impact"] += fill.impact_cost

        # 逐笔成交流水 (每笔买/卖独立记录, 含成交后账户余额与实现盈亏)
        self.fills.append({
            "date": fill.date,
            "code": fill.code,
            "direction": fill.direction,
            "price": round(fill.fill_price, 3),
            "qty": fill.quantity,
            "amount": round(amount, 2),
            "cost": round(fill.total_cost, 2),
            "cash_after": round(self.cash, 2),
            "pnl": round(realized_pnl, 2),
            "reason": fill.reason or "",
        })


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
         - 指定标的池 (universe) 时没有排名概念, topk_drop 自动退化为偏离度再平衡
    """

    name = "市值策略"

    def __init__(self, top_n=3, cap_min=15.0, cap_max=200.0,
                 weight_fraction=0.75, cap_change_threshold=0.04,
                 rebalance_threshold=0.05, min_listed_years=3.0,
                 exclude_st=True, exclude_ocf_diverge=True,
                 exclude_nonop_profit=True, exclude_no_dividend=True,
                 dividend_years=3, ma30_lookback=5, universe=None,
                 ma30_exit=True, rebalance_mode="deviation", rank_buffer=2):
        self.top_n = int(top_n)
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
        # 调仓方式: deviation=偏离度再平衡 (旧逻辑); topk_drop=排名缓冲 (借 Qlib Topk-Drop)
        self.rebalance_mode = str(rebalance_mode)
        self.rank_buffer = int(rank_buffer)      # topk_drop: 排名跌出 top_n+rank_buffer 才换出

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

    def _selection_kwargs(self, date):
        """选股过滤参数: 财务/分红上下文按 as-of 加载 (避免未来函数)"""
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
                    ma30_cache=self._ensure_ma30_cache(),
                    f_map=f_map, div_map=div_map, date=date)

    def on_init(self, context: dict) -> None:
        self.portfolio = context.get("portfolio")
        self.bars = context.get("bars")
        self.feat = context.get("feat")

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
            if abs(diff) < cur_price * 100:
                continue  # 差异不足100股, 跳过

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
        if portfolio.positions and self.ma30_exit:
            for code in list(portfolio.positions.keys()):
                if self._ma30_is_decreasing(code, date):
                    sells.append(code)
                    sell_reasons[code] = "ma30_exit"
                    self._entry_cap_ratios.pop(code, None)
                    self._entry_prices.pop(code, None)
                    self._target_weights.pop(code, None)

        # =========================================
        # 2. 空仓或换股后仓位不足 → 建仓 (MA30 上升过滤)
        # =========================================
        sell_codes = set(sells)
        remaining = [c for c in portfolio.positions.keys() if c not in sell_codes]
        need_fill = self.n_slots - len(remaining)

        # =========================================
        # 2a. Topk-Drop 换股 (借 Qlib 思路):
        #     持仓只有跌出候选前 top_n+rank_buffer 名才卖出;
        #     空槽位才补最靠前的候选; 不做偏离度再平衡
        #     → 换手从"每天全额重排"降到"只在排名变化时换股"
        # =========================================
        if self.rebalance_mode == "topk_drop" and snap and not self.universe:
            ranked = _filter_market_cap_universe(
                snap, feat=self.feat, **self._selection_kwargs(date))
            rank_map = {c: i + 1 for i, c in enumerate(ranked)}
            keep_limit = self.n_slots + self.rank_buffer
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
            if need_fill > 0:
                candidates = [c for c in ranked
                              if c not in portfolio.positions and c not in sell_codes][:need_fill]
                if candidates:
                    target_each = total_value * self.weight_fraction / self.n_slots
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
                        self._target_weights[code] = 1.0 / self.n_slots
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
                    selected.append(code)
            else:
                # 选股池: MA30 上升 + 市值/财务过滤 (已由 _filter_market_cap_universe 内置)
                selected = _filter_market_cap_universe(
                    snap, feat=self.feat, **self._selection_kwargs(date))
                # 排除已持有 + 已卖出的
                selected = [c for c in selected
                            if c not in portfolio.positions and c not in sell_codes]
            candidates = selected[:need_fill]

            # 对 sell_codes 也清理 _target_weights (可能已在上一步清)
            for code in sells:
                self._target_weights.pop(code, None)

            # 计算: 卖出后回笼资金 + 现有持仓市值 → 均分
            # 等比例目标: 总持有标的 (remaining + candidates) 均分 weight_fraction * total_value
            held_plus_new = remaining + candidates
            if held_plus_new:
                # 先卖出 sell_codes (全仓卖) — 走标准 sells 通道
                # 再用 rebalance_orders 对 remaining + candidates 等比例建仓
                # 卖单和买单在不同 pending queue, 但都是 T+1 执行
                # 我们用 buys 通道传新候选 → 引擎会自动计算数量
                buys = candidates

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

                # 空仓建仓走 rebalance_orders (精确控制数量)
                if not remaining and candidates:
                    target_each = total_value * self.weight_fraction / len(candidates)
                    targets = {code: target_each for code in candidates}
                    rebalance_orders = self._build_rebalance(date, targets)
                    buys = []  # 已由 rebalance_orders 处理

        # =========================================
        # 3. 动态调仓: 各标的按自身偏离补仓/减仓 (单只目标恒定, 保持仓位比例)
        #    价格/市值下跌 → 补仓; 上涨 → 减仓
        #    只调整自身偏离超阈值的标的, 不会因其他标的涨跌而被连带卖出
        # =========================================
        held_codes = [c for c in portfolio.positions.keys() if c in self._target_weights]
        # 排除当日要卖出的
        held_codes = [c for c in held_codes if c not in sell_codes]

        # 指定标的池时没有"排名"概念, topk_drop 退化为偏离度再平衡;
        # 否则 universe + topk_drop 会既不换股也不调仓 (一路持有到 MA30 走弱)
        _deviation_rebal = (self.rebalance_mode != "topk_drop") or bool(self.universe)
        if _deviation_rebal and held_codes and not rebalance_orders:
            # 单只目标市值: 账户总市值 × 目标仓位 / 持仓数
            # 用 total_value 而非持仓总市值 → 现金与持仓的比例也保持不变
            target_each = total_value * self.weight_fraction / max(len(held_codes), 1)
            targets = {}
            for code in held_codes:
                pos = portfolio.positions.get(code)
                if pos is None:
                    continue
                cur_price = self._current_price(code, date)
                if not cur_price or np.isnan(cur_price):
                    continue
                cur_mv = cur_price * pos.quantity
                # a) 自身价格/市值相对建仓基准的偏离: 跌 ≥ 阈值 → 补仓, 涨 ≥ 阈值 → 减仓
                #    优先用市值 (个股); ETF 无市值数据 → 用价格
                base_cap = self._entry_cap_ratios.get(code)
                cur_cap = self._current_cap(code, date)
                if base_cap and not np.isnan(cur_cap) and base_cap > 0:
                    cap_dev = cur_cap / base_cap - 1.0
                else:
                    base_px = self._entry_prices.get(code)
                    cap_dev = (cur_price / base_px - 1.0) if base_px else 0.0
                # b) 自身市值相对目标仓位的偏离
                mv_dev = (cur_mv / target_each - 1.0) if target_each > 0 else 0.0
                if abs(cap_dev) >= self.cap_change_threshold or \
                        abs(mv_dev) >= self.rebalance_threshold:
                    targets[code] = target_each

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
                per = self.portfolio.cash / self.top_n
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
                    qty = max(qty, 100)
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
        self.portfolio.apply_fill(fill)
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

        # 2) 处理昨日新产生的订单
        for order in self._pending_orders:
            if order.direction == "sell" and order.code in self._deferred_sells:
                continue  # 同一标的已有更早的顺延卖单, 以它为准
            row = self.bars.row(order.code, date)
            if row is None:
                if enforce and order.direction == "sell" and order.code in self.portfolio.positions:
                    self._deferred_sells[order.code] = order
                    self.fill_blocks["sell_suspended"] += 1
                elif order.direction == "buy":
                    self.fill_blocks["buy_suspended"] += 1
                continue
            block = _order_block_reason(self.bars, order, date, self.limit_rules)
            if block:
                if order.direction == "sell":
                    self._deferred_sells[order.code] = order
                    self.fill_blocks[f"sell_{block}"] += 1
                else:
                    self.fill_blocks[f"buy_{block}"] += 1
                continue
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
