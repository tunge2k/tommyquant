"""市值策略调仓回归测试

覆盖 2026-11-16 修复后的关键语义:
  1. 自身下跌 → 补仓; 自身上涨 → 减仓
  2. 只调整自身偏离的标的, 不因其他标的涨跌被连带卖出
  3. 先卖后买, 买单受可用现金约束, 现金不为负
  4. 100 股取整, 差异不足 100 股不下单
  5. 界面策略参数键名与函数签名一致 (P0-6 回归)

运行: py -3.13 -m unittest discover -s tests -v
"""
import inspect
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import backtest_engine as be  # noqa: E402
import data_store  # noqa: E402
from backtest_engine import MarketBarEvent, MarketCapStrategy  # noqa: E402

DATE = "2026-01-05"


class FakeBars:
    """按 (code, date) 返回行情行; 与 BarIndex.row 的最小契约一致"""

    def __init__(self, rows):
        self.rows = rows

    def row(self, code, date):
        return self.rows.get((code, str(date)[:10]))


class FakePortfolio:
    """只实现策略用到的接口: cash / positions / total_value"""

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


def make_strategy(**kw):
    params = dict(top_n=2, universe=["sh600000", "sz000001"], ma30_exit=False)
    params.update(kw)
    strat = MarketCapStrategy(**params)
    return strat


def orders_of(signal):
    return signal.rebalance_orders if signal else []


class RebalanceOrderTest(unittest.TestCase):
    def test_buy_on_self_drop_and_sell_on_self_rise(self):
        bars = FakeBars({
            ("sh600000", DATE): {"close": 10.0, "cap_yi": 80.0},
            ("sz000001", DATE): {"close": 10.0, "cap_yi": 120.0},
        })
        pf = FakePortfolio(cash=10000, positions={
            "sh600000": position(6000),   # 市值 6 万, 低于目标 7.5 万
            "sz000001": position(9000),   # 市值 9 万, 高于目标 7.5 万
        })
        strat = make_strategy()
        strat.portfolio = pf
        strat.bars = bars

        orders = strat._build_rebalance(DATE, {
            "sh600000": 75000.0,
            "sz000001": 75000.0,
        })

        # 先卖后买
        self.assertEqual([o.direction for o in orders], ["sell", "buy"])
        sell, buy = orders
        self.assertEqual(sell.code, "sz000001")
        self.assertEqual(buy.code, "sh600000")
        self.assertEqual(sell.quantity, 1500)
        self.assertEqual(buy.quantity, 1500)
        self.assertEqual(buy.quantity % 100, 0)

    def test_buy_limited_by_cash(self):
        bars = FakeBars({("sh600000", DATE): {"close": 10.0, "cap_yi": 80.0}})
        pf = FakePortfolio(cash=5000)
        strat = make_strategy(top_n=1)
        strat.portfolio = pf
        strat.bars = bars

        orders = strat._build_rebalance(DATE, {"sh600000": 100000.0})

        self.assertEqual(len(orders), 1)
        buy = orders[0]
        self.assertEqual(buy.direction, "buy")
        self.assertEqual(buy.quantity, 400)
        self.assertLessEqual(buy.quantity * 10.0 * 1.0015, 5000)

    def test_sell_proceeds_fund_buy_and_cash_not_negative(self):
        bars = FakeBars({
            ("sh600000", DATE): {"close": 10.0, "cap_yi": 80.0},
            ("sz000001", DATE): {"close": 10.0, "cap_yi": 120.0},
        })
        pf = FakePortfolio(cash=0, positions={"sh600000": position(10000)})
        strat = make_strategy()
        strat.portfolio = pf
        strat.bars = bars

        orders = strat._build_rebalance(DATE, {
            "sh600000": 5000.0,    # 卖出回款
            "sz000001": 30000.0,   # 用回款买入
        })

        self.assertEqual([o.direction for o in orders], ["sell", "buy"])
        sell, buy = orders
        proceeds = sell.quantity * 10.0 * 0.998
        self.assertLessEqual(buy.quantity * 10.0 * 1.0015, proceeds)
        self.assertGreater(buy.quantity, 0)

    def test_small_diff_below_one_lot_is_skipped(self):
        bars = FakeBars({("sh600000", DATE): {"close": 10.0, "cap_yi": 80.0}})
        pf = FakePortfolio(cash=100000, positions={"sh600000": position(100)})
        strat = make_strategy(top_n=1)
        strat.portfolio = pf
        strat.bars = bars

        orders = strat._build_rebalance(DATE, {"sh600000": 1900.0})
        self.assertEqual(orders, [])


class OnBarIsolationTest(unittest.TestCase):
    """单标的偏离不得连带触发其他标的的反向调仓"""

    def _run(self, price_a, cap_a, qty_a, price_b, cap_b, qty_b):
        bars = FakeBars({
            ("sh600000", DATE): {"close": price_a, "cap_yi": cap_a},
            ("sz000001", DATE): {"close": price_b, "cap_yi": cap_b},
        })
        pf = FakePortfolio(
            cash=20000,
            positions={"sh600000": position(qty_a), "sz000001": position(qty_b)},
            total_value=200000,
        )
        strat = make_strategy()
        strat.on_init({"portfolio": pf, "bars": bars, "feat": {}})
        strat._target_weights = {"sh600000": 0.5, "sz000001": 0.5}
        strat._entry_cap_ratios = {"sh600000": 100.0, "sz000001": 100.0}
        strat._entry_prices = {"sh600000": price_a, "sz000001": price_b}

        signal = strat.on_bar(MarketBarEvent(
            DATE, {"sh600000": {}, "sz000001": {}}, bars))
        return orders_of(signal)

    def test_drop_buys_only_that_symbol(self):
        # A 下跌 10% → 补仓; B 自身偏离不足 5% → 不动
        orders = self._run(price_a=9.0, cap_a=90.0, qty_a=7000,
                           price_b=10.0, cap_b=100.0, qty_b=7300)
        self.assertEqual(len(orders), 1, orders)
        self.assertEqual(orders[0].code, "sh600000")
        self.assertEqual(orders[0].direction, "buy")
        self.assertGreater(orders[0].quantity, 0)

    def test_rise_sells_only_that_symbol(self):
        # A 自身偏离不足 5%; B 上涨 10% → 减仓
        orders = self._run(price_a=10.0, cap_a=100.0, qty_a=7300,
                           price_b=11.0, cap_b=110.0, qty_b=7300)
        self.assertEqual(len(orders), 1, orders)
        self.assertEqual(orders[0].code, "sz000001")
        self.assertEqual(orders[0].direction, "sell")
        self.assertGreater(orders[0].quantity, 0)

    def test_drop_never_produces_sell_for_that_symbol(self):
        orders = self._run(price_a=9.0, cap_a=90.0, qty_a=7000,
                           price_b=10.0, cap_b=100.0, qty_b=7300)
        for o in orders:
            if o.code == "sh600000":
                self.assertEqual(o.direction, "buy")


class StrategyParamKeyTest(unittest.TestCase):
    """P0-6: 界面传入的参数键必须被选股函数接受"""

    def test_ui_keys_match_smallcap_rotation_signature(self):
        src_path = os.path.join(ROOT, "streamlit_app.py")
        with open(src_path, encoding="utf-8") as f:
            src = f.read()

        # 旧的错误键名不得再次出现
        for bad_key in ('"ma20_required"', '"ma60_required"',
                        '"turnover_min"', '"turnover_max"'):
            self.assertNotIn(bad_key, src, f"界面仍在使用错误参数名 {bad_key}")

        # 正确键名必须存在于选股函数签名中
        params = set(inspect.signature(be.select_smallcap_rotation).parameters)
        for good_key in ("require_ma20", "require_ma60", "turn_min", "turn_max"):
            self.assertIn(good_key, params)
            self.assertIn(f'"{good_key}"', src)


class LastRowValuationTest(unittest.TestCase):
    """数据缺口估值回归: 当日无K线时不得把持仓按 0 估值

    2026-11-16 的现象: 全市场数据只到 11-13, 但回测终点取了 MAX(date)=11-16,
    最后一天所有持仓取不到行情 → 期末净值瞬间塌成现金, 收益 -73%。
    """

    @staticmethod
    def _feat(dates, closes):
        return {"sh600000": pd.DataFrame({"date": pd.to_datetime(dates), "close": closes})}

    def test_last_row_returns_previous_close(self):
        feat = self._feat(["2026-11-11", "2026-11-12", "2026-11-13"], [10.0, 10.5, 11.0])
        bars = be.BarIndex(feat)
        r = bars.last_row("sh600000", "2026-11-16")
        self.assertIsNotNone(r)
        self.assertEqual(r["close"], 11.0)

    def test_last_row_is_none_before_first_bar(self):
        feat = self._feat(["2026-11-11", "2026-11-12"], [10.0, 10.5])
        bars = be.BarIndex(feat)
        self.assertIsNone(bars.last_row("sh600000", "2026-11-10"))
        self.assertIsNone(bars.last_row("sz000001", "2026-11-12"))

    def test_total_value_keeps_positions_on_missing_day(self):
        feat = self._feat(["2026-11-12", "2026-11-13"], [10.0, 11.0])
        bars = be.BarIndex(feat)
        pf = be.Portfolio(1000.0)
        pf.positions["sh600000"] = be.Position(
            "sh600000", pd.Timestamp("2026-11-12"), 10.0, 1000, 0.0, 0.0)
        # 11-16 无K线 → 用 11-13 收盘 11.0 估值, 而不是 0
        self.assertAlmostEqual(pf.total_value("2026-11-16", bars), 1000.0 + 11000.0, places=6)


class CoverageDateTest(unittest.TestCase):
    """db_stats 的 coverage_last_date 不得被个别标的的单行数据抬高"""

    def test_last_coverage_date_ignores_single_symbol_day(self):
        import tempfile
        import sqlite3
        old_path = data_store.DB_PATH
        tmp = tempfile.mkdtemp()
        try:
            data_store.DB_PATH = os.path.join(tmp, "t.db")
            data_store.invalidate_stats()
            data_store.init_db()
            conn = sqlite3.connect(data_store.DB_PATH)
            rows = []
            # 覆盖面阈值默认 1000 只: 构造 1200 只标的在 11-13/11-16 有数据
            for i in range(1200):
                sym = f"sz{i:06d}"
                for d in ("2026-11-13", "2026-11-16"):
                    rows.append((sym, d, 1, 1, 1, 10, 1, 1, 1, 1, "stock"))
            conn.executemany(
                "INSERT INTO daily_bar (symbol,date,open,high,low,close,volume,amount,"
                "outstanding_share,turnover,asset_type) VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
            conn.execute(
                "INSERT INTO daily_bar (symbol,date,open,high,low,close,volume,amount,"
                "outstanding_share,turnover,asset_type) VALUES "
                "('sh000001','2026-11-17',1,1,1,3500,1,1,1,1,'stock')")
            conn.commit()
            conn.close()

            self.assertEqual(data_store.last_coverage_date(min_symbols=10), "2026-11-16")
            self.assertEqual(data_store.db_stats(ttl=0)["coverage_last_date"], "2026-11-16")
            self.assertEqual(data_store.db_stats(ttl=0)["last_date"], "2026-11-17")
        finally:
            data_store.DB_PATH = old_path
            data_store.invalidate_stats()


class CostAccountingTest(unittest.TestCase):
    """加仓合并后的持仓: 交易成本只累计费用, 不得把买入本金当成成本"""

    def _fill(self, direction, qty, price, cost, date=DATE):
        return be.FillEvent(pd.Timestamp(date), "sh600000", direction, qty, price,
                            commission=cost, stamp_tax=0.0, transfer_fee=0.0,
                            slippage_cost=0.0, impact_cost=0.0)

    def test_merged_position_cost_excludes_principal(self):
        pf = be.Portfolio(1_000_000.0)
        pf.apply_fill(self._fill("buy", 1000, 10.0, 20.0))
        pf.apply_fill(self._fill("buy", 1000, 11.0, 22.0, "2026-01-06"))
        pf.apply_fill(self._fill("sell", 2000, 12.0, 30.0, "2026-01-07"))

        self.assertEqual(len(pf.trades), 1)
        t = pf.trades[0]
        self.assertAlmostEqual(t["cost"], 20.0 + 22.0 + 30.0, places=6)
        # 均价 10.5, 卖出 12.0, 2000 股 → 毛赚 3000, 扣成本 72
        self.assertAlmostEqual(t["pnl"], 3000.0 - 72.0, places=6)


class SellReasonTest(unittest.TestCase):
    """成交流水的「原因」必须能解释卖出动作 (MA30 走弱 / 调仓换股)"""

    def test_ma30_exit_sets_reason(self):
        strat = make_strategy(ma30_exit=True, cap_min=0, cap_max=1e6,
                              min_listed_years=0, exclude_st=False,
                              exclude_ocf_diverge=False, exclude_nonop_profit=False,
                              exclude_no_dividend=False)
        strat.portfolio = FakePortfolio(cash=1000, positions={"sh600000": position(1000)},
                                        total_value=11000.0)
        strat.bars = FakeBars({("sh600000", DATE): {"close": 10.0, "cap_yi": 80.0}})
        strat._ma30_is_decreasing = lambda code, date: True

        sig = strat.on_bar(MarketBarEvent(DATE, {}, strat.bars))
        self.assertIn("sh600000", sig.sells)
        self.assertEqual(sig.sell_reasons.get("sh600000"), "ma30_exit")

    def test_ui_reason_labels_cover_engine_codes(self):
        # 静态检查: 引擎会写出的每种 reason 在界面都有中文映射 (不 import Streamlit)
        with open(os.path.join(ROOT, "streamlit_app.py"), encoding="utf-8") as f:
            src = f.read()
        for code in ("mc_new_entry", "mc_rebalance", "rebalance",
                     "ma30_exit", "rank_drop", "hold_expire", "limit_up"):
            self.assertIn(f'"{code}":', src)


class TopkDropTest(unittest.TestCase):
    """Topk-Drop 调仓 (借 Qlib): 只在排名跌出 top_n+rank_buffer 时换出"""

    def _strat(self):
        return make_strategy(top_n=2, rank_buffer=2, rebalance_mode="topk_drop",
                             ma30_exit=False, universe=None)

    def test_rank_outside_buffer_is_sold(self):
        strat = self._strat()
        strat.portfolio = FakePortfolio(
            cash=1000,
            positions={"sh600001": position(1000), "sh600005": position(1000)},
            total_value=20000.0)
        strat.bars = FakeBars({
            ("sh600001", DATE): {"close": 10.0, "cap_yi": 20.0},
            ("sh600005", DATE): {"close": 10.0, "cap_yi": 90.0},
            ("sh600002", DATE): {"close": 10.0, "cap_yi": 30.0},
        })
        ranked = ["sh600001", "sh600002", "sz600003", "sz600004", "sh600005"]
        with mock.patch.object(be, "_filter_market_cap_universe", return_value=ranked):
            sig = strat.on_bar(MarketBarEvent(DATE, {"sh600001": {}}, strat.bars))

        # top_n=2 + buffer=2 → 保留前 4 名; 第 5 名的 sh600005 被换出
        self.assertEqual(sig.sells, ["sh600005"])
        self.assertEqual(sig.sell_reasons["sh600005"], "rank_drop")
        self.assertNotIn("sh600001", sig.sells)
        # 空出的 1 个槽位补排名最靠前的未持有标的
        self.assertEqual([o.code for o in sig.rebalance_orders], ["sh600002"])
        self.assertEqual(sig.rebalance_orders[0].direction, "buy")

    def test_rank_inside_buffer_is_kept(self):
        strat = self._strat()
        strat.portfolio = FakePortfolio(
            cash=1000,
            positions={"sh600001": position(1000), "sh600004": position(1000)},
            total_value=20000.0)
        strat.bars = FakeBars({
            ("sh600001", DATE): {"close": 10.0, "cap_yi": 20.0},
            ("sh600004", DATE): {"close": 10.0, "cap_yi": 80.0},
        })
        ranked = ["sh600001", "sh600002", "sz600003", "sh600004", "sh600005"]
        with mock.patch.object(be, "_filter_market_cap_universe", return_value=ranked):
            sig = strat.on_bar(MarketBarEvent(DATE, {"sh600001": {}}, strat.bars))

        # 第 4 名仍在缓冲内 → 不卖出、不补仓
        self.assertIsNone(sig)

    def test_topk_drop_skips_deviation_rebalance(self):
        strat = self._strat()
        strat.portfolio = FakePortfolio(
            cash=1000,
            positions={"sh600001": position(1000), "sh600002": position(1000)},
            total_value=20000.0)
        strat.bars = FakeBars({
            ("sh600001", DATE): {"close": 10.0, "cap_yi": 20.0},   # 市值翻倍
            ("sh600002", DATE): {"close": 10.0, "cap_yi": 30.0},
        })
        ranked = ["sh600001", "sh600002", "sz600003"]
        with mock.patch.object(be, "_filter_market_cap_universe", return_value=ranked):
            sig = strat.on_bar(MarketBarEvent(DATE, {"sh600001": {}}, strat.bars))
        # 排名都在前 4 → 即使市值大幅偏离也不产生调仓单
        self.assertIsNone(sig)


class UniverseRebalanceTest(unittest.TestCase):
    """指定标的池 + Topk-Drop: 没有排名概念, 仍须按偏离度调仓

    2026-11-18 反馈: 单只 ETF 从 2023-08 建仓一直持有到 2025-02 MA30 走弱,
    中间涨跌远超 4% 却没有任何调仓 —— universe 分支跳过了排名换股,
    topk_drop 又跳过了偏离度再平衡, 两条路都不走。
    """

    def test_universe_topk_drop_still_rebalances(self):
        strat = make_strategy(top_n=1, universe=["sz159509"], rank_buffer=2,
                              rebalance_mode="topk_drop", ma30_exit=False,
                              cap_change_threshold=0.04, rebalance_threshold=0.05)
        bars = FakeBars({("sz159509", DATE): {"close": 1.10, "cap_yi": np.nan}})
        pf = FakePortfolio(cash=0, positions={"sz159509": position(100000)},
                           total_value=110000.0)
        strat.portfolio = pf
        strat.bars = bars
        strat._target_weights = {"sz159509": 1.0}
        strat._entry_prices = {"sz159509": 1.0}   # ETF 无市值 → 按价格算偏离: +10%

        sig = strat.on_bar(MarketBarEvent(DATE, {"sz159509": {}}, bars))

        self.assertIsNotNone(sig, "universe + topk_drop 不应完全放弃调仓")
        self.assertEqual([o.direction for o in sig.rebalance_orders], ["sell"])
        self.assertEqual(sig.rebalance_orders[0].code, "sz159509")


class Ma30CacheTest(unittest.TestCase):
    """周线 MA30 预计算缓存: 与逐日 resample 口径一致, 数据不足时不做判断"""

    def _legacy_up(self, df, date, lookback=5):
        """旧实现口径: 每次按 date 截断后 resample 再算 MA30"""
        sub = df[["date", "close"]].dropna().copy()
        sub["date"] = pd.to_datetime(sub["date"])
        sub = sub[sub["date"] <= pd.Timestamp(date)]
        wk = sub.sort_values("date").set_index("date")["close"] \
                 .resample("W-FRI").last().dropna().values
        need = 30 + lookback
        if len(wk) < need:
            return None
        return bool(wk[-30:].mean() > wk[-need:-lookback].mean())

    def test_slope_up_and_insufficient_history(self):
        dates = pd.bdate_range("2020-01-01", "2022-12-31")
        closes = np.linspace(10, 30, len(dates))
        feat = {"sh600000": pd.DataFrame({"date": dates, "close": closes})}
        cache = be.build_ma30_cache(feat, lookback=5)
        self.assertTrue(be.ma30_slope_up(cache, "sh600000", "2022-12-30"))
        self.assertIsNone(be.ma30_slope_up(cache, "sh600000", "2020-03-01"))
        self.assertIsNone(be.ma30_slope_up(cache, "sz000001", "2022-12-30"))

    def test_matches_per_day_resample_on_every_day(self):
        """含当日涨跌的未走完周必须用当日收盘, 逐日与旧实现完全一致"""
        rnd = np.random.default_rng(7)
        dates = pd.bdate_range("2019-01-01", "2022-12-31")
        closes = 10 * np.exp(np.cumsum(rnd.normal(0, 0.03, len(dates))))
        df = pd.DataFrame({"date": dates, "close": closes})
        feat = {"sh600001": df}
        cache = be.build_ma30_cache(feat, lookback=5)
        checked = 0
        for d in dates[900:]:
            legacy = self._legacy_up(df, d)
            got = be.ma30_slope_up(cache, "sh600001", d)
            self.assertEqual(legacy, got, f"date={str(d)[:10]}")
            checked += 1
        self.assertGreater(checked, 100)


if __name__ == "__main__":
    unittest.main(verbosity=2)
