# -*- coding: utf-8 -*-
"""同花顺金融数据 API 接入层测试 (不联网, 全部用假响应)"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import data_store
import ths_source as ts


def _ms(day: str) -> int:
    return ts._to_ms(day)


def _envelope(items, ts_ms=1764000000000):
    return {"code": 0, "message": "success", "request_id": "req-1",
            "data": {"timestamp": ts_ms, "item": items}}


def _bar(day: str, close: float = 1.0):
    return {"date_ms": _ms(day), "open_price": close, "high_price": close,
            "low_price": close, "close_price": close,
            "volume": 100.0, "turnover": 100.0}


class CodeConvertTest(unittest.TestCase):
    """项目代码 <-> thscode"""

    def test_to_thscode_variants(self):
        self.assertEqual(ts.to_thscode("sh600519"), "600519.SH")
        self.assertEqual(ts.to_thscode("600519"), "600519.SH")
        self.assertEqual(ts.to_thscode("600519.sh"), "600519.SH")
        self.assertEqual(ts.to_thscode("sz159915"), "159915.SZ")
        self.assertEqual(ts.to_thscode("300750"), "300750.SZ")
        self.assertEqual(ts.to_thscode("430047"), "430047.BJ")
        self.assertEqual(ts.to_thscode("513100"), "513100.SH")

    def test_from_thscode_roundtrip(self):
        for sym in ("sh600519", "sz000001", "sz159915", "sh513100", "bj430047"):
            self.assertEqual(ts.from_thscode(ts.to_thscode(sym)), sym)

    def test_invalid_code_raises(self):
        with self.assertRaises(ts.ThsParamError):
            ts.to_thscode("abc")
        with self.assertRaises(ts.ThsParamError):
            ts.from_thscode("600519")

    def test_guess_asset_type(self):
        self.assertEqual(ts._guess_asset_type("513100.SH"), "etf")
        self.assertEqual(ts._guess_asset_type("159915.SZ"), "etf")
        self.assertEqual(ts._guess_asset_type("600519.SH"), "stock")
        self.assertEqual(ts._guess_asset_type("000001.SZ"), "stock")


class KeyTest(unittest.TestCase):
    """密钥读取与脱敏"""

    def test_env_key_and_mask(self):
        old = os.environ.get(ts.API_KEY_ENV)
        old_loaded = ts._ENV_LOADED
        ts._ENV_LOADED = True                  # 跳过 .env 文件
        try:
            os.environ[ts.API_KEY_ENV] = "sk-fuyao-abcdefghijklmnop"
            self.assertEqual(ts.get_api_key(), "sk-fuyao-abcdefghijklmnop")
            masked = ts.mask_key()
            self.assertNotIn("abcdefghijkl", masked)
            self.assertIn("***", masked)
        finally:
            if old is None:
                os.environ.pop(ts.API_KEY_ENV, None)
            else:
                os.environ[ts.API_KEY_ENV] = old
            ts._ENV_LOADED = old_loaded

    def test_missing_key_raises(self):
        old = os.environ.pop(ts.API_KEY_ENV, None)
        old_loaded = ts._ENV_LOADED
        ts._ENV_LOADED = True
        try:
            with self.assertRaises(ts.ThsAuthError):
                ts.get_api_key()
            self.assertEqual(ts.mask_key("short"), "***")
        finally:
            if old is not None:
                os.environ[ts.API_KEY_ENV] = old
            ts._ENV_LOADED = old_loaded


class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text or str(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class RequestTest(unittest.TestCase):
    """统一请求封装: 错误映射 / 重试 / 限流"""

    def setUp(self):
        self._old_get = ts.requests.get
        self._old_key = ts.get_api_key
        self._old_interval = ts.MIN_INTERVAL
        self._old_backoff = ts.BACKOFF_BASE
        ts.get_api_key = lambda: "test-key"
        ts.MIN_INTERVAL = 0.0
        ts.BACKOFF_BASE = 0.0
        self.calls = []

    def tearDown(self):
        ts.requests.get = self._old_get
        ts.get_api_key = self._old_key
        ts.MIN_INTERVAL = self._old_interval
        ts.BACKOFF_BASE = self._old_backoff

    def _serve(self, *responses):
        seq = list(responses)

        def fake(url, params=None, headers=None, timeout=None):
            self.calls.append({"url": url, "params": params, "headers": headers})
            return seq.pop(0) if len(seq) > 1 else seq[0]

        ts.requests.get = fake

    def test_auth_error_not_retried(self):
        self._serve(_Resp(200, {"code": 2001, "message": "未认证", "request_id": "r1"}))
        with self.assertRaises(ts.ThsAuthError) as ctx:
            ts._http_get_json("/api/meta/tickers/list")
        self.assertEqual(ctx.exception.code, 2001)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["headers"]["X-api-key"], "test-key")

    def test_no_data_error(self):
        self._serve(_Resp(200, {"code": 3001, "message": "标的不存在"}))
        with self.assertRaises(ts.ThsNoDataError):
            ts._http_get_json("/api/a-share/prices/snapshot")

    def test_param_error(self):
        self._serve(_Resp(200, {"code": 1003, "message": "越界"}))
        with self.assertRaises(ts.ThsParamError):
            ts._http_get_json("/api/a-share/prices/historical")

    def test_rate_limit_retried_then_success(self):
        self._serve(_Resp(200, {"code": 4001, "message": "限流"}),
                    _Resp(200, {"code": 4001, "message": "限流"}),
                    _Resp(200, _envelope([])))
        payload = ts._http_get_json("/api/a-share/calendar/trading-days")
        self.assertEqual(payload["code"], 0)
        self.assertEqual(len(self.calls), 3)

    def test_http_429_exhausts_retries(self):
        self._serve(_Resp(429, None, "too many"))
        with self.assertRaises(ts.ThsRateLimitError):
            ts._http_get_json("/api/a-share/calendar/trading-days")
        self.assertEqual(len(self.calls), ts.MAX_RETRIES + 1)

    def test_network_error_retried(self):
        err = ts.requests.RequestException("boom")

        def fake(*a, **kw):
            self.calls.append(1)
            if len(self.calls) < 3:
                raise err
            return _Resp(200, _envelope([]))

        ts.requests.get = fake
        self.assertEqual(ts._http_get_json("/x")["code"], 0)
        self.assertEqual(len(self.calls), 3)

    def test_non_json_response(self):
        self._serve(_Resp(200, None, "html"))
        with self.assertRaises(ts.ThsUpstreamError):
            ts._http_get_json("/x")


class BarsTest(unittest.TestCase):
    """日线: 窗口切分 / 批量 / ETF 5 年限制 / 归一化"""

    def setUp(self):
        self._old_http = ts._http_get_json
        self._old_type = ts.resolve_asset_type
        self.calls = []
        ts.resolve_asset_type = lambda s: "stock"

    def tearDown(self):
        ts._http_get_json = self._old_http
        ts.resolve_asset_type = self._old_type

    def test_stock_window_split_under_10_years(self):
        def fake(path, params=None):
            self.calls.append((path, dict(params or {})))
            blocks = [{"thscode": c, "timestamp": 1, "item": [_bar("2020-01-02")]}
                      for c in params["thscodes"].split(",")]
            return _envelope(blocks)

        ts._http_get_json = fake
        out = ts.fetch_daily_bars(["sh600519"], "2014-01-01", "2026-06-30")
        self.assertEqual(len(self.calls), 2)          # 10 年 + 剩余
        self.assertEqual(self.calls[0][0], "/api/a-share/prices/historical-batch")
        self.assertEqual(self.calls[0][1]["adjust"], "forward")
        # 第一段 2014-01-01 起满 10 年, 到 2023-12-31 截止
        self.assertEqual(ts._from_ms(self.calls[0][1]["start"]), "2014-01-01")
        self.assertEqual(ts._from_ms(self.calls[0][1]["end"]), "2023-12-31")
        self.assertEqual(ts._from_ms(self.calls[1][1]["start"]), "2024-01-01")
        self.assertFalse(out["sh600519"].empty)

    def test_stock_batch_max_10_codes_per_call(self):
        def fake(path, params=None):
            self.calls.append(params)
            return _envelope([{"thscode": c, "timestamp": 1, "item": [_bar("2024-01-02")]}
                              for c in params["thscodes"].split(",")])

        ts._http_get_json = fake
        syms = [f"sh6005{i:02d}" for i in range(12)]
        out = ts.fetch_daily_bars(syms, "2024-01-01", "2024-12-31")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(len(self.calls[0]["thscodes"].split(",")), 10)
        self.assertEqual(len(self.calls[1]["thscodes"].split(",")), 2)
        self.assertEqual(len(out), 12)

    def test_etf_window_out_of_range(self):
        ts.resolve_asset_type = lambda s: "etf"
        with self.assertRaises(ts.ThsRangeError):
            ts.fetch_daily_bars(["sh513100"], "2014-01-01", "2026-11-25")

    def test_etf_non_strict_clips_to_5_years(self):
        ts.resolve_asset_type = lambda s: "etf"

        def fake(path, params=None):
            self.calls.append((path, dict(params or {})))
            return _envelope([{"thscode": "513100.SH", "timestamp": 1,
                               "item": [_bar("2025-01-02")]}])

        ts._http_get_json = fake
        out = ts.fetch_daily_bars(["sh513100"], "2014-01-01", "2026-11-25",
                                  strict=False)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], "/api/fund/market/historical-batch")
        self.assertGreaterEqual(ts._from_ms(self.calls[0][1]["start"]),
                                ts.etf_history_floor())
        self.assertFalse(out["sh513100"].empty)

    def test_lof_rejected(self):
        ts.resolve_asset_type = lambda s: "lof"
        with self.assertRaises(ts.ThsParamError):
            ts.fetch_daily_bars(["sz161725"], "2024-01-01", "2026-01-01")

    def test_bars_frame_date_and_columns(self):
        df = ts._bars_frame([_bar("2024-01-02", 2.5), _bar("2024-01-03", 2.6)])
        self.assertEqual(list(df.columns), ts.BAR_COLUMNS)
        self.assertEqual(df["date"].tolist(), ["2024-01-02", "2024-01-03"])
        self.assertAlmostEqual(float(df["close"].iloc[-1]), 2.6)

    def test_duplicate_dates_merged_across_segments(self):
        seq = [
            _envelope([{"thscode": "600519.SH", "timestamp": 1,
                        "item": [_bar("2020-01-02", 1.0)]}]),
            _envelope([{"thscode": "600519.SH", "timestamp": 1,
                        "item": [_bar("2020-01-02", 1.1), _bar("2020-01-03", 1.2)]}]),
        ]

        def fake(path, params=None):
            return seq.pop(0)

        ts._http_get_json = fake
        out = ts.fetch_daily_bars(["sh600519"], "2014-01-01", "2026-06-30")
        df = out["sh600519"]
        self.assertEqual(len(df), 2)
        self.assertAlmostEqual(float(df.loc[df["date"] == "2020-01-02", "close"].iloc[0]), 1.1)


class SnapshotTest(unittest.TestCase):
    """快照归一化"""

    def setUp(self):
        self._old_http = ts._http_get_json
        self._old_type = ts.resolve_asset_type
        ts.resolve_asset_type = lambda s: "etf" if s.startswith("sh513") else "stock"

    def tearDown(self):
        ts._http_get_json = self._old_http
        ts.resolve_asset_type = self._old_type

    def test_etf_and_stock_rows(self):
        def fake(path, params=None):
            if path == "/api/fund/market/snapshot":
                return _envelope([{"thscode": "513100.SH", "last_price": 2.088,
                                   "price_change_ratio_pct": 1.9, "open_price": 2.0,
                                   "high_price": 2.1, "low_price": 1.99,
                                   "prev_price": 2.049, "volume": 100, "turnover": 200,
                                   "turnover_ratio_pct": 0.7}], ts_ms=1704124800000)
            return _envelope([{"thscode": "600519.SH", "last_price": 1465.19,
                               "price_change_ratio_pct": -0.65, "open_price": 1470,
                               "high_price": 1470, "low_price": 1456,
                               "prev_price": 1474.84, "volume": 10, "turnover": 20}],
                             ts_ms=1704124800000)

        ts._http_get_json = fake
        rows = ts.fetch_snapshot(["sh513100", "sh600519"])
        by_sym = {r["symbol"]: r for r in rows}
        self.assertEqual(set(by_sym), {"sh513100", "sh600519"})
        self.assertEqual(by_sym["sh513100"]["ts"], "2024-01-02 00:00:00")
        self.assertAlmostEqual(by_sym["sh513100"]["turnover"], 0.7)
        self.assertIsNone(by_sym["sh600519"]["turnover"])
        self.assertAlmostEqual(by_sym["sh600519"]["pre_close"], 1474.84)


class FinancialTest(unittest.TestCase):
    """财务映射"""

    def setUp(self):
        self._old_inc = ts.fetch_financials
        self._old_ind = ts.fetch_financial_indicators

    def tearDown(self):
        ts.fetch_financials = self._old_inc
        ts.fetch_financial_indicators = self._old_ind

    def test_build_fundamental_rows(self):
        inc = [{"thscode": "600519.SH", "period": "quarterly", "fiscal_year": 2024,
                "fiscal_period": "Q4", "report_date_ms": _ms("2025-03-29"),
                "period_end_ms": _ms("2024-12-31"),
                "operating_income": 1741.0, "operating_profit": 1240.0,
                "parent_holder_net_profit": 860.0, "basic_eps": 68.5}]
        bal = [{"period_end_ms": _ms("2024-12-31"), "assets_total": 1000.0,
                "total_debt": 250.0}]

        ts.fetch_financials = lambda sym, kind="income", *a, **kw: inc if kind == "income" else bal
        ts.fetch_financial_indicators = lambda sym, report: {
            "sale_gross_margin": 89.12, "index_weighted_avg_roe": 30.5,
            "net_profit_yoy_growth_ratio": 15.2, "operating_income_yoy_growth_ratio": 12.3}

        rows = ts.build_fundamental_rows("sh600519", quarters=1)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["symbol"], "sh600519")
        self.assertEqual(r["report_date"], "20241231")
        self.assertEqual(r["ann_date"], "2025-03-29")
        self.assertAlmostEqual(r["eps"], 68.5)
        self.assertAlmostEqual(r["revenue"], 1741.0)
        self.assertAlmostEqual(r["net_profit"], 860.0)
        self.assertAlmostEqual(r["debt_ratio"], 25.0)      # 250/1000*100
        self.assertAlmostEqual(r["roe"], 30.5)
        self.assertAlmostEqual(r["gross_margin"], 89.12)
        self.assertNotIn("bps", r)                         # 接口无此口径, 不臆造

    def test_indicator_aliases(self):
        """真实响应用 calculate_* 前缀, 与文档字段名不同, 需要兼容"""
        inc = [{"thscode": "600519.SH", "period": "quarterly", "fiscal_year": 2025,
                "fiscal_period": "Q3", "report_date_ms": _ms("2025-10-30"),
                "period_end_ms": _ms("2025-09-30"), "operating_income": 100.0,
                "parent_holder_net_profit": 50.0, "basic_eps": 4.0}]
        ts.fetch_financials = lambda sym, kind="income", *a, **kw: inc if kind == "income" else []
        ts.fetch_financial_indicators = lambda sym, report: {
            "calculate_parent_holder_net_profit_yoy_growth_ratio": 7.7,
            "calculate_operating_income_yoy_growth_ratio": 8.8}
        r = ts.build_fundamental_rows("sh600519", quarters=1)[0]
        self.assertAlmostEqual(r["profit_yoy"], 7.7)
        self.assertAlmostEqual(r["revenue_yoy"], 8.8)

    def test_indicator_percent_values(self):
        def fake(path, params=None):
            return {"code": 0, "message": "ok", "data": {"abilities": [
                {"ability": "profitability", "indicators": [
                    {"index_id": "sale_gross_margin", "value": "89.12000000"},
                    {"index_id": "earned_interest_multiple", "value": None}]}]}}

        old = ts._http_get_json
        ts._http_get_json = fake
        try:
            ind = ts.fetch_financial_indicators("sh600519", "2025-1")
        finally:
            ts._http_get_json = old
        self.assertAlmostEqual(ind["sale_gross_margin"], 89.12)
        self.assertIsNone(ind["earned_interest_multiple"])


class StoreIntegrationTest(unittest.TestCase):
    """落库集成 (临时库)"""

    def setUp(self):
        self._old_db = data_store.DB_PATH
        self._tmp = tempfile.mkdtemp()
        data_store.DB_PATH = os.path.join(self._tmp, "t.db")
        data_store.init_db()
        self._old_fetch = ts.fetch_daily_bars
        self._old_type = ts.resolve_asset_type
        ts.resolve_asset_type = lambda s: "etf"

    def tearDown(self):
        data_store.DB_PATH = self._old_db
        ts.fetch_daily_bars = self._old_fetch
        ts.resolve_asset_type = self._old_type

    def test_sync_daily_bars_writes_and_covers(self):
        import pandas as pd
        df = pd.DataFrame({
            "date": ["2026-11-24", "2026-11-25"],
            "open": [2.0, 2.1], "high": [2.1, 2.2], "low": [1.9, 2.0],
            "close": [2.05, 2.15], "volume": [1.0, 1.0], "amount": [2.0, 2.0]})
        ts.fetch_daily_bars = lambda *a, **kw: {"sh513100": df}

        report = ts.sync_daily_bars(["sh513100"], "2026-11-24", "2026-11-25")
        self.assertEqual(report["rows"], 2)
        self.assertEqual(report["written"], 1)
        conn = sqlite3.connect(data_store.DB_PATH)
        row = conn.execute("SELECT COUNT(*), MAX(asset_type) FROM daily_bar "
                           "WHERE symbol='sh513100'").fetchone()
        conn.close()
        self.assertEqual(row[0], 2)
        self.assertEqual(row[1], "etf")

    def test_sync_daily_bars_records_clipped_range(self):
        import pandas as pd
        ts.fetch_daily_bars = lambda *a, **kw: {
            "sh513100": pd.DataFrame(columns=ts.BAR_COLUMNS)}
        report = ts.sync_daily_bars(["sh513100"], "2014-01-01", "2026-11-25")
        self.assertEqual(report["empty"], ["sh513100"])
        self.assertTrue(report["clipped"])
        self.assertEqual(report["rows"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
