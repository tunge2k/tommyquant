"""回测「当前结果 vs 历史最优」状态机 + 自动保存回归测试

覆盖 2026-11-28 需求:
  1. 关系判定 (尚无最优 / 新最优 / 一致 / 更差) 与浮点容差边界
  2. 自动保存条件: 更优/无最优才写, 一致/更差不写 (不重复、不遗漏)
  3. 展示状态: 文案/颜色/是否弹确认/展示当前还是最优
  4. ensure_best_saved 落库幂等性 (临时 SQLite)

运行: py -3.13 -m unittest tests.test_bt_best_state -v
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import bt_best_state as bbs  # noqa: E402
import data_store  # noqa: E402


def best_rec(value, metric="total_return", created="2026-11-28 10:00:00"):
    return {"metric_name": metric, "metric_value": value, "created_at": created}


class ClassifyTest(unittest.TestCase):
    def test_no_best(self):
        self.assertEqual(bbs.classify(12.3, None), bbs.NO_BEST)
        self.assertEqual(bbs.classify(12.3, {}), bbs.NO_BEST)
        self.assertEqual(bbs.classify(12.3, {"metric_value": "bad"}), bbs.NO_BEST)

    def test_new_best(self):
        self.assertEqual(bbs.classify(10.000000002, best_rec(10.0)), bbs.NEW_BEST)
        self.assertEqual(bbs.classify(223.1, best_rec(100.0)), bbs.NEW_BEST)

    def test_same_within_eps(self):
        self.assertEqual(bbs.classify(10.0, best_rec(10.0)), bbs.SAME)
        self.assertEqual(bbs.classify(10.0000000005, best_rec(10.0)), bbs.SAME)
        # 经典浮点噪声 0.1+0.2 != 0.3
        self.assertEqual(bbs.classify(0.1 + 0.2, best_rec(0.3)), bbs.SAME)

    def test_worse(self):
        self.assertEqual(bbs.classify(9.999999998, best_rec(10.0)), bbs.WORSE)
        self.assertEqual(bbs.classify(-5.0, best_rec(0.0)), bbs.WORSE)

    def test_custom_eps(self):
        self.assertEqual(bbs.classify(10.4, best_rec(10.0), eps=0.5), bbs.SAME)
        self.assertEqual(bbs.classify(10.6, best_rec(10.0), eps=0.5), bbs.NEW_BEST)


class AutosaveTest(unittest.TestCase):
    def test_should_autosave(self):
        self.assertTrue(bbs.should_autosave(5.0, None))
        self.assertTrue(bbs.should_autosave(10.5, best_rec(10.0)))
        self.assertFalse(bbs.should_autosave(10.0, best_rec(10.0)))
        self.assertFalse(bbs.should_autosave(9.0, best_rec(10.0)))
        # 浮点噪声不触发重复保存
        self.assertFalse(bbs.should_autosave(0.1 + 0.2, best_rec(0.3)))

    def test_should_prompt(self):
        self.assertTrue(bbs.should_prompt(bbs.WORSE))
        self.assertFalse(bbs.should_prompt(bbs.WORSE, discarded=True))
        self.assertFalse(bbs.should_prompt(bbs.NEW_BEST))
        self.assertFalse(bbs.should_prompt(bbs.SAME))

    def test_metric_value_fallback(self):
        self.assertEqual(bbs.metric_value(None), 0.0)
        self.assertEqual(bbs.metric_value({"total_return": None}), 0.0)
        self.assertEqual(bbs.metric_value({"sharpe": 1.5}, "sharpe"), 1.5)


class OriginTest(unittest.TestCase):
    def test_origin_of(self):
        self.assertEqual(bbs.origin_of({"origin": "fresh"}), bbs.ORIGIN_FRESH)
        self.assertEqual(bbs.origin_of({"origin": "cache"}), bbs.ORIGIN_LOADED)
        self.assertEqual(bbs.origin_of({}), bbs.ORIGIN_LOADED)
        self.assertEqual(bbs.origin_of({"stale_params": True}), bbs.ORIGIN_HISTORY)

    def test_origin_caption(self):
        self.assertEqual(bbs.origin_caption({"origin": "fresh"}), "")
        self.assertIn("参数/区间已变更", bbs.origin_caption({"stale_params": True}))
        self.assertIn("行情数据已更新", bbs.origin_caption({"stale": True}))
        self.assertIn("已载入", bbs.origin_caption({"origin": "cache"}))


class BuildStateTest(unittest.TestCase):
    def test_no_best_first_run(self):
        s = bbs.build_state({"total_return": 12.34}, None, {"origin": "fresh"})
        self.assertEqual(s["relation"], bbs.NO_BEST)
        self.assertTrue(s["autosave"])
        self.assertFalse(s["prompt"])
        self.assertEqual(s["display"], "result")
        self.assertEqual(s["level"], "success")
        self.assertIn("已自动保存", s["message"])
        self.assertIn("尚无最优", s["summary"])

    def test_new_best_keeps_old_value_in_message(self):
        s = bbs.build_state({"total_return": 223.1}, best_rec(100.0),
                            {"origin": "fresh"})
        self.assertEqual(s["relation"], bbs.NEW_BEST)
        self.assertTrue(s["autosave"])
        self.assertIn("+223.10%", s["message"])
        self.assertIn("+100.00%", s["message"])
        # 本次刚算完 → 不再弹额外的 success 框 (由调用方决定), 但仍保留 relation
        self.assertEqual(s["origin"], bbs.ORIGIN_FRESH)

    def test_same_result(self):
        s = bbs.build_state({"total_return": 50.0}, best_rec(50.0),
                            {"origin": "cache"})
        self.assertEqual(s["relation"], bbs.SAME)
        self.assertFalse(s["autosave"])
        self.assertEqual(s["level"], "info")
        self.assertIn("与已保存最优一致", s["message"])

    def test_worse_prompts(self):
        s = bbs.build_state({"total_return": 80.0}, best_rec(100.0),
                            {"origin": "fresh"})
        self.assertEqual(s["relation"], bbs.WORSE)
        self.assertFalse(s["autosave"])
        self.assertTrue(s["prompt"])
        self.assertEqual(s["level"], "warning")
        self.assertEqual(s["display"], "result")

    def test_worse_discarded_shows_best(self):
        s = bbs.build_state({"total_return": 80.0}, best_rec(100.0),
                            {"origin": "cache"}, discarded=True)
        self.assertEqual(s["relation"], bbs.DISCARDED)
        self.assertFalse(s["prompt"])
        self.assertEqual(s["display"], "best")
        self.assertEqual(s["level"], "info")
        self.assertIn("已放弃本次结果", s["message"])

    def test_history_result_never_prompts(self):
        s = bbs.build_state({"total_return": 80.0}, best_rec(100.0),
                            {"stale_params": True})
        self.assertEqual(s["relation"], bbs.WORSE)
        self.assertFalse(s["prompt"])
        self.assertEqual(s["origin"], bbs.ORIGIN_HISTORY)
        self.assertIn("库内最近一次结果", s["message"])

    def test_metric_name_follows_best_record(self):
        s = bbs.build_state({"sharpe": 1.8}, best_rec(1.2, metric="sharpe"))
        self.assertEqual(s["metric_name"], "sharpe")
        self.assertEqual(s["relation"], bbs.NEW_BEST)
        self.assertIn("+1.80", s["message"])


class EnsureBestSavedTest(unittest.TestCase):
    def setUp(self):
        self._old_path = data_store.DB_PATH
        self._tmp = tempfile.mkdtemp()
        data_store.DB_PATH = os.path.join(self._tmp, "t.db")
        data_store.invalidate_stats()
        data_store.init_db()

    def tearDown(self):
        data_store.DB_PATH = self._old_path
        data_store.invalidate_stats()

    def _save(self, ret, settings=None):
        return bbs.ensure_best_saved(
            data_store, "ETF调仓策略", {"total_return": ret},
            {"top_n": 1}, "fp1", settings or {"erc_top_n": 1},
            {"performance": {"total_return": ret}})

    def test_save_once_then_idempotent(self):
        saved, prev = self._save(120.0)
        self.assertTrue(saved)
        self.assertIsNone(prev)
        rec = data_store.get_best_backtest("ETF调仓策略")
        self.assertAlmostEqual(rec["metric_value"], 120.0)
        self.assertEqual(rec["settings"]["erc_top_n"], 1)

        # 同值再调用 → 不再保存
        saved, prev = self._save(120.0)
        self.assertFalse(saved)
        self.assertAlmostEqual(prev["metric_value"], 120.0)

        # 更差 → 不保存
        saved, _ = self._save(90.0)
        self.assertFalse(saved)
        self.assertAlmostEqual(
            data_store.get_best_backtest("ETF调仓策略")["metric_value"], 120.0)

    def test_better_overwrites_single_row(self):
        self._save(120.0)
        saved, prev = self._save(223.1)
        self.assertTrue(saved)
        self.assertAlmostEqual(prev["metric_value"], 120.0)
        conn = data_store.get_conn()
        n = conn.execute("SELECT COUNT(*) FROM best_backtest").fetchone()[0]
        conn.close()
        self.assertEqual(n, 1)
        self.assertAlmostEqual(
            data_store.get_best_backtest("ETF调仓策略")["metric_value"], 223.1)

    def test_float_noise_does_not_resave(self):
        self._save(0.1 + 0.2)
        saved, _ = self._save(0.3)
        self.assertFalse(saved)


if __name__ == "__main__":
    unittest.main(verbosity=2)
