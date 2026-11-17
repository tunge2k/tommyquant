"""向量化回测内核 + 参数网格寻优

设计原则
--------
1. 只向量化"选股日截面"这一段 (原来每天要组装 5000 个 dict、再在 Python 里循环筛)
   —— 因子矩阵只建一次, 每个选股日退化为一次 numpy 布尔掩码 + 稳定排序
2. 日频撮合 / 持仓估值仍复用 backtest_engine.BarIndex 的 searchsorted 快路径
   (持仓数 <= top_n, 这点开销可忽略), 因此 T+1 / 涨停 / 成本语义与旧引擎完全一致
3. 对外返回结构与 backtest_engine.run_backtest 完全相同, UI 可直接替换

典型收益: 5000 只股票 × 5 年日线, 单次回测从几十秒降到亚秒级;
参数网格可在此基础上多进程并行。
"""
import os
import json
import shutil
import hashlib
import itertools
import random
import numpy as np
import pandas as pd

import backtest_engine as be


# 因子矩阵磁盘缓存: 每个列一个 .npy, 加载时 mmap, 进程启动无需重算
MATRIX_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "cache", "factor_matrix")
MATRIX_VER = "m1"
_FP_CACHE = {}


# 选股截面需要的行情列
SEL_COLS = [
    "open", "close", "amount", "limit_up",
    "ret_5", "ret_20", "ret_60",
    "cap_yi", "turnover_pct",
    "above_ma20", "above_ma60",
    "volume", "vol_ma5", "vol_expand",
]

# 需要财务上下文的策略
FUNDA_FIELDS = ["profit_yoy", "revenue_yoy", "pe_ttm", "roe", "debt_ratio", "goodwill_ratio"]


def _est_pe(close, eps, report_date):
    """与 backtest_engine._est_pe_pb 的 PE 分支保持一致 (TTM 折算)"""
    if not eps or not close or close <= 0:
        return None
    frac = {"0331": 0.25, "0630": 0.5, "0930": 0.75, "1231": 1.0}.get(
        str(report_date or "")[4:8], 1.0)
    eps_ttm = eps / frac
    return close / eps_ttm if eps_ttm > 0 else None


class SelectionMatrix:
    """选股日因子矩阵

    m[col] 形状 (S, N): S = 选股日数, N = 标的数
    has     形状 (S, N) bool: 该标的当日是否有行情
    """

    def __init__(self, feat: dict, sel_dates, with_funda: bool = False,
                 cols=None):
        self.codes = np.array(list(feat.keys()), dtype=object)
        self.sel_dates = [pd.Timestamp(d) for d in sorted(sel_dates)]
        cols = list(cols) if cols else list(SEL_COLS)
        S, N = len(self.sel_dates), len(self.codes)
        self.m = {c: np.full((S, N), np.nan, dtype=np.float32) for c in cols}
        self.has = np.zeros((S, N), dtype=bool)
        if S == 0 or N == 0:
            return

        seld = np.array(self.sel_dates, dtype="datetime64[ns]")

        for j, code in enumerate(self.codes):
            df = feat[code]
            d = df["date"].to_numpy(dtype="datetime64[ns]")
            if len(d) == 0:
                continue
            idx = np.searchsorted(d, seld)
            ok = idx < len(d)
            idxc = np.clip(idx, 0, len(d) - 1)
            hit = ok & (d[idxc] == seld)
            if not hit.any():
                continue
            si = np.flatnonzero(hit)
            src = idxc[hit]
            self.has[si, j] = True
            for c in cols:
                if c in df.columns:
                    self.m[c][si, j] = df[c].to_numpy()[src].astype(np.float32)

        # 财务上下文 (只有小市值轮动用到)
        self.funda = {}
        self.med_pe = np.full((S, N), np.nan, dtype=np.float32)
        self.med_roe = np.full((S, N), np.nan, dtype=np.float32)
        if with_funda:
            for f in FUNDA_FIELDS:
                self.funda[f] = np.full((S, N), np.nan, dtype=np.float32)
            close_arr = self.m["close"]
            for si, d in enumerate(self.sel_dates):
                ctx = be.fundamental_asof(str(d)[:10])
                mp = ctx["map"]
                med = ctx["median"]
                if not mp:
                    continue
                for j, code in enumerate(self.codes):
                    r = mp.get(code)
                    if not r:
                        continue
                    for f in FUNDA_FIELDS:
                        v = r.get(f)
                        if v is not None:
                            self.funda[f][si, j] = v
                    pe = r.get("pe_ttm")
                    if not pe:
                        pe = _est_pe(close_arr[si, j], r.get("eps"), r.get("report_date"))
                        if pe is not None:
                            self.funda["pe_ttm"][si, j] = pe
                    ind = r.get("industry") or "其他"
                    mpe, mroe = med.get(ind, (None, None))
                    if mpe:
                        self.med_pe[si, j] = mpe
                    if mroe is not None:
                        self.med_roe[si, j] = mroe


def _topk(codes, score, mask, top_n: int) -> list:
    """掩码过滤 + 稳定降序取前 top_n"""
    if top_n <= 0:
        return []
    idx = np.flatnonzero(mask)
    if idx.size == 0:
        return []
    s = score[idx]
    order = np.argsort(-s, kind="stable")
    return codes[idx[order[:top_n]]].tolist()


# ==================== 向量化选股 (与函数式策略语义对齐) ====================

def _sel_momentum(M: SelectionMatrix, si: int, p: dict) -> list:
    ret20 = M.m["ret_20"][si]
    cap = M.m["cap_yi"][si]
    turn = M.m["turnover_pct"][si]
    mask = M.has[si] & np.isfinite(ret20) & (ret20 >= p.get("ret_20_min", 5.0))
    mask &= np.isfinite(cap) & (cap > 0) & (cap <= p.get("cap_max", 200.0))
    # 原实现: NaN 换手率不会被剔除
    mask &= ~(turn < p.get("turn_min", 1.0)) & ~(turn > p.get("turn_max", 15.0))
    if p.get("require_ma20", True):
        mask &= (M.m["above_ma20"][si] == 1)
    return _topk(M.codes, np.nan_to_num(ret20, nan=-1e9), mask, p.get("top_n", 5))


def _sel_chip(M: SelectionMatrix, si: int, p: dict) -> list:
    cap = M.m["cap_yi"][si]
    turn = M.m["turnover_pct"][si]
    mask = M.has[si] & np.isfinite(cap) & (cap > 0) & (cap <= p.get("cap_max", 100.0))
    mask &= ~(turn < p.get("turn_min", 3.0))
    score = 100.0 - np.nan_to_num(cap, nan=1e9) + np.nan_to_num(turn, nan=0.0) * 5.0
    return _topk(M.codes, score, mask, p.get("top_n", 5))


def _sel_breakout(M: SelectionMatrix, si: int, p: dict) -> list:
    ret5 = M.m["ret_5"][si]
    cap = M.m["cap_yi"][si]
    mask = M.has[si] & np.isfinite(ret5) & (ret5 >= p.get("ret_5_min", 3.0))
    if p.get("vol_expand", True):
        mask &= (M.m["vol_expand"][si] == 1)
    mask &= np.isfinite(cap) & (cap > 0) & (cap <= p.get("cap_max", 150.0))
    return _topk(M.codes, np.nan_to_num(ret5, nan=-1e9), mask, p.get("top_n", 5))


def _sel_smallcap_rotation(M: SelectionMatrix, si: int, p: dict) -> list:
    cap = M.m["cap_yi"][si]
    ret20 = M.m["ret_20"][si]
    ret60 = M.m["ret_60"][si]
    turn = M.m["turnover_pct"][si]
    vol = M.m["volume"][si]
    volma = M.m["vol_ma5"][si]
    cap_max = p.get("cap_max", 30.0)

    mask = M.has[si] & np.isfinite(cap) & (cap > 0) & (cap <= cap_max)
    mask &= np.isfinite(ret20) & (ret20 >= p.get("ret_20_min", 5.0))
    r60min = p.get("ret_60_min", 0.0)
    if r60min:
        mask &= np.isfinite(ret60) & (ret60 >= r60min)
    if p.get("require_ma20", True):
        mask &= (M.m["above_ma20"][si] == 1)
    if p.get("require_ma60", True):
        mask &= (M.m["above_ma60"][si] == 1)
    mask &= ~(turn < p.get("turn_min", 3.0)) & ~(turn > p.get("turn_max", 20.0))
    # 缩量过滤: 仅当 vol_ma5>0 且 volume>0 时生效
    vr_min = p.get("volume_ratio_min", 0.8)
    with np.errstate(invalid="ignore", divide="ignore"):
        ratio = vol / volma
    shrink = np.isfinite(volma) & (volma > 0) & np.isfinite(vol) & (vol > 0) & (ratio < vr_min)
    mask &= ~shrink

    # 财务过滤 (有数据时生效)
    has_f = None
    if M.funda:
        py = M.funda["profit_yoy"][si]
        ry = M.funda["revenue_yoy"][si]
        pe = M.funda["pe_ttm"][si]
        roe = M.funda["roe"][si]
        debt = M.funda["debt_ratio"][si]
        gwr = M.funda["goodwill_ratio"][si]
        mpe = M.med_pe[si]
        mroe = M.med_roe[si]
        has_f = np.isfinite(py) | np.isfinite(ry) | np.isfinite(pe) | np.isfinite(roe)

        mask &= ~(np.isfinite(py) & (py < p.get("profit_yoy_min", 20.0)))
        mask &= ~(np.isfinite(ry) & (ry < p.get("revenue_yoy_min", 10.0)))
        mask &= ~(np.isfinite(pe) & ((pe <= 0) | (np.isfinite(mpe) & (pe > mpe))))
        mask &= ~(np.isfinite(debt) & (debt > p.get("max_debt_ratio", 70.0)))
        mask &= ~(np.isfinite(gwr) & (gwr > p.get("max_goodwill_ratio", 30.0)))
        mask &= ~(np.isfinite(roe) & np.isfinite(mroe) & (roe < mroe))

    score = (np.nan_to_num(ret20, nan=-1e9) * 0.35
             + np.nan_to_num(ret60, nan=0.0) * 0.15
             + (cap_max - np.nan_to_num(cap, nan=cap_max)) / cap_max * 20.0 * 0.10
             + np.nan_to_num(turn, nan=0.0) * 0.10)
    if has_f is not None:
        py0 = np.nan_to_num(M.funda["profit_yoy"][si], nan=0.0)
        roe0 = np.nan_to_num(M.funda["roe"][si], nan=0.0)
        pe0 = np.nan_to_num(M.funda["pe_ttm"][si], nan=0.0)
        score = score + np.clip(py0, None, 200.0) * 0.15 + np.clip(roe0, None, 30.0) * 0.10
        pos_pe = pe0 > 0
        score = score + np.where(pos_pe, (50.0 / np.where(pos_pe, pe0, 1.0)) * 5.0, 0.0)
    return _topk(M.codes, score, mask, p.get("top_n", 5))


def _sel_random(M: SelectionMatrix, si: int, p: dict) -> list:
    codes = [str(c) for c in M.codes[M.has[si]]]
    rng = random.Random(p.get("seed", 42))
    k = p.get("top_n", 5)
    return rng.sample(codes, min(k, len(codes)))


_VEC_SELECTORS = {
    be.select_momentum: (_sel_momentum, False,
                         ("ret_20", "cap_yi", "turnover_pct", "above_ma20")),
    be.select_chip: (_sel_chip, False, ("cap_yi", "turnover_pct")),
    be.select_breakout: (_sel_breakout, False, ("ret_5", "vol_expand", "cap_yi")),
    be.select_random: (_sel_random, False, ()),
    be.select_smallcap_rotation: (_sel_smallcap_rotation, True,
                                  ("close", "cap_yi", "ret_20", "ret_60", "turnover_pct",
                                   "volume", "vol_ma5", "above_ma20", "above_ma60")),
}


def _resolve(select_fn):
    """函数 -> (向量化选股, 是否需要财务上下文, 所需行情列)"""
    return _VEC_SELECTORS.get(select_fn, (None, False, ()))


# ==================== 回测主循环 ====================

class FastBacktest:
    """与 EventDrivenBacktest 等价的快速实现

    差异仅在于"选股"这一步走 numpy 矩阵; 撮合、成本、持仓、绩效全部复用原实现。
    """

    def __init__(self, feat: dict, M: SelectionMatrix, sel_vec, params: dict,
                 initial_capital: float = 1_000_000, top_n: int = 5,
                 hold_days: int = 5, limit_up_pct: float = 9.5,
                 start_date=None, end_date=None, cost_params: dict = None):
        self.feat = feat
        self.M = M
        self.sel_vec = sel_vec
        self.params = dict(params or {})
        self.top_n = top_n
        self.hold_days = hold_days
        self.initial_capital = initial_capital
        self.bars = be.build_date_index(feat)
        self._date_idx = {pd.Timestamp(d): i for i, d in enumerate(self.bars.all_dates)}
        self._sel_pos = {d: i for i, d in enumerate(M.sel_dates)}
        self.cost_model = be.CostModel(**(cost_params or {}))
        self.portfolio = be.Portfolio(initial_capital)
        self._pending = []
        self.curve_rows = []
        self.holdings_log = []
        self.trade_dates = set()
        self.lu_dates = set()

        dts = self.bars.all_dates
        if start_date:
            dts = dts[dts >= pd.Timestamp(start_date)]
        if end_date:
            dts = dts[dts <= pd.Timestamp(end_date)]
        self.dates = dts
        date_set = set(pd.Timestamp(d) for d in dts.tolist())
        self.sel_dates = [d for d in M.sel_dates if d in date_set]

    def _select(self, si: int) -> list:
        if int(self.M.has[si].sum()) < self.top_n:
            return []
        try:
            return self.sel_vec(self.M, si, self.params) or []
        except Exception:
            return []

    def _execute_orders(self, date):
        if not self._pending:
            return
        executed = False
        for order in self._pending:
            row = self.bars.row(order.code, date)
            if row is None:
                continue
            ref_px = row.get("open", row["close"])
            if not ref_px or ref_px <= 0:
                continue
            day_amount = row.get("amount", 0.0) or 0.0
            fp, comm, stamp, trans, slip, impact = self.cost_model.calc_fill(
                order.code, order.direction, order.quantity, ref_px, day_amount)
            fill = be.FillEvent(date, order.code, order.direction, order.quantity,
                                fp, comm, stamp, trans, slip, impact,
                                reason=getattr(order, "reason", "rebalance"))
            self.portfolio.apply_fill(fill)
            executed = True
        if executed:
            self.trade_dates.add(date)
        self._pending = []

    def _check_exits(self, date):
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
            if held >= self.hold_days:
                o = be.OrderEvent(date, code, "sell", pos.quantity)
                o.reason = "hold_expire"
                self._pending.append(o)
            elif row.get("limit_up", 0) == 1 and held >= 1:
                o = be.OrderEvent(date, code, "sell", pos.quantity)
                o.reason = "limit_up"
                self.lu_dates.add(date)
                self._pending.append(o)

    def run(self) -> dict:
        sel_set = set(self.sel_dates)
        for current_date in self.dates:
            ts = pd.Timestamp(current_date)
            self._execute_orders(ts)

            buys = []
            si = self._sel_pos.get(ts)
            if si is not None and ts in sel_set:
                buys = self._select(si)

            self._check_exits(ts)

            if buys:
                for code in list(self.portfolio.positions.keys()):
                    if code not in buys:
                        self._pending.append(be.OrderEvent(
                            ts, code, "sell", self.portfolio.positions[code].quantity))
                per = self.portfolio.cash / self.top_n
                for code in buys:
                    if code in self.portfolio.positions:
                        continue
                    row = self.bars.row(code, ts)
                    if row is None:
                        continue
                    ref_px = row.get("open", row["close"])
                    if not ref_px or ref_px <= 0:
                        continue
                    qty = max(int(per / ref_px / 100) * 100, 100)
                    self._pending.append(be.OrderEvent(ts, code, "buy", qty))

            pv = self.portfolio.total_value(ts, self.bars)
            self.curve_rows.append({
                "date": ts, "value": pv,
                "cash": self.portfolio.cash,
                "holdings": self.portfolio.holdings_count,
            })
            # 每日持仓快照
            if self.portfolio.positions:
                for code, pos in self.portfolio.positions.items():
                    row = self.bars.row(code, ts)
                    if row is None:
                        row = self.bars.last_row(code, ts)
                    px = row["close"] if row is not None else pos.entry_price
                    mv = px * pos.quantity
                    self.holdings_log.append({
                        "date": ts,
                        "code": code,
                        "qty": pos.quantity,
                        "cost_price": round(pos.entry_price, 3),
                        "close": round(px, 3),
                        "market_value": round(mv, 2),
                        "pnl": round((px - pos.entry_price) * pos.quantity, 2),
                    })

        if self.curve_rows:
            last = self.curve_rows[-1]
            last["value"] = self.portfolio.total_value(last["date"], self.bars)

        curve_df = pd.DataFrame(self.curve_rows)
        perf = be.calc_performance(curve_df, self.portfolio.trades, self.initial_capital)
        perf["cost_summary"] = dict(self.portfolio.cost_summary)
        return {
            "curve": curve_df,
            "trades": self.portfolio.trades,
            "fills": self.portfolio.fills,
            "holdings_log": self.holdings_log,
            "performance": perf,
            "selection_dates": sorted(self.sel_dates),
            "trade_dates": sorted(self.trade_dates),
            "lu_dates": sorted(self.lu_dates),
            "all_trade_days": [pd.Timestamp(d) for d in self.dates.tolist()],
        }


# ==================== 对外入口 ====================

def build_matrix(feat: dict, select_fn, start_date=None, end_date=None) -> SelectionMatrix:
    """构建(或复用)选股日因子矩阵

    三级命中: 内存缓存 → 磁盘 .npy (mmap) → 现算并落盘
    """
    _, need_f, cols = _resolve(select_fn)
    sel_dates = be.get_selection_dates(feat)
    key = (id(feat), need_f, cols, str(start_date), str(end_date))
    hit = _MATRIX_CACHE.get(key)
    if hit is not None and hit[0] is feat:
        return hit[1]

    dkey = _disk_key(need_f, cols, sel_dates)
    M = _load_matrix(dkey) if dkey else None
    if M is None:
        M = SelectionMatrix(feat, sel_dates, with_funda=need_f, cols=cols)
        if dkey:
            _save_matrix(M, dkey)

    if len(_MATRIX_CACHE) >= 3:
        _MATRIX_CACHE.pop(next(iter(_MATRIX_CACHE)))
    _MATRIX_CACHE[key] = (feat, M)
    return M


def _data_fp() -> str:
    """数据指纹 (决定矩阵是否过期); 取不到就禁用磁盘缓存"""
    if "fp" in _FP_CACHE:
        return _FP_CACHE["fp"]
    try:
        import data_store
        fp = str(data_store.data_fingerprint())
    except Exception:
        fp = ""
    _FP_CACHE["fp"] = fp
    return fp


def _disk_key(need_f, cols, sel_dates):
    fp = _data_fp()
    if not fp:
        return None
    raw = f"{MATRIX_VER}|{fp}|{need_f}|{','.join(cols)}|{len(sel_dates)}"
    return hashlib.sha1(raw.encode()).hexdigest()[:20]


def _load_matrix(dkey: str):
    d = os.path.join(MATRIX_DIR, dkey)
    meta_path = os.path.join(d, "meta.json")
    if not os.path.exists(meta_path):
        return None
    try:
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
        M = SelectionMatrix.__new__(SelectionMatrix)
        M.codes = np.array(meta["codes"], dtype=object)
        M.sel_dates = [pd.Timestamp(x) for x in meta["sel_dates"]]
        M.has = np.load(os.path.join(d, "has.npy"), mmap_mode="r")
        M.m = {c: np.load(os.path.join(d, c + ".npy"), mmap_mode="r")
               for c in meta["cols"]}
        M.funda = {c: np.load(os.path.join(d, "f_" + c + ".npy"), mmap_mode="r")
                   for c in meta.get("funda", [])}
        S, N = len(M.sel_dates), len(M.codes)
        if meta.get("funda"):
            M.med_pe = np.load(os.path.join(d, "med_pe.npy"), mmap_mode="r")
            M.med_roe = np.load(os.path.join(d, "med_roe.npy"), mmap_mode="r")
        else:
            M.med_pe = np.full((S, N), np.nan, dtype=np.float32)
            M.med_roe = np.full((S, N), np.nan, dtype=np.float32)
        return M
    except Exception:
        return None


def _save_matrix(M: SelectionMatrix, dkey: str):
    """原子写盘 + 只保留最近 8 份"""
    try:
        os.makedirs(MATRIX_DIR, exist_ok=True)
        d = os.path.join(MATRIX_DIR, dkey)
        tmp = d + ".tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp, exist_ok=True)
        np.save(os.path.join(tmp, "has.npy"), M.has)
        for c, a in M.m.items():
            np.save(os.path.join(tmp, c + ".npy"), a)
        for c, a in (M.funda or {}).items():
            np.save(os.path.join(tmp, "f_" + c + ".npy"), a)
        if M.funda:
            np.save(os.path.join(tmp, "med_pe.npy"), M.med_pe)
            np.save(os.path.join(tmp, "med_roe.npy"), M.med_roe)
        meta = {
            "ver": MATRIX_VER,
            "codes": [str(c) for c in M.codes],
            "sel_dates": [str(d) for d in M.sel_dates],
            "cols": list(M.m.keys()),
            "funda": list((M.funda or {}).keys()),
        }
        with open(os.path.join(tmp, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)
        shutil.rmtree(d, ignore_errors=True)
        os.replace(tmp, d)
        _prune_disk()
    except Exception:
        pass


def _prune_disk(keep: int = 8):
    try:
        dirs = [os.path.join(MATRIX_DIR, x) for x in os.listdir(MATRIX_DIR)
                if os.path.isdir(os.path.join(MATRIX_DIR, x))
                and not x.endswith(".tmp")]
        if len(dirs) <= keep:
            return
        dirs.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        for p in dirs[keep:]:
            shutil.rmtree(p, ignore_errors=True)
    except Exception:
        pass


_MATRIX_CACHE = {}


def supports_fast(select_fn) -> bool:
    return _resolve(select_fn)[0] is not None

def run_fast(feat: dict, select_fn, strategy_params: dict = None,
             initial_capital: float = 1_000_000, top_n: int = 5,
             hold_days: int = 5, limit_up_pct: float = 9.5,
             start_date=None, end_date=None, M: SelectionMatrix = None,
             cost_params: dict = None) -> dict:
    """快速回测入口 (返回结构与 run_backtest 一致)"""
    sel_vec, _, _ = _resolve(select_fn)
    if sel_vec is None:
        raise ValueError(f"策略未注册向量化实现: {getattr(select_fn, '__name__', select_fn)}")
    params = dict(strategy_params or {})
    if M is None:
        M = build_matrix(feat, select_fn)
    bt = FastBacktest(feat, M, sel_vec, params,
                      initial_capital=initial_capital, top_n=top_n,
                      hold_days=hold_days, limit_up_pct=limit_up_pct,
                      start_date=start_date, end_date=end_date,
                      cost_params=cost_params)
    return bt.run()


# ==================== 参数网格寻优 ====================

SWEEP_METRICS = ["total_return", "cagr", "max_drawdown", "sharpe",
                 "sortino", "calmar", "win_rate", "profit_factor", "trades_count"]


def expand_grid(grid: dict) -> list[dict]:
    """{'hold_days': [3,5,7], 'top_n': [3,5]} -> 所有笛卡尔组合"""
    keys = list(grid.keys())
    vals = []
    for k in keys:
        v = grid[k]
        vals.append(list(v) if isinstance(v, (list, tuple, set)) else [v])
    return [dict(zip(keys, c)) for c in itertools.product(*vals)]


_SWEEP_CTX = {}


def _sweep_worker(params: dict):
    ctx = _SWEEP_CTX
    p = dict(ctx["fixed"])
    p.update(params)
    try:
        r = run_fast(ctx["feat"], ctx["select_fn"], p,
                     initial_capital=ctx["initial_capital"],
                     top_n=p.get("top_n", ctx["top_n"]),
                     hold_days=p.get("hold_days", ctx["hold_days"]),
                     limit_up_pct=ctx["limit_up_pct"],
                     start_date=ctx["start_date"], end_date=ctx["end_date"],
                     M=ctx["M"])
        perf = r["performance"]
        row = {"params": p}
        for k in SWEEP_METRICS:
            row[k] = perf.get(k)
        return row
    except Exception as e:
        row = {"params": p}
        for k in SWEEP_METRICS:
            row[k] = None
        row["error"] = str(e)
        return row


def run_sweep(feat: dict, select_fn, grid: dict, fixed: dict = None,
              metric: str = "calmar", top: int = 20, workers: int = None,
              initial_capital: float = 1_000_000, top_n: int = 5,
              hold_days: int = 5, limit_up_pct: float = 9.5,
              start_date=None, end_date=None, max_combos: int = 2000) -> dict:
    """参数网格寻优

    Returns: {"results": [Top-N 按 metric 降序], "total": 组合数,
              "workers": 实际进程数, "metric": metric}
    """
    combos = expand_grid(grid or {})
    if max_combos and len(combos) > max_combos:
        combos = combos[:max_combos]

    M = build_matrix(feat, select_fn)

    # 需要财务的策略在小市值轮动上矩阵较重, 进程数别开太高
    if workers is None:
        workers = min(os.cpu_count() or 1, 8)

    _SWEEP_CTX.clear()
    _SWEEP_CTX.update({
        "feat": feat, "select_fn": select_fn, "M": M,
        "fixed": dict(fixed or {}),
        "initial_capital": initial_capital, "top_n": top_n,
        "hold_days": hold_days, "limit_up_pct": limit_up_pct,
        "start_date": start_date, "end_date": end_date,
    })

    results = []
    if workers > 1 and len(combos) > 1 and os.name != "nt":
        import multiprocessing as mp
        try:
            ctx = mp.get_context("fork")
            with ctx.Pool(processes=min(workers, len(combos))) as pool:
                results = pool.map(_sweep_worker, combos, chunksize=1)
        except Exception:
            results = [_sweep_worker(c) for c in combos]
    else:
        results = [_sweep_worker(c) for c in combos]

    def _key(r):
        v = r.get(metric)
        return (-1e18 if v is None or (isinstance(v, float) and np.isnan(v)) else -abs(v)
                if metric == "max_drawdown" else -v if v is not None else -1e18)

    if metric == "max_drawdown":
        results.sort(key=lambda r: (r.get(metric) is None, -(r.get(metric) or -1e9)))
    else:
        results.sort(key=lambda r: (r.get(metric) is None, -(r.get(metric) or -1e18)))

    return {
        "results": results[:top],
        "all": results,
        "total": len(combos),
        "metric": metric,
        "workers": workers if (workers > 1 and os.name != "nt") else 1,
    }
