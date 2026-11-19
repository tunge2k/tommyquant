"""建仓趋势确认 (周K MA30 连续 N 周上升) 回归测试

覆盖 2026-11-19 需求: 周K MA30 由降转升需连续 2 周(约10个交易日)才建仓,
避免刚建仓就遇 MA30 走弱清仓。

运行: py -3.13 -m unittest tests.test_ma30_entry -v
"""
import os
import sys
import unittest

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import backtest_engine as be  # noqa: E402


def make_cache(weekly_closes, lb=5):
    vals = np.asarray(weekly_closes, dtype="float64")
    wk_idx = pd.date_range("2020-01-03", periods=len(vals), freq="W-FRI").values
    return {
        "wk_idx": wk_idx,
        "prefix": np.concatenate(([0.0], np.cumsum(vals))),
        "ma30": pd.Series(vals).rolling(30).mean().to_numpy(),
        "dates": wk_idx,
        "closes": vals,
        "lb": lb,
    }


def last_friday(cache):
    return pd.Timestamp(cache["wk_idx"][-1])


class Ma30RisingWeeksTest(unittest.TestCase):
    def test_two_consecutive_rising_weeks(self):
        cache = {"sh600000": make_cache(range(1, 41))}   # 周线持续上涨
        self.assertTrue(be.ma30_rising_weeks(
            cache, "sh600000", last_friday(cache["sh600000"]), 2))

    def test_declining_ma30_rejected(self):
        cache = {"sh600000": make_cache(range(40, 0, -1))}  # 周线持续下跌
        self.assertFalse(be.ma30_rising_weeks(
            cache, "sh600000", last_friday(cache["sh600000"]), 2))

    def test_flat_ma30_rejected(self):
        cache = {"sh600000": make_cache([5.0] * 40)}
        self.assertFalse(be.ma30_rising_weeks(
            cache, "sh600000", last_friday(cache["sh600000"]), 1))

    def test_insufficient_history_returns_none(self):
        cache = {"sh600000": make_cache(range(1, 32))}   # 31 周, 不够 29+2
        self.assertIsNone(be.ma30_rising_weeks(
            cache, "sh600000", last_friday(cache["sh600000"]), 2))

    def test_disabled_weeks_returns_none(self):
        cache = {"sh600000": make_cache(range(1, 41))}
        self.assertIsNone(be.ma30_rising_weeks(
            cache, "sh600000", last_friday(cache["sh600000"]), 0))

    def test_strategy_entry_check_default_and_switch_off(self):
        strat = be.MarketCapStrategy(top_n=2, ma30_entry_weeks=2)
        self.assertEqual(strat.ma30_entry_weeks, 2)
        # 无 feat/周线数据 → 数据不足放行, 不拦截建仓
        self.assertTrue(strat._ma30_entry_ok("sh600000", "2026-01-05"))
        strat.ma30_entry_weeks = 0
        self.assertTrue(strat._ma30_entry_ok("sh600000", "2026-01-05"))

    def test_selection_kwargs_carry_entry_weeks(self):
        strat = be.MarketCapStrategy(top_n=2, ma30_entry_weeks=3)
        strat.feat = {}
        kw = strat._selection_kwargs(pd.Timestamp("2026-01-05"))
        self.assertEqual(kw["ma30_entry_weeks"], 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
