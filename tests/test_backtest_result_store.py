"""回测结果持久化回归测试

覆盖 2026-11-19 需求:
  1. 回测结果 (含成交/持仓明细) 能完整序列化并写入 SQLite
  2. 参数签名不变 → 取回最近一次结果; 参数变 → 取不到 (触发新回测)
  3. 同参数重复回测覆盖旧结果, 不新增行

运行: py -3.13 -m unittest tests.test_backtest_result_store -v
"""
import json
import os
import sys
import tempfile
import unittest

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import backtest_engine as be  # noqa: E402
import data_store  # noqa: E402


def sample_result():
    return {
        "curve": pd.DataFrame({
            "date": pd.to_datetime(["2026-01-05", "2026-01-06"]),
            "value": [1000000.0, 1012000.0],
        }),
        "trades": [{"code": "sh600000", "pnl": np.float64(12000.0)}],
        "fills": [{"date": "2026-01-06", "code": "sh600000", "direction": "buy",
                   "price": 10.0, "qty": 100, "amount": 1000.0, "cost": 1.2,
                   "pnl": 0.0, "cash_after": 998998.8, "reason": "mc_new_entry"}],
        "holdings_log": [{"date": "2026-01-06", "code": "sh600000", "qty": 100,
                          "cost_price": 10.0, "close": 10.1,
                          "market_value": np.float64(1010.0),
                          "pnl": np.float64(10.0)}],
        "performance": {"total_return": 1.2, "trades_count": 1,
                        "cost_summary": {"commission": np.float64(1.2)}},
        "selection_dates": ["2026-01-05"],
        "trade_dates": ["2026-01-06"],
        "lu_dates": [],
        "all_trade_days": ["2026-01-05", "2026-01-06"],
        "fill_blocks": {"buy_limit_up": 1},
    }


class SerializeRoundTripTest(unittest.TestCase):
    def test_fills_and_holdings_survive_json(self):
        payload = be.serialize_result(sample_result())
        self.assertEqual(len(payload["fills"]), 1)
        self.assertEqual(len(payload["holdings_log"]), 1)
        self.assertEqual(payload["fills"][0]["reason"], "mc_new_entry")
        # numpy 标量不得退化成字符串
        self.assertIsInstance(payload["holdings_log"][0]["market_value"], float)

        restored = be.deserialize_result(json.loads(
            json.dumps(payload, ensure_ascii=False, default=str)))
        self.assertEqual(restored["fills"], payload["fills"])
        self.assertEqual(restored["holdings_log"], payload["holdings_log"])
        self.assertEqual(len(restored["curve"]), 2)
        self.assertAlmostEqual(restored["performance"]["total_return"], 1.2)


class BacktestResultStoreTest(unittest.TestCase):
    def setUp(self):
        self._old_path = data_store.DB_PATH
        self._tmp = tempfile.mkdtemp()
        data_store.DB_PATH = os.path.join(self._tmp, "t.db")
        data_store.invalidate_stats()
        data_store.init_db()

    def tearDown(self):
        data_store.DB_PATH = self._old_path
        data_store.invalidate_stats()

    def test_save_and_get_by_param_sig(self):
        payload = be.serialize_result(sample_result())
        data_store.save_backtest_result(
            "市值策略", "sig_a", {"cap_min": 15}, "fp1", payload)
        rec = data_store.get_backtest_result("市值策略", "sig_a")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["fingerprint"], "fp1")
        self.assertTrue(rec["created_at"])
        self.assertEqual(rec["payload"]["fills"], payload["fills"])
        # 参数不同 → 取不到 (触发新回测)
        self.assertIsNone(data_store.get_backtest_result("市值策略", "sig_b"))
        # 但能取到该策略最近一次结果 (参数变更后先回显旧结果)
        latest = data_store.get_latest_backtest_result("市值策略")
        self.assertIsNotNone(latest)
        self.assertEqual(latest["param_sig"], "sig_a")

    def test_same_param_overwrites_previous_run(self):
        first = be.serialize_result(sample_result())
        data_store.save_backtest_result("市值策略", "sig", {}, "fp1", first)
        second = be.serialize_result(sample_result())
        second["performance"]["total_return"] = 9.9
        data_store.save_backtest_result("市值策略", "sig", {}, "fp2", second)

        rec = data_store.get_backtest_result("市值策略", "sig")
        self.assertEqual(rec["fingerprint"], "fp2")
        self.assertAlmostEqual(rec["payload"]["performance"]["total_return"], 9.9)
        conn = data_store.get_conn()
        n = conn.execute("SELECT COUNT(*) FROM backtest_result").fetchone()[0]
        conn.close()
        self.assertEqual(n, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
