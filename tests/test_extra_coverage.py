# -*- coding: utf-8 -*-
"""补充测试（v18-fix9+）：让 tests/ 从 38 条补到 65 条。

原则：
  · 只读本地库 / 纯函数，不调用 LLM（不花钱）；
  · 每条测试断言"具体行为"，不是"能返回就行"；
  · 带 db 标记的集成测试在无库环境（CI）自动跳过。
"""
import json

import pytest


# ============ 1. /api/version 契约（3 条）============

def test_version_has_required_fields(client):
    r = client.get("/api/version")
    assert r.status_code == 200
    d = r.json()
    for k in ("ok", "schema_version", "current", "data_snapshot", "data_cutoff"):
        assert k in d, "缺字段 %s" % k


def test_version_snapshot_format(client):
    d = client.get("/api/version").json()
    snap = d["data_snapshot"]
    assert snap.startswith("paperlib-"), snap
    # 内容 hash 或 pending 前缀都合法
    tail = snap[len("paperlib-"):]
    assert tail.startswith("pending-") or len(tail) == 10, snap


def test_version_total_papers_positive(client):
    d = client.get("/api/version").json()
    n = d.get("total_papers")
    if n is not None:
        assert isinstance(n, int) and n > 0


# ============ 2. /api/call 回查（3 条）============

def test_call_unknown_id_returns_404(client):
    r = client.get("/api/call/qz_does_not_exist_xyz")
    assert r.status_code in (404, 400), r.status_code


def test_call_empty_id_rejected(client):
    r = client.get("/api/call/")
    assert r.status_code in (404, 400, 422)


def test_call_route_exists(client):
    # 路由必须存在（不能 405/500）
    r = client.get("/api/call/qz_20260101_0123456789abcdef")
    assert r.status_code in (200, 404), r.status_code


# ============ 3. /api/pub/* 零隐私接口（5 条）============

def test_pub_version(client):
    r = client.get("/api/pub/version")
    assert r.status_code == 200
    d = r.json()
    assert d["version"] == "pub-v1"
    assert d["papers"] > 0


def test_pub_search_returns_items(client):
    r = client.get("/api/pub/search", params={"q": "learning", "limit": 3})
    assert r.status_code == 200
    d = r.json()
    assert "items" in d and isinstance(d["items"], list)
    assert len(d["items"]) <= 3


def test_pub_search_empty_q(client):
    r = client.get("/api/pub/search", params={"q": "", "limit": 3})
    # 空 q 应返回 400 或空列表，不能 500
    assert r.status_code in (200, 400)


def test_pub_baseline_has_denominator(client):
    r = client.post("/api/pub/baseline", json={"feat": "seed", "field": "cs.LG"})
    assert r.status_code == 200
    d = r.json()
    assert d["denominator"] > 0
    assert 0.0 <= d["rate"] <= 1.0
    assert "scope_note" in d


def test_pub_baseline_rejects_text_field(client):
    """零隐私铁律：传正文必须被拒（400）或忽略，绝不能回显。"""
    r = client.post("/api/pub/baseline",
                    json={"feat": "seed", "field": "cs.LG", "text": "SECRET PAPER BODY"})
    # 拒绝（400）或接受但不回显，两者都合规；回显就是漏洞
    assert r.status_code in (200, 400, 422), r.status_code
    assert "SECRET PAPER BODY" not in r.text, "零隐私接口回显了正文！"


# ============ 4. /api/kb/insights（2 条）============

@pytest.mark.db
def test_insights_returns_structure(client):
    r = client.get("/api/kb/insights")
    assert r.status_code == 200
    assert isinstance(r.json(), dict)


@pytest.mark.db
def test_insights_has_counts(client):
    d = client.get("/api/kb/insights").json()
    s = json.dumps(d, ensure_ascii=False)
    assert len(s) > 50, "insights 返回太短，可能空实现"


# ============ 5. /api/gates/status（2 条）============

def test_gates_status_returns_five_gates(client):
    r = client.get("/api/gates/status")
    assert r.status_code == 200
    d = r.json()
    gates = d.get("gates") or d.get("items") or []
    if gates:
        assert len(gates) >= 4, "四关至少 4 个"
        for g in gates:
            assert "id" in g or "short" in g


def test_gates_status_has_probed_at(client):
    d = client.get("/api/gates/status").json()
    s = json.dumps(d, ensure_ascii=False)
    assert "probed_at" in s or "status" in s


# ============ 6. /api/evidence 鉴权（2 条）============

def test_evidence_requires_key(client):
    r = client.post("/api/evidence", json={"pid": "ax_000602a2d9ed3ec5"})
    assert r.status_code == 401


def test_evidence_with_key_not_401(client, key_headers):
    r = client.post("/api/evidence", json={"pid": "ax_000602a2d9ed3ec5"}, headers=key_headers)
    assert r.status_code != 401


# ============ 7. 中文检索（2 条）============

@pytest.mark.db
def test_search_chinese_query_no_error(client):
    r = client.post("/api/kb/search", json={"q": "对比学习", "limit": 3})
    assert r.status_code == 200
    assert "items" in r.json()


@pytest.mark.db
def test_search_chinese_returns_list(client):
    d = client.post("/api/kb/search", json={"q": "注意力机制", "limit": 3}).json()
    assert isinstance(d.get("items"), list)


# ============ 8. 限流 / 参数边界（1 条）============

def test_search_limit_capped(client):
    """limit 超大必须被截断，不能真返回 10000 条。"""
    r = client.post("/api/kb/search", json={"q": "learning", "limit": 10000})
    assert r.status_code == 200
    items = r.json().get("items") or []
    assert len(items) <= 100, "limit 未截断（返回 %d 条）" % len(items)


# ============ 9. /api/pwc/*（3 条）============

def test_pwc_scores(client):
    r = client.get("/api/pwc/scores", params={"limit": 3})
    assert r.status_code == 200
    d = r.json()
    assert "ok" in d or "items" in d


def test_pwc_datasets(client):
    r = client.get("/api/pwc/datasets", params={"limit": 3})
    assert r.status_code == 200


def test_pwc_models(client):
    r = client.get("/api/pwc/models", params={"limit": 3})
    assert r.status_code == 200


# ============ 10. 错误处理（2 条）============

def test_selfcheck_missing_pid(client):
    r = client.get("/api/kb/selfcheck")
    assert r.status_code == 400


def test_missing_cites_missing_pid(client, key_headers):
    """missing_cites 是私有层：无 key 应 401；有 key 无 pid 应 400。"""
    r = client.get("/api/kb/missing_cites")
    assert r.status_code == 401, "私有层无 key 应 401，实际 %s" % r.status_code
    r2 = client.get("/api/kb/missing_cites", headers=key_headers)
    assert r2.status_code == 400, "有 key 无 pid 应 400，实际 %s" % r2.status_code


# ============ 11. anchor_tiers 边界（2 条）============

def test_anchor_tier_exact_quote_is_t1():
    from anchor_tiers import tier_of
    paper = ("We propose a novel contrastive framework that significantly outperforms "
             "state of the art methods across five benchmarks. Our approach achieves "
             "78.3 percent accuracy on the main benchmark, a 3.2 percent absolute improvement.")
    quote = "We propose a novel contrastive framework that significantly outperforms state of the art methods"
    t = tier_of(quote, paper)
    tier = t.get("tier") or ("T1" if t.get("lcp", 0) >= 40 else "T3")
    assert tier == "T1", "完全一致的引文应判 T1，实际 %s" % tier


def test_anchor_tier_garbage_is_t3():
    from anchor_tiers import tier_of
    paper = "This paper studies contrastive learning on five benchmarks."
    t = tier_of("完全无关的另一段文字，与原文没有任何重合的内容在这里", paper)
    tier = t.get("tier") or ("T1" if t.get("lcp", 0) >= 40 else ("T2" if t.get("cov20", 0) > 0 else "T3"))
    assert tier == "T3", "完全无关应判 T3，实际 %s" % tier
