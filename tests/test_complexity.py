# -*- coding: utf-8 -*-
"""complexity.py 纯函数测试"""
import pytest
from complexity import assess_complexity, get_level_config


# ---- assess_complexity ----

def test_assess_no_caller_returns_medium():
    """无 api_caller 时默认返回"中等"，不联网"""
    result = assess_complexity("如何教中学生一次函数")
    assert result == "中等", f"期望 '中等'，实际 '{result}'"


def test_assess_empty_topic_no_caller():
    """空 topic + 无 api_caller → "中等" """
    result = assess_complexity("")
    assert result == "中等", f"期望 '中等'，实际 '{result}'"


def test_assess_api_returns_level_a():
    """api_caller 返回 JSON level=A → "简单" """
    def mock_caller(prompt, **kw):
        return '{"level": "A", "reason": "成熟教学技巧"}'
    result = assess_complexity("如何教中学生一次函数", api_caller=mock_caller)
    assert result == "简单", f"期望 '简单'，实际 '{result}'"


def test_assess_api_returns_level_c():
    """api_caller 返回 JSON level=C → "复杂" """
    def mock_caller(prompt, **kw):
        return '{"level": "C", "reason": "多子系统耦合"}'
    result = assess_complexity("低温SOC估算", api_caller=mock_caller)
    assert result == "复杂", f"期望 '复杂'，实际 '{result}'"


def test_assess_api_returns_level_b():
    """api_caller 返回 JSON level=B → "中等" """
    def mock_caller(prompt, **kw):
        return '{"level": "B", "reason": "有成熟调优路径"}'
    result = assess_complexity("如何优化 MySQL 慢查询", api_caller=mock_caller)
    assert result == "中等", f"期望 '中等'，实际 '{result}'"


def test_assess_api_chinese_level():
    """api_caller 返回中文 level → 原样返回"""
    def mock_caller(prompt, **kw):
        return '{"level": "复杂", "reason": "跨学科"}'
    result = assess_complexity("某复杂课题", api_caller=mock_caller)
    assert result == "复杂", f"期望 '复杂'，实际 '{result}'"


def test_assess_api_exception_returns_medium():
    """api_caller 抛异常 → 回退"中等" """
    def bad_caller(prompt, **kw):
        raise ConnectionError("network down")
    result = assess_complexity("任意课题", api_caller=bad_caller)
    assert result == "中等", f"期望 '中等'，实际 '{result}'"


def test_assess_api_garbage_returns_medium():
    """api_caller 返回无法解析的内容 → 回退"中等" """
    def mock_caller(prompt, **kw):
        return "这不是 JSON，也没有有用的信息"
    result = assess_complexity("任意课题", api_caller=mock_caller)
    assert result == "中等", f"期望 '中等'，实际 '{result}'"


def test_assess_api_short_response_single_letter():
    """api_caller 返回短回复单个字母 A → "简单" """
    def mock_caller(prompt, **kw):
        return "A"
    result = assess_complexity("简单课题", api_caller=mock_caller)
    assert result == "简单", f"期望 '简单'，实际 '{result}'"


def test_assess_api_short_response_C():
    """api_caller 返回短回复单个字母 C → "复杂" """
    def mock_caller(prompt, **kw):
        return "C"
    result = assess_complexity("复杂课题", api_caller=mock_caller)
    assert result == "复杂", f"期望 '复杂'，实际 '{result}'"


# ---- get_level_config ----

def test_level_config_simple():
    cfg = get_level_config("简单")
    assert cfg == {"max_rounds": 1, "output": "quick"}, f"期望简单配置，实际 {cfg}"


def test_level_config_medium():
    cfg = get_level_config("中等")
    assert cfg == {"max_rounds": 2, "output": "standard"}, f"期望中等配置，实际 {cfg}"


def test_level_config_complex():
    cfg = get_level_config("复杂")
    assert cfg == {"max_rounds": 3, "output": "full"}, f"期望复杂配置，实际 {cfg}"


def test_level_config_unknown_defaults_to_medium():
    cfg = get_level_config("不存在的级别")
    assert cfg == get_level_config("中等"), f"未知级别应回退到中等配置"
