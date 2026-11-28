"""ETF 调仓回测引擎回归测试（纯合成数据，离线可跑）

覆盖:
  1. 10 年数据下 validate_result 全部通过
  2. 不足 10 年时年限校验失败
  3. 初始仓位 ≈ weight_fraction
  4. amount_20 排序选中成交额最大的 top_n
  5. 无估值数据时 pe_ttm 覆盖率不足 → 自动回退 momentum 且报告原因
  6. 暴涨后触发 4% 偏差调仓, 卖出后持仓市值回到目标中枢附近
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
        # 估值因子必须 mock: 否则会读本地库, 结果随数据同步情况变化
        self._p_val = mock.patch.object(er, "_load_valuation_maps", return_value={})
        self._p_name.start()
        self._p_prem.start()
        self._p_val.start()

    def tearDown(self):
        self._p_name.stop()
        self._p_prem.stop()
        self._p_val.stop()

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

    def test_default_target_position_is_90pct(self):
        """默认目标仓位 90%（2026-11-28：85/90/95% 对照后取 90%）"""
        self.assertAlmostEqual(er.default_params()["weight_fraction"], 0.90, places=4)
        r = er.run_etf_rebalance(make_trend_feat(),
                                 dict(start="2016-01-04", end="2026-11-27"))
        self.assertLess(abs(float(r["meta"]["initial_position_ratio"]) - 0.90), 0.03)

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
                      top_n=2, weight_fraction=0.75, deviation_threshold=0.04,
                      min_history=40,
                      rank_check_freq="off", rebalance_period="off")
        r = er.run_etf_rebalance(feat, params)

        dev_trades = [t for t in r["trades"]
                      if "4%" in t["reason"] and t["code"] == "sh513100"]
        self.assertTrue(dev_trades, "未产生 4% 偏差调仓")

        t = dev_trades[0]
        rows = [h for h in r["holdings_log"]
                if h["date"] == t["date"] and h["code"] == "sh513100"]
        self.assertTrue(rows)
        # top_n=2, weight_fraction=0.75, 初始 100 万 → 每只市值中枢 37.5 万
        # 调仓后持仓市值应回到中枢附近(±2%)
        center = 1_000_000 * 0.75 / 2
        self.assertLess(abs(rows[-1]["market_value"] - center) / center, 0.02)

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

    # ---------- 11: 份额拆分复权 ----------
    def test_split_adjusted_no_fake_deviation(self):
        """4:1 拆分的单日 -75% 不应被当成暴跌"""
        dates = pd.bdate_range("2019-01-01", periods=400)
        px = np.full(400, 2.0)
        px[300:] = 0.5               # 4:1 拆分, 原始序列单日 -75%
        feat = build_feat(dates, {"sh513100": px},
                          amounts={"sh513100": 5e8})
        params = dict(start=str(dates[0].date()), end=str(dates[-1].date()),
                      top_n=1, weight_fraction=0.75, deviation_threshold=0.04,
                      min_history=40,
                      rank_check_freq="off", rebalance_period="off")
        r = er.run_etf_rebalance(feat, params)
        dev = [t for t in r["trades"] if "4%" in t["reason"]]
        self.assertTrue(all(abs(t["dev_before"]) < 0.15 for t in dev),
                        f"拆分产生了极端偏差调仓: {dev}")
        curve = r["curve"]
        peak = curve["value"].cummax()
        self.assertGreater((curve["value"] / peak).min(), 0.9,
                           "拆分造成了假净值崩溃")

    # ---------- 12: 定期再平衡按「占总资产比例」判定 ----------
    def _run_periodic(self, closes, n_days=460):
        dates = pd.bdate_range("2019-01-01", periods=n_days)
        feat = build_feat(dates, closes, amounts={c: 5e8 for c in closes})
        params = dict(start=str(dates[0].date()), end=str(dates[-1].date()),
                      top_n=1, weight_fraction=0.75, deviation_threshold=0.04,
                      min_history=40, cash_guard_enabled=False,
                      rank_check_freq="off", rebalance_period="1y")
        return er.run_etf_rebalance(feat, params)

    def _ratio_after_trade(self, r, date):
        rows = [h for h in r["holdings_log"] if h["date"] == date]
        self.assertTrue(rows)
        mv = sum(h["market_value"] for h in rows)
        return mv / rows[0]["total_value"]

    def test_periodic_buys_when_total_assets_grow(self):
        """总资产增长(标的上涨 + 4%网格兑现)后, 持仓占比不足 → 应买入"""
        n = 460
        px = np.concatenate([np.ones(60), 1.0 + 0.0012 * np.arange(n - 60)])
        r = self._run_periodic({"sh513100": px})
        reb = [t for t in r["trades"] if t["reason"] == "定期再平衡"]
        self.assertTrue(reb, "应产生定期再平衡交易")
        self.assertTrue(all(t["direction"] == "buy" for t in reb),
                        f"总资产增长时定期再平衡应为买入, 实际: {reb}")
        for t in reb:
            self.assertLess(abs(self._ratio_after_trade(r, t["date"]) - 0.75), 0.03)

    def test_periodic_sells_when_position_overweight(self):
        """总资产下降(标的下跌 + 4%网格补仓)后, 持仓占比超配 → 应卖出"""
        n = 460
        px = np.concatenate([np.ones(60), 1.0 - 0.0008 * np.arange(n - 60)])
        r = self._run_periodic({"sh513100": px})
        reb = [t for t in r["trades"] if t["reason"] == "定期再平衡"]
        self.assertTrue(reb, "应产生定期再平衡交易")
        self.assertTrue(all(t["direction"] == "sell" for t in reb),
                        f"持仓超配时定期再平衡应为卖出, 实际: {reb}")
        for t in reb:
            self.assertLess(abs(self._ratio_after_trade(r, t["date"]) - 0.75), 0.03)

    def test_two_rules_no_conflict_on_rebalance_day(self):
        """再平衡日先按占比拉回 75% 并重置中枢, 当日 4% 网格不应再反向调仓"""
        n = 460
        px = np.concatenate([np.ones(60), 1.0 + 0.0012 * np.arange(n - 60)])
        r = self._run_periodic({"sh513100": px})
        reb_dates = {t["date"] for t in r["trades"] if t["reason"] == "定期再平衡"}
        self.assertTrue(reb_dates)
        for d in reb_dates:
            dev_same_day = [t for t in r["trades"]
                            if t["date"] == d and "4%" in t["reason"]]
            self.assertEqual(dev_same_day, [], f"再平衡日不应再有 4% 调仓: {dev_same_day}")

    # ---------- 13: 换股周期按「实际持有时间」而非自然年月 ----------
    def test_rank_swap_anchored_to_holding_time(self):
        dates = pd.bdate_range("2019-01-15", periods=320)   # 起始日非月初/季初
        t = np.arange(len(dates), dtype=float)
        # 513500 先强后弱, 513100 前平后强 → 建仓时选 513500, 3 个月后应换成 513100
        px_a = np.where(t < 60, 1.0, 1.0 + 0.002 * (t - 60))
        px_b = np.where(t < 100, 1.0 + 0.002 * t, 1.2 - 0.002 * (t - 100))
        feat = build_feat(dates, {"sh513100": px_a, "sh513500": px_b},
                          amounts={"sh513100": 5e8, "sh513500": 3e8})
        params = dict(start=str(dates[0].date()), end=str(dates[-1].date()),
                      top_n=1, weight_fraction=0.75, min_history=40,
                      rank_check_freq="3m", rebalance_period="off")
        r = er.run_etf_rebalance(feat, params)
        swaps = [x for x in r["trades"] if "换股" in x["reason"]]
        self.assertTrue(swaps, "3 个月周期应产生换股")
        entry = pd.Timestamp(dates[39])          # 第 40 根 K 线建仓
        expected = entry + pd.DateOffset(months=3)
        first = pd.Timestamp(swaps[0]["date"])
        self.assertLessEqual(abs((first - expected).days), 5,
                             f"首次换股应在建仓后 3 个月({expected:%Y-%m-%d})，"
                             f"实际 {first:%Y-%m-%d}")

    # ---------- 14: 月度/季度/半年周期可用 ----------
    def test_supported_rank_periods(self):
        dates = pd.bdate_range("2019-01-02", periods=520)   # 约 2 年
        px = np.linspace(1.0, 1.25, len(dates))
        feat = build_feat(dates, {"sh513100": px}, amounts={"sh513100": 5e8})
        base = dict(start=str(dates[0].date()), end=str(dates[-1].date()),
                    top_n=1, weight_fraction=0.75, min_history=40,
                    rebalance_period="off")
        counts = {}
        for freq, expect in (("1m", 18), ("3m", 6), ("6m", 3)):
            r = er.run_etf_rebalance(feat, dict(base, rank_check_freq=freq))
            counts[freq] = r["meta"]["annual_swaps"]
            self.assertGreaterEqual(counts[freq], expect,
                                    f"{freq} 周期换股次数不足: {counts[freq]} < {expect}")
            chk = [c for c in er.validate_result(r, dict(base, rank_check_freq=freq))
                   if "换股" in c["name"]]
            self.assertTrue(chk and chk[0]["passed"], chk)
        self.assertGreater(counts["1m"], counts["3m"])
        self.assertGreater(counts["3m"], counts["6m"])

    # ---------- 15: 高目标仓位 + 最大回撤段的资金充足性 ----------
    def test_high_weight_cash_guard_in_drawdown(self):
        dates = pd.bdate_range("2019-01-01", periods=700)
        t = np.arange(700)
        # 前 60 天走平, 之后持续下跌约 55% (覆盖最大回撤段)
        px = np.where(t < 60, 1.0, np.maximum(0.45, 1.0 - 0.0016 * (t - 60)))
        feat = build_feat(dates, {"sh513100": px}, amounts={"sh513100": 5e8})
        params = dict(start=str(dates[0].date()), end=str(dates[-1].date()),
                      top_n=1, weight_fraction=0.95, min_history=40,
                      rank_check_freq="off", rebalance_period="off")
        r = er.run_etf_rebalance(feat, params)
        # 现金不允许为负 (买入必须受可用资金约束)
        self.assertGreaterEqual(float(r["curve"]["cash"].min()), -1e-6)
        fills = pd.DataFrame(r["fills"])
        if len(fills):
            self.assertGreaterEqual(float(fills["cash_after"].min()), -1e-6)
        # 95% 目标仓位: 初始仓位应接近 95%, 且资金不足跳过次数被如实统计
        self.assertLess(abs(float(r["meta"]["initial_position_ratio"]) - 0.95), 0.02)
        self.assertGreaterEqual(int(r["meta"]["cash_short_skips"]), 1,
                                "深跌期应出现资金不足跳过")

    # ---------- 10: 指标注册表 ----------
    def test_list_metrics(self):
        keys = {k for k, _ in er.list_metrics()}
        for k in ("momentum", "amount_20", "scale", "premium", "pe_ttm"):
            self.assertIn(k, keys)
        # 每个 spec 必备字段
        for k, spec in er.METRIC_SPECS.items():
            for field in ("label", "higher_better", "available", "note"):
                self.assertIn(field, spec)


class CashGuardTest(unittest.TestCase):
    """现金安全垫保护: 余额 < 倍数×单次调仓需求 且当日有卖出调仓 → 额外加卖"""

    def setUp(self):
        self._p_name = mock.patch.object(er, "_etf_name_map", return_value=NAMES)
        self._p_prem = mock.patch.object(er, "_load_premium_map", return_value={})
        # 估值因子必须 mock: 否则会读本地库, 结果随数据同步情况变化
        self._p_val = mock.patch.object(er, "_load_valuation_maps", return_value={})
        self._p_name.start()
        self._p_prem.start()
        self._p_val.start()

    def tearDown(self):
        self._p_name.stop()
        self._p_prem.stop()
        self._p_val.stop()

    def _run(self, rising=True, **over):
        n = 400
        dates = pd.bdate_range("2019-01-01", periods=n)
        t = np.arange(n) - 60
        step = 0.0015 if rising else -0.0012
        px = np.where(t < 0, 1.0, 1.0 + step * np.maximum(t, 0))
        feat = build_feat(dates, {"sh513100": px}, amounts={"sh513100": 5e8})
        params = dict(start=str(dates[0].date()), end=str(dates[-1].date()),
                      top_n=1, weight_fraction=0.95, min_history=40,
                      rank_check_freq="off", rebalance_period="off")
        params.update(over)
        r = er.run_etf_rebalance(feat, params)
        return r, params

    def test_extra_sell_when_cash_below_threshold(self):
        r, params = self._run()
        guard = r["meta"]["cash_guard"]
        self.assertTrue(guard["enabled"])
        log = r["meta"]["cash_guard_log"]
        self.assertTrue(log, "95% 仓位 + 上涨触发卖出时应记录现金保护")

        for e in log:
            # 触发前余额必须真的低于预警阈值
            self.assertLess(e["trigger_cash"], e["threshold"])
            # 实际卖出量 > 初始计划卖出量, 且额外量不超过上限
            self.assertGreater(e["extra_qty"], 0)
            self.assertEqual(e["actual_qty"], e["planned_qty"] + e["extra_qty"])
            self.assertLessEqual(e["extra_qty"],
                                 e["planned_qty"] * guard["max_extra"] + 1e-9)
            # 只补到预警线, 不过度卖出
            self.assertLessEqual(e["cash_after"], e["threshold"] + 1e-6)
            # 调整后实际仓位 < 原目标仓位, 且审计字段齐全
            self.assertLess(e["position_pct_after"], e["target_position"])
            for k in ("date", "trigger_cash", "planned_qty", "actual_qty",
                      "target_position", "position_pct_after", "cash_after"):
                self.assertIn(k, e)
        self.assertEqual(guard["triggers"], len(log))

        checks = er.validate_result(r, params)
        gchk = [c for c in checks if "现金保护" in c["name"]]
        self.assertTrue(gchk and gchk[0]["passed"], gchk)

    def test_zero_max_extra_blocks_extra_sell(self):
        r, params = self._run(cash_guard_max_extra=0.0)
        self.assertEqual(r["meta"]["cash_guard_log"], [])
        self.assertEqual(r["meta"]["cash_guard"]["triggers"], 0)
        gchk = [c for c in er.validate_result(r, params) if "现金保护" in c["name"]]
        self.assertTrue(gchk and gchk[0]["passed"], gchk)

    def test_disabled_guard_keeps_original_behavior(self):
        r = self._run(cash_guard_enabled=False)[0]
        self.assertFalse(r["meta"]["cash_guard"]["enabled"])
        self.assertEqual(r["meta"]["cash_guard_log"], [])

    def test_no_trigger_on_buy_only_days(self):
        """下跌日只有补仓买入, 不触发加卖 (保护仅作用于卖出调仓)"""
        r = self._run(rising=False)[0]
        self.assertEqual(r["meta"]["cash_guard_log"], [])
        self.assertGreaterEqual(int(r["meta"]["cash_short_skips"]), 1)


if __name__ == "__main__":
    unittest.main()
