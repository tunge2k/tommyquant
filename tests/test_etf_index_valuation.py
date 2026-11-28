"""蛋卷指数估值接入测试

覆盖:
  1. ETF 名称 -> 跟踪指数映射 (精确/别名/不误判)
  2. PE/PB 百分位计算无未来函数
  3. 估值类指标覆盖率达标时生效, 缺失估值的标的不参与排序
  4. 覆盖率不足时回退动量

运行: py -3.13 -m pytest tests/test_etf_index_valuation.py -q
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

import data_manager as dm  # noqa: E402
import etf_rebalance as er  # noqa: E402

IDX = {
    "SH000300": "沪深300",
    "SZ399006": "创业板",
    "SZ399975": "证券公司",
    "SZ399989": "中证医疗",
    "NDX": "纳指100",
    "SP500": "标普500",
}

NAMES = {
    "sh513100": "国泰纳斯达克100ETF",
    "sh513500": "博时标普500ETF",
    "sh513030": "华安德国30ETF",
    "sz159941": "广发纳斯达克100ETF",
}


def build_feat(dates, closes, amount=5e8):
    feat = {}
    for code, close in closes.items():
        close = np.asarray(close, dtype=float)
        n = len(dates)
        df = pd.DataFrame({
            "date": dates, "open": close, "high": close * 1.002,
            "low": close * 0.998, "close": close,
            "volume": np.full(n, 1e6), "amount": np.full(n, amount),
            "outstanding_share": np.full(n, 1e9),
        })
        df["pct"] = df["close"].pct_change() * 100
        for k in (20, 60, 120, 250):
            df[f"ret_{k}"] = df["close"].pct_change(k) * 100
        df["amount_ma20"] = df["amount"].rolling(20).mean()
        df["cap_yi"] = df["close"] * df["outstanding_share"] / 1e8
        roll_max = df["close"].rolling(250).max()
        df["max_dd_250"] = (df["close"] - roll_max) / roll_max
        feat[code] = df
    return feat


class MappingTest(unittest.TestCase):

    def test_exact_index_name(self):
        self.assertEqual(dm.match_track_index("沪深300ETF华泰柏瑞", IDX)[0], "SH000300")
        self.assertEqual(dm.match_track_index("创业板ETF易方达", IDX)[0], "SZ399006")

    def test_alias_name(self):
        self.assertEqual(dm.match_track_index("券商ETF华宝", IDX)[0], "SZ399975")
        self.assertEqual(dm.match_track_index("纳指ETF国泰", IDX)[0], "NDX")
        self.assertEqual(dm.match_track_index("标普500ETF博时", IDX)[0], "SP500")

    def test_no_false_positive(self):
        """名称里带指数名但不是同一指数的 ETF 不能被误映射"""
        for nm in ("创业板软件ETF国泰", "医疗设备ETF大成", "沪深300红利ETF",
                   "日经225ETF华夏", "恒生医疗ETF博时"):
            self.assertIsNone(dm.match_track_index(nm, IDX), nm)


class ValuationAttachTest(unittest.TestCase):

    def test_percentile_has_no_lookahead(self):
        dates = pd.bdate_range("2016-01-01", periods=1600)
        cal = pd.DatetimeIndex(dates)
        vals = {str(d.date()): {"sh513100": float(i + 1)} for i, d in enumerate(dates)}

        sym = {"code": "sh513100"}
        er._attach_valuation(sym, {"pe": vals, "pb": {}}, cal)
        pct = sym["pe_pct"]
        self.assertTrue(np.isnan(pct.iloc[0]))
        self.assertGreater(float(pct.iloc[-1]), 0.99)

        # 截断未来数据后重算, 同一交易日的百分位必须完全一致
        cut = 700
        vals2 = {k: v for k, v in vals.items() if k <= str(dates[cut].date())}
        sym2 = {"code": "sh513100"}
        er._attach_valuation(sym2, {"pe": vals2, "pb": {}}, cal)
        self.assertAlmostEqual(float(pct.iloc[cut]),
                               float(sym2["pe_pct"].iloc[cut]), places=6)

    def test_missing_symbol_is_all_nan(self):
        dates = pd.bdate_range("2020-01-01", periods=400)
        cal = pd.DatetimeIndex(dates)
        vals = {str(d.date()): {"sh513100": 12.0} for d in dates}
        sym = {"code": "sz159941"}
        er._attach_valuation(sym, {"pe": vals, "pb": {}}, cal)
        self.assertTrue(sym["pe"].isna().all())
        self.assertTrue(sym["pe_pct"].isna().all())


class ValuationMetricTest(unittest.TestCase):

    def setUp(self):
        self._ps = [
            mock.patch.object(er, "_etf_name_map", return_value=NAMES),
            mock.patch.object(er, "_load_premium_map", return_value={}),
        ]
        for p in self._ps:
            p.start()

    def tearDown(self):
        for p in self._ps:
            p.stop()

    def _feat_and_vals(self):
        dates = pd.bdate_range("2016-01-04", periods=2700)
        closes = {c: 2.0 + 0.2 * i for i, c in enumerate(NAMES)}
        feat = build_feat(dates, closes)
        pe = {"sh513100": 30.0, "sh513500": 10.0, "sh513030": 20.0}
        vals = {"pe": {str(d.date()): dict(pe) for d in dates[::5]}, "pb": {}}
        return feat, vals

    def test_pe_metric_used_when_coverage_enough(self):
        feat, vals = self._feat_and_vals()
        with mock.patch.object(er, "_load_valuation_maps", return_value=vals):
            r = er.run_etf_rebalance(feat, dict(
                start=str(feat["sh513100"]["date"].iloc[0].date()),
                end="2026-01-30", top_n=1, rank_metric="pe_ttm"))
        self.assertEqual(r["meta"]["rank_metric_effective"], "pe_ttm")
        self.assertEqual(r["meta"]["rank_metric_fallback_reason"], "")
        buys = [t for t in r["trades"] if t["reason"] == "建仓"]
        self.assertTrue(buys)
        self.assertEqual(buys[0]["code"], "sh513500")   # PE 最低

    def test_fallback_when_no_valuation(self):
        feat, _ = self._feat_and_vals()
        with mock.patch.object(er, "_load_valuation_maps", return_value={}):
            r = er.run_etf_rebalance(feat, dict(rank_metric="pe_ttm", top_n=1))
        self.assertEqual(r["meta"]["rank_metric_effective"], "momentum")
        self.assertIn("40%", r["meta"]["rank_metric_fallback_reason"])


if __name__ == "__main__":
    unittest.main()
