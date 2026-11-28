"""ETF 调仓回测引擎（独立实现，不依赖也不修改现有事件驱动引擎）

策略口径（用户已确认，不含任何止损）:
  1. 标的池: 仅「外盘 ETF」——`price_limits.is_etf(code)` 且 `is_overseas_etf(名称)` 为真；
     名称缺失时用 code 兜底放行。港股/中概由 `is_overseas_etf` 内部剔除。
  2. 仓位: 初始建仓买入总资产的 `weight_fraction`(默认 75%)，等权分给 `top_n` 只。
  3. 4% 偏差调仓: 非周期、逐日检查。单标的持仓市值相对「市值中枢」的偏离
     > `deviation_threshold`(默认 0.04) 时，调回中枢市值。
     中枢 = 最近一次建仓/换股/定期再平衡确定的目标市值（建仓时默认
     100 万 × 75% / 1 = 75 万）；只在定期再平衡时重置，不会每日变动。
     注意: 偏离按「市值/中枢-1」计算，不是权重百分点——75% 仓位下价格涨 23.9%
     权重只变动约 3.8 个百分点，若按权重偏离 4% 判定会长期空转。
     这是「低买高卖」触发: 涨过 +4% 卖出超出部分、跌破 -4% 用现金补足。
  4. 定期换股: 距上次换股满 `rank_check_freq`（1m/3m/6m/1y/off，按实际持有
     时间计算，不按自然年月）时按排名指标重排全池，只保留前 `top_n`：
     卖出非目标标的，买入新目标标的（沿用当前市值中枢）。
  5. 定期再平衡: 周期触发（距上次满 `rebalance_period`），按「占总资产比例」
     判定: 目标市值 = 当前总资产 × `weight_fraction` / 持仓数。总资产增长而持仓
     占比不足 → 买入补足；超配 → 卖出。完成后重置 4% 网格的市值中枢。
     与第 3 条严格区分: 本规则是周期+占比口径，第 3 条是每日+固定中枢口径。
  6. 无止损: 不实现任何 MA30/回撤/动量止损清仓逻辑，全部卖出只来源于换股或再平衡。
  7. 指标回退: 所选指标 `available=False` 或历史覆盖率 < 60% 时，自动回退到 momentum，
     并在结果的 meta 里如实写明 `rank_metric_effective` / `rank_metric_fallback_reason`。
     能由 bars 推导的指标（规模=cap_yi、成交额、波动率、回撤）自动计算；
     拿不到的（PE/PB/百分位/稀疏的溢价率历史）自动回退，绝不外推假数据。

撮合口径: 逐日循环、**当日收盘价**撮合（低频调仓策略可接受，比 T+1 更简单）。
买入按 100 股整手取整，卖出可全部卖出；现金不足时按可用现金等比缩减。

与 `run_backtest` 返回结构保持一致，便于现有 Streamlit 报告页直接渲染。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import price_limits
from backtest_engine import (
    CostModel,
    _etf_name_map,
    calc_performance,
    is_overseas_etf,
)

DEFAULT_METRIC = "momentum"

# 排序指标注册表: key -> {label(中文), higher_better(原值越大越好), available, note}
METRIC_SPECS: dict = {
    "momentum": {
        "label": "综合动量(20/120/250日)",
        "higher_better": True,
        "available": True,
        "note": "",
    },
    "mom_20": {"label": "20日动量", "higher_better": True, "available": True, "note": ""},
    "mom_60": {"label": "60日动量", "higher_better": True, "available": True, "note": ""},
    "mom_120": {"label": "120日动量", "higher_better": True, "available": True, "note": ""},
    "mom_250": {"label": "250日动量", "higher_better": True, "available": True, "note": ""},
    "amount_20": {
        "label": "20日均成交额",
        "higher_better": True,
        "available": True,
        "note": "",
    },
    "scale": {
        "label": "规模(ETF规模)",
        "higher_better": True,
        "available": False,
        "note": "ETF 日线无份额历史(outstanding_share 缺失), 仅近期才有规模数据",
    },
    "max_dd_250": {
        "label": "近250日回撤(浅者优先)",
        "higher_better": True,
        "available": True,
        "note": "值为负, 越大越接近 0 (回撤越浅)",
    },
    "vol_60": {
        "label": "60日波动率(低者优先)",
        "higher_better": False,
        "available": True,
        "note": "pct 的 60 日滚动标准差, 排序时取负",
    },
    "premium": {
        "label": "溢价率(低者优先)",
        "higher_better": False,
        "available": True,
        "note": "来自 data_store ETF 增强因子, 历史可能稀疏; 覆盖率不足时自动回退",
    },
    "pe_ttm": {
        "label": "市盈率TTM",
        "higher_better": False,
        "available": False,
        "note": "ETF 无 PE/PB 数据",
    },
    "pb": {
        "label": "市净率",
        "higher_better": False,
        "available": False,
        "note": "ETF 无 PE/PB 数据",
    },
    "pe_pct": {
        "label": "PE 历史百分位",
        "higher_better": False,
        "available": False,
        "note": "ETF 无 PE/PB 历史, 无法计算百分位",
    },
    "pb_pct": {
        "label": "PB 历史百分位",
        "higher_better": False,
        "available": False,
        "note": "ETF 无 PE/PB 历史, 无法计算百分位",
    },
}

# 直接取列的指标 -> (列名, 方向符号)
_METRIC_COLS = {
    "mom_20": ("ret_20", 1.0),
    "mom_60": ("ret_60", 1.0),
    "mom_120": ("ret_120", 1.0),
    "mom_250": ("ret_250", 1.0),
    "amount_20": ("amount_ma20", 1.0),
    "scale": ("cap_yi", 1.0),
    "max_dd_250": ("max_dd_250", 1.0),
    "vol_60": ("vol_60", -1.0),
}

# 综合动量权重 (缺失按可得权重归一化)
_MOM_WEIGHTS = (("ret_20", 0.2), ("ret_120", 0.3), ("ret_250", 0.5))

def list_metrics() -> list[tuple[str, str]]:
    """返回 [(key, 中文标签), ...]，供界面下拉框使用"""
    return [(k, v["label"]) for k, v in METRIC_SPECS.items()]


def default_params() -> dict:
    """默认参数（全部可选）"""
    return {
        "initial_capital": 1_000_000,
        "start": None,
        "end": None,
        "top_n": 3,
        "weight_fraction": 0.75,
        "deviation_threshold": 0.04,
        "rank_metric": DEFAULT_METRIC,
        "rank_check_freq": "1y",
        "rebalance_period": "1y",
        "min_amount": 0.01,     # 亿元
        "min_history": 250,     # K 线根数
        "overseas_only": True,
        "cost": None,
    }


# ==================== 数据准备 ====================

def _load_premium_map() -> dict:
    """读取溢价率因子 {date: {symbol: 值}}，兼容不同函数名；无库/无表时返回空"""
    try:
        import data_store
        fn = getattr(data_store, "etf_ext_maps", None) or getattr(
            data_store, "load_etf_factor_maps", None)
        if fn is None:
            return {}
        return (fn() or {}).get("premium") or {}
    except Exception:
        return {}


def _empty_result() -> dict:
    """空结果: 与正常返回同键, 便于 UI 统一处理"""
    return {
        "curve": pd.DataFrame({"date": pd.Series(dtype="datetime64[ns]"),
                               "value": pd.Series(dtype=float),
                               "cash": pd.Series(dtype=float),
                               "market_value": pd.Series(dtype=float)}),
        "trades": [],
        "fills": [],
        "performance": {},
        "selection_dates": [],
        "trade_dates": [],
        "lu_dates": [],
        "holdings_log": [],
        "all_trade_days": [],
        "meta": {
            "rank_metric_effective": DEFAULT_METRIC,
            "rank_metric_fallback_reason": "",
            "annual_swaps": 0,
            "deviation_trades": 0,
            "cash_short_skips": 0,
            "initial_position_ratio": 0.0,
            "years": 0.0,
            "universe_size": 0,
        },
    }


def _adjust_splits(df: pd.DataFrame) -> tuple[pd.Series, bool]:
    """识别并复权「份额拆分」造成的单日价格跳变。

    ETF 拆分日会出现一次非经济性跳变(如 4:1 拆分 → 单日 -75%)，库内 close
    若未做拆分复权，会被引擎误判为暴跌: 触发 -75% 的假"4%偏差调仓"、
    在净值曲线上留下假回撤，并使拆分后 250 天的动量因子被虚增。
    这里对单日跌幅 >60% 或涨幅 >150% 的跳变按比例前向复权(只调整跳变日之前
    的价格)，保证序列连续。ETF 是篮子指数，单日这种幅度只可能是拆分。
    返回 (复权后收盘价, 是否发生过调整)。
    """
    close = pd.to_numeric(df["close"], errors="coerce").astype(float)
    ratio = close / close.shift(1)
    split = (ratio < 0.4) | (ratio > 2.5)
    if not split.any():
        return close, False
    factor = np.ones(len(close), dtype=float)
    adj = 1.0
    for i in range(len(close) - 1, -1, -1):
        factor[i] = adj
        r = ratio.iloc[i]
        if split.iloc[i] and np.isfinite(r) and r > 0:
            adj *= float(r)
    return close * pd.Series(factor, index=close.index), True


def _prepare_symbol(code: str, name: str, df: pd.DataFrame,
                    calendar: pd.DatetimeIndex) -> dict:
    """把单只 ETF 的因子表对齐到回测交易日历, 缺失日向前填充"""
    d = pd.to_datetime(df["date"])
    raw_idx = pd.DatetimeIndex(d)

    def _align(s: pd.Series) -> pd.Series:
        s = s.copy()
        s.index = raw_idx
        return s.reindex(calendar).ffill()

    def _ser(col: str) -> pd.Series:
        if col not in df.columns:
            return pd.Series(np.nan, index=calendar, dtype=float)
        return _align(pd.to_numeric(df[col], errors="coerce"))

    close_adj, adjusted = _adjust_splits(df)
    sym = {"code": code, "name": name}
    sym["close"] = _align(close_adj)
    for col in ("amount", "amount_ma20", "cap_yi"):
        sym[col] = _ser(col)

    if adjusted:
        # 库内 pct/动量/回撤已被拆分污染, 用复权价重算全部价格类因子
        sym["pct"] = _align(close_adj.pct_change() * 100)
        for k in (20, 60, 120, 250):
            sym[f"ret_{k}"] = _align(close_adj.pct_change(k) * 100)
        roll_max = close_adj.rolling(250).max()
        sym["max_dd_250"] = _align((close_adj - roll_max) / roll_max)
    else:
        for k in (20, 60, 120, 250):
            sym[f"ret_{k}"] = _ser(f"ret_{k}")
        sym["max_dd_250"] = _ser("max_dd_250")
        sym["pct"] = (_ser("pct") if "pct" in df.columns
                      else _align(close_adj.pct_change() * 100))
    sym["vol_60"] = sym["pct"].rolling(60).std()

    # 截至每个交易日可用的 K 线根数（用于 min_history 过滤）
    counts = np.searchsorted(raw_idx.values, calendar.values, side="right")
    sym["nbar"] = pd.Series(counts, index=calendar)
    return sym


def _build_symbols(feat: dict, calendar: pd.DatetimeIndex,
                   overseas_only: bool, name_map: dict) -> list[dict]:
    """筛选标的池: 仅场内基金 + (可选)外盘 ETF"""
    symbols = []
    for code, df in feat.items():
        if df is None or len(df) == 0 or "date" not in df.columns:
            continue
        if not price_limits.is_etf(code):
            continue
        name = str(name_map.get(code) or "")
        # 名称缺失时无法判断区域, 按规格用 code 兜底放行
        if overseas_only and name and not is_overseas_etf(name):
            continue
        symbols.append(_prepare_symbol(code, name or code, df, calendar))
    symbols.sort(key=lambda s: s["code"])
    return symbols


def _resolve_metric(requested: str, symbols: list[dict],
                    calendar: pd.DatetimeIndex, premium_map: dict) -> tuple:
    """确定实际使用的排序指标，必要时回退 momentum

    返回 (effective_metric, fallback_reason)。reason 为空串表示未回退。
    """
    spec = METRIC_SPECS.get(requested)
    if spec is None:
        return DEFAULT_METRIC, f"未知排序指标 {requested}，已自动回退到 {DEFAULT_METRIC}"
    if not spec["available"]:
        return DEFAULT_METRIC, (
            f"指标 {requested} 不可用（{spec['note']}），已自动回退到 {DEFAULT_METRIC}")

    # 历史覆盖率: 抽样交易日内有多少比例能取到有效指标值
    cov = _coverage(requested, symbols, calendar, premium_map)
    if cov < 0.60:
        return DEFAULT_METRIC, (
            f"指标 {requested} 历史覆盖率 {cov:.0%} < 60%，"
            f"已自动回退到 {DEFAULT_METRIC}")
    return requested, ""


def _score(symbol: dict, d, metric: str, premium_map: dict) -> float:
    """取某标的在某日的排序分数（越大越优先）"""
    if metric == "premium":
        v = (premium_map.get(str(d)[:10]) or {}).get(symbol["code"])
        if v is None:
            return np.nan
        try:
            v = float(v)
        except (TypeError, ValueError):
            return np.nan
        return -v if np.isfinite(v) else np.nan
    if metric == "momentum":
        num = den = 0.0
        for col, w in _MOM_WEIGHTS:
            v = symbol[col].get(d, np.nan)
            if np.isfinite(v):
                num += float(v) * w
                den += w
        return num / den if den > 0 else np.nan
    col, sign = _METRIC_COLS.get(metric, (_METRIC_COLS["mom_20"][0], 1.0))
    v = symbol[col].get(d, np.nan)
    return sign * float(v) if np.isfinite(v) else np.nan


def _coverage(metric: str, symbols: list[dict], calendar: pd.DatetimeIndex,
              premium_map: dict) -> float:
    """抽样估计指标历史覆盖率"""
    if len(calendar) == 0 or not symbols:
        return 0.0
    step = max(1, len(calendar) // 60)
    total = ok = 0
    for d in calendar[::step]:
        for s in symbols:
            if s["nbar"].get(d, 0) < 1:
                continue
            total += 1
            if np.isfinite(_score(s, d, metric, premium_map)):
                ok += 1
    return ok / total if total else 0.0


# ==================== 回测引擎 ====================

class _RebalanceEngine:
    """ETF 调仓回测核心状态机（逐日循环、收盘价撮合）"""

    def __init__(self, symbols: list[dict], calendar: pd.DatetimeIndex,
                 params: dict, metric: str, premium_map: dict):
        self.symbols = symbols
        self.by_code = {s["code"]: s for s in symbols}
        self.calendar = calendar
        self.p = params
        self.metric = metric
        self.premium = premium_map

        self.initial = float(params["initial_capital"])
        self.cash = self.initial
        self.top_n = max(int(params["top_n"]), 1)
        self.weight_fraction = float(params["weight_fraction"])
        self.dev_threshold = float(params["deviation_threshold"])
        self.min_amount = float(params["min_amount"])
        self.min_history = int(params["min_history"])
        self.rank_period = _parse_period(params["rank_check_freq"])
        self.reb_period = _parse_period(params["rebalance_period"])

        cost = params.get("cost")
        if not isinstance(cost, dict):
            cost = {}   # 兼容把缓存指纹串放在 cost 上的调用方
        self.cm = CostModel(**(cost or {}))

        self.positions: dict = {}   # code -> {qty, cost, entry_date, entry_fee}
        self.targets: list = []     # 最近一次选出的目标标的
        self.entered = False
        self.entry_date = None
        self.next_reb = None
        # 换股/再平衡都按「实际持有时间」计算: 下次日期 = 上次日期 + 周期偏移
        self.next_rank = None
        self.cash_short_skips = 0     # 资金不足未能执行的 4% 补仓次数

        self.trades: list = []
        self.fills: list = []          # 成交流水 (兼容报告页字段契约)
        self.holdings_log: list = []
        self.curve_rows: list = []
        self.trade_dates: set = set()
        self.annual_swaps = 0
        self.deviation_trades = 0
        self.initial_ratio = 0.0
        # 目标持仓市值中枢 (每只标的): 初始建仓时 = 初始总资产 × 仓位比例 / 持仓数。
        # 4% 网格围绕该「市值中枢」而非「权重」触发, 见 _deviation_check。
        self.center_mv = 0.0

    # ---------- 行情辅助 ----------
    def _price(self, code, d):
        s = self.by_code.get(code)
        if s is None:
            return None
        px = s["close"].get(d, np.nan)
        if not np.isfinite(px) or px <= 0:
            return None
        return float(px)

    def _day_amount(self, code, d):
        s = self.by_code.get(code)
        if s is None:
            return 0.0
        v = s["amount"].get(d, 0.0)
        return float(v) if np.isfinite(v) else 0.0

    def _total_value(self, d) -> float:
        """账户总市值 = 现金 + 各持仓按当日收盘价估值"""
        tv = self.cash
        for code, pos in self.positions.items():
            px = self._price(code, d)
            if px is None:
                px = pos["cost"]
            tv += px * pos["qty"]
        return tv

    # ---------- 选品 ----------
    def _candidates(self, d) -> list:
        """当日候选池, 按指标从优到劣排序"""
        out = []
        floor = self.min_amount * 1e8   # 亿元 -> 元
        for s in self.symbols:
            if s["nbar"].get(d, 0) < self.min_history:
                continue
            amt = s["amount_ma20"].get(d, np.nan)
            if not np.isfinite(amt) or float(amt) < floor:
                continue
            sc = _score(s, d, self.metric, self.premium)
            if not np.isfinite(sc):
                continue
            out.append((float(sc), s["code"]))
        out.sort(key=lambda x: (-x[0], x[1]))
        return [code for _, code in out]

    # ---------- 交易执行 ----------
    def _record(self, d, code, direction, qty, price, amount, fees, reason,
                pnl=0.0, dev_before=None, entry=None, closing_fee=0.0):
        s = self.by_code.get(code)
        name = s["name"] if s else code
        ts = pd.Timestamp(d)
        trade = {
            "date": ts,
            "code": code,
            "name": name,
            "direction": direction,
            "price": round(price, 4),
            "quantity": int(qty),
            "amount": round(amount, 2),
            "reason": reason,
            "pnl": round(pnl, 2),
            # 以下键兼容 calc_performance / 现有报告页
            "buy_date": entry["entry_date"] if (entry and direction == "sell") else ts,
            "sell_date": ts if direction == "sell" else None,
            "buy_price": round(entry["cost"], 4) if (entry and direction == "sell") else round(price, 4),
            "sell_price": round(price, 4) if direction == "sell" else None,
            "buy_qty": int(qty),
            "sell_qty": int(qty) if direction == "sell" else 0,
            "shares": int(qty),
            "days": (ts - pd.Timestamp(entry["entry_date"])).days if (entry and direction == "sell") else 0,
            "cost": round(fees + closing_fee, 2),
            "dev_before": None if dev_before is None else round(float(dev_before), 4),
        }
        self.trades.append(trade)
        self.trade_dates.add(ts)
        # 成交流水 (报告页按事件驱动引擎字段渲染: date/time/code/direction/qty/
        # price/amount/cost/pnl/cash_after/reason); 此处 self.cash 已是成交后余额
        self.fills.append({
            "date": ts,
            "time": "00:00",
            "code": code,
            "direction": direction,
            "price": round(price, 4),
            "qty": int(qty),
            "amount": round(amount, 2),
            "cost": round(fees + closing_fee, 2),
            "pnl": round(pnl, 2),
            "cash_after": round(float(self.cash), 2),
            "reason": reason,
        })

    def _buy(self, code, target_mv, d, reason, dev_before=None):
        """按目标市值买入, 100 股整手, 受可用现金约束"""
        px = self._price(code, d)
        if px is None or target_mv <= 0:
            return
        qty = int(target_mv / px // 100) * 100
        if qty < 100:
            return
        # 先按含费单价粗约束, 避免大额时逐手递减
        max_qty = int(self.cash / (px * 1.002) // 100) * 100
        qty = min(qty, max_qty)
        day_amt = self._day_amount(code, d)
        while qty >= 100:
            fp, comm, stamp, trans, slip, impact = self.cm.calc_fill(
                code, "buy", qty, px, day_amt)
            amount = fp * qty
            fees = comm + stamp + trans + slip + impact
            if amount + fees <= self.cash + 1e-9:
                break
            qty -= 100
        if qty < 100:
            return

        self.cash -= amount + fees
        pos = self.positions.get(code)
        if pos is None:
            self.positions[code] = {
                "qty": qty, "cost": fp, "entry_date": pd.Timestamp(d),
                "entry_fee": fees,
            }
        else:
            new_qty = pos["qty"] + qty
            pos["cost"] = (pos["cost"] * pos["qty"] + fp * qty) / new_qty
            pos["qty"] = new_qty
            pos["entry_fee"] += fees
        self._record(d, code, "buy", qty, fp, amount, fees, reason,
                     pnl=0.0, dev_before=dev_before)

    def _sell(self, code, qty, d, reason, dev_before=None):
        """卖出指定股数; qty >= 持仓 时视为全清"""
        pos = self.positions.get(code)
        px = self._price(code, d)
        if pos is None or px is None or qty <= 0:
            return
        qty = int(min(qty, pos["qty"]))
        if qty <= 0:
            return
        fp, comm, stamp, trans, slip, impact = self.cm.calc_fill(
            code, "sell", qty, px, self._day_amount(code, d))
        amount = fp * qty
        fees = comm + stamp + trans + slip + impact
        self.cash += amount - fees

        entry = {"entry_date": pos["entry_date"], "cost": pos["cost"]}
        closing_fee = 0.0
        pos["qty"] -= qty
        if pos["qty"] <= 0:
            # 全清: 把建仓费用计入本笔成本
            closing_fee = pos["entry_fee"]
            del self.positions[code]
        pnl = (fp - entry["cost"]) * qty - fees - closing_fee
        self._record(d, code, "sell", qty, fp, amount, fees, reason,
                     pnl=pnl, dev_before=dev_before, entry=entry,
                     closing_fee=closing_fee)

    # ---------- 事件 ----------
    def _try_entry(self, d):
        """初始建仓: 区间内第一个候选池非空的交易日"""
        cands = self._candidates(d)
        if not cands:
            return
        picks = cands[:self.top_n]
        tv = self._total_value(d)
        per = tv * self.weight_fraction / len(picks)
        self.center_mv = per          # 目标市值中枢 (默认 75% × 100万 / 1 = 75万)
        for code in picks:
            self._buy(code, per, d, "建仓")
        self.entered = True
        self.entry_date = pd.Timestamp(d)
        self.targets = list(picks)
        # 周期以实际建仓日为锚点, 不再按自然年/月边界 (2026-11-28 修正)
        if self.rank_period:
            self.next_rank = self.entry_date + self.rank_period[1]
        if self.reb_period:
            self.next_reb = self.entry_date + self.reb_period[1]
        tv = self._total_value(d)
        mv = sum((self._price(c, d) or 0) * p["qty"] for c, p in self.positions.items())
        self.initial_ratio = mv / tv if tv > 0 else 0.0

    def _rank_swap(self, d):
        """定期换股: 重排全池, 只留前 top_n, 卖非目标、买新目标"""
        picks = self._candidates(d)[:self.top_n]
        if not picks:
            return
        reason = (f"{self.rank_period[0]}换股" if self.rank_period else "排名换股")
        # 先卖出非目标标的（回笼现金）
        for code in list(self.positions):
            if code not in picks:
                self._sell(code, self.positions[code]["qty"], d, reason)
        # 再买入新目标标的到目标市值中枢 (换股不改变 4% 网格的市值中枢)
        tv = self._total_value(d)
        per = self.center_mv if self.center_mv > 0 else tv * self.weight_fraction / len(picks)
        for code in picks:
            if code not in self.positions:
                self._buy(code, per, d, reason)
        self.targets = list(picks)
        self.annual_swaps += 1

    def _periodic_rebalance(self, d):
        """定期再平衡（周期触发）: 把持仓占总资产比例拉回 weight_fraction。

        与 4% 偏差调仓的严格区分（两套规则互斥，不共用判定基准）:
          本函数 —— 周期触发（每 rebalance_period 年）+ 按「占总资产比例」判定:
            目标市值 = 当前总资产 × weight_fraction / 持仓数。
            总资产增长而持仓占比不足 → **买入**补足（截图中 2017-10-19 的情形）;
            持仓占比超配 → 卖出。
          4% 调仓 —— 每日非周期触发 + 按「固定市值中枢」判定，见 _deviation_check。
        再平衡完成后把 4% 网格的市值中枢重置为新的目标市值，两类规则衔接而不冲突。
        """
        targets = self.targets or list(self.positions)
        if not targets:
            return
        reason = "定期再平衡"
        tv = self._total_value(d)
        per = tv * self.weight_fraction / len(targets)
        if per <= 0:
            return
        # 先卖出占比超配的标的（回笼现金），再买入占比不足的标的
        for code in list(self.positions):
            if code not in targets:
                continue
            px = self._price(code, d)
            if px is None:
                continue
            mv = px * self.positions[code]["qty"]
            if mv > per:
                qty = int((mv - per) / px // 100) * 100
                if qty >= 100:
                    self._sell(code, qty, d, reason)
        for code in targets:
            px = self._price(code, d)
            if px is None:
                continue
            pos = self.positions.get(code)
            mv = px * pos["qty"] if pos else 0.0
            if mv < per:
                self._buy(code, per - mv, d, reason)
        # 4% 网格中枢重置: 下一周期内以新的 75% 目标市值为基准做高抛低吸
        self.center_mv = per

    def _deviation_check(self, d):
        """4% 网格调仓（非周期，逐日触发）: 持仓市值相对「市值中枢」偏离 > 阈值时调回中枢。

        与 _periodic_rebalance 的边界:
          - 本函数: 每日检查、按「固定市值中枢」判定，用于高抛低吸; 与总资产规模无关。
          - 定期再平衡: 周期触发、按「占总资产比例」判定，用于把仓位拉回 75%。
        关键口径: 偏离 = 当前持仓市值 / 市值中枢 - 1 (相对市值, 非权重百分点)。
        例: 中枢 75 万, 标的上涨后市值 78.1 万 → 偏离 +4.1% → 卖出 3.1 万回到 75 万。
        这样单边上涨/下跌行情也能持续触发, 不会像「权重偏离」那样因 75% 仓位下
        价格涨 4% 权重只变动约 0.8 个百分点而长期空转。
        """
        base = self.center_mv
        if base <= 0:
            return
        sells, buys = [], []
        for code, pos in list(self.positions.items()):
            px = self._price(code, d)
            if px is None:
                continue
            mv = px * pos["qty"]
            dev = mv / base - 1.0
            if dev > self.dev_threshold:
                qty = int((mv - base) / px // 100) * 100
                if qty >= 100:
                    sells.append((code, qty, dev))
            elif dev < -self.dev_threshold:
                buys.append((code, base - mv, dev))
        # 先卖后买: 卖出回笼的现金可立即用于同日低配补仓
        n0 = len(self.trades)
        for code, qty, dev in sells:
            if code in self.positions:
                self._sell(code, qty, d, "4%偏差调仓", dev_before=dev)
        # 买入前做可用资金充足性校验:
        # 目标仓位 85%/95% 时现金垫很薄, 最大回撤期连续下跌会让可用现金耗尽。
        # 这里按可用现金缩减买入额(优先保证能成交), 不足 1 手则跳过并计数,
        # 不产生「挂了单却没有成交」的假调仓, 也不会出现负现金。
        avail = max(float(self.cash), 0.0)
        for code, amount, dev in buys:
            if code not in self.positions:
                continue
            px = self._price(code, d)
            if px is None:
                continue
            need = min(float(amount), avail)
            if need < px * 100 * 1.002:      # 含费后连 1 手都买不起
                self.cash_short_skips += 1
                continue
            cash0 = self.cash
            self._buy(code, need, d, "4%偏差调仓", dev_before=dev)
            avail = max(avail - (cash0 - float(self.cash)), 0.0)
        # 只统计真实成交
        self.deviation_trades += len(self.trades) - n0

    def _log_holdings(self, d):
        tv = self._total_value(d)
        for code, pos in self.positions.items():
            px = self._price(code, d) or pos["cost"]
            mv = px * pos["qty"]
            self.holdings_log.append({
                "date": pd.Timestamp(d),
                "code": code,
                "name": self.by_code[code]["name"] if code in self.by_code else code,
                "qty": pos["qty"],
                "cost_price": round(pos["cost"], 3),
                "close": round(px, 3),
                "market_value": round(mv, 2),
                "pnl": round((px - pos["cost"]) * pos["qty"], 2),
                "weight": round(mv / tv, 4) if tv > 0 else 0.0,
                "total_value": round(tv, 2),
            })

    def run(self) -> dict:
        for d in self.calendar:
            d = pd.Timestamp(d)
            if not self.entered:
                self._try_entry(d)
            else:
                # 换股: 距上次换股满一个配置周期 (按实际持有时间, 非自然年月)
                if self.next_rank is not None and d >= self.next_rank:
                    self._rank_swap(d)
                    while self.next_rank is not None and d >= self.next_rank:
                        self.next_rank = self.next_rank + self.rank_period[1]
                # 再平衡: 距上次再平衡满一个配置周期
                if self.next_reb is not None and d >= self.next_reb:
                    self._periodic_rebalance(d)
                    while self.next_reb is not None and d >= self.next_reb:
                        self.next_reb = self.next_reb + self.reb_period[1]

            self._deviation_check(d)
            self._log_holdings(d)
            _tv = float(self._total_value(d))
            self.curve_rows.append({"date": d, "value": _tv,
                                    "cash": float(self.cash),
                                    "market_value": _tv - float(self.cash)})

        curve = pd.DataFrame(self.curve_rows, columns=["date", "value", "cash", "market_value"])
        perf = calc_performance(curve, self.trades, self.initial)
        years = 0.0
        if len(curve) >= 2:
            years = (curve["date"].iloc[-1] - curve["date"].iloc[0]).days / 365.25
        return {
            "curve": curve,
            "trades": self.trades,
            "fills": self.fills,
            "performance": perf,
            "selection_dates": [],
            "trade_dates": sorted(self.trade_dates),
            "lu_dates": [],
            "holdings_log": self.holdings_log,
            "all_trade_days": [pd.Timestamp(d) for d in self.calendar],
            "meta": {
                "rank_metric_effective": self.metric,
                "rank_metric_fallback_reason": self.p.get("_fallback_reason", ""),
                "annual_swaps": self.annual_swaps,
                "deviation_trades": self.deviation_trades,
                "cash_short_skips": self.cash_short_skips,
                "initial_position_ratio": round(self.initial_ratio, 6),
                "years": round(years, 4),
                "universe_size": len(self.symbols),
            },
        }


# 周期注册表: 配置值 -> (中文标签, DateOffset 参数)。同时兼容旧设置值。
_PERIOD_SPECS = {
    "1m": ("每月", {"months": 1}),
    "3m": ("每季度", {"months": 3}),
    "6m": ("每半年", {"months": 6}),
    "1y": ("每年", {"years": 1}),
    "2y": ("每两年", {"years": 2}),
    "monthly": ("每月", {"months": 1}),
    "quarterly": ("每季度", {"months": 3}),
    "yearly": ("每年", {"years": 1}),
}


def _parse_period(period):
    """周期配置 -> (中文标签, pd.DateOffset, 折合年数)；off/None/非法 -> None

    换股与再平衡都以「实际持有时间」为锚点：下次日期 = 上次日期 + 该偏移，
    不按自然年/月边界切分（2026-11-28 修正）。
    """
    s = str(period or "").strip().lower()
    if not s or s in ("off", "none", "0"):
        return None
    spec = _PERIOD_SPECS.get(s)
    if spec is None:
        return None
    label, kw = spec
    years = float(kw.get("years", 0)) + kw.get("months", 0) / 12.0
    return label, pd.DateOffset(**kw), years


# ==================== 对外入口 ====================

def run_etf_rebalance(feat: dict, params: dict) -> dict:
    """ETF 调仓回测主入口

    参数见 `default_params()`；返回 dict 与 `run_backtest` 同构：
      curve(date,value,cash,market_value) / trades / fills / performance /
      selection_dates / trade_dates / lu_dates / holdings_log / all_trade_days / meta

    `meta` 额外报告: rank_metric_effective、rank_metric_fallback_reason、
    annual_swaps、deviation_trades、initial_position_ratio、years、universe_size。
    """
    p = default_params()
    p.update({k: v for k, v in (params or {}).items() if v is not None})
    p["top_n"] = max(int(p["top_n"]), 1)

    if not feat:
        return _empty_result()

    # 全市场交易日历（取所有 feat 日期并集，保证曲线覆盖每个交易日）
    all_dates = []
    for df in feat.values():
        if df is not None and len(df) and "date" in df.columns:
            all_dates.append(pd.to_datetime(df["date"]).values)
    if not all_dates:
        return _empty_result()
    calendar = pd.DatetimeIndex(np.unique(np.concatenate(all_dates))).sort_values()

    if p.get("start") is not None:
        calendar = calendar[calendar >= pd.Timestamp(p["start"])]
    if p.get("end") is not None:
        calendar = calendar[calendar <= pd.Timestamp(p["end"])]
    if len(calendar) < 2:
        return _empty_result()

    try:
        name_map = _etf_name_map() or {}
    except Exception:
        name_map = {}

    symbols = _build_symbols(feat, calendar, bool(p["overseas_only"]), name_map)
    if not symbols:
        return _empty_result()

    requested = str(p["rank_metric"] or DEFAULT_METRIC)
    premium_map = _load_premium_map() if requested == "premium" else {}
    metric, reason = _resolve_metric(requested, symbols, calendar, premium_map)
    p["_fallback_reason"] = reason

    engine = _RebalanceEngine(symbols, calendar, p, metric, premium_map)
    return engine.run()


def validate_result(result: dict, params: dict) -> list[dict]:
    """回测结果验收清单（不抛异常，失败项 passed=False 供界面标红）

    校验项:
      1. 回测年限 >= 10 年
      2. 初始仓位 ≈ weight_fraction（±2%）
      3. 换股次数 ≈ 区间年数 / 换股周期（按实际持有时间计算）
      4. 每笔 4% 偏差调仓的实际偏离 >= 3.5%
      5. 全程无止损类交易
    """
    p = default_params()
    p.update({k: v for k, v in (params or {}).items() if v is not None})
    meta = result.get("meta") or {}
    trades = result.get("trades") or []
    checks = []

    years = float(meta.get("years", 0.0) or 0.0)
    checks.append({
        "name": "回测年限≥10年",
        "passed": bool(years >= 10.0 - 1e-9),
        "detail": f"回测区间 {years:.2f} 年",
    })

    weight_fraction = float(p["weight_fraction"])
    ratio = float(meta.get("initial_position_ratio", 0.0) or 0.0)
    checks.append({
        "name": "初始仓位≈weight_fraction",
        "passed": bool(abs(ratio - weight_fraction) <= 0.02),
        "detail": f"初始仓位 {ratio:.2%}，目标 {weight_fraction:.2%}（容差±2%）",
    })

    swaps = int(meta.get("annual_swaps", 0) or 0)
    rp = _parse_period(p["rank_check_freq"])
    if rp is None:
        expected, tol, name = 0.0, 0.5, "换股次数（不换股）"
        detail = f"换股 {swaps} 次（配置为不换股）"
    else:
        expected = years / rp[2] if rp[2] > 0 else 0.0
        tol = max(2.5, expected * 0.25)
        name = "换股次数≈周期数"
        detail = (f"换股 {swaps} 次，区间 {years:.2f} 年，"
                  f"按「{rp[0]}」期望约 {expected:.1f} 次（容差±{tol:.1f}）")
    checks.append({
        "name": name,
        "passed": bool(abs(swaps - expected) <= tol),
        "detail": detail,
    })

    dev_trades = [t for t in trades if "4%" in str(t.get("reason", ""))]
    # 偏离取绝对值: 卖出为 +偏离, 买入为 -偏离, 两者都应在阈值附近
    bad_dev = [t for t in dev_trades
               if (t.get("dev_before") is None
                   or abs(float(t.get("dev_before"))) < 0.035 - 1e-9)]
    checks.append({
        "name": "4%偏差调仓实际偏离≥3.5%",
        "passed": len(bad_dev) == 0,
        "detail": (f"共 {len(dev_trades)} 笔 4% 偏差调仓，"
                   f"不合格 {len(bad_dev)} 笔"),
    })

    stop_trades = [t for t in trades if "止损" in str(t.get("reason", ""))]
    checks.append({
        "name": "全程无止损类交易",
        "passed": len(stop_trades) == 0,
        "detail": f"止损类交易 {len(stop_trades)} 笔",
    })
    return checks
