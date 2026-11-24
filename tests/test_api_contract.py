# -*- coding: utf-8 -*-
"""API 契约测试：/api/version、检索、鉴权边界、selfcheck、missing_cites、PwC、call 回放

原则：
  · 只读公开数据；
  · 不调用 v18 引擎（/api/evidence 只测 401 与缺 pid 的 400，绝不带 pid 调）；
  · 每个断言都能从响应字段复算。
"""
import re

import pytest

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def _no_heavy_snapshot_hash(monkeypatch):
    """API 测试不触发 12GB 全量哈希：快照 hash 由 test_snapshot_hash.py 专门测。"""
    import snapshot_hash
    monkeypatch.setattr(snapshot_hash, "snapshot_id",
                        lambda paths, **kw: "paperlib-test-fixture")


def test_health_200(client):
    r = client.get("/health")
    assert r.status_code == 200
    d = r.json()
    assert d.get("status") == "ok"
    assert "service" in d


def test_version_200_and_fields(client):
    r = client.get("/api/version")
    assert r.status_code == 200
    d = r.json()
    for k in ("ok", "schema_version", "current", "api_version", "data_snapshot", "total_papers"):
        assert k in d, "缺字段 %s" % k
    assert d["ok"] is True
    assert re.match(r"^paperlib-", d["data_snapshot"])


def test_schema_version_injected_on_api(client):
    d = client.get("/api/meta").json()
    assert d.get("schema_version") == "2026-11-21"
    assert d.get("api_version") == "v2026-11-21"


def test_meta_is_public(client):
    d = client.get("/api/meta").json()
    assert d["name"] == "qunzhi"
    assert any(c.get("id") == "kb_search" for c in d.get("capabilities") or [])


def test_kb_search_get_public(client):
    r = client.get("/api/kb/search", params={"q": "transformer", "limit": 3})
    assert r.status_code == 200
    d = r.json()
    assert isinstance(d.get("items"), list)
    assert isinstance(d.get("total"), int)
    assert d["total"] >= 1
    assert len(d["items"]) <= 3
    assert "call_id" in d


def test_kb_search_post_public(client):
    r = client.post("/api/kb/search", json={"q": "transformer", "limit": 2})
    assert r.status_code == 200
    d = r.json()
    assert d["returned"] <= 2


def test_kb_search_limit_capped(client):
    # SEARCH_TOTAL_CAP=600：单次展示/翻页上限，不能一次把全库吐出来
    r = client.get("/api/kb/search", params={"q": "transformer", "limit": 1000})
    assert r.status_code == 200
    d = r.json()
    assert d["returned"] <= 600
    assert len(d["items"]) <= 600


def test_evidence_no_key_401(client):
    r = client.post("/api/evidence", json={"pid": "ax_2510.19425"})
    assert r.status_code == 401
    assert "private" in r.json().get("detail", "") or "口令" in r.json().get("detail", "")


def test_evidence_with_key_missing_pid_400(client, key_headers):
    # 缺 pid 在 handler 入口就返回，不会触发引擎（不花钱）
    r = client.post("/api/evidence", json={}, headers=key_headers)
    assert r.status_code == 400
    assert "pid" in r.text


def test_missing_cites_no_key_401(client):
    r = client.get("/api/kb/missing_cites", params={"pid": "ax_2510.19425"})
    assert r.status_code == 401


def test_missing_cites_structure(client, key_headers, fulltext_pid):
    r = client.get("/api/kb/missing_cites", params={"pid": fulltext_pid, "top": 3},
                   headers=key_headers)
    assert r.status_code == 200
    d = r.json()
    for k in ("ok", "pid", "n_refs", "siblings", "threshold", "candidates"):
        assert k in d, "缺字段 %s" % k
    assert d["ok"] is True
    assert isinstance(d["candidates"], list)
    assert len(d["candidates"]) <= 3


def test_selfcheck_returns_20_features(client, fulltext_pid):
    r = client.get("/api/kb/selfcheck", params={"pid": fulltext_pid})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["n_features_total"] == 20
    assert len(d["rows"]) == 20
    assert d["n_features"] == 20
    # 20 个特征每行都要有 feat / has 两个字段，便于下游按行复算
    for row in d["rows"]:
        assert "feat" in row and "has" in row


def test_selfcheck_no_pid_400(client):
    r = client.get("/api/kb/selfcheck")
    assert r.status_code == 400
    assert "pid" in r.text


def test_pwc_scores_public(client):
    r = client.get("/api/pwc/scores", params={"limit": 2})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["total"] == 205840
    assert len(d["items"]) == 2
    assert "call_id" in d


def test_call_replay_public(client):
    d = client.get("/api/pwc/scores", params={"limit": 1}).json()
    cid = d["call_id"]
    r = client.get("/api/call/%s" % cid)
    assert r.status_code == 200
    replay = r.json()
    assert replay.get("total") == d.get("total")
    assert replay.get("call_id") == cid or replay.get("replayed") is True


def test_call_unknown_404(client):
    r = client.get("/api/call/qz_0000000000000000")
    assert r.status_code == 404


def test_public_api_does_not_require_key(client):
    # 公开层：不带 key 也必须 200
    for path in ("/api/version", "/api/meta", "/api/pwc/scores?limit=1"):
        assert client.get(path).status_code == 200, path
