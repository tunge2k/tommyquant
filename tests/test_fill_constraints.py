"""涨跌停撮合约束集成测试 (事件驱动引擎)

运行: py -3.13 -m unittest tests.test_fill_constraints -v
"""
import os
import sys
import unittest
from unittest import mock

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import backtest_engine as be  # noqa: E402
import fast_backtest as fastbt  # noqa: E402


class ScriptedStrategy(be.BaseStrategy):
    """按日期脚本产生买卖信号, 用于验证引擎撮合行为"""

    def __init__(self, script):
        self.script = script

    def on_bar(self, event):
        spec = self.script.get(str(event.date)[:10]) or {}
        if not spec:
            return None
        return be.SignalEvent(
            event.date,
            buys=spec.get("buys", []),
            sells=spec.get("sells", []),
        )


def make_feat(code, prices):
    """prices: [(open, high, low, close), ...] 从 2026-01-05 起连续交易日"""
    dates = pd.bdate_range("2026-01-05", periods=len(prices))
    rows = []
    for d, (o, h, l, c) in zip(dates, prices):
        rows.append({
            "date": d, "open": o, "high": h, "low": l, "close": c,
            "volume": 1_000_000, "amount": 1e8,
            "pct": 0.0, "limit_up": 0,
        })
    return {code: pd.DataFrame(rows)}


class TestFillConstraints(unittest.TestCase):
    def _run(self, feat, strategy):
        with mock.patch.object(be, "fundamental_asof",
                               return_value={"map": {}, "median": 0}):
            engine = be.EventDrivenBacktest(
                feat, strategy, initial_capital=200_000, top_n=1,
                start_date="2026-01-05", end_date="2026-01-31",
                limit_rules={"enforce": True, "strict_open": True},
            )
            return engine.run()

    def test_buy_rejected_on_limit_up_open(self):
        code = "sh600000"
        prices = [
            (10.0, 10.1, 9.9, 10.0),   # 信号日
            (11.0, 11.0, 11.0, 11.0),  # 一字涨停, 应拒买
            (11.2, 11.3, 11.0, 11.1),
            (11.1, 11.2, 11.0, 11.1),
        ]
        result = self._run(make_feat(code, prices),
                           ScriptedStrategy({str(pd.bdate_range("2026-01-05", periods=1)[0])[:10]:
                                             {"buys": [code]}}))
        self.assertEqual(result["fill_blocks"]["buy_limit_up"], 1)
        self.assertEqual(len(result["trades"]), 0)
        self.assertEqual(len(result["curve"]), 4)

    def test_sell_deferred_after_limit_down(self):
        code = "sh600000"
        dates = pd.bdate_range("2026-01-05", periods=5)
        prices = [
            (10.0, 10.1, 9.9, 10.0),   # d0 买入信号
            (10.0, 10.1, 9.9, 10.0),   # d1 买入成交
            (10.0, 10.1, 9.9, 10.0),   # d2 卖出信号
            (9.0, 9.0, 9.0, 9.0),      # d3 一字跌停, 卖不出 → 顺延
            (9.0, 9.2, 8.9, 9.1),      # d4 可卖
        ]
        script = {
            str(dates[0])[:10]: {"buys": [code]},
            str(dates[2])[:10]: {"sells": [code]},
        }
        result = self._run(make_feat(code, prices), ScriptedStrategy(script))
        self.assertEqual(result["fill_blocks"]["sell_limit_down"], 1)
        self.assertEqual(len(result["trades"]), 1)
        self.assertEqual(str(result["trades"][0]["sell_date"])[:10], str(dates[4])[:10])

    def test_enforce_switch_off_keeps_old_behavior(self):
        code = "sh600000"
        prices = [
            (10.0, 10.1, 9.9, 10.0),
            (11.0, 11.0, 11.0, 11.0),
            (11.0, 11.1, 10.9, 11.0),
        ]
        feat = make_feat(code, prices)
        sig_date = str(pd.bdate_range("2026-01-05", periods=1)[0])[:10]
        with mock.patch.object(be, "fundamental_asof",
                               return_value={"map": {}, "median": 0}):
            engine = be.EventDrivenBacktest(
                feat, ScriptedStrategy({sig_date: {"buys": [code]}}),
                initial_capital=200_000, top_n=1,
                limit_rules={"enforce": False},
            )
            result = engine.run()
        self.assertEqual(result["fill_blocks"]["buy_limit_up"], 0)
        self.assertEqual(result["curve"]["holdings"].iloc[-1], 1)


class TestFastEngineParity(unittest.TestCase):
    """向量化引擎与事件引擎使用同一套撮合规则"""

    def _run(self, feat, code, sel_date, hold_days=1):
        M = fastbt.SelectionMatrix(feat, [sel_date])
        bt = fastbt.FastBacktest(
            feat, M, lambda M_, si, p: [code], {"top_n": 1},
            initial_capital=200_000, top_n=1, hold_days=hold_days,
            limit_rules={"enforce": True, "strict_open": True},
        )
        with mock.patch.object(be, "fundamental_asof",
                               return_value={"map": {}, "median": 0}):
            return bt.run()

    def test_buy_blocked_on_limit_up(self):
        code = "sh600000"
        prices = [
            (10.0, 10.1, 9.9, 10.0),
            (11.0, 11.0, 11.0, 11.0),
            (11.2, 11.3, 11.0, 11.1),
        ]
        feat = make_feat(code, prices)
        result = self._run(feat, code, feat[code]["date"].iloc[0])
        self.assertEqual(result["fill_blocks"]["buy_limit_up"], 1)
        self.assertEqual(len(result["trades"]), 0)

    def test_sell_deferred_on_limit_down(self):
        code = "sh600000"
        dates = pd.bdate_range("2026-01-05", periods=5)
        prices = [
            (10.0, 10.1, 9.9, 10.0),   # d0 选股
            (10.0, 10.1, 9.9, 10.0),   # d1 买入
            (9.0, 9.0, 9.0, 9.0),      # d2 一字跌停, 卖不出
            (9.0, 9.2, 8.9, 9.1),      # d3 卖出
            (9.1, 9.2, 9.0, 9.1),
        ]
        feat = make_feat(code, prices)
        result = self._run(feat, code, feat[code]["date"].iloc[0], hold_days=1)
        self.assertEqual(result["fill_blocks"]["sell_limit_down"], 1)
        self.assertEqual(len(result["trades"]), 1)
        self.assertEqual(str(result["trades"][0]["sell_date"])[:10], str(dates[3])[:10])


if __name__ == "__main__":
    unittest.main()
