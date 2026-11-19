#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""基金数据查询 Skill — 统一配置。

凭证策略：
  1. 唯一来源：用户级 JSON 凭据文件 ~/.htbr/credentials.json（字段 {"api_key": "..."}）。
     不读环境变量、不写 shell 配置、无第二来源回退。
  2. 写入统一走 `python config.py save-key <KEY>`：先用该 Key 请求网关 /auth/token
     验证，成功才原子落盘（同目录临时文件 + os.replace）；失败不落盘。
  3. 业务调用使用短期 Token（Key 仅在换取 Token 时使用，Key 文件本身不参与业务请求）。

BASE_URL 三级优先：环境变量 HTBR_BASE_URL > 本技能目录 config.local.json 的 base_url > localhost 默认
（换环境只改 config.local.json，代码不动；临时覆盖用环境变量）。

CLI:
  python config.py status              # 查看凭据配置状态（configured/missing/corrupt），不回显 Key
  python config.py save-key <KEY>      # 验证并保存 Key（验证失败不落盘、不覆盖旧值）

退出码（status）: 0=已配置  3=未配置  4=配置文件损坏/不可读
"""
import json
import os
import tempfile
import uuid
import time
from pathlib import Path

# ---- 凭据文件位置（唯一 Key 来源；测试可用 HTBR_HOME 指向临时目录注入） ----
_HOME_OVERRIDE = os.environ.get("HTBR_HOME")
_HOME_DIR = Path(_HOME_OVERRIDE) if _HOME_OVERRIDE else Path.home()
CREDENTIALS_DIR = _HOME_DIR / ".htbr"
CREDENTIALS_FILE = CREDENTIALS_DIR / "credentials.json"


def _load_local_base_url():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.local.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return (json.load(f) or {}).get("base_url") or None
    except (OSError, ValueError):
        return None


BASE_URL = os.environ.get("HTBR_BASE_URL") or _load_local_base_url() or "http://localhost:8080/api/v1"


class CredentialsCorruptError(RuntimeError):
    """凭据文件存在但不可解析（损坏/不可读）。不得静默当作未配置。"""


def _read_api_key_impl() -> str | None:
    """每次现读凭据文件返回 API Key（不做 import 期快照）。

    - 文件不存在 / api_key 为空 → None（未配置）
    - 文件存在但 JSON 损坏 / 不可读 → CredentialsCorruptError（明确报错，不静默降级）
    - 任何输出/异常信息不包含 Key 内容。
    """
    try:
        raw = CREDENTIALS_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as e:
        raise CredentialsCorruptError(f"凭据文件不可读: {CREDENTIALS_FILE}（{type(e).__name__}）") from e
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise CredentialsCorruptError(
            f"凭据文件损坏（不是合法 JSON）: {CREDENTIALS_FILE}；请删除该文件后重新执行 save-key"
        ) from e
    if not isinstance(data, dict):
        raise CredentialsCorruptError(
            f"凭据文件结构错误（顶层应为 JSON 对象）: {CREDENTIALS_FILE}；请删除该文件后重新执行 save-key"
        )
    key = data.get("api_key")
    if not isinstance(key, str) or not key.strip():
        raise CredentialsCorruptError(
            f"凭据文件缺少有效 api_key 字段: {CREDENTIALS_FILE}；请删除该文件后重新执行 save-key"
        )
    return key.strip()


def read_api_key() -> str | None:
    """对外统一读入口：转发 _read_api_key_impl()。

    独立成函数是为了兼容测试对 read_api_key 做的 mock.patch（直接替换 impl 无法生效）。
    """
    return _read_api_key_impl()


# 兼容保留：模块级 API_KEY 为 import 时的一次性快照（供旧脚本 `from config import API_KEY`）。
# 认证路径（api_client）不使用它，改调 read_api_key() 现读，避免换 Key 后沿用旧值。
# import 期文件损坏时快照记为 None（坏文件 ≠ 无文件），损坏状态由调用方现读时报出。
try:
    API_KEY = _read_api_key_impl()
except CredentialsCorruptError:
    API_KEY = None


def verify_key(api_key: str, base_url: str | None = None) -> tuple[bool, str]:
    """用给定 Key 请求 BASE_URL + /auth/token 验证（与 api_client 同一契约 §3.2）。

    返回 (ok, message)：ok=True message 为空串；ok=False message 为不泄露 Key 的失败原因。
    网络失败也返回 False（验证不通过就不落盘）。
    """
    from urllib import error as url_error, request as url_request

    url = f"{(base_url or BASE_URL).rstrip('/')}/auth/token"
    payload = {
        "apiKey": api_key,
        "skillId": "htbr-etf-skill",
        "nonce": str(uuid.uuid4()),
        "timestamp": int(time.time()),
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = url_request.Request(
        url, data=body, headers={"Content-Type": "application/json;charset=UTF-8"}, method="POST"
    )
    try:
        with url_request.urlopen(req, timeout=30) as resp:
            status, raw = resp.status, resp.read().decode("utf-8", errors="replace")
    except url_error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", errors="replace")
        except Exception:
            raw = ""
        status = e.code
        if status == 401:
            return False, "Key 无效（401 认证未通过）"
        try:
            resp_obj = json.loads(raw)
        except ValueError:
            resp_obj = {}
        code = ""
        if isinstance(resp_obj, dict):
            err = resp_obj.get("error") or {}
            code = str(err.get("code") or "") if isinstance(err, dict) else ""
        if code:
            return False, f"Key 验证失败（HTTP {status}，错误码 {code}）"
        return False, f"Key 验证失败（HTTP {status}）"
    except url_error.URLError as e:
        return False, f"网关不可达（{e.reason}）；请检查网络/VPN 后重试"
    except OSError as e:
        return False, f"网络请求失败（{type(e).__name__}）；请检查网络后重试"

    try:
        resp_obj = json.loads(raw)
    except ValueError:
        resp_obj = {}
    if status == 200 and isinstance(resp_obj, dict) and resp_obj.get("success"):
        return True, ""
    return False, f"Key 验证失败（HTTP {status}）"


def save_api_key(api_key: str, base_url: str | None = None) -> tuple[bool, str]:
    """验证并保存 Key：先 verify_key，通过才原子写入凭据文件。

    - 验证失败：不落盘、不覆盖已有旧值，返回 (False, 原因)。
    - 写入方式：同目录临时文件 + os.replace（原子替换）。
    - 权限：POSIX 目录 0700 / 文件 0600；Windows 继承用户配置文件目录 ACL。
    """
    if not api_key or not api_key.strip():
        return False, "Key 为空，未保存"
    api_key = api_key.strip()

    ok, message = verify_key(api_key, base_url)
    if not ok:
        return False, message

    try:
        CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            try:
                os.chmod(CREDENTIALS_DIR, 0o700)
            except OSError:
                pass
        fd, tmp_name = tempfile.mkstemp(
            dir=str(CREDENTIALS_DIR), prefix=".credentials-", suffix=".tmp"
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"api_key": api_key}, f, ensure_ascii=False)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            if os.name == "posix":
                os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, CREDENTIALS_FILE)
        except BaseException:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
    except OSError as e:
        return False, f"凭据文件写入失败（{type(e).__name__}）；未改动原有配置"
    return True, ""


def _status() -> int:
    """status 子命令：输出配置状态与来源，绝不回显 Key（哪怕片段）。"""
    source = str(CREDENTIALS_FILE)
    try:
        key = read_api_key()
    except CredentialsCorruptError as e:
        print(f"corrupt: {e}")
        print(f"来源: {source}")
        return 4
    if key:
        print(f"configured: API Key 已配置（来源: {source}）")
        return 0
    print(f"missing: 未配置 API Key（来源: {source} 不存在或为空）")
    print("保存方式: python \"<技能目录>/config.py\" save-key \"<Key>\"")
    return 3


def main(argv: list[str]) -> int:
    args = argv[1:]
    if not args:
        print(__doc__.strip(), file=os.sys.stderr)
        return 2
    cmd = args[0]
    if cmd == "status":
        return _status()
    if cmd == "save-key":
        if len(args) != 2:
            print('用法: python config.py save-key "<KEY>"', file=os.sys.stderr)
            return 2
        ok, message = save_api_key(args[1])
        if ok:
            print("已验证并保存 API Key（来源: %s）。保存后立即可用，无需重启终端。" % CREDENTIALS_FILE)
            print('验证: python "<技能目录>/config.py" status')
            return 0
        print(f"Key 验证失败，未保存（{message}）。请核对 Key 后重试；问题持续请联系管理员。", file=os.sys.stderr)
        return 1
    print(__doc__.strip(), file=os.sys.stderr)
    return 2


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv))
