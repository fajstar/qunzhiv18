# -*- coding: utf-8 -*-
"""PrivacyScrubber · 用户上传论文 / 评审文本脱敏
================================================

核心原则
--------
**用户上传的论文是最敏感数据，绝不出本机。**
本模块在"入库前"把可识别个人身份的信息替换为占位符，原文不落盘、不返回、
不进入任何对外 API。

分层策略（避免误伤）
--------------------
1. **全文强模式**（任何位置都替换，误伤率极低）：
   邮箱、ORCID、基金号、带凭据的 URL、显式电话。
2. **仅作者块**：标题之后、Abstract 之前的前置区，识别作者行 / 单位行。
   这样不会把正文里的 "Adam"（优化器）、"Alice"（数据集名）误改成 `[AUTHOR]`。
3. **仅致谢 / 作者贡献 / 基金段**：只在这些段落里按"感谢 + 人名"模式脱敏。
4. **数字 / 统计量永远保留**：对账、复算需要；本模块没有任何"泛数字"规则。

对外接口
--------
- ``scrub(text)``                 → 脱敏后的文本
- ``scrub_with_stats(text)``      → ``{text, redactions, sections}``
- ``scrub_review(text)``          → 评审原文脱敏（评审人 ID → [REVIEWER_n]）
- ``scrub_payload(obj, keys=...)``→ 递归脱敏 JSON 中指定字段
- ``looks_local_ip(ip)``          → 上传"仅本机"判断的辅助函数

设计取舍
--------
- 不做"全文人名 NER"：论文正文里的专有名词（Adam / BERT / Alice）会被误伤，
  且 NER 结果不可复现。只处理结构化的作者/致谢区。
- 不做"全文单位替换"：正文引用其他单位时不应被删。只处理前置区。
- 占位符固定为 ``[EMAIL]`` / ``[AUTHOR]`` / ``[AFFILIATION]`` / ``[GRANT]`` /
  ``[ORCID]`` / ``[PHONE]`` / ``[REVIEWER_n]``，便于下游统计与对账。
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any, Dict, Iterable, List, Tuple

__all__ = [
    "scrub",
    "scrub_with_stats",
    "scrub_review",
    "scrub_payload",
    "looks_local_ip",
    "PATTERNS",
    "PLACEHOLDERS",
]

PLACEHOLDERS = {
    "email": "[EMAIL]",
    "orcid": "[ORCID]",
    "grant": "[GRANT]",
    "phone": "[PHONE]",
    "author": "[AUTHOR]",
    "affiliation": "[AFFILIATION]",
    "reviewer": "[REVIEWER_%s]",
}

# ---------------------------------------------------------------------------
# 1) 全文强模式：这些几乎不可能误伤，任何位置都替换
# ---------------------------------------------------------------------------
PATTERNS: List[Tuple[str, re.Pattern, str]] = [
    (
        "email",
        re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),
        PLACEHOLDERS["email"],
    ),
    (
        "orcid",
        re.compile(r"\b\d{4}-\d{4}-\d{4}-\d{3}[\dXx]\b"),
        PLACEHOLDERS["orcid"],
    ),
    (
        # Grant / Fund / 基金号 / 批准号 / 项目编号，后跟 6 位以上编号
        "grant",
        re.compile(
            r"(?i)\b(?:grant|fund(?:ing)?|award|project)\s*(?:no\.?|number|id)?\s*[:：#]?\s*"
            r"([A-Z]{0,6}[\s-]?\d{5,}[A-Z0-9-]*)"
        ),
        PLACEHOLDERS["grant"],
    ),
    (
        "grant",
        re.compile(
            r"(?:基金|资助|项目)\s*(?:编号|号|批准号)?\s*[:：#]?\s*"
            r"([A-Z]{0,6}[\s-]?\d{5,}[A-Z0-9-]*)"
        ),
        PLACEHOLDERS["grant"],
    ),
    (
        # 无 "Grant" 关键词的基金编号：NSFC-62176001 / 2021ZD0110000 等
        "grant",
        re.compile(
            r"\b(?:NSFC|NNSFC|NIH|NSF|ERC|DFG|EPSRC|JSPS|KAKENHI)[-\s]?"
            r"[A-Z0-9-]*\d{5,}[A-Z0-9-]*\b"
        ),
        PLACEHOLDERS["grant"],
    ),
    (
        "grant",
        re.compile(r"\b(?:19|20)\d{2}[A-Z]{1,4}\d{5,}[A-Z0-9-]*\b"),
        PLACEHOLDERS["grant"],
    ),
    (
        # 仅在明确的电话语境里替换，避免误伤 11 位统计数字
        "phone",
        re.compile(
            r"(?i)(?:tel|phone|mobile|电话|手机)\s*[:：]?\s*"
            r"(\+?\d[\d\s()-]{7,}\d)"
        ),
        PLACEHOLDERS["phone"],
    ),
    (
        # URL 里的 user:password@ 凭据
        "url_credential",
        re.compile(r"(?i)\b(https?://)[^/\s:@]+:[^/\s@]+@"),
        r"\1[REDACTED]@",
    ),
]

# ---------------------------------------------------------------------------
# 2) 结构与段落识别
# ---------------------------------------------------------------------------
_HEADING_RE = re.compile(
    r"^\s*(?:\d+\.?\s*)?"
    r"(abstract|摘要|acknowledg\w*|致谢|references|参考文献|"
    r"author\s+contributions?|作者贡献|funding|基金|"
    r"introduction|引言|1\s+introduction)\b",
    re.I,
)

_ACK_RE = re.compile(
    r"^\s*(?:\d+\.?\s*)?(acknowledg\w*|致谢|author\s+contributions?|作者贡献|funding|基金)\b",
    re.I,
)

_ABSTRACT_RE = re.compile(
    r"^\s*(?:\d+\.?\s*)?(abstract|摘要|introduction|引言|1\s+introduction)\b",
    re.I | re.M,
)

_AFFILIATION_WORDS = re.compile(
    r"(?i)\b("
    r"universit(?:y|ies)|institut(?:e|ion)|department|dept\.?|school|college|"
    r"laborator(?:y|ies)|academy|labs?\b|inc\.?|ltd\.?|corp(?:oration)?|"
    r"hospital|center|centre|research"
    r")\b|大学|学院|研究所|研究院|实验室|系\b|公司|中心"
)

# 姓名 token：John Smith / J. Smith / Smith 等；不匹配全小写普通词
_NAME_TOKEN = re.compile(r"[A-Z][A-Za-z'’\-]+(?:\s+[A-Z]\.)?(?:\s+[A-Z][A-Za-z'’\-]+)?")
_CJK_NAME_TOKEN = re.compile(r"[\u4e00-\u9fa5]{2,4}")

# 正文里常见的"不是人名"的大写词（避免作者行误判）
_NOT_NAMES = {
    "Abstract", "Introduction", "Keywords", "Preprint", "Under", "Review",
    "Corresponding", "Author", "Authors", "Equal", "Contribution", "Senior",
    "Department", "University", "Institute", "School", "College", "Laboratory",
}


def _looks_like_author_line(line: str) -> bool:
    """判断一行是否像作者行（只在前置区调用）。"""
    s = line.strip()
    if not (3 <= len(s) <= 240):
        return False
    if _AFFILIATION_WORDS.search(s):
        return False
    if s.endswith(".") and " " in s and len(s.split()) > 12:
        return False
    # 至少 2 个姓名 token，且有分隔符
    toks = [t for t in _NAME_TOKEN.findall(s) if t not in _NOT_NAMES and len(t) >= 2]
    seps = bool(re.search(r"[,，、]|\band\b|&", s))
    cjk = _CJK_NAME_TOKEN.findall(s)
    if seps and (len(toks) >= 2 or len(cjk) >= 2):
        return True
    # 单个姓名 + 上标数字（"John Smith 1,2"）
    if len(toks) >= 1 and re.search(r"\d", s) and seps:
        return True
    return False


def _split_sections(text: str) -> List[Dict[str, Any]]:
    """按行扫描，返回 [{heading, start, end}]；只标出我们关心的段。"""
    lines = text.splitlines(keepends=True)
    out: List[Dict[str, Any]] = []
    cur = None
    pos = 0
    for ln in lines:
        m = _HEADING_RE.match(ln)
        if m:
            if cur is not None:
                cur["end"] = pos
                out.append(cur)
                cur = None
            cur = {"heading": m.group(1).lower(), "start": pos, "end": None,
                   "content_start": pos + len(ln)}
        pos += len(ln)
    if cur is not None:
        cur["end"] = pos
        out.append(cur)
    return out


# ---------------------------------------------------------------------------
# 3) 作者前置区
# ---------------------------------------------------------------------------
def _scrub_front_matter(text: str) -> Tuple[str, int]:
    """标题之后、Abstract 之前：作者行 / 单位行。返回 (text, n_replaced)。"""
    m = _ABSTRACT_RE.search(text)
    end = m.start() if m else min(len(text), 3000)
    head = text[:end]
    # 标题 = 第一行非空；保留，不动
    lines = head.splitlines(keepends=True)
    first_nonempty = next((i for i, ln in enumerate(lines) if ln.strip()), None)
    if first_nonempty is None:
        return text, 0
    n = 0
    for i, ln in enumerate(lines):
        if i <= first_nonempty:
            continue
        body = ln.strip()
        if not body:
            continue
        if re.match(r"(?i)^(arxiv|preprint|under review|doi|http)", body):
            continue
        if _AFFILIATION_WORDS.search(body) and len(body) <= 300:
            new = "  " + PLACEHOLDERS["affiliation"] + "\n"
            n += 1
        elif _looks_like_author_line(body):
            new = "  " + PLACEHOLDERS["author"] + "\n"
            n += 1
        else:
            continue
        lines[i] = new
    return "".join(lines) + text[end:], n


# ---------------------------------------------------------------------------
# 4) 致谢 / 作者贡献段：只处理"感谢/贡献 + 人名"
# ---------------------------------------------------------------------------
_THANKS_RE = re.compile(
    r"(?i)((?:we\s+(?:sincerely\s+|warmly\s+)?thank|thanks\s+to|grateful\s+to|"
    r"感谢|致谢|多谢)\s*[:：]?\s*)([^\n。；;]{2,240})"
)


def _scrub_person_names(fragment: str) -> Tuple[str, int]:
    n = 0

    def repl(m: "re.Match[str]") -> str:
        nonlocal n
        tok = m.group(0)
        if tok in _NOT_NAMES:
            return tok
        # 至少两个单词，或带中间缩写，才当人名；单个大写词风险高，保留
        if len(tok.split()) < 2 and not _CJK_NAME_TOKEN.fullmatch(tok):
            return tok
        n += 1
        return PLACEHOLDERS["author"]

    out = _NAME_TOKEN.sub(repl, fragment)
    # 中文姓名：只在"感谢/致谢"片段里替换 2-4 字连续中文（排除常见词）
    cjk_common = {" reviewer", "reviewers", "审稿人", "编辑", "老师", "同学", "团队", "基金", "项目"}
    out = _CJK_NAME_TOKEN.sub(
        lambda m: m.group(0) if m.group(0) in cjk_common else PLACEHOLDERS["author"], out
    )
    return out, n


def _scrub_sections(text: str) -> Tuple[str, int]:
    secs = _split_sections(text)
    n = 0
    # 从后往前替换，保证偏移不失效
    for sec in reversed(secs):
        h = sec["heading"]
        if not (_ACK_RE.match(h) or "acknowledg" in h or "致谢" in h
                or "contribution" in h or "贡献" in h or "funding" in h or "基金" in h):
            continue
        start, end = sec["start"], sec["end"]
        body = text[start:end]

        def thanks_repl(m: "re.Match[str]") -> str:
            nonlocal n
            fixed, k = _scrub_person_names(m.group(2))
            n += k
            return m.group(1) + fixed

        body2 = _THANKS_RE.sub(thanks_repl, body)
        if body2 != body:
            text = text[:start] + body2 + text[end:]
    return text, n


# ---------------------------------------------------------------------------
# 5) 对外接口
# ---------------------------------------------------------------------------
def scrub_with_stats(text: str, mode: str = "upload") -> Dict[str, Any]:
    """脱敏并返回计数。``mode``: upload / review。"""
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    original_len = len(text)
    counts: Dict[str, int] = {}
    out = text
    for name, pat, repl in PATTERNS:
        out, k = pat.subn(repl, out)
        if k:
            counts[name] = counts.get(name, 0) + k

    if mode == "review":
        out, rv = _scrub_reviewer_ids(out)
        if rv:
            counts["reviewer_id"] = counts.get("reviewer_id", 0) + rv
        out, na = _scrub_sections(out)
        if na:
            counts["author"] = counts.get("author", 0) + na
        return {"text": out, "redactions": counts,
                "total": sum(counts.values()), "mode": mode,
                "original_chars": original_len, "output_chars": len(out)}

    out, na = _scrub_front_matter(out)
    if na:
        counts["author_or_affiliation"] = counts.get("author_or_affiliation", 0) + na
    out, ns = _scrub_sections(out)
    if ns:
        counts["author"] = counts.get("author", 0) + ns
    return {"text": out, "redactions": counts,
            "total": sum(counts.values()), "mode": mode,
            "original_chars": original_len, "output_chars": len(out)}


def scrub(text: str, mode: str = "upload") -> str:
    """只返回脱敏后的文本。"""
    return scrub_with_stats(text, mode=mode)["text"]


_REVIEWER_RE = re.compile(
    r"(?i)\b(reviewer|referee|审稿人|评审人)\s*[#＃:：\-]?\s*([0-9]{1,3}|[A-Z])\b"
)


def _scrub_reviewer_ids(text: str) -> Tuple[str, int]:
    mapping: Dict[str, str] = {}
    n = 0

    def repl(m: "re.Match[str]") -> str:
        nonlocal n
        key = m.group(2).upper()
        if key not in mapping:
            mapping[key] = PLACEHOLDERS["reviewer"] % (len(mapping) + 1)
            n += 1
        return mapping[key]

    return _REVIEWER_RE.sub(repl, text), n


def scrub_review(text: str) -> str:
    """评审原文脱敏：评审人 ID 稳定编号，邮箱/作者名按段处理。"""
    return scrub_with_stats(text, mode="review")["text"]


# 可能出现在 API 响应里的正文字段名；只对这些字段做递归脱敏
SENSITIVE_KEYS = frozenset({
    "text", "quote", "snippet", "excerpt", "content", "raw", "markdown",
    "abstract", "evidence", "matched", "body", "comment", "review", "reply",
})


def scrub_payload(obj: Any, keys: Iterable[str] = SENSITIVE_KEYS, mode: str = "upload") -> Any:
    """递归脱敏 JSON 中指定字段的字符串值；其它字段原样返回。"""
    keys = frozenset(keys)

    def walk(x: Any) -> Any:
        if isinstance(x, dict):
            return {k: (scrub(v, mode=mode) if k in keys and isinstance(v, str) else walk(v))
                    for k, v in x.items()}
        if isinstance(x, list):
            return [walk(v) for v in x]
        return x

    return walk(obj)


def looks_local_ip(ip: str) -> bool:
    """回环 / 私网 / 链路本地 → 视为"本机"。空值按不安全处理。"""
    try:
        addr = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return False
    return bool(addr.is_loopback or addr.is_private or addr.is_link_local)
