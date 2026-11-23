# -*- coding: utf-8 -*-
"""review_anchors.py 纯函数测试"""
import pytest
import review_anchors as ra


# ---- _resolve_units ----

def test_resolve_units_callable():
    """callable 输入 → 3 个 (None, fn) 元组"""
    fn = lambda a, b: {"winner": "无差别"}
    units = ra._resolve_units(fn)
    assert len(units) == 3, f"期望 3 个单元，实际 {len(units)}"
    for name, f in units:
        assert name is None
        assert f is fn


def test_resolve_units_dict():
    """dict{模型: fn} 输入 → 3 个 (模型名, fn) 元组"""
    fns = {f"model_{i}": (lambda a, b: {}) for i in range(3)}
    units = ra._resolve_units(fns)
    assert len(units) == 3, f"期望 3 个单元，实际 {len(units)}"
    names = [m for m, _ in units]
    assert set(names) == set(fns.keys())


def test_resolve_units_invalid_type():
    """非法类型 → TypeError"""
    with pytest.raises(TypeError):
        ra._resolve_units(42)


def test_resolve_units_empty_dict():
    """空 dict → TypeError"""
    with pytest.raises(TypeError):
        ra._resolve_units({})


def test_resolve_units_wrong_count():
    """dict 元素数 ≠ 3 → ValueError（2*2=4 ≠ 6）"""
    fns = {"m1": lambda a, b: {}, "m2": lambda a, b: {}}
    with pytest.raises(ValueError, match="评审单元数"):
        ra._resolve_units(fns)


# ---- anchor_a_total_failed ----

def test_anchor_a_total_failed_empty():
    """空 votes → True（一次都没跑成）"""
    assert ra.anchor_a_total_failed({"anchor_a_votes": []}) is True


def test_anchor_a_total_failed_none():
    """votes 为 None → True"""
    assert ra.anchor_a_total_failed({"anchor_a_votes": None}) is True


def test_anchor_a_total_failed_has_votes():
    """有投票 → False"""
    assert ra.anchor_a_total_failed({"anchor_a_votes": ["完整版胜", "完整版胜"]}) is False


# ---- _cache_key ----

def test_cache_key_deterministic():
    """相同输入 → 相同 key"""
    fn = lambda a, b: {}
    units = [(None, fn)] * 3
    mats = {"original": "A", "cut60": "B", "self": "A"}
    k1 = ra._cache_key(units, mats)
    k2 = ra._cache_key(units, mats)
    assert k1 == k2, "相同输入应产生相同 cache key"


def test_cache_key_differs_on_materials():
    """不同材料 → 不同 key"""
    fn = lambda a, b: {}
    units = [(None, fn)] * 3
    k1 = ra._cache_key(units, {"original": "A", "cut60": "B", "self": "A"})
    k2 = ra._cache_key(units, {"original": "X", "cut60": "Y", "self": "X"})
    assert k1 != k2, "不同材料应产生不同 cache key"


# ---- check_anchors (integration with mock) ----

def test_check_anchors_both_pass():
    """mock 评审器：完整版胜 + 自身无差别 → passed=True"""
    ra._ANCHOR_CACHE.clear()
    materials = {
        "original": "This is a long and complete version of the report with detailed analysis and thorough discussion of all aspects.",
        "cut60": "Short.",
        "self": "This is a long and complete version of the report with detailed analysis and thorough discussion of all aspects.",
    }

    def mock_pairwise(text_a, text_b):
        if text_a == text_b:
            return {"winner": "无差别", "model": "mock", "raw": ""}
        elif len(text_a) > len(text_b):
            return {"winner": "甲", "model": "mock", "raw": ""}
        else:
            return {"winner": "乙", "model": "mock", "raw": ""}

    result = ra.check_anchors(mock_pairwise, materials=materials, use_cache=False)
    assert result["passed"] is True, f"期望 passed=True，实际 {result}"
    assert result["detail"]["anchor_a_passed"] is True
    assert result["detail"]["anchor_b_passed"] is True


def test_check_anchors_anchor_b_fail():
    """评审器对相同文本判"甲胜" → 锚点乙不过 → passed=False"""
    ra._ANCHOR_CACHE.clear()
    materials = {
        "original": "Complete text here with enough length.",
        "cut60": "Short.",
        "self": "Complete text here with enough length.",
    }

    def mock_pairwise(text_a, text_b):
        return {"winner": "甲", "model": "mock", "raw": ""}

    result = ra.check_anchors(mock_pairwise, materials=materials, use_cache=False)
    assert result["detail"]["anchor_b_passed"] is False, "锚点乙应不通过"
    assert result["passed"] is False
