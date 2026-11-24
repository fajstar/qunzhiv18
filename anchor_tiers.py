# -*- coding: utf-8 -*-
"""anchor_tiers.py · T1/T2/T3 锚定分档（纯函数，v18-fix9 第 2 项）

口径来自 `qunzhi-experiments/03_impl/evidence.py`（冻结版，未改阈值）：
  T1 = 归一化后 evidence 与原文最长连续公共片段 >= 40 字
  T2 = 最长片段 < 40 字，但 evidence 的 20-gram 与原文有交集（cov20 > 0）
  T3 = cov20 = 0

注意：这是“引文是否在原文里找得到”，**不是“数字/结论是否正确”**。
T1 只要求存在一段 >=40 字的连续原文；引文里改一个数字，剩下的长片段仍可能 >=40 字，
所以仍会判 T1（见 第 2 项负对照 1）。不要把锚定率当成事实正确率。
"""
from __future__ import annotations

import unicodedata

LCP_MIN = 40
GRAM_N = 20
SIM_MERGE = 0.70
SIM_DEDUP = 0.45


def norm(s: str) -> str:
    """NFKC → 去掉所有空白 → 小写（与 evidence.py 一致）"""
    s = unicodedata.normalize("NFKC", s or "")
    return "".join(ch for ch in s if not ch.isspace()).lower()


def lcp_len(a: str, b: str) -> int:
    """最长连续公共片段长度。用窗口包含判断（C 层 find），与 DP 结果等价但快得多。"""
    n = len(a)
    if not n or not b:
        return 0

    def has(L: int) -> bool:
        for i in range(n - L + 1):
            if a[i:i + L] in b:
                return True
        return False

    lo, hi = 0, n
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if mid == 0 or has(mid):
            lo = mid
        else:
            hi = mid - 1
    return lo


def tier_of(evidence: str, paper: str) -> dict:
    """返回 {tier, lcp, cov20}；空 evidence/paper -> T3"""
    e, p = norm(evidence), norm(paper)
    if not e or not p:
        return {"tier": "T3", "lcp": 0, "cov20": 0.0}
    l = lcp_len(e, p)
    if len(e) >= GRAM_N:
        ge = {e[i:i + GRAM_N] for i in range(len(e) - GRAM_N + 1)}
        gp = {p[i:i + GRAM_N] for i in range(len(p) - GRAM_N + 1)}
        c = len(ge & gp) / len(ge) if ge else 0.0
    else:
        c = 0.0
    if l >= LCP_MIN:
        return {"tier": "T1", "lcp": l, "cov20": c}
    if c > 0:
        return {"tier": "T2", "lcp": l, "cov20": c}
    return {"tier": "T3", "lcp": l, "cov20": c}
