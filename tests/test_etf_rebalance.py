"""ETF 调仓回测引擎回归测试（纯合成数据，离线可跑）

覆盖:
  1. 10 年数据下 validate_result 全部通过
  2. 不足 10 年时年限校验失败
  3. 初始仓位 ≈ weight_fraction
  4. amount_20 排序选中成交额最大的 top_n
  5. pe_ttm 不可用 → 自动回退 momentum 且报告原因
  6. 暴涨后触发 4% 偏差调仓, 卖出后权重回到目标附近
  7. 全程无止损类交易
  8. 年度换股次数 ≈ 年数
  9. 收益率曲线完整、升序、无 NaN
 10. list_metrics 覆盖关键指标

运行: py -3.13 -m pytest tests/test_etf_rebalance.py -q
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

import etf_rebalance as er  # noqa: E402

# 外盘 ETF 名称映射（测试中 mock 掉数据库依赖）
NAMES = {
    "sh513100": "国泰纳斯达克100ETF",
    "sh513500": "博时标普500ETF",
    "sh513030": "华安德国30ETF",
    "sh513520": "华夏野村日经225ETF",
    "sz159941": "广发纳斯达克100ETF",
}

CODES = list(NAMES)
# 成交额从大到小: 513100 > 513500 > 513030 > 513520 > 159941
AMOUNTS = {
    "sh513100": 5e8,
    "sh513500": 3e8,
    "sh513030": 2e8,
    "sh513520": 1e8,
    "sz159941": 4e7,
}
DRIFTS = {
    "sh513100": 0.30,
    "sh513500": 0.18,
    "sh513030": 0.10,
    "sh513520": 0.05,
    "sz159941": -0.02,
}
BASES = {
    "sh513100": 2.0,
    "sh513500": 3.0,
    "sh513030": 1.2,
    "sh513520": 1.6,
    "sz159941": 0.9,
}


def build_feat(dates, closes, amounts=None, shares=None):
    """按合成收盘价构造 feat（补齐引擎所需因子列）"""
    amounts = amounts or {}
    shares = shares or {}
    feat = {}
    for code, close in closes.items():
        close = np.asarray(close, dtype=float)
        n = len(dates)
        amt = float(amounts.get(code, 5e8))
        share = float(shares.get(code, 1e9))
        df = pd.DataFrame({
            "date": dates,
            "open": close,
            "high": close * 1.002,
            "low": close * 0.998,
            "close": close,
            "volume": np.full(n, 1e6),
            "amount": np.full(n, amt),
            "outstanding_share": np.full(n, share),
        })
        df["pct"] = df["close"].pct_change() * 100
        for k in (5, 20, 60, 120, 250):
            df[f"ret_{k}"] = df["close"].pct_change(k) * 100
        df["amount_ma20"] = df["amount"].rolling(20).mean()
        df["cap_yi"] = df["close"] * df["outstanding_share"] / 1e8
        roll_max = df["close"].rolling(250).max()
        df["max_dd_250"] = (df["close"] - roll_max) / roll_max
        feat[code] = df
    return feat


def make_trend_feat(n_days=3200, start="2014-12-01"):
    """走势明显分化的 5 只外盘 ETF，覆盖 10 年以上"""
    dates = pd.bdate_range(start, periods=n_days)
    t = np.arange(n_days, dtype=float)
    closes = {}
    for i, code in enumerate(CODES):
        base, drift = BASES[code], DRIFTS[code]
        wig = 1 + 0.03 * np.sin(t / 23.0 + i) + 0.012 * np.sin(t / 7.0 + i * 2)
        closes[code] = base * np.exp(drift * t / 252.0) * wig
    return build_feat(dates, closes, AMOUNTS)


class EtfRebalanceTest(unittest.TestCase):

    def setUp(self):
        self._p_name = mock.patch.object(er, "_etf_name_map", return_value=NAMES)
        self._p_prem = mock.patch.object(er, "_load_premium_map", return_value={})
        self._p_name.start()
        self._p_prem.start()

    def tearDown(self):
        self._p_name.stop()
        self._p_prem.stop()

    # ---------- 1 & 9: 10 年完整回测 ----------
    def test_ten_years_validate_all_pass(self):
        feat = make_trend_feat()
        params = dict(start="2016-01-04", end="2026-11-27", top_n=3)
        r = er.run_etf_rebalance(feat, params)

        self.assertGreater(len(r["curve"]), 2000)
        checks = er.validate_result(r, params)
        failed = [c for c in checks if not c["passed"]]
        self.assertEqual(failed, [], f"存在未通过校验项: {failed}")
        self.assertGreaterEqual(r["meta"]["years"], 10.0)
        self.assertEqual(r["meta"]["universe_size"], 5)

    def test_curve_shape_and_monotonic_dates(self):
        feat = make_trend_feat()
        r = er.run_etf_rebalance(feat, dict(start="2016-01-04", end="2026-11-27"))
        curve = r["curve"]
        self.assertGreater(len(curve), 2000)
        self.assertFalse(curve["value"].isna().any())
        self.assertTrue(curve["date"].is_monotonic_increasing)
        self.assertTrue(pd.api.types.is_datetime64_any_dtype(curve["date"]))

    # ---------- 2: 年限不足 ----------
    def test_short_period_fails_years_check(self):
        feat = make_trend_feat()
        params = dict(start="2024-01-02", end="2026-01-02")
        r = er.run_etf_rebalance(feat, params)
        checks = er.validate_result(r, params)
        year_check = [c for c in checks if "年限" in c["name"]]
        self.assertEqual(len(year_check), 1)
        self.assertFalse(year_check[0]["passed"])

    # ---------- 3: 初始仓位 ----------
    def test_initial_position_ratio(self):
        feat = make_trend_feat()
        params = dict(start="2016-01-04", end="2026-11-27", top_n=3,
                      weight_fraction=0.75)
        r = er.run_etf_rebalance(feat, params)
        ratio = r["meta"]["initial_position_ratio"]
        self.assertLess(abs(ratio - 0.75), 0.03)

        # 用持仓明细交叉验证首日仓位
        first_date = r["holdings_log"][0]["date"]
        rows = [h for h in r["holdings_log"] if h["date"] == first_date]
        tv = rows[0]["total_value"]
        mv = sum(h["market_value"] for h in rows)
        self.assertLess(abs(mv / tv - 0.75), 0.03)

    # ---------- 4: amount_20 排序 ----------
    def test_amount_metric_selects_largest(self):
        feat = make_trend_feat()
        params = dict(start="2015-06-01", end="2017-06-30", top_n=2,
                      rank_metric="amount_20", min_history=60)
        r = er.run_etf_rebalance(feat, params)
        self.assertEqual(r["meta"]["rank_metric_effective"], "amount_20")
        entry_buys = [t for t in r["trades"] if t["reason"] == "建仓"]
        self.assertEqual({t["code"] for t in entry_buys},
                         {"sh513100", "sh513500"})

    # ---------- 5: 不可用指标回退 ----------
    def test_unavailable_metric_falls_back(self):
        feat = make_trend_feat()
        params = dict(start="2016-01-04", end="2026-11-27", rank_metric="pe_ttm")
        r = er.run_etf_rebalance(feat, params)
        self.assertEqual(r["meta"]["rank_metric_effective"], "momentum")
        self.assertTrue(r["meta"]["rank_metric_fallback_reason"])

    # ---------- 6: 4% 偏差调仓 ----------
    def test_deviation_trade_after_surge(self):
        dates = pd.bdate_range("2019-01-01", periods=400)
        flat = np.ones(400)
        surge = flat.copy()
        surge[300:] *= 1.30          # 第 300 个交易日暴涨 30% (>10%)
        closes = {"sh513100": surge, "sh513500": flat}
        feat = build_feat(dates, closes,
                          amounts={"sh513100": 5e8, "sh513500": 5e8})
        params = dict(start=str(dates[0].date()), end=str(dates[-1].date()),
                      top_n=2, weight_fraction=0.75, min_history=40,
                      rank_check_freq="off", rebalance_period="off")
        r = er.run_etf_rebalance(feat, params)

        dev_trades = [t for t in r["trades"]
                      if "4%" in t["reason"] and t["code"] == "sh513100"]
        self.assertTrue(dev_trades, "未产生 4% 偏差调仓")

        t = dev_trades[0]
        rows = [h for h in r["holdings_log"]
                if h["date"] == t["date"] and h["code"] == "sh513100"]
        self.assertTrue(rows)
        # top_n=2, weight_fraction=0.75 → 目标权重 0.375
        self.assertLess(abs(rows[-1]["weight"] - 0.375), 0.02)

    # ---------- 7: 无止损 ----------
    def test_no_stop_loss_trades(self):
        feat = make_trend_feat()
        r = er.run_etf_rebalance(feat, dict(start="2016-01-04", end="2026-11-27"))
        self.assertTrue(r["trades"])
        for t in r["trades"]:
            self.assertNotIn("止损", t["reason"])

    # ---------- 8: 年度换股 ----------
    def test_annual_swaps(self):
        feat = make_trend_feat()
        params = dict(start="2021-01-04", end="2023-12-29", top_n=2,
                      rank_check_freq="yearly")
        r = er.run_etf_rebalance(feat, params)
        self.assertGreaterEqual(r["meta"]["annual_swaps"], 2)

    # ---------- 10: 指标注册表 ----------
    def test_list_metrics(self):
        keys = {k for k, _ in er.list_metrics()}
        for k in ("momentum", "amount_20", "scale", "premium", "pe_ttm"):
            self.assertIn(k, keys)
        # 每个 spec 必备字段
        for k, spec in er.METRIC_SPECS.items():
            for field in ("label", "higher_better", "available", "note"):
                self.assertIn(field, spec)


if __name__ == "__main__":
    unittest.main()
