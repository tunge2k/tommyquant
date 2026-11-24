# -*- coding: utf-8 -*-
"""ETF 风控因子测试: 溢价过滤 / 资金流过滤 / 市场资金流风控

运行: py -3.13 -m unittest discover -s tests -p "test_etf_risk_factors.py" -v
"""
import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backtest_engine as be      # noqa: E402
import data_store                 # noqa: E402


def _snap():
    """3 只 ETF 的 dict 快照 (足够的成交额与动量, 均非 MA30 拦截)"""
    return {
        "sh510300": {"amount_ma20": 8e8, "ret_60": 0.20},
        "sh513070": {"amount_ma20": 5e8, "ret_60": 0.30},
        "sh515650": {"amount_ma20": 6e8, "ret_60": 0.10},
    }


def _filter(**kw):
    args = dict(mom_days=60, min_amount=0.5, min_history=0, ma30_up_only=False)
    args.update(kw)
    return be._filter_etf_universe(_snap(), feat=None, date="2026-11-18", **args)


class TestPremiumFilter(unittest.TestCase):
    def test_high_premium_excluded(self):
        got = _filter(premium_cap=1.0, premium_map={"sh513070": 2.5})
        self.assertNotIn("sh513070", got)
        self.assertIn("sh510300", got)

    def test_negative_premium_kept(self):
        got = _filter(premium_cap=1.0, premium_map={"sh510300": -0.3})
        self.assertIn("sh510300", got)

    def test_missing_data_not_filtered(self):
        got = _filter(premium_cap=1.0, premium_map={})
        self.assertEqual(len(got), 3)

    def test_cap_zero_disables(self):
        got = _filter(premium_cap=0.0, premium_map={"sh513070": 9.9})
        self.assertIn("sh513070", got)


class TestFlowFilter(unittest.TestCase):
    def test_redemption_excluded(self):
        got = _filter(flow_min=-3.0, flow_map={"sh515650": -5.0})
        self.assertNotIn("sh515650", got)
        self.assertIn("sh513070", got)

    def test_inflow_kept(self):
        got = _filter(flow_min=-3.0, flow_map={"sh510300": 2.0})
        self.assertIn("sh510300", got)

    def test_missing_flow_not_filtered(self):
        got = _filter(flow_min=-3.0, flow_map={})
        self.assertEqual(len(got), 3)


class TestMarketGuard(unittest.TestCase):
    def test_market_off_returns_empty(self):
        self.assertEqual(_filter(market_ok=False), [])

    def test_market_unknown_no_block(self):
        self.assertEqual(len(_filter(market_ok=None)), 3)


class TestFactorLoading(unittest.TestCase):
    def test_load_maps(self):
        tmp = os.path.join(tempfile.gettempdir(), "_etf_factor_probe.db")
        if os.path.exists(tmp):
            os.remove(tmp)
        old = data_store.DB_PATH
        data_store.DB_PATH = tmp
        try:
            data_store.init_db()
            data_store.upsert_etf_quote_ext("sh510300", [
                {"date": "2026-11-18", "premium_ratio": 0.52}])
            data_store.upsert_etf_capital("sh510300", [
                {"date": "2026-11-18", "near5_netbuy": 5e8, "market_scale": 1e10}])
            data_store.upsert_etf_market_flow([
                {"date": "2026-11-18", "category": "stock", "net_value": 1e10}])
            m = data_store.load_etf_factor_maps()
            self.assertAlmostEqual(m["premium"]["2026-11-18"]["sh510300"], 0.52)
            # 5e8 / 1e10 * 100 = 5%
            self.assertAlmostEqual(m["flow"]["2026-11-18"]["sh510300"], 5.0)
            self.assertAlmostEqual(m["market"]["2026-11-18"]["stock"], 100.0)
        finally:
            data_store.DB_PATH = old
            if os.path.exists(tmp):
                os.remove(tmp)

    def test_strategy_kwargs_without_data_keeps_old_behavior(self):
        """无增强数据时, 选品结果与接入前一致 (不变式)"""
        st = be.MarketCapStrategy(top_n=2, pool_type="etf",
                                  etf_premium_cap=1.0, etf_flow_min=-3.0)
        st.feat = {}
        st._etf_ext = {"premium": {}, "flow": {}, "market": {}}
        kw = st._selection_kwargs("2026-11-18")
        self.assertIsNone(kw.get("premium_map"))
        self.assertIsNone(kw.get("flow_map"))
        # 本用例只验证「溢价/资金流缺数据时不改变选品」, 与选品范围(仅外盘)无关
        kw.pop("overseas_only", None)
        got = be._filter_market_cap_universe(_snap(), **kw)
        self.assertEqual(len(got), 3)


class TestUniverseRegion(unittest.TestCase):
    """选品范围: 只做跨境(外盘)ETF, 剔除港股系"""

    def test_overseas_kept(self):
        for name in ("纳指ETF广发", "标普500ETF华夏", "日经225ETF华安",
                     "德国ETF华安", "东南亚科技ETF华泰柏瑞", "美国50ETF易方达"):
            self.assertTrue(be.is_overseas_etf(name), name)

    def test_hk_excluded(self):
        for name in ("恒生ETF华夏", "港股通互联网ETF富国", "H股ETF",
                     "香港科技ETF南方", "中概互联网ETF"):
            self.assertFalse(be.is_overseas_etf(name), name)
            self.assertEqual(be.classify_etf_region(name), "hk", name)

    def test_domestic_index_with_overseas_brand_excluded(self):
        # 标普A股红利 / MSCI中国A股 是境内指数, 不能当外盘
        for name in ("标普A股红利ETF华宝", "MSCI中国A股ETF摩根", "MSCI中国ETF招商"):
            self.assertFalse(be.is_overseas_etf(name), name)

    def test_plain_domestic_excluded(self):
        self.assertFalse(be.is_overseas_etf("沪深300ETF华泰柏瑞"))
        self.assertFalse(be.is_overseas_etf("黄金ETF华安"))


class TestEtfDividendAdjust(unittest.TestCase):
    """ETF 分红后复权: 除息日的价格缺口必须被抹平"""

    def _frame(self, closes, start="2026-01-05"):
        import pandas as pd
        dates = pd.bdate_range(start, periods=len(closes))
        c = np.array(closes, dtype=float)
        return pd.DataFrame({"date": dates, "open": c, "high": c * 1.01,
                             "low": c * 0.99, "close": c})

    def test_ex_date_gap_removed(self):
        # 除息日每份分红 0.10 元, 价格 2.00 -> 1.90 (看似 -5%)
        df = self._frame([2.00, 2.00, 1.90, 1.90, 1.90])
        out = be._adjust_fund_dividends(df, [("2026-01-07", 0.10)])
        pct = out["close"].pct_change().to_numpy()
        self.assertAlmostEqual(pct[2], 0.0, places=6)

    def test_no_events_untouched(self):
        df = self._frame([2.00, 1.90, 1.90])
        out = be._adjust_fund_dividends(df, [])
        self.assertTrue(np.allclose(out["close"], df["close"]))

    def test_history_before_ex_date_unchanged(self):
        df = self._frame([2.00, 2.00, 1.90, 1.90])
        out = be._adjust_fund_dividends(df, [("2026-01-07", 0.10)])
        self.assertAlmostEqual(out["close"].iloc[1], 2.00)   # 除息前的K线不动
        self.assertGreater(out["close"].iloc[3], 1.90)       # 除息后被放大


if __name__ == "__main__":
    unittest.main(verbosity=2)
