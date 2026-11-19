#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""基金查询 Skill — 最终答案校验（借鉴易方达 guardrails 模式，见 08-友商Skill参考.md）。

用法: 最终回答用户前必须调用 finalize_answer()；校验失败则改写后重试。

校验项（命中任一即拒绝，须改写正文后重试）：
  1. 结论先至：正文首个非空段未以「结论：」开头（结构要求见 SKILL.md「回答结构：结论先至」）；
  2. 禁用语：投资建议类话术；
  3. 内部细节泄漏：环境变量名 / 认证头 / 配置文件 / 网关原始地址 / 原始 JSON 字样
     （双通道：原始正则 + 折叠比对，防字符分隔/全角/零宽混淆绕过，见 _internal_detail_hit）；
  4. 无效占位值：None / null / -888.89。
全部通过后追加品牌尾注（对已含尾注的输入先剥离再追加，幂等）。
"""
from __future__ import annotations

import re
import unicodedata

# 品牌尾注已定稿：文案唯一权威来源 docs/htbr-ai-skill/01-需求清单.md §3.2，变更须走 po 需求变更流程
DISCLAIMER = "本服务提供的所有功能及资讯仅供参考。服务内容由 AI 生成，受技术特性与数据实时变化影响，可能存在偏差。数据来源于公开披露渠道（交易所、基金公司定期报告等），可能存在更新延迟或口径差异，请以基金合同、官方公告等原始披露文件为准,**基金的过往业绩及其净值高低并不预示其未来业绩表现**，本服务内容不构成对未来业绩的预测或承诺，亦不构成任何投资建议。用户使用本服务时，请自行做好数据核验与风险把控，独立判断并承担相应风险。更多信息请访问华泰柏瑞基金官网www.huatai-pb.com或拨打客服热线400-888-0001。"

# 凭证引导文案已定稿：唯一权威来源。凭证缺失/失效时对外输出必须取 finalize_key_guide() 的返回值，
# 禁止 AI 手写或改写这三步的措辞与格式（避免多处重复文案漂移，见 SKILL.md「凭证引导输出」）。
KEY_GUIDE_URL = "https://esa.huatai-pb.com/skillReg/app-cli-front/#/"

KEY_GUIDE = (
    "当前未检测到有效的 API Key，请自助获取后提供给我：\n"
    "① 打开 " + KEY_GUIDE_URL + "\n"
    "② 通过手机号获取密钥（API KEY)\n"
    "③ 将密钥粘贴至对话框"
)

FORBIDDEN_PHRASES = (
    "推荐买入", "建议买入", "建议买这只", "现在适合买", "适合买入",
    "应该买", "应该卖", "应该加仓", "应该减仓", "稳赚", "保本",
)

FORBIDDEN_INTERNAL_PATTERNS = (
    r"Authorization", r"Bearer\s+[A-Za-z0-9._\-]+",
    r"config\.py", r"BASE_URL", r"api[_-]?key", r"HTBR_",
    r"https?://[^\s]*gateway[^\s]*", r"raw\s?JSON", r"原始\s?JSON",
)

# 折叠通道禁词：与 FORBIDDEN_INTERNAL_PATTERNS 对应、已按 _fold() 口径写成的折叠形态 token
# （纯字母数字/汉字，无分隔符；正文折叠后做字面包含比对）。防误杀口径：
#  - 「apikey」收编空格话术「API Key」的泄漏（PoC 向量 6/9）。官方凭证引导文案（KEY_GUIDE）
#    由 finalize_key_guide() 直出、不经 finalize_answer()，且其首行不满足「结论先至」，
#    本就不走本校验，故无新增误杀。
#  - 不收「htbr」裸前缀：会误杀含华泰柏瑞英文缩写 HTBR 的正常正文；只收完整变量名折叠形态。
#  - 不收「gateway」裸词：正文提及「网关(gateway)」属正常表述；网关原始地址由折叠正则拦。
FOLDED_FORBIDDEN_TOKENS = (
    "authorization",            # 认证头字段名（A u t h o r… / ＡＵＴＨ… / 零宽拆分等混淆）
    "bearer",                   # Bearer 认证头（含 B e a r e r + token 拆分）
    "config", "configpy",       # 配置文件名 config.py / config_.py / c.o.n.f.i.g 及其混淆
    "baseurl",                  # 环境变量 BASE_URL
    "apikey",                   # api[_-]?key / API Key / a p i k e y 等全部混淆形态
    "htbrapikey",               # 环境变量 HTBR_API_KEY 完整名
    "htbrbaseurl",              # 环境变量 HTBR_BASE_URL 完整名
    "rawjson", "原始json",      # 原始 JSON 字样（含 rawJSON / 原始 JSON 变体）
)

# 折叠文本上的正则（需跨字符匹配的形态），与 FOLDED_FORBIDDEN_TOKENS 互补。
FOLDED_FORBIDDEN_REGEXPS = (
    r"https?[a-z0-9]*gateway",  # 网关 URL：schema 与 gateway 之间被分隔符拆开（如 g a t e w a y）
)

INVALID_VALUE_PATTERNS = (r"\bNone\b", r"\bnull\b", r"-888\.89")

AUTO_REPLACEMENTS = (("%%", "%"), ("元元/份", "元/份"), ("亿元亿元", "亿元"), ("\r\n", "\n"))

# 结论先至：正文首个非空段必须以「结论：」开头。
# 容忍 Markdown 装饰前缀（引用号/标题号/列表符/加粗星号）与全角/半角冒号，
# 即 "结论：…"、"**结论**：…"、"## 结论：…" 均通过；缺冒号或改用「小结」等词不通过。
CONCLUSION_HEAD_PATTERN = r"^[\s>#*\-•·]*结论[\s*]*[：:]\s*\S"


def _normalize_text(text: str) -> str:
    for old, new in AUTO_REPLACEMENTS:
        text = text.replace(old, new)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    text = re.sub(rf"(?:\n+)?{re.escape(DISCLAIMER)}(?:\s*{re.escape(DISCLAIMER)})*$", "", text).strip()
    return text


def _first_hit(patterns, text):
    for pat in patterns:
        if re.search(pat, text, flags=re.IGNORECASE):
            return pat
    return None


def _fold(text: str) -> str:
    """混淆归一化：NFKC 折叠全角/兼容字符 → casefold 统一小写 → 剔除所有非字母数字/汉字字符。

    作用是把「A u t h o r i z a t i o n」「ＨＴＢＲ＿ＡＰＩ＿ＫＥＹ」「Autho\u200brization」等
    空格/点号/下划线/连字符/全角/零宽分隔混淆统一还原成无分隔的纯字符流，
    如 api-key / api_key / API Key → 均折叠为 apikey，再与折叠禁词做字面包含比对。
    """
    text = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", text)


def _internal_detail_hit(body: str) -> str | None:
    """内部细节泄漏双通道检测（防混淆绕过）。

    通道一：原有原始正则（拦未混淆写法，含 IGNORECASE）；
    通道二：折叠通道——正文折叠后与 FOLDED_FORBIDDEN_TOKENS 字面包含比对 +
    FOLDED_FORBIDDEN_REGEXPS 正则匹配（拦字符分隔/全角/零宽混淆写法）。
    命中返回可读的命中描述，未命中返回 None。
    """
    pat = _first_hit(FORBIDDEN_INTERNAL_PATTERNS, body)
    if pat:
        return pat
    folded = _fold(body)
    for token in FOLDED_FORBIDDEN_TOKENS:
        if token in folded:
            return f"obfuscated form of {token!r}"
    for rx in FOLDED_FORBIDDEN_REGEXPS:
        if re.search(rx, folded):
            return f"obfuscated form of {rx!r}"
    return None


def _first_content_line(text: str) -> str:
    """返回首个非空行（结论先至只看正文首段，空行不计）。"""
    for line in text.splitlines():
        if line.strip():
            return line
    return ""


def _check_conclusion_first(text: str) -> None:
    """结论先至：正文首个非空段必须以「结论：」开头（见 SKILL.md「回答结构：结论先至」）。"""
    if not re.match(CONCLUSION_HEAD_PATTERN, _first_content_line(text)):
        raise ValueError(
            'final answer does not lead with a conclusion: '
            'the first non-empty line must start with "结论："'
        )


def finalize_answer(answer: str) -> str:
    """校验并规范化最终答案。只应向用户输出返回值；校验失败需改写后重试。"""
    if not isinstance(answer, str):
        raise TypeError("answer must be a string")
    body = _normalize_text(answer)
    if not body:
        raise ValueError("final answer is empty")
    _check_conclusion_first(body)
    for pats, msg in ((FORBIDDEN_PHRASES, "contains forbidden advice phrase"),
                      (INVALID_VALUE_PATTERNS, "exposes invalid internal value")):
        pat = _first_hit(pats, body)
        if pat:
            raise ValueError(f"final answer {msg}: {pat}")
    hit = _internal_detail_hit(body)
    if hit:
        raise ValueError(f"final answer leaks internal detail: {hit}")
    return f"{body}\n\n{DISCLAIMER}"


def validate_answer(answer: str) -> None:
    """仅校验（测试/评估用）。"""
    finalize_answer(answer)


def finalize_key_guide(extra: str = "") -> str:
    """凭证引导成品文案（硬输出，唯一权威来源）。

    凭证缺失/失效（凭据文件未配置或损坏，或 KEY_INVALID / KEY_EXPIRED / KEY_REVOKED / 401）时，
    对外回复必须使用本函数的返回值，禁止 AI 手写或改写三步引导的措辞与格式。

    参数 extra：可选补充说明（如「本次查询无法完成，拿到 Key 后我会先验证一次再继续」），
    会作为独立段落追加在固定三步之后；未提供时只输出固定三步。
    """
    guide = KEY_GUIDE
    if extra and isinstance(extra, str) and extra.strip():
        guide = f"{guide}\n\n{extra.strip()}"
    return guide
