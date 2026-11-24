# -*- coding: utf-8 -*-
"""T1/T2/T3 分档函数测试（冻结口径：LCP_MIN=40，GRAM_N=20）

这些用例锁住 4 个边界：
  · 40 字连续 → T1；39 字 → T2（因为 20-gram 仍有交集）；
  · 20–39 字重叠 → T2；
  · 完全找不到 → T3；
  · 引文 <20 字 → T3（证据太短，不可核验）。
"""
import anchor_tiers as A


PAPER = (
    "在此我们提出一种方法，训练时使用固定的超参数 0.33 作为阈值。"
    "实验只在 fashion-MNIST 上进行，未与其他去噪架构比较，也没有报告方差与置信区间，"
    "所以结论的稳健性无法判断。"
)


def test_norm_removes_whitespace_and_lowercases():
    assert A.norm(" A b\nC\t") == "abc"
    assert A.norm("ＡＢＣ") == "abc"      # NFKC 全角转半角


def test_boundary_40_chars_is_T1():
    paper = "A" * 40
    r = A.tier_of("A" * 40, paper)
    assert r["tier"] == "T1"
    assert r["lcp"] == 40


def test_boundary_39_chars_is_T2_not_T1():
    paper = "A" * 40
    r = A.tier_of("A" * 39, paper)
    assert r["lcp"] == 39
    assert r["tier"] == "T2"
    assert r["cov20"] > 0


def test_short_overlap_is_T2():
    ev = "未与其他去噪架构比较，也没有报告方差与置信区间"
    r = A.tier_of(ev, PAPER)
    assert r["tier"] == "T2"
    assert 20 <= r["lcp"] < 40


def test_no_overlap_is_T3():
    r = A.tier_of("作者应当补充统计显著性检验", PAPER)
    assert r["tier"] == "T3"
    assert r["cov20"] == 0.0


def test_short_evidence_is_T3_even_if_verbatim():
    r = A.tier_of("超参数 0.33", PAPER)
    assert r["tier"] == "T3"
    assert r["lcp"] < 20


def test_fabricated_sentence_is_T3():
    r = A.tier_of(
        "The authors report a 3.7x speedup on a secret benchmark not described in this paper.",
        PAPER)
    assert r["tier"] == "T3"


def test_numeric_tamper_can_still_be_T1():
    """T1 是“引文在原文里找得到”，不是“数字对”。

    引文里改一个数字，只要还剩 >=40 字连续原文，仍会判 T1。
    这条测试把这个已知盲区写死，防止有人把锚定率当事实正确率。
    """
    # 前后各有一段长原文；只改中间 5 个字符，后面 60 字仍连续 → 仍 T1
    paper = "A" * 20 + "12345" + "B" * 60
    bad = "A" * 20 + "99999" + "B" * 60
    r = A.tier_of(bad, paper)
    assert r["tier"] == "T1"      # 改掉的只是中间 5 字，B 段 60 字仍逐字命中
    assert r["lcp"] == 60
