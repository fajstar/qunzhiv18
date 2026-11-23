# -*- coding: utf-8 -*-
"""input_intent.py 纯函数测试"""
import pytest
from input_intent import assess_intent, _heuristic, _parse


# ---- assess_intent: 空输入 ----

def test_empty_topic_returns_c():
    """空 topic → intent=C, clarity=0, needs_clarify=True"""
    r = assess_intent("")
    assert r["intent"] == "C", f"期望 C，实际 {r['intent']}"
    assert r["clarity"] == 0, f"期望 clarity=0，实际 {r['clarity']}"
    assert r["needs_clarify"] is True
    assert r["source"] == "empty"


def test_none_topic_returns_c():
    """None topic → intent=C"""
    r = assess_intent(None)
    assert r["intent"] == "C"


# ---- assess_intent: 启发式（无 api_caller）----

def test_research_question_intent_a():
    """含"如何"+明确对象 → intent=A（研究问题）"""
    r = assess_intent("如何提升师范生课堂参与度")
    assert r["intent"] == "A", f"期望 A，实际 {r['intent']}"
    assert r["source"] == "heuristic"


def test_comparison_intent_b():
    """含"还是"等比较词 → intent=B"""
    r = assess_intent("课题经费该买设备还是招研究生")
    assert r["intent"] == "B", f"期望 B，实际 {r['intent']}"


def test_vague_idea_intent_c():
    """无研究问句、无比较词、很短 → intent=C"""
    r = assess_intent("做一下课堂改革")
    assert r["intent"] == "C", f"期望 C，实际 {r['intent']}"
    assert r["needs_clarify"] is True


def test_research_question_with_metric():
    """含"如何"+指标词+足够长度 → intent=A, clarity 较高"""
    r = assess_intent("如何提升学生成绩和课堂参与度")
    assert r["intent"] == "A", f"期望 A，实际 {r['intent']}"
    assert r["clarity"] >= 54, f"clarity 应 >= 54，实际 {r['clarity']}"


# ---- _heuristic ----

def test_heuristic_returns_all_fields():
    """_heuristic 返回完整字段集"""
    r = _heuristic("如何优化 MySQL 慢查询", "")
    for key in ("intent", "clarity", "needs_clarify", "missing", "rewritten", "reason", "source"):
        assert key in r, f"缺少字段 {key}"
    assert r["source"] == "heuristic"


def test_heuristic_missing_fields():
    """缺主体/时间/指标时 missing 非空"""
    r = _heuristic("优化一下", "")
    assert len(r["missing"]) > 0, "应有缺失字段提示"


# ---- _parse ----

def test_parse_valid_json():
    """合法 JSON → 解析成功"""
    raw = '{"intent":"A","clarity":80,"missing":["时间尺度"],"rewritten":"如何提升？","reason":"明确"}'
    r = _parse(raw, "如何提升", "")
    assert r is not None
    assert r["intent"] == "A"
    assert r["clarity"] == 80
    assert r["source"] == "model"


def test_parse_none_returns_none():
    """raw=None → None"""
    assert _parse(None, "topic", "") is None


def test_parse_empty_returns_none():
    """raw='' → None"""
    assert _parse("", "topic", "") is None


def test_parse_no_json_returns_none():
    """无 JSON 结构 → None"""
    assert _parse("这不是 JSON", "topic", "") is None


def test_parse_invalid_intent_returns_none():
    """intent 不在 ABC → None"""
    raw = '{"intent":"X","clarity":50}'
    assert _parse(raw, "topic", "") is None


def test_parse_clarity_clamped():
    """clarity 超出 0-100 被截断"""
    raw = '{"intent":"A","clarity":200}'
    r = _parse(raw, "topic", "")
    assert r["clarity"] == 100, f"clarity 应截断为 100，实际 {r['clarity']}"


# ---- assess_intent with api_caller ----

def test_assess_with_api_caller_success():
    """api_caller 返回合法 JSON → source=model"""
    def mock_caller(prompt, **kw):
        return '{"intent":"B","clarity":70,"missing":[],"rewritten":"选哪个？","reason":"比较"}'
    r = assess_intent("A还是B", api_caller=mock_caller)
    assert r["intent"] == "B"
    assert r["source"] == "model"


def test_assess_with_api_caller_failure_falls_back():
    """api_caller 抛异常 → 回退启发式"""
    def bad_caller(prompt, **kw):
        raise RuntimeError("timeout")
    r = assess_intent("如何提升师范生的课堂参与度", api_caller=bad_caller)
    assert r["source"] == "heuristic"
    assert r["intent"] == "A"
