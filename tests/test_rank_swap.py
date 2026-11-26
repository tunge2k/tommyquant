"""rank_swap 开关回归测试 (2026-11-24)

业务诉求: 不能频繁换股, 只有当持仓数不符合设定时才换股。
口径:
  - rank_swap=False (界面默认): 不做「排名跌出换股」, 持仓排名跌出但未止损时继续持有;
    仅在持仓数 < top_n (止损清出空槽) 时按当日排名补位。
  - 关闭排名换股后, 股票池恢复走偏离度再平衡 (低买高卖 ±4% / ≥5% 权重),
    保证仓位管理不停摆 (ETF 偏离度口径不变)。
  - rank_swap=True (勾选设置页): 恢复原 Topk-Drop 排名换股行为。

运行: py -3.13 -m pytest tests/test_rank_swap.py -q
"""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import backtest_engine as be  # noqa: E402
from backtest_engine import MarketBarEvent, MarketCapStrategy  # noqa: E402

DATE = "2026-01-05"


class FakeBars:
    def __init__(self, rows):
        self.rows = rows

    def row(self, code, date):
        return self.rows.get((code, str(date)[:10]))

    def last_row(self, code, date):
        # 与真实 BarIndex 一致: 无数据返回 None, 让 _current_price 退化为 nan
        return None


class FakePortfolio:
    def __init__(self, cash, positions=None, total_value=None):
        self.cash = float(cash)
        self.positions = positions or {}
        self._total_value = total_value

    def total_value(self, date, bars):
        if self._total_value is not None:
            return float(self._total_value)
        pv = self.cash
        for code, pos in self.positions.items():
            r = bars.row(code, date)
            if r is not None:
                pv += r["close"] * pos.quantity
        return pv


def position(qty):
    return SimpleNamespace(quantity=int(qty), entry_price=10.0, entry_date=DATE)


def orders_of(signal):
    return signal.rebalance_orders if signal else []


class RankSwapDisabledTest(unittest.TestCase):
    """rank_swap=False: 排名跌出不换股, 仅持仓数不足时补位"""

    def _strat(self, **kw):
        params = dict(top_n=2, rank_buffer=2, rebalance_mode="topk_drop",
                      ma30_exit=False, universe=None, rank_swap=False)
        params.update(kw)
        return MarketCapStrategy(**params)

    def test_rank_drop_does_not_sell_when_disabled(self):
        """排名跌出缓冲但 rank_swap=False → 不产生 rank_drop 卖出, 也不补仓 (持仓已满)"""
        strat = self._strat()
        strat.portfolio = FakePortfolio(
            cash=50000,
            positions={"sh600001": position(7500), "sh600005": position(7500)},
            total_value=200000.0)  # 各 7.5 万 = 等权目标, 价格=入场价 → 偏离度不触发
        strat.bars = FakeBars({
            ("sh600001", DATE): {"close": 10.0, "cap_yi": 20.0},
            ("sh600005", DATE): {"close": 10.0, "cap_yi": 90.0},
        })
        strat._target_weights = {"sh600001": 0.5, "sh600005": 0.5}
        strat._entry_prices = {"sh600001": 10.0, "sh600005": 10.0}

        # sh600005 排名第 5 > keep(top_n2+buffer2=4); rank_swap=True 时会被换出
        ranked = ["sh600001", "sh600002", "sz600003", "sz600004", "sh600005"]
        with mock.patch.object(be, "_filter_market_cap_universe", return_value=ranked):
            sig = strat.on_bar(MarketBarEvent(DATE, {"sh600001": {}}, strat.bars))

        # 既无 rank_drop 卖出, 也无任何动作 (持仓数=目标, 偏离度不触发)
        self.assertIsNone(sig)

    def test_refill_when_holdings_below_target(self):
        """持仓数 < top_n → 即使 rank_swap=False 也按当日排名补位 (不留空仓)"""
        strat = self._strat(top_n=2)
        strat.portfolio = FakePortfolio(
            cash=50000,
            positions={"sh600001": position(7500)},  # 只持 1 只, 缺 1 个槽
            total_value=200000.0)
        strat.bars = FakeBars({
            ("sh600001", DATE): {"close": 10.0, "cap_yi": 20.0},
            ("sh600002", DATE): {"close": 10.0, "cap_yi": 30.0},
        })
        strat._target_weights = {"sh600001": 0.5}
        strat._entry_prices = {"sh600001": 10.0}

        ranked = ["sh600001", "sh600002", "sz600003"]
        with mock.patch.object(be, "_filter_market_cap_universe", return_value=ranked):
            sig = strat.on_bar(MarketBarEvent(DATE, {"sh600001": {}}, strat.bars))

        buys = [o.code for o in orders_of(sig) if o.direction == "buy"]
        self.assertIn("sh600002", buys)  # 补位排名最靠前的未持有标的
        # 不得把存量持仓因排名换股卖掉
        self.assertNotIn("sh600001", sig.sells if sig else [])

    def test_refill_rebalances_overweight_existing_position(self):
        """补位时存量重仓须同步卖回等权, 释放新标的建仓资金"""
        strat = self._strat(top_n=2)
        strat.portfolio = FakePortfolio(
            cash=50000, positions={"sh600001": position(15000)}, total_value=200000.0)
        strat.bars = FakeBars({
            ("sh600001", DATE): {"close": 10.0, "cap_yi": 20.0},
            ("sh600002", DATE): {"close": 10.0, "cap_yi": 30.0},
        })
        strat._target_weights = {"sh600001": 1.0}
        strat._entry_prices = {"sh600001": 10.0}
        with mock.patch.object(be, "_filter_market_cap_universe",
                               return_value=["sh600001", "sh600002"]):
            sig = strat.on_bar(MarketBarEvent(DATE, {"sh600001": {}}, strat.bars))
        orders = orders_of(sig)
        self.assertEqual([(o.code, o.direction, o.quantity) for o in orders],
                         [("sh600001", "sell", 7500), ("sh600002", "buy", 7500)])
        self.assertNotIn("sh600001", sig.sells)  # 等权调仓, 不是排名换出

    def test_rank_drop_still_sells_when_enabled(self):
        """rank_swap=True (勾选) → 恢复排名跌出换股, 保证开关双向有效"""
        strat = self._strat(rank_swap=True)
        strat.portfolio = FakePortfolio(
            cash=50000,
            positions={"sh600001": position(7500), "sh600005": position(7500)},
            total_value=200000.0)
        strat.bars = FakeBars({
            ("sh600001", DATE): {"close": 10.0, "cap_yi": 20.0},
            ("sh600002", DATE): {"close": 10.0, "cap_yi": 30.0},
            ("sh600005", DATE): {"close": 10.0, "cap_yi": 90.0},
        })
        strat._target_weights = {"sh600001": 0.5, "sh600005": 0.5}
        strat._entry_prices = {"sh600001": 10.0, "sh600005": 10.0}

        ranked = ["sh600001", "sh600002", "sz600003", "sz600004", "sh600005"]
        with mock.patch.object(be, "_filter_market_cap_universe", return_value=ranked):
            sig = strat.on_bar(MarketBarEvent(DATE, {"sh600001": {}}, strat.bars))

        self.assertIsNotNone(sig)
        self.assertIn("sh600005", sig.sells)
        self.assertEqual(sig.sell_reasons.get("sh600005"), "rank_drop")


class StockDeviationRebalanceWithRankSwapOffTest(unittest.TestCase):
    """rank_swap=False 时股票池必须仍走偏离度调仓 (低买高卖), 否则仓位管理停摆"""

    def test_stock_rise_triggers_deviation_sell_when_rank_swap_off(self):
        strat = MarketCapStrategy(top_n=1, rebalance_mode="topk_drop",
                                  rank_swap=False, ma30_exit=False,
                                  universe=None, pool_type="stock")
        # 持仓市值 11 万, 入场价 10 → 现价 11 (+10% ≥ 4% 价格触发)
        strat.portfolio = FakePortfolio(
            cash=0, positions={"sh600001": position(10000)},
            total_value=110000.0)
        strat.bars = FakeBars({("sh600001", DATE): {"close": 11.0, "cap_yi": 100.0}})
        strat._target_weights = {"sh600001": 1.0}
        strat._entry_prices = {"sh600001": 10.0}

        # 排名第 1, 即使 rank_swap 开启也不会触发排名换股 → 隔离出偏离度效果
        with mock.patch.object(be, "_filter_market_cap_universe",
                               return_value=["sh600001"]):
            sig = strat.on_bar(MarketBarEvent(DATE, {"sh600001": {}}, strat.bars))

        self.assertIsNotNone(sig, "rank_swap=False 时股票仍应走偏离度调仓")
        sells = [o for o in sig.rebalance_orders if o.direction == "sell"]
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0].code, "sh600001")
        self.assertGreater(sells[0].quantity, 0)

    def test_stock_drop_triggers_deviation_buy_when_rank_swap_off(self):
        strat = MarketCapStrategy(top_n=1, rebalance_mode="topk_drop",
                                  rank_swap=False, ma30_exit=False,
                                  universe=None, pool_type="stock")
        # 持仓市值 9 万, 入场价 10 → 现价 9 (-10% ≤ -4% 价格触发 → 补仓)
        strat.portfolio = FakePortfolio(
            cash=50000, positions={"sh600001": position(10000)},
            total_value=140000.0)
        strat.bars = FakeBars({("sh600001", DATE): {"close": 9.0, "cap_yi": 100.0}})
        strat._target_weights = {"sh600001": 1.0}
        strat._entry_prices = {"sh600001": 10.0}

        with mock.patch.object(be, "_filter_market_cap_universe",
                               return_value=["sh600001"]):
            sig = strat.on_bar(MarketBarEvent(DATE, {"sh600001": {}}, strat.bars))

        self.assertIsNotNone(sig)
        buys = [o for o in sig.rebalance_orders if o.direction == "buy"]
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0].code, "sh600001")
        self.assertGreater(buys[0].quantity, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
