"""涨跌停规则单元测试

运行: py -3.13 -m unittest tests.test_price_limits -v
"""
import os
import sys
import unittest
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import price_limits as pl  # noqa: E402


def bar(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c}


class TestBoardLimits(unittest.TestCase):
    def test_board_classification(self):
        self.assertEqual(pl.board_of("sh600000"), "main")
        self.assertEqual(pl.board_of("sz000001"), "main")
        self.assertEqual(pl.board_of("sz300750"), "gem")
        self.assertEqual(pl.board_of("sz301001"), "gem")
        self.assertEqual(pl.board_of("sh688981"), "star")
        self.assertEqual(pl.board_of("bj430047"), "bj")

    def test_limit_pct(self):
        self.assertAlmostEqual(pl.price_limit_pct("sh600000", "2026-11-18"), 0.10)
        self.assertAlmostEqual(pl.price_limit_pct("sz300750", "2026-11-18"), 0.20)
        self.assertAlmostEqual(pl.price_limit_pct("sh688981", "2026-11-18"), 0.20)
        self.assertAlmostEqual(pl.price_limit_pct("bj430047", "2026-11-18"), 0.30)

    def test_gem_before_reform(self):
        self.assertAlmostEqual(pl.price_limit_pct("sz300001", date(2020, 8, 21)), 0.10)
        self.assertAlmostEqual(pl.price_limit_pct("sz300001", date(2020, 8, 24)), 0.20)

    def test_st_rules(self):
        # 主板 ST: 2026-07-06 前 5%, 之后 10%
        self.assertAlmostEqual(pl.price_limit_pct("sh600001", "2026-07-03", is_st=True), 0.05)
        self.assertAlmostEqual(pl.price_limit_pct("sh600001", "2026-07-06", is_st=True), 0.10)
        # 创业板 ST 仍为 20%
        self.assertAlmostEqual(pl.price_limit_pct("sz300001", "2026-01-05", is_st=True), 0.20)

    def test_etf_detection(self):
        self.assertTrue(pl.is_etf("sh510300"))
        self.assertTrue(pl.is_etf("sh588000"))
        self.assertTrue(pl.is_etf("sz159915"))
        self.assertTrue(pl.is_etf("sz161725"))
        self.assertFalse(pl.is_etf("sh600000"))
        self.assertFalse(pl.is_etf("sz300750"))
        self.assertFalse(pl.is_etf("sh688981"))


class TestLimitPrice(unittest.TestCase):
    def test_round_cent(self):
        self.assertEqual(pl.round_cent(8.547), 8.55)
        self.assertEqual(pl.round_cent(6.993), 6.99)
        self.assertEqual(pl.round_cent(10.005), 10.01)
        self.assertEqual(pl.round_cent(10.0), 10.0)

    def test_limit_prices(self):
        up, down = pl.limit_prices(7.77, 0.10)
        self.assertEqual(up, 8.55)
        self.assertEqual(down, 6.99)
        up, down = pl.limit_prices(10.0, 0.20)
        self.assertEqual((up, down), (12.0, 8.0))


class TestFillBlock(unittest.TestCase):
    def test_buy_blocked_at_limit_up_open(self):
        # 主板昨日收盘 10 元, 今日开盘 11 元即涨停价
        reason = pl.fill_block_reason(bar(11, 11, 11, 11), 10.0, "sh600000",
                                      "2026-11-18", "buy", strict_open=True)
        self.assertEqual(reason, "limit_up")

    def test_buy_allowed_below_limit(self):
        reason = pl.fill_block_reason(bar(10.8, 11.0, 10.5, 10.9), 10.0, "sh600000",
                                      "2026-11-18", "buy", strict_open=True)
        self.assertIsNone(reason)

    def test_gem_uses_20pct(self):
        # 创业板 10% 不是涨停, 20% 才是
        self.assertIsNone(pl.fill_block_reason(bar(11.0, 11.2, 10.9, 11.0), 10.0,
                                               "sz300750", "2026-11-18", "buy"))
        self.assertEqual(pl.fill_block_reason(bar(12.0, 12.0, 12.0, 12.0), 10.0,
                                              "sz300750", "2026-11-18", "buy"),
                         "limit_up")

    def test_sell_blocked_at_limit_down_open(self):
        reason = pl.fill_block_reason(bar(9.0, 9.2, 9.0, 9.1), 10.0, "sh600000",
                                      "2026-11-18", "sell", strict_open=True)
        self.assertEqual(reason, "limit_down")

    def test_one_price_mode_only_blocks_sealed(self):
        # 开盘触板但随后打开: one_price 模式下允许成交
        self.assertIsNone(pl.fill_block_reason(bar(11.0, 11.0, 10.5, 10.8), 10.0,
                                               "sh600000", "2026-11-18", "buy",
                                               strict_open=False))
        self.assertEqual(pl.fill_block_reason(bar(11.0, 11.0, 11.0, 11.0), 10.0,
                                              "sh600000", "2026-11-18", "buy",
                                              strict_open=False), "limit_up")

    def test_missing_prev_close_is_not_blocked(self):
        self.assertIsNone(pl.fill_block_reason(bar(11, 11, 11, 11), None,
                                               "sh600000", "2026-11-18", "buy"))


class TestLimitUpFactor(unittest.TestCase):
    def _frame(self, end_close=12.0):
        import pandas as pd
        n = 130
        close = [10.0] * (n - 1) + [end_close]
        return pd.DataFrame({
            "date": pd.bdate_range("2026-01-05", periods=n),
            "open": close, "high": close, "low": close, "close": close,
            "volume": 1_000_000, "turnover": 0.01,
            "outstanding_share": 1e8, "amount": 1e8,
        })

    def test_board_aware_limit_up_flag(self):
        import backtest_engine as be
        # +12%: 主板算涨停, 创业板不算
        main = be.add_factors(self._frame(11.2), code="sh600000")
        gem = be.add_factors(self._frame(11.2), code="sz300750")
        # +20%: 创业板才算涨停
        gem_20 = be.add_factors(self._frame(12.0), code="sz300750")
        legacy = be.add_factors(self._frame(11.2))
        self.assertEqual(int(main["limit_up"].iloc[-1]), 1)
        self.assertEqual(int(gem["limit_up"].iloc[-1]), 0)
        self.assertEqual(int(gem_20["limit_up"].iloc[-1]), 1)
        self.assertEqual(int(legacy["limit_up"].iloc[-1]), 1)


if __name__ == "__main__":
    unittest.main()
