"""稳健参数评估与滚动样本外检验单元测试

运行: py -3.13 -m unittest tests.test_robust_backtest -v
"""
import os
import sys
import unittest

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import robust_backtest as rb  # noqa: E402


START, END = "2023-01-01", "2026-01-01"


def segment_runner(table):
    """按验证段起点返回预设绩效; table[params_name][segment_index]"""

    def run_one(params, start, end):
        windows = rb.split_windows(START, END, 4)
        idx = next(i for i, (ws, _) in enumerate(windows) if ws == pd.Timestamp(start))
        calmar, trades = table[params["name"]][idx]
        return {
            "performance": {
                "calmar": calmar,
                "total_return": calmar,
                "trades_count": trades,
            },
            "trades": [{}] * trades,
        }

    return run_one


class TestSplitWindows(unittest.TestCase):
    def test_windows_are_disjoint_and_cover_range(self):
        ws = rb.split_windows(START, END, 4)
        self.assertEqual(len(ws), 4)
        for i in range(3):
            self.assertLess(ws[i][1], ws[i + 1][0])
        self.assertEqual(ws[0][0], pd.Timestamp(START))
        self.assertEqual(ws[-1][1], pd.Timestamp(END))

    def test_short_range_downgrades_k(self):
        ws = rb.split_windows("2026-01-01", "2026-03-01", 8)
        self.assertLessEqual(len(ws), 2)


class TestRobustSweep(unittest.TestCase):
    def test_stable_params_beat_lucky_params(self):
        table = {
            "stable": [(1.0, 100)] * 4,
            "lucky": [(3.0, 100), (-1.0, 100), (-1.0, 100), (-1.0, 100)],
            "thin": [(2.0, 1)] * 4,
        }
        combos = [{"name": n} for n in table]
        out = rb.run_robust_sweep(segment_runner(table), combos,
                                  rb.split_windows(START, END, 4),
                                  objective="calmar", min_trades=30)
        self.assertEqual(out["rows"][0]["params"]["name"], "stable")
        thin = next(r for r in out["rows"] if r["params"]["name"] == "thin")
        self.assertTrue(thin["disqualified"])
        self.assertIn("交易数不足", thin["reason"])

    def test_degenerate_objective_rejected(self):
        for obj in ("win_rate", "trades_count", "avg_hold_days"):
            with self.assertRaises(ValueError):
                rb.normalize_objective(obj)
        self.assertEqual(rb.normalize_objective("卡玛"), "calmar")

    def test_champion_convergence(self):
        table = {
            "a": [(2.0, 50), (2.0, 50), (2.0, 50), (2.0, 50)],
            "b": [(1.0, 50), (1.0, 50), (1.0, 50), (1.0, 50)],
        }
        out = rb.run_robust_sweep(segment_runner(table),
                                  [{"name": "a"}, {"name": "b"}],
                                  rb.split_windows(START, END, 4),
                                  min_trades=10)
        conv = rb.champion_convergence(out["rows"], 4)
        self.assertEqual(conv["label"], "unanimous")


    def test_failed_segment_does_not_abort_sweep(self):
        def flaky(params, start, end):
            if params["name"] == "boom":
                raise RuntimeError("data missing")
            return {"performance": {"calmar": 1.0, "total_return": 1.0,
                                    "trades_count": 100}}

        out = rb.run_robust_sweep(flaky, [{"name": "boom"}, {"name": "ok"}],
                                  rb.split_windows(START, END, 3),
                                  min_trades=30)
        boom = next(r for r in out["rows"] if r["params"]["name"] == "boom")
        self.assertTrue(boom["disqualified"])
        self.assertIsNone(boom["score"])
        self.assertEqual(out["rows"][0]["params"]["name"], "ok")

    def test_plateau_ratio(self):
        rows = [
            {"params": {"x": 1}, "score": 1.0, "disqualified": False},
            {"params": {"x": 2}, "score": 0.9, "disqualified": False},
            {"params": {"x": 3}, "score": 0.2, "disqualified": False},
        ]
        champ = rows[0]
        ratio = rb.plateau_ratio(rows, champ, 3, threshold=0.7)
        self.assertIsNotNone(ratio)
        self.assertGreater(ratio, 0.0)


class TestWalkForward(unittest.TestCase):
    def test_folds_have_no_overlap(self):
        folds = rb.generate_folds(START, END, train_days=365, test_days=90)
        self.assertGreater(len(folds), 3)
        for f in folds:
            self.assertEqual(f["test"][0], f["train"][1] + pd.Timedelta(days=1))

    def test_oos_aggregation(self):
        def run_one(params, start, end):
            # 训练窗按参数 x 排名; 测试窗固定 +1%
            return {"performance": {
                "calmar": float(params["x"]),
                "total_return": 1.0,
                "trades_count": 100,
            }}

        folds = rb.generate_folds(START, END, train_days=365, test_days=90)
        combos = [{"x": 1}, {"x": 3}, {"x": 2}]
        out = rb.run_walkforward(run_one, combos, folds,
                                 objective="calmar", min_trades=30)
        self.assertEqual(out["valid_folds"], len(folds))
        for rec in out["folds"]:
            self.assertEqual(rec["params"], {"x": 3})
        expected = ((1.01 ** len(folds)) - 1) * 100
        self.assertAlmostEqual(out["compounded_return"], expected, places=6)
        self.assertAlmostEqual(out["consistency"], 1.0)

    def test_trade_gate_skips_fold(self):
        def run_one(params, start, end):
            return {"performance": {
                "calmar": 1.0, "total_return": 1.0, "trades_count": 2,
            }}

        folds = rb.generate_folds(START, END, train_days=365, test_days=90)[:2]
        out = rb.run_walkforward(run_one, [{"x": 1}], folds, min_trades=30)
        self.assertEqual(out["valid_folds"], 0)
        self.assertTrue(all(f["skipped"] for f in out["folds"]))


if __name__ == "__main__":
    unittest.main()
