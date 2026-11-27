# -*- coding: utf-8 -*-
"""review_cache 纯函数测试：不联网、不依赖网关、不碰真实缓存目录。"""
import os

import pytest

import review_cache


@pytest.fixture(autouse=True)
def _isolated_cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(review_cache, "CACHE_DIR", str(tmp_path / "cache"))
    return tmp_path / "cache"


REPORT = "本课题拟研究城市热岛效应与绿地率的关系，采用遥感反演方法。"
PAYLOAD = {"ok": True, "review_v6": {"anchors_passed": True}, "cached": False}


def test_key_for_same_input_same_key():
    a = review_cache.key_for(REPORT, "热岛", None)
    b = review_cache.key_for(REPORT, "热岛", None)
    assert a == b
    assert len(a) == 24


def test_key_for_one_char_change_changes_key():
    a = review_cache.key_for(REPORT, "热岛", None)
    b = review_cache.key_for(REPORT.replace("绿地率", "绿地覆盖率"), "热岛", None)
    assert a != b


def test_key_for_none_and_empty_baseline_identical():
    assert (review_cache.key_for(REPORT, "热岛", None)
            == review_cache.key_for(REPORT, "热岛", ""))


def test_put_get_roundtrip(_isolated_cache_dir):
    key = review_cache.key_for(REPORT, "", None)
    assert review_cache.put(key, PAYLOAD) is True
    assert (_isolated_cache_dir / (key + ".json")).exists()
    assert review_cache.get(key) == PAYLOAD


def test_get_expired_when_ttl_zero(_isolated_cache_dir):
    key = review_cache.key_for(REPORT, "", None)
    assert review_cache.put(key, PAYLOAD) is True
    assert review_cache.get(key, ttl_sec=0) is None
    assert review_cache.get(key) == PAYLOAD


def test_corrupted_file_returns_none_without_raising(_isolated_cache_dir):
    key = review_cache.key_for(REPORT, "", None)
    _isolated_cache_dir.mkdir(parents=True, exist_ok=True)
    path = _isolated_cache_dir / (key + ".json")
    path.write_text("{not valid json!!", encoding="utf-8")
    assert review_cache.get(key) is None
    assert review_cache.age_sec(key) is not None


def test_age_sec_and_missing_key():
    key = review_cache.key_for(REPORT, "", None)
    assert review_cache.age_sec(key) is None
    assert review_cache.put(key, PAYLOAD) is True
    age = review_cache.age_sec(key)
    assert isinstance(age, int) and age >= 0


def test_invalid_key_rejected(_isolated_cache_dir):
    assert review_cache.get("../evil") is None
    assert review_cache.put("../evil", PAYLOAD) is False
    assert review_cache.age_sec("../evil") is None
    if _isolated_cache_dir.exists():
        assert os.listdir(_isolated_cache_dir) == []