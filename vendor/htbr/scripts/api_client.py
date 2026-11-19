#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""基金数据查询 Skill — 零依赖 HTTP 客户端（纯标准库，借鉴方正 api_client.py 风格）。

用法:
  python3 scripts/api_client.py invoke <api_id> '<json参数>'

凭证: 用户级 JSON 凭据文件 ~/.htbr/credentials.json（唯一来源；Key 由用户在
      etf-skill 官网自助获取，保存统一走 `python <技能目录>/config.py save-key "<Key>"`）→
      换短期 Token（缓存复用、临期刷新）→ Bearer 调用（见 config.py）。
      认证路径每次调用 config.read_api_key() 现读文件，换 Key 后立即生效，不沿用旧值。
契约: docs/htbr-ai-skill/02-接口契约.md（认证接口 §3；错误码 §2.3）。
安全: 任何输出/异常信息不包含 api_key/token 明文。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib import error, request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import CredentialsCorruptError, read_api_key, BASE_URL
from guardrails import finalize_key_guide, KEY_GUIDE  # 引导文案唯一权威来源，不得内联第二份

try:  # 审计是增强件不是依赖：凭证退出路径绝不因缺 guide_audit.py 而崩
    from guide_audit import record_emit
except ImportError:  # pragma: no cover
    record_emit = None

SKILL_ID = "htbr-etf-skill"
TIMEOUT = 300

# 强制环节提醒：调用成功后把「回复前必须走 scripts/finalize.py」打印到 stderr，
# 让调用方在组装回复前恰好看到这条硬约束（只走 stderr，不影响 stdout JSON 解析）。
GUARDRAIL_REMINDER = (
    "[强制] 最终回复用户的正文必须由本技能 scripts/finalize.py 产出"
    "（自动校验：结论先至 / 禁用语 / 内部细节泄漏 / 无效值，通过后追加品牌尾注），"
    '用法：python "<SKILL_DIR>/scripts/finalize.py" - < 正文文件，'
    "或把正文写入文件后作为参数传入；正文首段须以「结论：」开头，不得手写回复正文、不得自行粘贴尾注。"
)
TOKEN_REFRESH_WINDOW = 120  # 距 exp ≤120s 时刷新（契约 §3.2 refresh_window）

# ---- Skill 更新自动检查（v0.1.0 起内嵌，不依赖调用方自觉） ----
# 版本检查绑定在业务调用链上：每次会话首次调用 invoke 时自动静默执行一次，
# 用本地标记文件控制频次（脚本侧无法感知会话边界，以时间间隔近似"每会话首次"）。
# 发现新版时把 [SKILL_UPDATE_REQUIRED] 强提示打到 stderr，复用 GUARDRAIL_REMINDER
# "组装回复前必然看到"的模式；任何失败（404/网络/鉴权）都静默跳过，绝不阻塞业务。
SKILL_ROOT = Path(__file__).resolve().parent.parent
VERSION_CHECK_MARKER = SKILL_ROOT / ".version_check"   # 记录上次检查时间戳（epoch 秒）
VERSION_CHECK_INTERVAL = 3                            # 距上次检查 ≥3s 才重新检查
FRONTMATTER_VERSION = re.compile(r"^version\s*:\s*(\S+)\s*$", re.M)
_update_reminder: str | None = None  # invoke 成功后由 main() 追加打印到 stderr

# 模块级 Token 缓存（仅内存，不落盘）
_token_cache: dict[str, Any] = {"access_token": None, "exp": 0}


def _post(path: str, payload: dict, bearer: str | None = None) -> tuple[int, Any, dict]:
    """POST JSON，返回 (http_status, 解析后的响应dict或原始文本, 响应头)。"""
    url = f"{BASE_URL.rstrip('/')}{path}"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json;charset=UTF-8"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    req = request.Request(url, data=body, headers=headers, method="POST")
    try:
        with request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, _parse_json(raw), dict(resp.headers)
    except error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        return e.code, _parse_json(raw), dict(e.headers)
    except error.URLError as e:
        raise SystemExit(f"网络请求失败，请检查网络或稍后重试（{e.reason}）") from e


def _parse_json(raw: str) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _err_code(resp: Any) -> str:
    """从统一响应中取 error.code（响应非 JSON 或结构异常时返回空串）。"""
    if isinstance(resp, dict):
        err = resp.get("error") or {}
        return str(err.get("code") or "") if isinstance(err, dict) else ""
    return ""


def _trace_id(resp: Any) -> str:
    if isinstance(resp, dict):
        meta = resp.get("meta") or {}
        return str(meta.get("traceId") or "") if isinstance(meta, dict) else ""
    return ""


def _get_token(force_refresh: bool = False) -> str:
    """用 API Key 换短期 Token（契约 §3.2），模块级缓存复用，临期自动刷新。

    Key 每次经 config.read_api_key() 现读凭据文件（不沿用 import 期快照）；
    凭据文件损坏时明确报错退出，不静默当作未配置。

    401 KEY_INVALID/KEY_EXPIRED/KEY_REVOKED: 中文引导后退出，不重试。
    400 PARAM_INVALID（nonce/timestamp 类）: 换新 nonce/timestamp 重试一次。
    """
    now = time.time()
    if (not force_refresh and _token_cache["access_token"]
            and now < _token_cache["exp"] - TOKEN_REFRESH_WINDOW):
        return _token_cache["access_token"]

    try:
        api_key = read_api_key()
    except CredentialsCorruptError as e:
        raise SystemExit(f"{e}；请先修复或删除该文件后重新 save-key。")
    if not api_key:
        _exit_need_key()

    def _do_request() -> tuple[int, Any, dict]:
        payload = {
            "apiKey": api_key,
            "skillId": SKILL_ID,
            "nonce": str(uuid.uuid4()),
            "timestamp": int(time.time()),
        }
        return _post("/auth/token", payload)

    status, resp, _ = _do_request()
    if status == 400 and _err_code(resp) == "PARAM_INVALID":
        # nonce 重复/时间戳过期：换新参数重试一次
        status, resp, _ = _do_request()

    if status == 200 and isinstance(resp, dict) and resp.get("success"):
        data = resp.get("data") or {}
        _token_cache["access_token"] = data.get("accessToken")
        _token_cache["exp"] = now + int(data.get("expiresIn") or 0)
        return _token_cache["access_token"]

    code = _err_code(resp)
    if code in ("KEY_INVALID", "KEY_EXPIRED", "KEY_REVOKED"):
        _exit_need_key(code)

    if status == 401:
        raise SystemExit("认证失败（Key 校验未通过），请确认已通过 config.py save-key 保存有效 Key；问题持续请联系管理员。")
    if status == 429:
        _exit_rate_limited(resp, {})
    _fail_generic(status, resp, "获取访问令牌失败")


def local_version() -> str:
    """读本地 SKILL.md frontmatter 的 version 字段；缺失视为空（触发更新提示）。"""
    try:
        text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    except OSError:
        return ""
    m = FRONTMATTER_VERSION.search(text)
    return m.group(1) if m else ""


def _maybe_check_update() -> None:
    """静默版本检查：每会话首次业务调用自动触发一次（频次由本地标记文件控制）。

    发现新版时把 [SKILL_UPDATE_REQUIRED] 强提示写入 _update_reminder，由 main()
    在业务成功后随 GUARDRAIL_REMINDER 一并打到 stderr。任何失败（404/网络/鉴权）
    都静默跳过并记录标记，绝不阻塞业务调用。
    """
    global _update_reminder
    now = time.time()
    try:
        has_key = bool(read_api_key())
    except CredentialsCorruptError:
        has_key = False
    if not has_key:
        return  # 无 Key：跳过版本检查，避免在引导路径上重复打印凭证文案
    try:
        last = float(VERSION_CHECK_MARKER.read_text(encoding="utf-8").strip())
        if now - last < VERSION_CHECK_INTERVAL:
            return
    except (OSError, ValueError):
        pass  # 无标记/损坏标记：当作首次检查

    try:
        status, resp, _ = _post(
            "/skill/version/check",
            {"skillId": SKILL_ID, "currentVersion": local_version()},
            bearer=_get_token(),
        )
        if status == 200 and isinstance(resp, dict) and resp.get("success"):
            d = resp.get("data") or {}
            if d.get("updateRequired"):
                latest = d.get("latestVersion") or "未知"
                changelog = str(d.get("changelog") or "").strip()
                _update_reminder = (
                    "[SKILL_UPDATE_REQUIRED] 本 Skill 有新版本"
                    f"（本地 {local_version() or '未知'} → 最新 {latest}）。\n"
                    "必须先向当前对话的用户展示版本差异与变更说明，并询问是否更新；"
                    "用户明确同意后，才能执行："
                    'python "<SKILL_DIR>/scripts/skill_updater.py" update --confirm'
                    "（update 命令强制要求 --confirm，不带该参数会直接拒绝执行）；\n"
                    "用户拒绝则按当前本地版本继续使用，不得擅自更新、不得自行绕过。"
                )
                if changelog:
                    _update_reminder += "\n变更说明: " + changelog
    except SystemExit:
        pass  # 鉴权/网络/接口不存在等：静默跳过，不阻塞业务
    finally:
        try:
            VERSION_CHECK_MARKER.write_text(str(int(now)), encoding="utf-8")
        except OSError:
            pass


def invoke(api_id: str, payload: dict) -> dict:
    """调用业务接口 POST /business/{api_id}:invoke（Bearer Token）。

    401 TOKEN_EXPIRED/TOKEN_INVALID: 强制换新 Token 后重试一次。
    403 SCOPE_DENIED / 404 API_NOT_FOUND / 429 / 5xx: 按 §2.3 话术退出。
    """
    _maybe_check_update()  # 版本自动检查：静默、不阻塞；发现新版只置提醒，不打断本次调用
    path = f"/business/{api_id}:invoke"

    def _do_call(token: str) -> tuple[int, Any, dict]:
        return _post(path, payload, bearer=token)

    status, resp, headers = _do_call(_get_token())
    if status == 401 and _err_code(resp) in ("TOKEN_EXPIRED", "TOKEN_INVALID"):
        # 重新换 Token 后重试一次
        status, resp, headers = _do_call(_get_token(force_refresh=True))

    if status == 200 and isinstance(resp, dict) and resp.get("success"):
        return resp  # 完整统一响应（含 meta.trace_id / meta.quota），由调用方展示

    code = _err_code(resp)
    if status == 403 or code == "SCOPE_DENIED":
        detail = (resp.get("error") or {}).get("detail") if isinstance(resp, dict) else None
        missing = detail.get("missingScope") if isinstance(detail, dict) else None
        scope_hint = f"（缺少 scope: {missing}）" if missing else "（缺少对应 scope）"
        raise SystemExit(f"当前 Key 无权访问该接口{scope_hint}，请联系管理员开通授权，请勿尝试绕过。")
    if status == 404 or code == "API_NOT_FOUND":
        raise SystemExit(f"接口不存在: {api_id}。请检查 api_id 是否正确，以及 BASE_URL 是否指向本冲刺网关环境。")
    if status == 429 or code in ("RATE_LIMITED", "QUOTA_EXCEEDED"):
        _exit_rate_limited(resp, headers)
    if status >= 500:
        raise SystemExit(f"服务暂不可用，请稍后重试；如持续失败请联系管理员并提供 trace_id: {_trace_id(resp)}")
    _fail_generic(status, resp, "调用失败")


def _exit_need_key(reason: str = "") -> "None":
    """凭证不可用：唯一动作是停止并向当前对话的用户索取，Agent 不得自行检索/猜测 Key。

    stdout = 可直接回复用户的引导成品（guardrails.finalize_key_guide() 硬输出，
    与 scripts/key_guide.py 的输出字节一致）；stderr = 给 Agent 的执行指令；退出码 3。
    """
    why = {
        "KEY_INVALID": "当前配置的 API Key 无效",
        "KEY_EXPIRED": "当前配置的 API Key 已过期",
        "KEY_REVOKED": "当前配置的 API Key 已被吊销或停用",
    }.get(reason, "当前未检测到可用的 API Key")
    final_text = finalize_key_guide(f"{why}，本轮取数已停止；拿到 Key 后我会先验证一次再继续。")
    print(final_text)
    if record_emit is not None:  # 主触发位同样留痕：指纹与 key_guide.py 一致（固定三步部分），可复核
        import hashlib

        record_emit(
            style="api_client.exit_need_key",
            extra=why,
            fingerprint=hashlib.sha256(KEY_GUIDE.encode("utf-8")).hexdigest()[:16],
            lines=len(final_text.splitlines()),
        )
    print(
        "[NEED_KEY] 凭证不可用，本轮取数已停止。"
        "把上面 stdout 的引导文案原样作为回复发给用户（不得改写三步措辞与格式），"
        "然后结束本轮、等待用户提供；"
        "严禁自行检索或猜测凭证：不要翻历史会话日志、旧版目录、zip 备份、其它技能包或注册表缓存，"
        "不要复用任何曾经出现过的字符串。"
        "用户提供 Key 后，统一执行 python \"<SKILL_DIR>/config.py\" save-key \"<Key>\""
        "（先用该 Key 请求网关验证，通过才保存；保存成功后同一会话立即可用，无需重启终端），"
        "然后重新调用一次验证。",
        file=sys.stderr,
    )
    raise SystemExit(3)


def _exit_rate_limited(resp: Any, headers: dict) -> None:
    code = _err_code(resp)
    retry_after = ""
    for key, val in headers.items():
        if key.lower() == "retry-after":
            retry_after = str(val)
            break
    if code == "QUOTA_EXCEEDED":
        wait = f"（距配额重置约 {retry_after} 秒）" if retry_after else ""
        raise SystemExit(
            f"今日调用配额已用尽（每日 500 次）{wait}，请明日再试或联系管理员提升配额；当日请勿继续调用。"
        )
    wait = f"建议等待 {retry_after} 秒后重试" if retry_after else "请稍后重试"
    raise SystemExit(f"请求过于频繁（RATE_LIMITED），{wait}，请勿高频重试。trace_id: {_trace_id(resp)}")


def _fail_generic(status: int, resp: Any, action: str) -> None:
    msg = ""
    if isinstance(resp, dict):
        err = resp.get("error") or {}
        msg = str(err.get("message") or "") if isinstance(err, dict) else ""
    trace = _trace_id(resp)
    hint = f"（trace_id: {trace}）" if trace else ""
    raise SystemExit(f"{action}: HTTP {status}{' ' + msg if msg else ''}{hint}。请稍后重试或联系管理员。")


def main(argv: list[str]) -> int:
    usage = __doc__.strip()
    if len(argv) < 2:
        print(usage, file=sys.stderr)
        return 2
    cmd, args = argv[1], argv[2:]
    if cmd == "register":
        print("register 已下线：本平台不提供自助注册，Key 由用户在 etf-skill 官网自助获取（不再由平台管理员下发）。",
              file=sys.stderr)
        print(finalize_key_guide("请提供 Key 后再继续；严禁自行检索或猜测凭证。"))
        return 3
    if cmd == "invoke":
        if len(args) < 2:
            print("用法: python3 scripts/api_client.py invoke <api_id> '<json>'", file=sys.stderr)
            return 2
        try:
            payload = json.loads(args[1])
        except ValueError:
            print(f"第二个参数必须是合法 JSON，收到: {args[1]!r}", file=sys.stderr)
            return 2
        resp = invoke(args[0], payload)
        print(json.dumps(resp, ensure_ascii=False, indent=2))
        print(GUARDRAIL_REMINDER, file=sys.stderr)
        if _update_reminder:
            print(_update_reminder, file=sys.stderr)
        return 0
    print(usage, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
