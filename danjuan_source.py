"""蛋卷（danjuanfunds）指数估值数据源 —— 免费、无需 Key

用于给 ETF 提供 PE/PB 历史（ETF 本身没有估值，取「跟踪指数」的口径）:
  GET /djapi/index_eva/dj?page=1&size=200        全量估值指数清单 (63 个)
  GET /djapi/index_eva/detail/{code}             最新 PE/PB 与历史百分位
  GET /djapi/index_eva/pe_history/{code}?day=all PE 周频历史 (2016-11 起)
  GET /djapi/index_eva/pb_history/{code}?day=all PB 周频历史

注意:
  1. 接口必须伪装成浏览器 UA，否则被拦截；
  2. 历史是**周频**（约 516 个点），不是日频；
  3. 返回的是指数估值，不是 ETF 净值/溢价率，界面需注明「跟踪指数口径」。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

BASE = "https://danjuanfunds.com/djapi/index_eva"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


class DanjuanError(RuntimeError):
    """蛋卷接口调用失败（网络 / 非 0 result_code）"""


def _get_json(url: str, timeout: float = 20.0, retries: int = 2) -> dict:
    """带 UA / 超时 / 重试的 GET，失败抛 DanjuanError"""
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            rc = payload.get("result_code", 0)
            if rc not in (0, "0"):
                raise DanjuanError(f"result_code={rc} {payload.get('message') or ''}".strip())
            return payload
        except DanjuanError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            last = exc
            if i < retries:
                time.sleep(0.5 * (i + 1))
    raise DanjuanError(f"请求失败 {url}: {last}")


def _ts_to_day(ts) -> str:
    """毫秒时间戳 -> YYYY-MM-DD（UTC 口径，与接口自带的 date 字段一致）"""
    return datetime.fromtimestamp(float(ts) / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")


def fetch_index_list() -> list[dict]:
    """全部可估值指数 [{"index_code": "SH000300", "name": "沪深300"}, ...]"""
    data = _get_json(f"{BASE}/dj?page=1&size=200").get("data") or {}
    items = data.get("items") or []
    out = []
    for it in items:
        code, name = it.get("index_code"), it.get("name")
        if code and name:
            out.append({"index_code": str(code), "name": str(name)})
    return out


def fetch_detail(code: str) -> dict | None:
    """单个指数最新估值；指数不存在时返回 None"""
    data = (_get_json(f"{BASE}/detail/{code}").get("data") or {})
    if not data.get("index_code"):
        return None
    return data


def fetch_history(code: str, field: str = "pe") -> list[tuple[str, float]]:
    """指数 PE/PB 全历史 [(YYYY-MM-DD, 值), ...]，按日期升序去重

    field: "pe" / "pb"。值为 0（指数亏损或缺失）的点会被丢弃。
    """
    field = str(field).lower()
    if field not in ("pe", "pb"):
        raise ValueError("field 只能是 pe 或 pb")
    payload = _get_json(f"{BASE}/{field}_history/{code}?day=all")
    arr = (payload.get("data") or {}).get(f"index_eva_{field}_growths") or []
    out = {}
    for item in arr:
        v = item.get(field)
        ts = item.get("ts")
        if v is None or ts is None:
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if v <= 0:            # 0 / 负数表示亏损或缺失，不能参与估值排序
            continue
        out[_ts_to_day(ts)] = v
    return sorted(out.items())


def fetch_valuation(code: str) -> dict:
    """一次拿齐某指数的 PE/PB 历史: {code, name, pe: [...], pb: [...]}"""
    detail = fetch_detail(code) or {}
    return {
        "code": code,
        "name": detail.get("name") or "",
        "pe": fetch_history(code, "pe"),
        "pb": fetch_history(code, "pb"),
    }
