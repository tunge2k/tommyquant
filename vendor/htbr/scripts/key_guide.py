#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""基金 Skill — 凭证引导文案生成器（硬输出的唯一入口）。

凭证缺失/失效时，AI 必须调用本脚本并用其 stdout 作为对外回复的引导文案，
禁止手写或改写三步引导的措辞与格式。文案唯一权威来源是 guardrails.KEY_GUIDE。

用法:
  python scripts/key_guide.py                     # 只输出固定三步引导
  python scripts/key_guide.py "<补充说明>"         # 固定三步 + 一段补充说明

退出码:
  0  成功（stdout = 可直接回复用户的凭证引导成品文案）
  2  用法错误
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from guardrails import finalize_key_guide, KEY_GUIDE  # noqa: E402

try:  # 审计是增强件不是依赖：缺 guide_audit.py（如按旧契约手工组装）时只丢审计，不丢引导
    from guide_audit import record_emit  # noqa: E402
except ImportError:  # pragma: no cover
    record_emit = None


def _audit(final_text: str, extra: str) -> None:
    """审计留痕（append-only，写入失败自动降级不阻断）：证明生成器被真正调用过。"""
    if record_emit is None:
        return
    import hashlib

    # 指纹只取固定三步部分（剥掉 extra 段），任何触发位的同一份文案指纹一致，可复核
    fp = hashlib.sha256(KEY_GUIDE.encode("utf-8")).hexdigest()[:16]
    record_emit(style="key_guide.py", extra=extra, fingerprint=fp, lines=len(final_text.splitlines()))


def main(argv: list[str]) -> int:
    args = argv[1:]
    if len(args) > 1:
        print("用法: python scripts/key_guide.py [补充说明]", file=sys.stderr)
        return 2
    extra = args[0] if args else ""
    final_text = finalize_key_guide(extra)
    print(final_text)
    _audit(final_text, extra)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
