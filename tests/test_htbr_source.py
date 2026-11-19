# -*- coding: utf-8 -*-
"""华泰柏瑞 ETF 数据源接入测试（不访问网络 / 不写真实库）

运行: py -3.13 -m unittest tests.test_htbr_source -v
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import htbr_source as hs          # noqa: E402
import data_manager as dm         # noqa: E402


class TestNormalize(unittest.TestCase):
    def test_quote_units(self):
        recs = [{
            "tradingday": "2026-11-18", "turnovervalue": "157667.793",
            "turnovervolume": "3.796", "turnoverrate": "3.625", "avgprice": "4.154",
            "changepct": "-0.024", "rangepct": "0.77", "discountratio": "0.065",
            "premiumrate": None, "ifsuspend": "未停牌",
        }]
        r = dm._norm_quote_rows(recs)[0]
        self.assertEqual(r["date"], "2026-11-18")
        self.assertAlmostEqual(r["turnover_value"], 157667.793e4, places=3)
        self.assertAlmostEqual(r["turnover_volume"], 3.796e8, places=3)
        self.assertEqual(r["turnover_rate"], 3.625)
        self.assertIsNone(r["premium_ratio"])      # 空值不写 0
        self.assertEqual(r["is_suspend"], 0)

    def test_quote_suspended(self):
        r = dm._norm_quote_rows([{"tradingday": "2026-11-18", "ifsuspend": "停牌"}])[0]
        self.assertEqual(r["is_suspend"], 1)

    def test_capital_units(self):
        recs = [{
            "tradingday": "2026-11-18", "unitnv": 4.0585, "closepricelist": 4.058,
            "exchangeshares": 4968408.77, "marketscale": 20164286.993045,
            "netsubscriptionshares": 134100.0, "financenetbuyvalue": -0.33,
            "financebalancevalue": 322194.2, "near3daynetbuy": 0.0926,
            "near5daynetbuy": -1.1292, "near20daynetbuy": -2.1243,
            "near60daynetbuy": 5.0134,
        }]
        r = dm._norm_capital_rows(recs)[0]
        self.assertAlmostEqual(r["exchange_shares"], 4968408.77e4, places=1)
        self.assertAlmostEqual(r["market_scale"], 20164286.993045e4, places=1)
        # 规模 == 净值 × 份额（单位统一后自洽）
        ratio = r["market_scale"] / (r["unit_nv"] * r["exchange_shares"])
        self.assertAlmostEqual(ratio, 1.0, places=6)

    def test_margin_units(self):
        recs = [{
            "tradingday": "2026-11-18", "financebuyvalue": 100.5,
            "financerefundvalue": 80.5, "financenetbuyvalue": 20.0,
            "securitysellvolume": 3.0, "securityrefundvolume": 1.0,
            "securitynetsellvolume": 2.0, "financevalue": 322194.2,
            "securityvalue": 50.0, "securityvolume": 8.0,
            "negotiablemvoffinance": 1.2, "negotiablemvofsecurity": 0.1,
        }]
        r = dm._norm_margin_rows(recs)[0]
        self.assertAlmostEqual(r["finance_buy"], 100.5e4, places=3)
        self.assertEqual(r["security_sell_vol"], 3.0)   # 数量类不换单位
        self.assertEqual(r["finance_pct"], 1.2)

    def test_flow_categories(self):
        recs = [{"enddate": "2026-11-18",
                 "alletftotalbuyvalue": 10, "alletftotalsellvalue": 8,
                 "alletfnetbuyvalue": 2, "alletfnum": 927,
                 "stocketfnetbuyvalue": 1.5, "stocketfnum": 800}]
        rows = dm._norm_flow_rows(recs)
        cats = {r["category"]: r for r in rows}
        self.assertEqual(set(cats), {"all", "stock", "bond", "commodity", "monetary", "qdii"})
        self.assertEqual(cats["all"]["net_value"], 2)
        self.assertEqual(cats["all"]["etf_count"], 927)
        self.assertEqual(cats["stock"]["net_value"], 1.5)
        self.assertIsNone(cats["bond"]["net_value"])    # 缺失写 None


class TestClient(unittest.TestCase):
    def test_parse_json_skips_prefix(self):
        resp = hs._parse_json('提示文本\n{"success": true, "data": {}}')
        self.assertTrue(resp["success"])

    def test_quota_gate(self):
        with mock.patch.object(hs, "available", return_value=(True, "")), \
             mock.patch.object(hs, "quota_used", return_value=hs.SAFE_LIMIT):
            with self.assertRaises(hs.HtbrQuotaExceeded):
                hs.invoke("fund-resolve", {"secuCode": "510300"})

    def test_no_credential(self):
        with mock.patch.object(hs, "available", return_value=(False, "未配置 API Key")):
            with self.assertRaises(hs.HtbrNoCredential):
                hs.invoke("fund-resolve", {"secuCode": "510300"})

    def test_success(self):
        with mock.patch.object(hs, "available", return_value=(True, "")), \
             mock.patch.object(hs, "quota_used", return_value=0), \
             mock.patch.object(hs, "_bump_quota", return_value=1), \
             mock.patch.object(hs, "_respect_interval"), \
             mock.patch.object(hs, "_run",
                               return_value=(0, '{"success": true, "data": {"ok": 1}}', "")):
            resp = hs.invoke("fund-resolve", {"secuCode": "510300"})
        self.assertEqual(resp["data"]["ok"], 1)

    def test_exit_code_3(self):
        with mock.patch.object(hs, "available", return_value=(True, "")), \
             mock.patch.object(hs, "quota_used", return_value=0), \
             mock.patch.object(hs, "_bump_quota", return_value=1), \
             mock.patch.object(hs, "_respect_interval"), \
             mock.patch.object(hs, "_run", return_value=(3, "", "[NEED_KEY]")):
            with self.assertRaises(hs.HtbrNoCredential):
                hs.invoke("fund-resolve", {"secuCode": "510300"})

    def test_quota_exceeded_message(self):
        with mock.patch.object(hs, "available", return_value=(True, "")), \
             mock.patch.object(hs, "quota_used", return_value=0), \
             mock.patch.object(hs, "_bump_quota", return_value=1), \
             mock.patch.object(hs, "_respect_interval"), \
             mock.patch.object(hs, "_run",
                               return_value=(1, "", "今日调用配额已用尽（每日 500 次）")):
            with self.assertRaises(hs.HtbrQuotaExceeded):
                hs.invoke("fund-resolve", {"secuCode": "510300"})

    def test_records_of_variants(self):
        self.assertEqual(hs.records_of({"data": {"records": [1]}}), [1])
        self.assertEqual(hs.records_of({"data": [2]}), [2])
        self.assertEqual(hs.records_of({"data": {}}), [])


class TestOrchestration(unittest.TestCase):
    def test_skips_without_credential(self):
        with mock.patch.object(hs, "available", return_value=(False, "未配置 API Key")):
            r = dm.ensure_etf_extras()
        self.assertEqual(r["symbols"], 0)
        self.assertIn("skipped", r)


if __name__ == "__main__":
    unittest.main(verbosity=2)
