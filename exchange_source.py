# -*- coding: utf-8 -*-
"""ETF 官方数据源: 交易所份额 + 东财单位净值

与 `htbr_source.py` 的分工:
  - 这里只拿「交易所/东财公开接口就能拿到」的字段 —— 沪市 ETF 场内份额、单位净值;
  - 华泰柏瑞继续负责两融、持仓估值、全市场资金流等官方/东财没有的数据。

两个接口都不是正式 API, 依赖 UA + Referer, 可能随网站改版失效;
因此统一走 `_get_json` 的重试与限速, 失败抛 `SourceError` 由上层降级。

实测 (2026-11-19):
  - 上交所 `STAT_DATE` 可回溯任意历史日, 单次返回当日全市场 (2020-01-02 169 只 / 2026-11-18 925 只);
  - 深交所只有当日规模快照、没有份额, 因此本模块只覆盖沪市 (`sh` 前缀);
  - 东财净值历史可回溯到 2015 年以前, 分页取全量。
"""
import os
import time
from datetime import datetime

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
TIMEOUT = 25
RETRIES = 3
MIN_INTERVAL = 0.6          # 同一批调用的最小间隔(秒), 避免把公开接口打挂
_last_call = [0.0]

SSE_URL = "https://query.sse.com.cn/commonQuery.do"
SSE_SQL_ID = "COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L"
SSE_REFERER = "https://www.sse.com.cn/"

NAV_URL = "https://api.fund.eastmoney.com/f10/lsjz"
NAV_REFERER = "http://fundf10.eastmoney.com/"


class SourceError(RuntimeError):
    """公开接口不可用 (网络/改版/限流)"""


def _throttle():
    gap = time.time() - _last_call[0]
    if gap < MIN_INTERVAL:
        time.sleep(MIN_INTERVAL - gap)
    _last_call[0] = time.time()


def _get_json(url: str, referer: str, params: dict) -> dict:
    """带限速与重试的 GET; 非 JSON/非 200 一律抛 SourceError"""
    last = None
    for i in range(RETRIES):
        _throttle()
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT,
                             headers={"User-Agent": UA, "Referer": referer,
                                      "Accept": "*/*"})
            if r.status_code == 200:
                return r.json()
            last = f"HTTP {r.status_code}"
        except Exception as ex:      # noqa: BLE001 - 公开接口异常类型不可控
            last = f"{type(ex).__name__}: {ex}"
        if i < RETRIES - 1:
            time.sleep(2 * (i + 1))
    raise SourceError(f"{url} 获取失败: {last}")


def _num(v, scale: float = 1.0):
    """公开接口的数值是字符串, 空串/None 返回 None"""
    try:
        if v is None or str(v).strip() == "":
            return None
        return float(v) * scale
    except (TypeError, ValueError):
        return None


def parse_sse_shares(payload: dict) -> list[dict]:
    """解析上交所 ETF 份额响应

    返回 [{symbol, date, shares, name}]; shares 单位「份」(原始 TOT_VOL 为万份)。
    """
    rows = []
    for it in (payload or {}).get("result") or []:
        code = str(it.get("SEC_CODE") or "").strip()
        day = str(it.get("STAT_DATE") or "").strip()
        shares = _num(it.get("TOT_VOL"), 1e4)
        if not code or not day or shares is None:
            continue
        rows.append({"symbol": "sh" + code, "date": day, "shares": shares,
                     "name": str(it.get("SEC_NAME") or "")})
    return rows


def fetch_sse_shares(day: str) -> list[dict]:
    """上交所某个交易日的全市场 ETF 份额 (day 形如 2026-11-18)"""
    out, page, pages = [], 1, 1
    while page <= pages:
        payload = _get_json(SSE_URL, SSE_REFERER, {
            "sqlId": SSE_SQL_ID,
            "isPagination": "true",
            "pageHelp.pageSize": 2000,
            "pageHelp.pageNo": page,
            "STAT_DATE": day,
        })
        out.extend(parse_sse_shares(payload))
        pages = int(((payload or {}).get("pageHelp") or {}).get("pageCount") or 1)
        page += 1
    return out


def parse_nav_list(payload: dict) -> list[dict]:
    """解析东财净值响应; 返回 [{date, nav}]"""
    rows = []
    data = (payload or {}).get("Data") or {}
    for it in data.get("LSJZList") or []:
        day = str(it.get("FSRQ") or "").strip()
        nav = _num(it.get("DWJZ"))
        if day and nav and nav > 0:
            rows.append({"date": day, "nav": nav})
    return rows


def fetch_nav_history(code: str, start: str, end: str,
                      page_size: int = 200, max_pages: int = 60) -> list[dict]:
    """东财 ETF 单位净值历史 (code 为 6 位代码)

    按时间倒序分页返回, 直到某页不足 page_size 或超出 max_pages。
    """
    out, page = [], 1
    while page <= max_pages:
        payload = _get_json(NAV_URL, NAV_REFERER, {
            "fundCode": code, "pageIndex": page, "pageSize": page_size,
            "startDate": start, "endDate": end,
        })
        rows = parse_nav_list(payload)
        out.extend(rows)
        if len(rows) < page_size:
            break
        page += 1
    return out


def lot_share_price(shares: float, close: float, nav: float | None = None) -> float:
    """规模估算: 份额 × 净值; 净值缺失时退回收盘价 (误差即溢价率, 通常 <1%)"""
    price = nav if (nav and nav > 0) else close
    if not shares or not price:
        return 0.0
    return float(shares) * float(price)


def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def env_enabled(name: str = "ETF_OFFICIAL_ENABLED") -> bool:
    """开关: 默认开启, 设为 0 可完全关闭官方源 (排查问题用)"""
    return str(os.environ.get(name, "1")).strip() not in ("0", "false", "False", "")
