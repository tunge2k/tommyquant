#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""基金 Skill — 凭证引导审计（append-only 事件日志）。

为什么需要这一层
----------------
把约束写在 SKILL.md 里，对模型只是「请求」，不是「强制」：它完全可以**不调用生成器**，
自己写一段语义相近的引导发出去——这种违规无痕，且逐行比对拦不住（因为生成器根本没被调用）。
实测已发生过：跑出了正确的四步原文（工具结果里有），回复时却改写成「技能会引导你去官网获取 Key」。

本模块把「生成」与「校验」两个动作落盘成 append-only 事件日志，让违规从**无痕**变成
**有痕、可追责、可人工复核**。它改变不了「模型可以不调用」这个结构性事实，
但能让「没调用」变成一个可以被查出来的事实，而不是一个只有模型自己知道的空白。

日志位置（按优先级）
--------------------
1. 环境变量 ``HTBR_GUIDE_AUDIT_DIR``
2. 技能目录下 ``.audit/key_guide.jsonl``

写入失败一律降级为「不审计」并提示一次 stderr：审计不得阻断正常取数。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

AUDIT_DIR_ENV = "HTBR_GUIDE_AUDIT_DIR"
LOG_NAME = "key_guide.jsonl"

_warned = False
_probe_cache: bool | None = None


def skill_dir() -> Path:
    """技能根目录（本文件位于 <SKILL_DIR>/scripts/ 下）。"""
    return Path(__file__).resolve().parent.parent


def audit_dir() -> Path:
    override = os.environ.get(AUDIT_DIR_ENV)
    if override:
        return Path(override).expanduser()
    return skill_dir() / ".audit"


def log_path() -> Path:
    return audit_dir() / LOG_NAME


def _warn_once(msg: str) -> None:
    global _warned
    if not _warned:
        print(f"[audit] {msg}", file=sys.stderr)
        _warned = True


def _append(record: dict) -> None:
    record.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    record["pid"] = os.getpid()
    try:
        target = audit_dir()
        target.mkdir(parents=True, exist_ok=True)
        with open(target / LOG_NAME, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        _warn_once(f"审计日志写入失败（不影响取数与校验）：{exc}")


def writable() -> bool:
    """审计目录是否可写。可写时才启用「来源校验」，只读部署自动降级。"""
    global _probe_cache
    if _probe_cache is not None:
        return _probe_cache
    try:
        target = audit_dir()
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink(missing_ok=True)
        _probe_cache = True
    except OSError:
        _probe_cache = False
    return _probe_cache


def read_events(limit: int = 0) -> list[dict]:
    path = log_path()
    if not path.is_file():
        return []
    events: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return events[-limit:] if limit else events


def last_emit() -> dict | None:
    for ev in reversed(read_events()):
        if ev.get("event") == "emit":
            return ev
    return None


def record_emit(*, style: str, extra: str, fingerprint: str, lines: int) -> None:
    """记录一次引导文案生成（= 生成器被真正调用的证据）。"""
    _append(
        {
            "event": "emit",
            "style": style,
            "extra_len": len(extra or ""),
            "extra_head": (extra or "")[:120],
            "fingerprint": fingerprint,
            "lines": lines,
            "cwd": os.getcwd(),
        }
    )


def record_check(*, ok: bool, detail: str, fingerprint: str, source_matched: bool) -> None:
    """记录一次发送前校验（含来源校验结果）。"""
    _append(
        {
            "event": "check",
            "ok": ok,
            "source_matched": source_matched,
            "fingerprint": fingerprint,
            "detail": (detail or "")[:400],
            "cwd": os.getcwd(),
        }
    )


# ---- CLI 消费端：让「生成器到底有没有被调用」可以直接查证 ----


def _cli_show(limit: int) -> int:
    for ev in read_events(limit=limit):
        print(json.dumps(ev, ensure_ascii=False))
    return 0


def _cli_verify() -> int:
    """退出码 0 = 至少一次 emit；1 = 日志里没有 emit（生成器从未被调用，属违规证据）。"""
    ev = last_emit()
    if ev is None:
        print("[verify] 无 emit 事件：凭证引导文案不是由生成器产出的（或日志不可写）。", file=sys.stderr)
        return 1
    print(f"[verify] 最近一次生成器调用：{json.dumps(ev, ensure_ascii=False)}")
    return 0


def main(argv: list[str]) -> int:
    """用法:
    python scripts/guide_audit.py show [N]   # 查看最近 N 条审计事件（默认 20）
    python scripts/guide_audit.py verify     # 查证生成器是否被调用过（exit 0=是 / 1=否）
    """
    args = argv[1:]
    if not args:
        print(main.__doc__ or "", file=sys.stderr)
        return 2
    cmd = args[0]
    if cmd == "show":
        limit = int(args[1]) if len(args) > 1 else 20
        return _cli_show(limit)
    if cmd == "verify":
        return _cli_verify()
    print(f"未知子命令：{cmd}", file=sys.stderr)
    print(main.__doc__ or "", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
