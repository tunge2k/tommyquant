# -*- coding: utf-8 -*-
"""交易所官方 ETF 份额 / 东财净值 数据源测试 (不联网)"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import data_store
import exchange_source as es


class ParseTest(unittest.TestCase):
    """响应解析与单位换算"""

    def test_parse_sse_shares_units(self):
        payload = {"result": [
            {"SEC_CODE": "510300", "SEC_NAME": "300ETF", "STAT_DATE": "2026-11-18",
             "TOT_VOL": "4968408.77"},
            {"SEC_CODE": "588000", "SEC_NAME": "科创50", "STAT_DATE": "2026-11-18",
             "TOT_VOL": ""},                       # 空值丢弃
            {"SEC_CODE": "", "SEC_NAME": "bad", "STAT_DATE": "2026-11-18",
             "TOT_VOL": "1"},
        ]}
        rows = es.parse_sse_shares(payload)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "sh510300")
        self.assertEqual(rows[0]["date"], "2026-11-18")
        self.assertAlmostEqual(rows[0]["shares"], 4968408.77 * 1e4, places=1)

    def test_parse_nav_list(self):
        payload = {"Data": {"LSJZList": [
            {"FSRQ": "2026-11-18", "DWJZ": "4.1234"},
            {"FSRQ": "2026-11-17", "DWJZ": ""},
        ]}}
        rows = es.parse_nav_list(payload)
        self.assertEqual(rows, [{"date": "2026-11-18", "nav": 4.1234}])

    def test_fetch_sse_shares_pagination(self):
        calls = []

        def fake(url, referer, params):
            calls.append(params["pageHelp.pageNo"])
            return {"result": [{"SEC_CODE": f"5103{params['pageHelp.pageNo']:02d}",
                                "SEC_NAME": "x", "STAT_DATE": "2026-11-18",
                                "TOT_VOL": "1"}],
                    "pageHelp": {"pageCount": 2}}

        old = es._get_json
        es._get_json = fake
        try:
            rows = es.fetch_sse_shares("2026-11-18")
        finally:
            es._get_json = old
        self.assertEqual(calls, [1, 2])
        self.assertEqual(len(rows), 2)

    def test_fetch_nav_history_stops_on_short_page(self):
        calls = []

        def fake(url, referer, params):
            calls.append(params["pageIndex"])
            n = 3 if params["pageIndex"] == 1 else 1
            return {"Data": {"LSJZList": [
                {"FSRQ": f"2020-01-0{i + 1}", "DWJZ": "1.0"} for i in range(n)]}}

        old = es._get_json
        es._get_json = fake
        try:
            rows = es.fetch_nav_history("510300", "2020-01-01", "2020-12-31", page_size=3)
        finally:
            es._get_json = old
        self.assertEqual(calls, [1, 2])
        self.assertEqual(len(rows), 4)


class StoreTest(unittest.TestCase):
    """落库合并与因子读取"""

    def setUp(self):
        self._old = data_store.DB_PATH
        self._tmp = tempfile.mkdtemp()
        data_store.DB_PATH = os.path.join(self._tmp, "t.db")
        data_store.init_db()
        conn = sqlite3.connect(data_store.DB_PATH)
        days = ["2026-11-10", "2026-11-11", "2026-11-12",
                "2026-11-13", "2026-11-16", "2026-11-17"]
        conn.executemany(
            "INSERT INTO daily_bar (symbol, date, close) VALUES ('sh510300', ?, ?)",
            [(d, 1.0) for d in days])
        conn.commit()
        conn.close()
        self._days = days

    def tearDown(self):
        data_store.DB_PATH = self._old

    def test_upsert_merges_shares_and_nav(self):
        data_store.upsert_etf_share_daily(
            [{"symbol": "sh510300", "date": "2026-11-17", "shares": 1e8}])
        data_store.upsert_etf_share_daily(
            [{"symbol": "sh510300", "date": "2026-11-17", "nav": 0.95}])
        conn = data_store.get_conn()
        row = conn.execute(
            "SELECT shares, nav FROM etf_share_daily "
            "WHERE symbol='sh510300' AND date='2026-11-17'").fetchone()
        conn.close()
        self.assertAlmostEqual(row[0], 1e8)
        self.assertAlmostEqual(row[1], 0.95)

    def test_factor_maps_from_official_data(self):
        shares = [1.0e8, 1.1e8, 1.2e8, 1.3e8, 1.4e8, 1.5e8]
        data_store.upsert_etf_share_daily([
            {"symbol": "sh510300", "date": d, "shares": s, "nav": 0.95}
            for d, s in zip(self._days, shares)])
        maps = data_store.load_etf_factor_maps()
        last = self._days[-1]
        # 近 5 个交易日净申购 0.5e8 / 当前份额 1.5e8
        self.assertAlmostEqual(maps["flow"][last]["sh510300"], 50000000 / 1.5e8 * 100, places=4)
        # 收盘 1.0 / 净值 0.95 - 1
        self.assertAlmostEqual(maps["premium"][last]["sh510300"], 5.263157, places=4)
        # 前 5 天没有足够历史, 不产生资金流因子
        self.assertNotIn(self._days[3], maps["flow"])

    def test_htbr_premium_wins_over_official(self):
        data_store.upsert_etf_share_daily(
            [{"symbol": "sh510300", "date": self._days[-1],
              "shares": 1e8, "nav": 0.90}])
        conn = data_store.get_conn()
        conn.execute("INSERT INTO etf_quote_ext (symbol, date, premium_ratio) "
                     "VALUES ('sh510300', ?, 1.5)", (self._days[-1],))
        conn.commit()
        conn.close()
        maps = data_store.load_etf_factor_maps()
        self.assertAlmostEqual(maps["premium"][self._days[-1]]["sh510300"], 1.5)


class ToggleTest(unittest.TestCase):

    def test_env_toggle(self):
        old = os.environ.pop("ETF_OFFICIAL_ENABLED", None)
        try:
            self.assertTrue(es.env_enabled())
            os.environ["ETF_OFFICIAL_ENABLED"] = "0"
            self.assertFalse(es.env_enabled())
        finally:
            os.environ.pop("ETF_OFFICIAL_ENABLED", None)
            if old is not None:
                os.environ["ETF_OFFICIAL_ENABLED"] = old


if __name__ == "__main__":
    unittest.main(verbosity=2)
