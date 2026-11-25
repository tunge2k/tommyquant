"""
全市场 ETF 月度动量回测（VectorBT 独立实现）

运行：py -3.13 vectorbt_etf_backtest.py --start 2016-01-01 --weighting momentum
详细规则、数据源和局限见 docs/vectorbt_etf_strategy.md。

实现边界：Pandas 在每个历史月末完成全市场筛选；VectorBT 只负责下一交易日
开盘订单撮合、共享现金、逐笔费用与绩效统计。不使用任何未来价格。
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import warnings
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import vectorbt as vbt

# ============================================================================
# 可调参数：所有策略、成本、数据源参数集中在这里
# ============================================================================
ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "stock.db"
ANNOUNCEMENT_CSV = ROOT / "data" / "etf_termination_events.csv"
OUTPUT_DIR = ROOT / "output" / "vectorbt_etf"

START_DATE = "2016-01-01"          # 回测起点；指标仍使用起点之前的历史数据
END_DATE = None                    # None = 数据库最后日期
INITIAL_CASH = 1_000_000.0
TOP_N = 5                          # 动量最强前 N；有效标的不足 N 时全取
WEIGHTING_MODE = "momentum"        # momentum=动量加权；equal=等权
TARGET_RISK_WEIGHT = 0.75          # 风险 ETF 合计目标仓位
RISK_WEIGHT_TOLERANCE = 0.04       # 整体仓位容差：71%–79%
MOMENTUM_DAYS = 60                 # 60 个交易日收益率
CROSS_BORDER_PREMIUM_LIMIT_PCT = 1.5
MIN_FUND_SCALE_YUAN = 100_000_000  # 基金规模 >= 1 亿元
MIN_LISTED_CALENDAR_DAYS = 182     # 上市满半年（自然日）
AMOUNT_MA_DAYS = 20                # 规则未给窗口，默认近 20 个交易日
MIN_AVG_DAILY_AMOUNT_YUAN = 50_000_000  # 规则未给数值，默认 5000 万元
MONEY_ETF_SYMBOL = "sh511880"      # 默认避险/剩余资金标的

COMMISSION_RATE = 0.0001           # 双边佣金率，万分之一
MIN_COMMISSION_YUAN = 0.2          # 每笔最低佣金
SLIPPAGE_RATE = 0.0005             # 单边滑点，万分之五
LOT_SIZE = 100                     # 场内 ETF 最小交易单位 100 份

REFERENCE_FFILL_TRADING_DAYS = 5   # 规模/溢价最多沿用 5 个交易日
UNKNOWN_REGION_AS_CROSS_BORDER = True  # 名称缺失时保守校验溢价，防止漏检
STRICT_REFERENCE_DATA = False      # True 时，公告数据源不存在直接终止

# 仅用于判断“是否需校验跨境溢价”，不是额外的投资范围过滤。
CROSS_BORDER_KEYWORDS = (
    "纳指", "纳斯达克", "标普", "道琼斯", "道指", "美国", "美股", "日经",
    "东证", "日本", "德国", "法国", "英国", "欧洲", "东南亚", "越南",
    "印度", "沙特", "巴西", "亚太", "新兴市场", "全球", "海外", "QDII",
    "恒生", "港股", "香港", "H股", "国企", "中概", "港科", "港股通",
)
TERMINATION_KEYWORDS = ("清盘", "终止", "合并")


@dataclass
class MarketData:
    """所有矩阵均为 index=交易日、columns=ETF 代码；字段绝不混用。"""
    raw_open: pd.DataFrame
    raw_close: pd.DataFrame
    adj_open: pd.DataFrame            # 复权市场价：仅用于收益、成交和估值
    adj_close: pd.DataFrame
    amount: pd.DataFrame              # 单日真实成交额（元）
    fund_scale: pd.DataFrame          # 真实基金规模（元），不是单位净值
    premium_pct: pd.DataFrame         # 溢价率（百分数）
    names: dict[str, str]
    first_bar: pd.Series
    termination_dates: dict[str, pd.Timestamp]
    announcement_source: str
    full_calendar: pd.DatetimeIndex


def _read_sql(conn: sqlite3.Connection, sql: str) -> pd.DataFrame:
    df = pd.read_sql_query(sql, conn)
    if "date" in df:
        df["date"] = pd.to_datetime(df["date"])
    return df


def _wide(df: pd.DataFrame, value: str, calendar: pd.DatetimeIndex,
          columns: pd.Index, ffill_limit: int | None = None) -> pd.DataFrame:
    """长表转历史横截面；只允许使用信号日或此前数据，禁止 backfill。"""
    if df.empty:
        return pd.DataFrame(np.nan, index=calendar, columns=columns, dtype=float)
    out = (df.pivot_table(index="date", columns="symbol", values=value,
                          aggfunc="last")
           .reindex(index=calendar, columns=columns).astype(float))
    if ffill_limit is not None:
        out = out.ffill(limit=ffill_limit)
    return out


def _dividend_payments(conn: sqlite3.Connection) -> dict[str, dict[pd.Timestamp, float]]:
    """累计分红转成每次实际每份分红；除息日生效，不用未来累计值倒推历史。"""
    try:
        df = _read_sql(conn, "SELECT symbol,date,cum_div FROM etf_dividend ORDER BY symbol,date")
    except (sqlite3.OperationalError, pd.errors.DatabaseError):
        return {}
    out: dict[str, dict[pd.Timestamp, float]] = {}
    for symbol, g in df.groupby("symbol", sort=False):
        prev, payments = 0.0, {}
        for row in g.itertuples():
            if not np.isfinite(row.cum_div):
                continue
            pay = float(row.cum_div) - prev
            prev = float(row.cum_div)
            if pay > 0:
                payments[pd.Timestamp(row.date)] = pay
        out[symbol] = payments
    return out


def _total_return_prices(raw_open: pd.DataFrame, raw_close: pd.DataFrame,
                         dividends: dict[str, dict[pd.Timestamp, float]]
                         ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """市场价格后复权：分红和 1 拆 N/N 合 1 不产生虚假动量或亏损。

    VectorBT 不会自动给 ETF 持仓发放现金红利，因此成交与估值统一使用复权
    市场价。这是收益口径的变通，不把单位净值当成市场价格。
    """
    adj_open = raw_open.astype(float).copy()
    adj_close = raw_close.astype(float).copy()
    for code in raw_close.columns:
        close = raw_close[code].to_numpy(dtype=float)
        open_ = raw_open[code].to_numpy(dtype=float)
        factor, factors = 1.0, np.ones(len(close), dtype=float)
        payments = dividends.get(code, {})
        dates = raw_close.index
        for i in range(1, len(close)):
            prev, cur = close[i - 1], close[i]
            if np.isfinite(prev) and prev > 0 and np.isfinite(cur) and cur > 0:
                ratio = cur / prev
                # A 股 ETF 单日真实涨跌不会达到该幅度，视为份额折算。
                if ratio > 1.25 or ratio < 0.80:
                    factor /= ratio
                pay = payments.get(dates[i], 0.0)
                if pay > 0 and prev > pay:
                    factor *= prev / (prev - pay)
            factors[i] = factor
        adj_close[code] = close * factors
        adj_open[code] = open_ * factors
    return adj_open, adj_close


def load_market_data(db_path: Path, announcement_csv: Path) -> MarketData:
    """加载全市场 ETF 完整历史，而非在每月重新下载或读取当前快照。"""
    conn = sqlite3.connect(str(db_path))
    try:
        bars = _read_sql(conn, """
            SELECT symbol,date,open,close,amount FROM daily_bar
            WHERE substr(symbol,1,3)='sh5' OR substr(symbol,1,4)='sz159'
            ORDER BY date,symbol
        """)
        names_df = pd.read_sql_query("SELECT symbol,name FROM etf_name", conn)
        scale_df = _read_sql(conn, "SELECT symbol,date,market_scale FROM etf_capital")
        premium_df = _read_sql(conn, "SELECT symbol,date,premium_ratio FROM etf_quote_ext")
        dividends = _dividend_payments(conn)
        try:
            event_df = _read_sql(
                conn, "SELECT symbol,ann_date AS date,event_type FROM etf_termination_event")
            announcement_source = "sqlite:etf_termination_event"
        except (sqlite3.OperationalError, pd.errors.DatabaseError):
            event_df = pd.DataFrame(columns=["symbol", "date", "event_type"])
            announcement_source = ""
    finally:
        conn.close()

    if bars.empty:
        raise ValueError(f"{db_path} 没有 ETF 日线数据")
    calendar = pd.DatetimeIndex(sorted(bars["date"].unique()))
    raw_close = bars.pivot_table(index="date", columns="symbol", values="close",
                                 aggfunc="last").reindex(calendar).astype(float)
    raw_open = bars.pivot_table(index="date", columns="symbol", values="open",
                                aggfunc="last").reindex(calendar, columns=raw_close.columns).astype(float)
    amount = bars.pivot_table(index="date", columns="symbol", values="amount",
                              aggfunc="last").reindex(calendar, columns=raw_close.columns).astype(float)
    if MONEY_ETF_SYMBOL not in raw_close.columns:
        raise ValueError(f"默认货币 ETF {MONEY_ETF_SYMBOL} 不在 daily_bar 中")

    names = {r.symbol: str(r.name or "") for r in names_df.itertuples()}
    fund_scale = _wide(scale_df, "market_scale", calendar, raw_close.columns,
                       REFERENCE_FFILL_TRADING_DAYS)
    premium_pct = _wide(premium_df, "premium_ratio", calendar, raw_close.columns,
                        REFERENCE_FFILL_TRADING_DAYS)
    adj_open, adj_close = _total_return_prices(raw_open, raw_close, dividends)
    first_bar = pd.Series({c: raw_close[c].first_valid_index() for c in raw_close.columns})

    if event_df.empty and announcement_csv.exists():
        event_df = pd.read_csv(announcement_csv)
        event_df["date"] = pd.to_datetime(event_df["ann_date"])
        announcement_source = f"csv:{announcement_csv}"
    termination_dates: dict[str, pd.Timestamp] = {}
    for row in event_df.itertuples():
        text = f"{getattr(row, 'event_type', '')}"
        if any(k in text for k in TERMINATION_KEYWORDS):
            symbol = str(row.symbol)
            d = pd.Timestamp(row.date)
            if symbol not in termination_dates or d < termination_dates[symbol]:
                termination_dates[symbol] = d

    return MarketData(raw_open, raw_close, adj_open, adj_close, amount,
                      fund_scale, premium_pct, names, first_bar, termination_dates,
                      announcement_source, calendar)


def is_cross_border(code: str, data: MarketData) -> bool:
    """名称仅用于溢价规则；未知名称按跨境保守校验，不借此缩小普通 ETF 池。"""
    name = data.names.get(code, "")
    if not name:
        return UNKNOWN_REGION_AS_CROSS_BORDER
    return any(k.lower() in name.lower() for k in CROSS_BORDER_KEYWORDS)


def screen_universe(signal_date: pd.Timestamp, data: MarketData,
                    momentum: pd.DataFrame, amount_ma: pd.DataFrame,
                    top_n: int, weighting_mode: str) -> dict:
    """在某个月末完成规则规定的顺序过滤、排序和权重计算。"""
    # 尚未有首个行情的标的不进入任何筛选步骤，杜绝未来上市标的进入历史池。
    active = [c for c in data.raw_close.columns
              if pd.notna(data.first_bar.get(c)) and data.first_bar[c] <= signal_date
              and c != MONEY_ETF_SYMBOL]
    stats = {"active": len(active)}

    scale = data.fund_scale.loc[signal_date]
    passed = [c for c in active
              if np.isfinite(scale.get(c, np.nan)) and scale[c] >= MIN_FUND_SCALE_YUAN]
    stats["after_scale"] = len(passed)                    # 过滤 1a：规模 < 1 亿

    passed = [c for c in passed
              if c not in data.termination_dates
              or data.termination_dates[c] > signal_date]
    stats["after_termination"] = len(passed)              # 过滤 1b：清盘/终止/合并公告

    amt = amount_ma.loc[signal_date]
    passed = [c for c in passed
              if np.isfinite(amt.get(c, np.nan)) and amt[c] >= MIN_AVG_DAILY_AMOUNT_YUAN]
    stats["after_amount"] = len(passed)                   # 过滤 1c：日均成交额

    passed = [c for c in passed
              if (signal_date - data.first_bar[c]).days >= MIN_LISTED_CALENDAR_DAYS]
    stats["after_listing"] = len(passed)                  # 过滤 1d：上市不足半年

    premium = data.premium_pct.loc[signal_date]
    kept = []
    for c in passed:                                      # 过滤 2：仅跨境 ETF 校验溢价
        if is_cross_border(c, data):
            value = premium.get(c, np.nan)
            if not np.isfinite(value) or value > CROSS_BORDER_PREMIUM_LIMIT_PCT:
                continue
        kept.append(c)
    passed = kept
    stats["after_premium"] = len(passed)

    mom = momentum.loc[signal_date]
    passed = [c for c in passed if np.isfinite(mom.get(c, np.nan)) and mom[c] >= 0]
    stats["after_momentum"] = len(passed)                 # 过滤 3：60 日收益 < 0

    ranked = sorted(passed, key=lambda c: (-mom[c], c))
    selected = ranked[:top_n] if len(ranked) >= top_n else ranked
    if selected and weighting_mode == "momentum":
        scores = np.array([max(float(mom[c]), 0.0) for c in selected])
        weights = scores / scores.sum() if scores.sum() > 0 else np.full(len(selected), 1 / len(selected))
    else:
        weights = np.full(len(selected), 1 / len(selected)) if selected else np.array([])
    return {"date": signal_date, "selected": selected,
            "weights": dict(zip(selected, weights)), "stats": stats}


def build_schedule(data: MarketData, start: pd.Timestamp, end: pd.Timestamp,
                   top_n: int, weighting_mode: str) -> tuple[list[dict], pd.DataFrame]:
    """月末收盘出信号、下一交易日执行；最后一月无后续交易日则不产生信号。"""
    momentum = data.adj_close.pct_change(MOMENTUM_DAYS, fill_method=None)
    amount_ma = data.amount.rolling(AMOUNT_MA_DAYS, min_periods=AMOUNT_MA_DAYS).mean()
    full = data.full_calendar
    month_ends = pd.Series(full, index=full).groupby(full.to_period("M")).max().tolist()
    schedule = []
    for signal_date in month_ends:
        if signal_date < start or signal_date > end:
            continue
        pos = full.searchsorted(signal_date, side="right")
        if pos >= len(full):                              # 无下一交易日，无法执行
            continue
        execution_date = full[pos]
        if execution_date > end:
            continue
        item = screen_universe(signal_date, data, momentum, amount_ma, top_n, weighting_mode)
        item["execution_date"] = execution_date
        schedule.append(item)
    return schedule, momentum


def _commission(shares: float, execution_price: float) -> float:
    """精确实现 max(成交额×佣金率, 最低佣金)，ETF 不收印花税。"""
    if shares <= 0:
        return 0.0
    return max(shares * execution_price * COMMISSION_RATE, MIN_COMMISSION_YUAN)


def _affordable_lots(cash: float, execution_price: float) -> int:
    shares = max(0, math.floor(cash / execution_price / LOT_SIZE) * LOT_SIZE)
    while shares > 0 and shares * execution_price + _commission(shares, execution_price) > cash + 1e-8:
        shares -= LOT_SIZE
    return shares


def build_orders(schedule: list[dict], data: MarketData) -> pd.DataFrame:
    """VectorBT 变通核心：把动态集合、容差和货币 ETF 转成股数差额订单。

    此函数只使用执行日开盘价和此前持仓。成交价已含滑点，费用逐笔预计算；
    卖单先于买单，资金不足时逐手缩减，不融资、不做空。
    """
    positions: dict[str, float] = {c: 0.0 for c in data.raw_close.columns}
    cash, previous_selected = INITIAL_CASH, None
    orders: list[dict] = []

    def execute(date, signal_date, code, signed_shares, reason):
        nonlocal cash
        if signed_shares == 0:
            return False
        raw_price = data.adj_open.at[date, code]
        if not np.isfinite(raw_price) or raw_price <= 0:
            orders.append({"signal_date": signal_date, "execution_date": date, "code": code,
                           "side": "buy" if signed_shares > 0 else "sell", "shares": 0,
                           "execution_price": np.nan, "commission": 0, "reason": reason,
                           "status": "skipped_no_open"})
            return False
        side = "buy" if signed_shares > 0 else "sell"
        slipped = raw_price * (1 + SLIPPAGE_RATE if signed_shares > 0 else 1 - SLIPPAGE_RATE)
        shares = abs(int(signed_shares))
        fee = _commission(shares, slipped)
        cash -= shares * slipped + fee if signed_shares > 0 else -(shares * slipped - fee)
        positions[code] = positions.get(code, 0.0) + signed_shares
        orders.append({"signal_date": signal_date, "execution_date": date, "code": code,
                       "side": side, "shares": shares, "execution_price": slipped,
                       "commission": fee, "reason": reason, "status": "sent"})
        return True

    for item in schedule:
        date, signal_date = item["execution_date"], item["date"]
        selected, weights = item["selected"], item["weights"]
        current_set = previous_selected or []
        same_set = set(selected) == set(current_set)
        # 无开盘价的持仓用最近收盘价估值，但绝不把该填充价用于成交。
        mark = data.adj_open.loc[date].where(data.adj_open.loc[date].notna(),
                                             data.adj_close.ffill().shift(1).loc[date])
        equity = cash + sum(n * mark.get(c, np.nan) for c, n in positions.items()
                            if n > 0 and np.isfinite(mark.get(c, np.nan)))
        if equity <= 0:
            raise ValueError(f"{date.date()} 权益非正，无法继续回测")
        risky_value = sum(positions.get(c, 0) * mark.get(c, np.nan)
                          for c in positions if c != MONEY_ETF_SYMBOL
                          and positions.get(c, 0) > 0 and np.isfinite(mark.get(c, np.nan)))
        risk_weight = risky_value / equity

        targets = {c: 0.0 for c, n in positions.items() if n > 0 and c != MONEY_ETF_SYMBOL}
        needs_risk_rebalance = not same_set or risk_weight < TARGET_RISK_WEIGHT - RISK_WEIGHT_TOLERANCE \
            or risk_weight > TARGET_RISK_WEIGHT + RISK_WEIGHT_TOLERANCE
        if not same_set:
            reason = "set_changed"                         # 集合变化：忽略容差，直接到目标
        elif needs_risk_rebalance:
            reason = "weight_outside_tolerance"            # 集合不变：71%–79% 之外回 75%
        else:
            reason = "within_tolerance"                    # 集合不变且仓位在容差内：不操作
        if needs_risk_rebalance:
            targets.update({c: equity * TARGET_RISK_WEIGHT * weights[c] for c in selected})
        else:
            targets.update({c: positions.get(c, 0.0) for c in selected})

        # 先卖风险资产。目标股数按含买入滑点的价格向下取 100 份整数倍。
        target_shares = {}
        for c in set(targets) | set(selected):
            p = data.adj_open.at[date, c]
            if not np.isfinite(p) or p <= 0:
                target_shares[c] = positions.get(c, 0.0)   # 停牌时保留，不伪造成交
            elif needs_risk_rebalance:
                buy_price = p * (1 + SLIPPAGE_RATE)
                target_shares[c] = math.floor(targets.get(c, 0.0) / buy_price / LOT_SIZE) * LOT_SIZE
            else:
                target_shares[c] = positions.get(c, 0.0)
        for c in sorted(target_shares):
            diff = target_shares[c] - positions.get(c, 0.0)
            if diff < 0:
                execute(date, signal_date, c, int(diff), reason)

        # 计划风险买单；先计算总成本，必要时只卖一次货币 ETF 补足资金。
        plans = []
        for c in selected:                                 # 已按动量排名排序
            diff = int(target_shares.get(c, 0) - positions.get(c, 0))
            p = data.adj_open.at[date, c]
            if diff > 0 and np.isfinite(p) and p > 0:
                slipped = p * (1 + SLIPPAGE_RATE)
                plans.append([c, diff, slipped, _commission(diff, slipped)])
        required = sum(n * p + fee for _, n, p, fee in plans)
        if required > cash + 1e-8:
            p = data.adj_open.at[date, MONEY_ETF_SYMBOL]
            held = int(positions.get(MONEY_ETF_SYMBOL, 0))
            if np.isfinite(p) and p > 0 and held > 0:
                sell_price = p * (1 - SLIPPAGE_RATE)
                need = required - cash
                sell_n = min(held, math.ceil(need / sell_price / LOT_SIZE) * LOT_SIZE)
                while sell_n < held and sell_n * sell_price - _commission(sell_n, sell_price) < need - 1e-8:
                    sell_n += LOT_SIZE
                execute(date, signal_date, MONEY_ETF_SYMBOL, -sell_n, "fund_risk_buys")
        # 极端情况下货币 ETF 也不足，按排名逐手缩减，不透支。
        for plan in plans:
            c, n, slipped, _ = plan
            affordable = _affordable_lots(cash, slipped)
            n = min(n, affordable)
            if n > 0:
                execute(date, signal_date, c, n, reason)

        # 剩余资金全部买货币 ETF；不足一手/最低佣金的零头自然留现金。
        p = data.adj_open.at[date, MONEY_ETF_SYMBOL]
        if np.isfinite(p) and p > 0 and cash > 0:
            buy_price = p * (1 + SLIPPAGE_RATE)
            n = _affordable_lots(cash, buy_price)
            if n > 0:
                execute(date, signal_date, MONEY_ETF_SYMBOL, n, "residual_to_money_etf")
        previous_selected = selected

    return pd.DataFrame(orders)


def run_vectorbt(orders: pd.DataFrame, data: MarketData,
                 start: pd.Timestamp, end: pd.Timestamp) -> vbt.Portfolio:
    """把自定义订单交给 VectorBT；逐笔 fixed_fees 实现最低佣金。"""
    calendar = data.full_calendar[(data.full_calendar >= start) & (data.full_calendar <= end)]
    codes = data.raw_close.columns
    close = data.adj_close.reindex(index=calendar, columns=codes)
    open_ = data.adj_open.reindex(index=calendar, columns=codes)
    size = pd.DataFrame(0.0, index=calendar, columns=codes)
    fixed_fees = pd.DataFrame(0.0, index=calendar, columns=codes)
    sent = orders[orders["status"] == "sent"] if not orders.empty else orders
    for row in sent.itertuples():
        d = pd.Timestamp(row.execution_date)
        sign = 1 if row.side == "buy" else -1
        size.at[d, row.code] += sign * row.shares
        fixed_fees.at[d, row.code] += row.commission
    # VectorBT 的 size_type="amount" 指资产数量；这里 size 是 ETF 份额，不是成交额。
    return vbt.Portfolio.from_orders(
        close=close, price=open_, size=size, size_type="amount", direction="longonly",
        fees=0.0, fixed_fees=fixed_fees, slippage=SLIPPAGE_RATE,
        init_cash=INITIAL_CASH, cash_sharing=True, group_by=True, call_seq="auto",
        allow_partial=False, ffill_val_price=True, update_value=True, freq="D",
    )


def _metric(value, scale: float = 1.0):
    """绩效转成标准 JSON 数值；零收益等场景的无穷夏普用 null 表示。"""
    if isinstance(value, (pd.Series, pd.DataFrame)):
        value = np.asarray(value).reshape(-1)[0]
    result = float(value) * scale
    return round(result, 4) if np.isfinite(result) else None


def save_outputs(pf: vbt.Portfolio, orders: pd.DataFrame, schedule: list[dict],
                 data: MarketData, output_dir: Path, params: dict) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    value, cash = pf.value(), pf.cash()
    if isinstance(value, pd.DataFrame):
        value, cash = value.iloc[:, 0], cash.iloc[:, 0]
    # 共享现金时默认按组合聚合；风险仓位必须按单资产市值计算，不能拿组合总值代替。
    asset_value = pf.asset_value(group_by=False)
    if isinstance(asset_value, pd.Series):
        asset_value = asset_value.to_frame()
    risky = asset_value.drop(columns=[MONEY_ETF_SYMBOL], errors="ignore").sum(axis=1)
    daily = pd.DataFrame({"date": value.index, "total_value": value.to_numpy(),
                          "cash": np.asarray(cash), "risk_value": risky.to_numpy()})
    daily["risk_weight"] = daily["risk_value"] / daily["total_value"]
    daily.to_csv(output_dir / "daily_equity.csv", index=False, encoding="utf-8-sig")
    orders.to_csv(output_dir / "orders.csv", index=False, encoding="utf-8-sig")
    pf.orders.records_readable.to_csv(output_dir / "vectorbt_orders.csv",
                                      index=False, encoding="utf-8-sig")

    selection_rows = []
    for item in schedule:
        row = {"signal_date": item["date"], "execution_date": item["execution_date"],
               **item["stats"], "selected_count": len(item["selected"]),
               "selected": ",".join(item["selected"]),
               "weights": json.dumps({k: round(float(v), 8) for k, v in item["weights"].items()})}
        selection_rows.append(row)
    pd.DataFrame(selection_rows).to_csv(output_dir / "selection_log.csv",
                                        index=False, encoding="utf-8-sig")

    signal_dates = [x["date"] for x in schedule]
    quality = {
        "price_rows": int(data.raw_close.notna().sum().sum()),
        "scale_non_null_at_rebalance": int(data.fund_scale.loc[signal_dates].notna().sum().sum()) if signal_dates else 0,
        "premium_non_null_at_rebalance": int(data.premium_pct.loc[signal_dates].notna().sum().sum()) if signal_dates else 0,
        "announcement_source": data.announcement_source or None,
        "warnings": [],
    }
    if not data.announcement_source:
        quality["warnings"].append("未提供清盘/终止/合并公告历史；该过滤不完整，使用 --strict-data 可改为直接失败")
    if quality["scale_non_null_at_rebalance"] == 0 and signal_dates:
        quality["warnings"].append("再平衡时点没有基金规模历史，按规则所有风险标的均被剔除")
    (output_dir / "data_quality.json").write_text(
        json.dumps(quality, ensure_ascii=False, indent=2), encoding="utf-8")

    sent = orders[orders["status"] == "sent"] if not orders.empty else orders
    summary = {
        **params,
        "start": str(value.index[0].date()), "end": str(value.index[-1].date()),
        "final_value": _metric(value.iloc[-1]),
        "total_return_pct": _metric(pf.total_return(), 100),
        "max_drawdown_pct": _metric(pf.max_drawdown(), 100),
        "sharpe_ratio": _metric(pf.sharpe_ratio()),
        "rebalance_count": len(schedule), "intended_order_count": len(sent),
        "vectorbt_order_count": len(pf.orders.records),
        "warnings": quality["warnings"],
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(2, 1, figsize=(12, 7), sharex=True,
                             gridspec_kw={"height_ratios": [3, 1]})
    axes[0].plot(value.index, value.to_numpy(), label="Total equity")
    axes[0].set_title("ETF monthly momentum (VectorBT)")
    axes[0].legend(); axes[0].grid(alpha=0.25)
    axes[1].plot(daily["date"], daily["risk_weight"] * 100, color="darkorange")
    axes[1].axhline(75, color="gray", ls="--", lw=0.8)
    axes[1].set_ylabel("Risk weight (%)"); axes[1].grid(alpha=0.25)
    fig.tight_layout(); fig.savefig(output_dir / "equity.png", dpi=150); plt.close(fig)
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    print(f"输出目录：{output_dir}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--start", default=START_DATE)
    parser.add_argument("--end", default=END_DATE)
    parser.add_argument("--top-n", type=int, default=TOP_N)
    parser.add_argument("--weighting", choices=["momentum", "equal"], default=WEIGHTING_MODE)
    parser.add_argument("--announcement-csv", type=Path, default=ANNOUNCEMENT_CSV)
    parser.add_argument("--output", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--strict-data", action="store_true", default=STRICT_REFERENCE_DATA)
    args = parser.parse_args()
    if args.top_n <= 0:
        parser.error("--top-n 必须大于 0")

    data = load_market_data(args.db, args.announcement_csv)
    if args.strict_data and not data.announcement_source:
        raise ValueError("严格模式：缺少清盘/终止/合并公告数据源，拒绝把无记录当作无公告")
    start = pd.Timestamp(args.start)
    end = pd.Timestamp(args.end) if args.end else data.full_calendar[-1]
    if start >= end:
        raise ValueError("回测起点必须早于终点")
    schedule, _ = build_schedule(data, start, end, args.top_n, args.weighting)
    orders = build_orders(schedule, data)
    pf = run_vectorbt(orders, data, start, end)
    params = {"weighting": args.weighting, "top_n": args.top_n,
              "target_risk_weight": TARGET_RISK_WEIGHT,
              "risk_weight_tolerance": RISK_WEIGHT_TOLERANCE,
              "momentum_days": MOMENTUM_DAYS,
              "min_fund_scale_yuan": MIN_FUND_SCALE_YUAN,
              "min_avg_daily_amount_yuan": MIN_AVG_DAILY_AMOUNT_YUAN,
              "commission_rate": COMMISSION_RATE, "min_commission_yuan": MIN_COMMISSION_YUAN,
              "slippage_rate": SLIPPAGE_RATE, "lot_size": LOT_SIZE,
              "money_etf": MONEY_ETF_SYMBOL, "database": str(args.db)}
    save_outputs(pf, orders, schedule, data, args.output, params)


if __name__ == "__main__":
    warnings.filterwarnings("ignore", category=FutureWarning)
    main()
