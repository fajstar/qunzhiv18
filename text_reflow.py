# -*- coding: utf-8 -*-
"""PDF 全文重排（面向 /read 阅读页）。

库内 fulltext_z 是 PyMuPDF 抽出的“视觉行”：每 60-80 字符一个换行。
直接丢给 white-space:pre-wrap 会变成“一句一行”。本模块只做版式恢复：

* 行内换行 -> 空格（行尾连字符按词性合回，如 ingre-\\ndients -> ingredients）
* 空行 -> 段落分隔
* 常见节标题 / 短标题 -> ``## ``（一级）或 ``### ``（子节）
* 图注 / 表注 / 列表 -> 独立段落
* 表格、公式等短行块 -> 保留物理换行，避免被拼成一段乱码

不删除正文内容；只合并换行、修连字符、插入标题标记。
"""

from __future__ import annotations

import re
import statistics

# 功能词：标题判定时不计入 Title-Case 分母
STOP = set(
    """a an the and or but nor for of to in on at by with from as is are was were be been
    being this that these those it its their our we you they he she i not no into over under
    via vs versus per than then so such can could may might will would should do does did has
    have had""".split()
)

# 句首常见词：短句即使首字母大写，也不当作标题
SENT_STARTERS = set(
    """we the this these those it its our ours their in on for with from as at by to of a an if
    when while unlike both all most some many such two three four one""".split()
)

TOP_LEVEL = {
    re.sub(r"\s+", " ", x).strip().lower()
    for x in (
        "abstract|introduction|related work|related works|background|preliminaries|method|"
        "methods|methodology|approach|experiments|experiment|experimental setup|results|result|"
        "evaluation|discussion|conclusion|conclusions|references|bibliography|appendix|"
        "supplementary material|acknowledgments|acknowledgements|limitations"
    ).split("|")
}
SUB_LEVEL = {
    re.sub(r"\s+", " ", x).strip().lower()
    for x in (
        "tasks|task|models|model|metrics|metric|analysis|ablation|problem formulation|"
        "proposed method|proposed approach|setup"
    ).split("|")
}
KNOWN = TOP_LEVEL | SUB_LEVEL

# 行尾连字符：若左片段本身就是常见复合词成分，保留连字符；否则视为断词合回
COMPOUND_LEFT = set(
    """self multi pre post non cross well best machine evidence factual forward output open
    closed long short high low large small real full half one two three four anti auto inter
    intra over under sub super un meta micro macro model data task user human language prompt
    score soft hard fine coarse few shot zero rule based free form and versus better
    subjectivity logprob""".split()
)

CAPTION_RE = re.compile(r"^(?:figure|fig\.?|table|algorithm|listing)\s*\d+[.:]", re.I)
BULLET_RE = re.compile(
    r"^(?:[\u2022\u00b7\u25aa\u25e6\u25cf\u25cb\u25a0\u25a1]|[-*])\s+|^\d{1,2}[.)]\s+"
)
NUM_HEAD_RE = re.compile(r"^(?:\d+(?:\.\d+)*|[A-Z])\.?\s+")
SENT_END = (".", "?", "!")
ARXIV_STAMP_RE = re.compile(
    r"^arXiv:\d{4}\.\d{4,5}v\d+\s+\[[a-z.\-]+\]", re.I
)
REF_START_RE = re.compile(
    r"^[A-Z][A-Za-z\u00c0-\u024f'\u2019\-]+,\s+(?:[A-Z]\.|[A-Z][a-z]+)"
)


def _norm_known(s: str) -> str:
    t = NUM_HEAD_RE.sub("", s.strip()).rstrip(":").strip()
    return re.sub(r"\s+", " ", t).lower()


def _title_score(s: str) -> float:
    """Title-Case 比例：大写/数字开头的实词 / 实词总数。"""
    toks = re.findall(r"[A-Za-z][A-Za-z0-9\u2019'\-]*|\d+", s)
    cap = low = 0
    for tok in toks:
        for part in (p for p in tok.split("-") if p):
            if part.lower() in STOP:
                continue
            if part[0].isupper() or part.isupper() or part[0].isdigit():
                cap += 1
            elif part[0].islower():
                low += 1
    total = cap + low
    return (cap / total) if total else 0.0


def _all_caps(s: str) -> bool:
    letters = [c for c in s if c.isalpha()]
    return bool(letters) and all(c.isupper() for c in letters) and len(s.split()) <= 7


def _is_caption(s: str) -> bool:
    return bool(CAPTION_RE.match(s.strip()))


def _is_bullet(s: str) -> bool:
    return bool(BULLET_RE.match(s.strip()))


def _tableish(s: str) -> bool:
    """表格/公式行：短行、不以完整句子结尾（短单元格允许标点）。"""
    s = s.strip()
    if not s or len(s) > 46:
        return False
    if s.endswith((".", "?", "!")) and len(s) > 25:
        return False
    return True


def _heading_kind(s: str, prev: str, nxt: str, refs: bool = False):
    """返回 'top' / 'sub' / None。"""
    s = s.strip()
    if not s or len(s) > 90:
        return None
    words = s.split()
    if len(words) > 12:
        return None
    if _is_caption(s) or _is_bullet(s):
        return None

    known = _norm_known(s)
    if known in TOP_LEVEL:
        return "top"
    if refs:
        # 参考文献区里任何大写短行都可能是文献标题，不该当节标题
        return None
    if known in SUB_LEVEL:
        return "sub"
    if ARXIV_STAMP_RE.match(s):
        return None

    prev_ok = (not prev) or prev.endswith(SENT_END) or (_norm_known(prev) in KNOWN)
    if not prev_ok:
        return None
    # 折行标题：下一行可能以小写介词开头（on GPT-Family Flagships）
    if nxt and nxt[:1].islower():
        if not (_title_score(s) >= 0.8 and len(nxt) <= 46 and _title_score(nxt) >= 0.8):
            return None
    if _all_caps(s):
        return "sub"
    if len(s) > 78 or s.endswith((",", ";", ":")):
        return None
    if ". " in s or s.endswith(("?", "!")):
        return None

    # 句首大写、全小写的短节标题：Prompt components. / Scope. / HelpSteer2 setup.
    if 1 <= len(words) <= 5 and len(s) <= 55 and s.endswith(".") and s[0].isupper():
        first = re.split(r"[^A-Za-z0-9]+", s.lower())[0]
        if first not in SENT_STARTERS and any(c.islower() for c in s):
            return "sub"

    # Title-Case 短行：Calibration Error and Score Spread Improve
    if len(words) <= 9 and s[0].isupper() and not s.endswith(".") and _title_score(s) >= 0.7:
        return "sub"
    return None


def _join(a: str, b: str) -> str:
    """合并两个物理行，处理行尾连字符断词。"""
    if not a:
        return b
    if a.endswith("-"):
        left = re.split(r"[\s(]", a[:-1])[-1].lower()
        # 下一行大写/数字：多数是真连字符（GPT-\\n5.2、self-\\nDebate）
        if b[:1].isdigit() or b[:1].isupper() or left.isdigit():
            return a + b
        if left in COMPOUND_LEFT:
            return a + b
        # ingre-\\ndients / ro-\\nbustness / Sum-\\nmEval
        return a[:-1] + b
    return a + " " + b


def _table_run(lines, i: int, n: int):
    """从 i 开始的连续短行块（>=4 行）视为表格/公式，保留物理换行。"""
    if not _tableish(lines[i]):
        return None
    j = i
    while j < n and lines[j].strip() and _tableish(lines[j]):
        j += 1
    return j if j - i >= 4 else None


# ============================================================
# UI 词清洗（2026-11-21）
# 有些论文的 fulltext_z 是从 arXiv HTML 抽的，混入了页面 UI 文字。
# 只匹配精确的 UI 行，不误伤正文。
# ============================================================
UI_LINE_RE = re.compile(
    r"^("
    r"Skip to main content"
    r"|Title: Content selection saved\.?.*"
    r"|Title: Description:.*"
    r"|Title:\\s*$"
    r"|Description:\\s*$"
    r"|Affiliation:\\s*$"
    r"|Email:\\s*$"
    r"|Description: License: arXiv\.org.*"
    r"|License: arXiv\.org perpetual non-exclusive license"
    r"|Report issue"
    r"|Back to arXiv"
    r"|Content selection saved\.?.*"
    r"|arXiv:\d{4}\.\d{4,5}v\d+ \[[^\]]+\] \d{1,2} \w{3} \d{4}"
    r"|arXiv:\d{4}\.\d{4,5}"
    r"|View PDF"
    r"|HTML\s*\(experimental\)"
    r"|Change to browse by:"
    r")$",
    re.IGNORECASE,
)


# 精确匹配的 UI 行（避免正则转义问题）
EXACT_UI = {
    "Title:", "Description:", "Affiliation:", "Email:",
    "Skip to main content", "Report issue", "Back to arXiv",
    "View PDF", "HTML (experimental)", "Change to browse by:",
}
# 前缀匹配的 UI 行
UI_PREFIX = (
    "Title: ", "Description: ", "License: arXiv.org",
    "Content selection saved", "arXiv:", "Skip to main",
)


def clean_ui_lines(text: str) -> str:
    """去掉 arXiv HTML 页面混入的 UI 行（精确匹配 + 前缀匹配 + 正则）"""
    if not text:
        return text
    out = []
    for ln in text.split("\n"):
        s = ln.strip()
        if not s:
            out.append(ln)
            continue
        if s in EXACT_UI:
            continue
        if s.startswith(UI_PREFIX):
            continue
        if UI_LINE_RE.match(s):
            continue
        out.append(ln)
    return "\n".join(out)


def reflow_pdf_text(text: str) -> str:
    text = clean_ui_lines(text)
    """把 PDF 抽取文本重排成 ``## 标题`` + 段落 + 表格块的纯文本。"""
    if not text:
        return ""

    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\f", "\n\n")
    text = text.replace("\u00a0", " ")
    text = "".join(ch for ch in text if ch == "\n" or ch == "\t" or ord(ch) >= 32)
    lines = [line.rstrip() for line in text.split("\n")]
    n = len(lines)

    # 第一个强节标题（Abstract / Introduction）之前视为题名/作者/单位
    first_strong = None
    for i, line in enumerate(lines):
        if line.strip() and _norm_known(line) in {"abstract", "introduction"}:
            first_strong = i
            break

    blank_count = sum(1 for line in lines if not line.strip())
    # 有些 PDF 每行之间都插空行（blank ratio ~50%），此时单空行不是段落分隔
    blank_heavy = blank_count > max(8, n * 0.2)

    prose_lens = [len(line.strip()) for line in lines if len(line.strip()) >= 35]
    width = statistics.median(prose_lens) if prose_lens else 60

    out: list[str] = []
    cur: list[str] = []
    para_kind = None
    refs = False
    top_active = False
    prev_heading = False

    def flush():
        nonlocal cur, para_kind
        if cur:
            acc = cur[0]
            for piece in cur[1:]:
                acc = _join(acc, piece)
            out.append(acc)
            cur = []
        para_kind = None

    i = 0
    while i < n:
        s = lines[i].strip()
        if not s:
            j = i
            while j < n and not lines[j].strip():
                j += 1
            if (not blank_heavy) or (j - i) >= 2:
                flush()
                prev_heading = False
            i = j
            continue

        # 题目/作者/单位：每行独立成段，避免被拼成一大段
        if first_strong is not None and i < first_strong:
            if cur and para_kind == "prose":
                cur.append(s)
            else:
                flush()
                cur = [s]
                para_kind = "prose"
            i += 1
            if cur and cur[-1].endswith(":") and i < first_strong:
                continue
            flush()
            prev_heading = False
            continue

        # arXiv 侧边栏戳（不是正文），单独一行，不并入摘要
        if ARXIV_STAMP_RE.match(s):
            if cur and cur[-1].endswith("-"):
                cur[-1] = cur[-1][:-1]
            flush()
            out.append(s)
            prev_heading = False
            i += 1
            continue

        # 表格 / 公式：保留物理换行
        table_end = _table_run(lines, i, n)
        if table_end:
            flush()
            out.append("\n".join(lines[k].strip() for k in range(i, table_end)))
            prev_heading = False
            i = table_end
            continue

        prev = lines[i - 1].strip() if i > 0 else ""
        nxt = lines[i + 1].strip() if i + 1 < n else ""
        kind = _heading_kind(s, prev, nxt, refs=refs)

        if kind:
            flush()
            norm = _norm_known(s)
            if kind == "top":
                level = "##"
                refs = norm in {"references", "bibliography"}
                top_active = True
            else:
                level = "###" if top_active else "##"

            heading = s
            j = i + 1
            # 合并被 PDF 折行的标题（已知节标题不合并，避免 "Method" 吞掉下一行）
            if kind == "sub":
                while j < n:
                    t = lines[j].strip()
                    if not t or _is_caption(t) or _is_bullet(t):
                        break
                    if _norm_known(t) in KNOWN:
                        break
                    if heading.endswith(SENT_END):
                        break
                    if len(t) > 58 or len(t.split()) > 10:
                        break
                    if t.endswith((",", ";", ":")):
                        break
                    if t[:1].islower():
                        if len(t) > 46:
                            break
                        after = lines[j + 1].strip() if j + 1 < n else ""
                        if after and after[:1].islower():
                            break
                    else:
                        if _title_score(t) < 0.8:
                            break
                        if t.endswith((".", "?", "!")):
                            break
                    heading = _join(heading, t)
                    j += 1
            out.append(level + " " + heading)
            prev_heading = True
            i = j
            continue

        if _is_caption(s):
            flush()
            cur = [s]
            para_kind = "caption"
            prev_heading = False
            i += 1
            continue

        if _is_bullet(s):
            flush()
            cur = [s]
            para_kind = "bullet"
            prev_heading = False
            i += 1
            continue

        if refs and cur and REF_START_RE.match(s):
            flush()

        if cur and para_kind in ("caption", "bullet"):
            cur.append(s)
            prev_heading = False
            i += 1
            continue

        # 段落边界：上一行是短句尾，且下一行另起一句
        boundary = False
        if cur:
            plast = cur[-1]
            if (
                plast.endswith(SENT_END)
                and len(plast) < width * 0.9
                and (s[:1].isupper() or s[:1].isdigit())
            ):
                boundary = True
            if s[:1].islower() and plast.endswith(SENT_END) and len(plast) < width * 0.75:
                boundary = True
        if boundary:
            flush()
        cur.append(s)
        para_kind = "prose"
        prev_heading = False
        i += 1

    flush()
    return "\n\n".join(block.rstrip() for block in out if block.strip())
