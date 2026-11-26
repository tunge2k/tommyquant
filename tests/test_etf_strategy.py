"""ETF 策略回归测试

覆盖:
  1. ETF 池按动量降序排名, 且只保留场内基金
  2. 成交额流动性门槛 / 数据长度门槛生效
  3. 股票池显式排除 ETF (cap_yi=0 不会再混进小市值排序)
  4. MarketCapStrategy(pool_type="etf") 的选股参数不加载财务/分红上下文
  5. ETF 代码归一化 (东财/新浪两种写法)

运行: py -3.13 -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from unittest import mock

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import backtest_engine as be  # noqa: E402
import data_store  # noqa: E402
from backtest_engine import BarIndex, MarketCapStrategy  # noqa: E402


def _make_feat(n=160):
    """构造 3 只标的的日线因子: 高动量 ETF / 低动量 ETF / 高动量股票"""
    dates = pd.bdate_range("2024-01-02", periods=n)

    def frame(daily_ret, amount):
        close = 1.0 * np.cumprod(np.full(n, 1.0 + daily_ret))
        return pd.DataFrame({
            "date": dates, "open": close, "high": close * 1.01,
            "low": close * 0.99, "close": close,
            "volume": amount / close, "amount": amount,
            "turnover": 0.01, "outstanding_share": 1e9,
        })

    feat = {}
    for code, ret, amt in (("sh510300", 0.0010, 5e8),    # 60日涨幅约 +6%
                           ("sz159915", 0.0030, 2e8),    # 60日涨幅约 +20%
                           ("sh600000", 0.0040, 9e8)):   # 股票, 不应进 ETF 池
        feat[code] = be.add_factors(frame(ret, amt), code=code)
    return feat, dates[-1]


class TestEtfFilters(unittest.TestCase):
    def test_dict_path_ranks_by_momentum(self):
        snap = {
            "sh510300": {"amount_ma20": 5e8, "ret_60": 6.0},
            "sz159915": {"amount_ma20": 2e8, "ret_60": 20.0},
            "sh588000": {"amount_ma20": 1e7, "ret_60": 40.0},   # 成交额不足
            "sh600000": {"amount_ma20": 9e8, "ret_60": 99.0},   # 股票
        }
        out = be._filter_etf_universe(snap, mom_days=60, min_amount=0.5,
                                      ma30_up_only=False)
        self.assertEqual(out, ["sz159915", "sh510300"])

    def test_dict_path_min_history(self):
        feat = {"sh510300": pd.DataFrame({"date": pd.bdate_range("2024-01-02", periods=50)})}
        snap = {"sh510300": {"amount_ma20": 5e8, "ret_60": 10.0}}
        out = be._filter_etf_universe(snap, feat=feat, date="2024-03-15",
                                      min_amount=0.5, min_history=120,
                                      ma30_up_only=False)
        self.assertEqual(out, [])

    def test_crosssection_path(self):
        feat, last = _make_feat()
        cs = BarIndex(feat).snapshot(last)
        out = be._filter_etf_universe(cs, feat=feat, date=last, mom_days=60,
                                      min_amount=0.5, min_history=120,
                                      ma30_up_only=False)
        self.assertEqual(out, ["sz159915", "sh510300"])

    def test_crosssection_allows_insufficient_ma30_history(self):
        feat, last = _make_feat(n=160)  # 满足120根日K, 但不足35根周K
        cs = BarIndex(feat).snapshot(last)
        cache = be.build_ma30_cache(feat, 5)
        out = be._filter_etf_universe(
            cs, feat=feat, date=last, min_amount=0.5, min_history=120,
            ma30_up_only=True, ma30_entry_weeks=2, ma30_cache=cache)
        self.assertEqual(out, ["sz159915", "sh510300"])

    def test_multi_period_momentum_prefers_long_term_strength(self):
        snap = {
            "sh510300": {"amount_ma20": 5e8, "ret_60": 30.0,
                         "ret_120": 5.0, "ret_250": 5.0},
            "sh513100": {"amount_ma20": 5e8, "ret_60": 10.0,
                         "ret_120": 20.0, "ret_250": 30.0},
        }
        out = be._filter_etf_universe(snap, mom_days=60, min_amount=0.5,
                                      ma30_up_only=False)
        self.assertEqual(out, ["sh513100", "sh510300"])

    def test_stopped_etf_can_reenter_without_new_entry_gates(self):
        snap = {
            "sh513100": {"amount_ma20": 5e8, "ret_20": -5.0, "ret_60": -10.0,
                         "above_ma60": 0, "ma60_slope": -0.01, "max_dd_250": -0.4},
            "sh513500": {"amount_ma20": 5e8, "ret_20": 5.0, "ret_60": 8.0,
                         "above_ma60": 1, "ma60_slope": 0.01, "max_dd_250": -0.1},
        }
        cache = {"sh513100": object(), "sh513500": object()}
        with (mock.patch.object(be, "ma30_slope_up", return_value=True),
              mock.patch.object(be, "ma30_rising_weeks", return_value=False),
              mock.patch.object(be, "_entry_confirm_ok", return_value=False)):
            out = be._filter_etf_universe(
                snap, date="2026-11-26", ma30_cache=cache, ma30_entry_weeks=2,
                reentry_codes={"sh513100"}, min_amount=0.5)
        self.assertEqual(out, ["sh513100"])

    def test_stock_pool_excludes_etf(self):
        snap = {
            "sh510300": {"cap_yi": 10.0},
            "sh600000": {"cap_yi": 20.0},
        }
        out = be._filter_market_cap_universe(
            snap, feat=None, date=None, cap_min=0, cap_max=1e6,
            min_listed_years=0, exclude_st=False, exclude_ocf_diverge=False,
            exclude_nonop_profit=False, exclude_no_dividend=False,
            ma30_up_only=False)
        self.assertEqual(out, ["sh600000"])

    def test_stock_crosssection_path_excludes_etf(self):
        feat, last = _make_feat()
        cs = BarIndex(feat).snapshot(last)
        out = be._filter_market_cap_universe(
            cs, feat=feat, date=last, cap_min=0, cap_max=1e6,
            min_listed_years=0, exclude_st=False, exclude_ocf_diverge=False,
            exclude_nonop_profit=False, exclude_no_dividend=False,
            ma30_up_only=False)
        self.assertIn("sh600000", out)
        self.assertNotIn("sh510300", out)
        self.assertNotIn("sz159915", out)


class TestEtfStrategyParams(unittest.TestCase):
    def test_selection_kwargs_skip_fundamentals(self):
        s = MarketCapStrategy(top_n=3, pool_type="etf", etf_mom_days=20,
                              etf_min_amount=1.5, etf_min_history=200)
        kw = s._selection_kwargs("2024-06-03")
        self.assertEqual(kw["pool_type"], "etf")
        self.assertEqual(kw["etf_mom_days"], 20)
        self.assertEqual(kw["etf_min_amount"], 1.5)
        self.assertEqual(kw["etf_min_history"], 200)
        self.assertNotIn("f_map", kw)
        self.assertNotIn("div_map", kw)

    def test_stock_pool_type_default(self):
        s = MarketCapStrategy(top_n=3)
        self.assertEqual(s.pool_type, "stock")
        kw = s._selection_kwargs("2024-06-03")
        self.assertIn("cap_min", kw)
        self.assertNotIn("etf_mom_days", kw)


class TestEtfSymbolNorm(unittest.TestCase):
    def test_norm(self):
        self.assertEqual(data_store._to_etf_symbol("510300"), "sh510300")
        self.assertEqual(data_store._to_etf_symbol("sh510300"), "sh510300")
        self.assertEqual(data_store._to_etf_symbol("159915"), "sz159915")
        self.assertEqual(data_store._to_etf_symbol("sz159915"), "sz159915")
        self.assertIsNone(data_store._to_etf_symbol("600000"))
        self.assertIsNone(data_store._to_etf_symbol(""))


class TestFundSplitAdjust(unittest.TestCase):
    def test_split_is_back_adjusted(self):
        """1拆4: 4.0 → 1.0, 之前的 4.0 应回填为 1.0, 成交量反向放大"""
        n = 40
        close = [4.0] * (n - 1) + [1.0, 1.02, 1.01, 1.03]
        vol = [1e6] * (n - 1) + [4e6, 4e6, 4e6, 4e6]
        df = pd.DataFrame({"open": close, "high": close, "low": close,
                           "close": close, "volume": vol,
                           "amount": [4e6] * (n - 1) + [4e6] * 4})
        out = be._adjust_fund_splits(df)
        self.assertAlmostEqual(out["close"].iloc[0], 1.0, places=6)
        self.assertAlmostEqual(out["close"].iloc[-1], 1.03, places=6)
        self.assertAlmostEqual(out["volume"].iloc[0], 4e6, places=0)
        # 调整后无 -70% 级别的跳变
        pct = out["close"].pct_change().dropna()
        self.assertLess(pct.abs().max(), 0.25)

    def test_no_split_untouched(self):
        close = [1.0 + i * 0.001 for i in range(30)]
        df = pd.DataFrame({"open": close, "high": close, "low": close,
                           "close": close, "volume": [1e6] * 30})
        out = be._adjust_fund_splits(df)
        self.assertAlmostEqual(out["close"].iloc[0], close[0], places=9)


if __name__ == "__main__":
    unittest.main()
