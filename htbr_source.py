# -*- coding: utf-8 -*-
"""华泰柏瑞 ETF 数据源客户端

职责：把技能自带的 scripts/api_client.py 包装成可编程调用的数据源：
  - 串行限流（最小间隔 3 秒，技能要求）
  - 本地配额计数与硬闸（每日 500 次，留 50 次余量给人工查询）
  - 统一错误语义（凭据 / 配额 / 频率 / 网关不可用 / 接口不存在）

技能目录：vendor/htbr（由 deploy_update.py 一并部署到容器）
凭据：~/.htbr/credentials.json（可由 HTBR_HOME 指定上级目录），不进版本库
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.join(BASE_DIR, "vendor", "htbr")
CLIENT = os.path.join(SKILL_DIR, "scripts", "api_client.py")
PYTHON = sys.executable or "python3"

MIN_INTERVAL = 3.0          # 技能建议调用间隔 ≥3 秒
DAILY_LIMIT = 500           # 网关每日配额
SAFE_LIMIT = 450            # 本地硬闸，保留余量
RATE_LIMIT_WAIT = 6.0       # 429 后等待秒数


class HtbrError(Exception):
    """数据源异常基类"""


class HtbrNoCredential(HtbrError):
    """凭据缺失/失效（技能退出码 3）"""


class HtbrQuotaExceeded(HtbrError):
    """当日配额耗尽或触及本地硬闸"""


class HtbrRateLimited(HtbrError):
    """频率超限，重试后仍失败"""


class HtbrUnavailable(HtbrError):
    """网关/接口不可用（503、超时、404 等）"""


_last_call_at = 0.0
_mem_quota = {"date": "", "used": 0}   # data_store 不可用时的内存兜底


# ==================== 配额计数 ====================

def _quota_key() -> str:
    return "htbr_calls_" + datetime.now().strftime("%Y%m%d")


def quota_used() -> int:
    """当日已用调用次数（持久化在 app_settings；失败时退回内存计数）"""
    try:
        from data_store import get_setting
        return int(get_setting(_quota_key(), "0") or 0)
    except Exception:
        today = datetime.now().strftime("%Y%m%d")
        return _mem_quota["used"] if _mem_quota["date"] == today else 0


def _bump_quota(n: int = 1) -> int:
    used = quota_used() + n
    try:
        from data_store import set_setting
        set_setting(_quota_key(), str(used))
    except Exception:
        _mem_quota.update(date=datetime.now().strftime("%Y%m%d"), used=used)
    return used


def quota_status() -> dict:
    used = quota_used()
    return {"date": datetime.now().strftime("%Y-%m-%d"), "used": used,
            "limit": DAILY_LIMIT, "safe_limit": SAFE_LIMIT,
            "remain": max(0, SAFE_LIMIT - used)}


# ==================== 可用性 ====================

def _credentials_path() -> Path:
    home = os.environ.get("HTBR_HOME") or str(Path.home())
    return Path(home) / ".htbr" / "credentials.json"


def available() -> tuple[bool, str]:
    """检查技能运行时与凭据是否就绪，返回 (可用, 原因)"""
    if not os.path.exists(CLIENT):
        return False, "未找到技能运行时 vendor/htbr/scripts/api_client.py"
    path = _credentials_path()
    if not path.exists():
        return False, f"未配置 API Key（缺少 {path}）"
    return True, ""


# ==================== 调用 ====================

def _respect_interval() -> None:
    """保证与上一次真实调用间隔 ≥ MIN_INTERVAL"""
    global _last_call_at
    gap = time.time() - _last_call_at
    if _last_call_at and gap < MIN_INTERVAL:
        time.sleep(MIN_INTERVAL - gap)
    _last_call_at = time.time()


def _run(api_id: str, payload: dict) -> tuple[int, str, str]:
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    proc = subprocess.run(
        [PYTHON, CLIENT, "invoke", api_id, json.dumps(payload, ensure_ascii=False)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=180, env=env)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _parse_json(text: str) -> dict:
    """从 stdout 中截取统一响应 JSON（忽略前置提示文本）"""
    start = text.find("{")
    if start < 0:
        raise HtbrUnavailable("返回内容不是 JSON")
    try:
        return json.loads(text[start:])
    except ValueError as e:
        raise HtbrUnavailable(f"返回 JSON 解析失败: {e}") from e


def invoke(api_id: str, payload: dict) -> dict:
    """调用业务接口，返回完整统一响应 {"success":..., "data":..., "meta":...}

    异常语义见模块头；调用失败时不会返回半成品数据。
    """
    ok, reason = available()
    if not ok:
        raise HtbrNoCredential(reason)
    if quota_used() >= SAFE_LIMIT:
        raise HtbrQuotaExceeded(f"已触及本地配额硬闸 {SAFE_LIMIT}/{DAILY_LIMIT}，今日停止调用")

    rc, out, err = 0, "", ""
    for attempt in (1, 2):
        _respect_interval()
        rc, out, err = _run(api_id, payload)
        _bump_quota()
        if rc == 0:
            break
        if rc == 3:
            raise HtbrNoCredential("API Key 不可用（技能退出码 3）")
        if "配额已用尽" in err:
            raise HtbrQuotaExceeded(err.strip())
        if "请求过于频繁" in err or "RATE_LIMITED" in err:
            if attempt == 1:
                time.sleep(RATE_LIMIT_WAIT)
                continue
            raise HtbrRateLimited(err.strip())
        raise HtbrUnavailable(f"{api_id} 调用失败(rc={rc}): {err.strip()[:300]}")

    resp = _parse_json(out)
    if not isinstance(resp, dict) or not resp.get("success"):
        raise HtbrUnavailable(f"{api_id} 返回未成功: {str(resp)[:300]}")
    return resp


def data_of(resp: dict):
    """取 $.data（统一响应容器）"""
    return (resp or {}).get("data")


def records_of(resp: dict, key: str = "records") -> list:
    """取 $.data[key] 列表；无数据返回空列表"""
    data = data_of(resp)
    if isinstance(data, dict):
        val = data.get(key)
        if isinstance(val, list):
            return val
    if isinstance(data, list):
        return data
    return []


# ==================== 接口薄封装 ====================

def resolve_inner_code(secu_code: str) -> str | None:
    """6 位场内代码 → 基金内码（零命中返回 None，多候选取第一个并记日志）"""
    resp = invoke("fund-resolve", {"secuCode": str(secu_code)})
    cands = records_of(resp, "candidates") or (data_of(resp) or {}).get("candidates") or []
    if not cands:
        return None
    return str(cands[0].get("innerCode"))


def daily_quote(secu_code: str, begin: str, end: str) -> list:
    """场内基金日行情（不复权）"""
    resp = invoke("fund-daily-quote", {
        "secuCode": str(secu_code), "beginDate": begin, "endDate": end,
        "adjustingMethod": 3})
    data = data_of(resp)
    if isinstance(data, dict):
        return data.get("quotes") or []
    return data if isinstance(data, list) else []


def capital_report(inner_code, begin: str, end: str) -> list:
    """ETF 资金报表（unit=3：金额万元 / 份额万份）"""
    resp = invoke("etf-capital-report", {
        "innerCode": inner_code, "beginDate": begin, "endDate": end, "unit": 3})
    return records_of(resp)


def margin(inner_code, begin: str, end: str) -> list:
    """ETF 两融明细（indexCycle=1 日频，unit=3 万元）"""
    resp = invoke("etf-margin", {
        "innerCode": inner_code, "beginDate": begin, "endDate": end,
        "indexCycle": 1, "unit": 3})
    return records_of(resp)


def market_flow(begin: str, end: str) -> list:
    """全市场 ETF 资金流统计（begin/end 为 yyyyMMdd）"""
    resp = invoke("etf-capital-flow-stats", {
        "beginDate": begin, "endDate": end, "indexCycle": 1, "unit": 1})
    return records_of(resp)


def valuation(inner_code, begin: str, end: str) -> list:
    """持仓加权估值（报告期）；begin 为空表示不设下界"""
    payload = {"innerCode": inner_code}
    if begin:
        payload["beginDate"] = begin
    if end:
        payload["endDate"] = end
    resp = invoke("fund-valuation", payload)
    return records_of(resp)
