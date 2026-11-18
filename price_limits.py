"""A股涨跌停规则: 板块/ST/日期感知 + 交易所价位取整 + 撮合拦截判定

参考 tick-stock-panel (MIT) 的 price_limits 口径, 只保留本项目需要的部分:
- 主板 10%, 创业板/科创板 20%, 北交所 30%
- 创业板 2020-08-24 注册制改革前为 10%; 主板 ST 在 2026-07-06 前为 5%、之后 10%
- 涨跌停价按交易所口径以"分"为单位四舍五入 (不是简单 prev*1.1)

本模块零数据库依赖, 可被事件驱动引擎、向量化引擎与单元测试共用。
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP

MAIN_BOARD_LIMIT = 0.10
GROWTH_BOARD_LIMIT = 0.20
BEIJING_BOARD_LIMIT = 0.30
LEGACY_MAIN_BOARD_ST_LIMIT = 0.05

GEM_20PCT_LIMIT_START_DATE = date(2020, 8, 24)
MAIN_BOARD_ST_LIMIT_CHANGE_DATE = date(2026, 7, 6)

# 价格比较容差: 半分钱, 覆盖浮点误差与 0.01 最小变动价位
PRICE_TOL = 0.0051

_GEM_PREFIXES = ("300", "301", "302")
_STAR_PREFIXES = ("688", "689")
_BJ_PREFIXES = ("4", "8")
# 场内基金: 深 15/16 (ETF/LOF), 沪 50/51/52/56/58 (LOF/ETF)
_FUND_PREFIXES = ("15", "16", "50", "51", "52", "56", "58")


def pure_code(code: str) -> str:
    """sh600000 / sz300750 / 600000 -> 六位代码"""
    s = str(code or "").strip().lower()
    if s[:2] in ("sh", "sz", "bj"):
        return s[2:]
    return s


def board_of(code: str) -> str:
    """返回 main / gem / star / bj, 供展示与规则判定"""
    s = str(code or "").strip().lower()
    p = pure_code(s)
    if s.startswith("bj") or (len(s) == 6 and p.startswith(_BJ_PREFIXES)):
        return "bj"
    if p.startswith(_GEM_PREFIXES):
        return "gem"
    if p.startswith(_STAR_PREFIXES):
        return "star"
    return "main"


def is_etf(code: str) -> bool:
    """场内基金 (ETF/LOF): 佣金与最低收费与股票不同, 涨跌停仍为 10%"""
    return pure_code(code).startswith(_FUND_PREFIXES)


def _to_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def board_limit_pct(code: str) -> float:
    """不考虑 ST 与历史改革日的板块基础涨跌幅"""
    board = board_of(code)
    if board == "bj":
        return BEIJING_BOARD_LIMIT
    if board in ("gem", "star"):
        return GROWTH_BOARD_LIMIT
    return MAIN_BOARD_LIMIT


def price_limit_pct(code: str, trade_date=None, is_st: bool = False) -> float:
    """某标的在某交易日的有效涨跌幅 (小数, 如 0.10)

    - 创业板 2020-08-24 前与主板同为 10%
    - 主板 ST 在 2026-07-06 前为 5%, 之后与主板一致 10%
    - 创业板/科创板 ST 仍为 20%, 北交所 ST 仍为 30%
    """
    board = board_of(code)
    base = board_limit_pct(code)
    d = _to_date(trade_date)
    if board == "gem" and d is not None and d < GEM_20PCT_LIMIT_START_DATE:
        base = MAIN_BOARD_LIMIT
    if (
        is_st
        and base == MAIN_BOARD_LIMIT
        and (d is None or d < MAIN_BOARD_ST_LIMIT_CHANGE_DATE)
    ):
        return LEGACY_MAIN_BOARD_ST_LIMIT
    return base


def round_cent(value: float) -> float:
    """按交易所四舍五入到分 (Decimal 避免 10.005*100 的浮点截断)"""
    return float(
        Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    )


def limit_prices(prev_close: float, pct: float) -> tuple[float, float]:
    """(涨停价, 跌停价), 已按分取整"""
    prev = float(prev_close)
    return round_cent(prev * (1 + pct)), round_cent(prev * (1 - pct))


def at_price(price: float, target: float) -> bool:
    return abs(float(price) - float(target)) <= PRICE_TOL


def is_one_price_bar(row: dict) -> bool:
    """一字板: 开高低收四价合一 (忽略缺失值)"""
    vals = [row.get(k) for k in ("open", "high", "low", "close")]
    vals = [float(v) for v in vals if v is not None]
    if len(vals) < 4:
        return False
    return max(vals) - min(vals) <= 0.0001


def fill_block_reason(
    row: dict,
    prev_close: float | None,
    code: str,
    trade_date=None,
    direction: str = "buy",
    *,
    is_st: bool = False,
    strict_open: bool = True,
) -> str | None:
    """T+1 开盘撮合是否应被涨跌停拦截。

    strict_open=True: 开盘价已触及涨/跌停价即拦 (更保守, 排队大概率不成交)
    strict_open=False: 仅拦一字板 (与 TSP open_t+1 口径一致)

    返回 "limit_up" / "limit_down" / None。
    """
    if prev_close is None or float(prev_close) <= 0 or not row:
        return None
    pct = price_limit_pct(code, trade_date, is_st)
    up, down = limit_prices(prev_close, pct)
    o = row.get("open")
    c = row.get("close")
    if o is None or c is None:
        return None
    o, c = float(o), float(c)

    one_price = is_one_price_bar(row)
    if direction == "buy":
        if strict_open:
            if at_price(o, up):
                return "limit_up"
        elif one_price and at_price(c, up):
            return "limit_up"
        return None

    if direction == "sell":
        if strict_open:
            if at_price(o, down):
                return "limit_down"
        elif one_price and at_price(c, down):
            return "limit_down"
        return None
    return None
