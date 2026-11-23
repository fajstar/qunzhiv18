# -*- coding: utf-8 -*-
"""privacy_scrubber.py 纯函数测试"""
import pytest
from privacy_scrubber import scrub, scrub_with_stats, scrub_review, scrub_payload, looks_local_ip


# ---- scrub: 邮箱 ----

def test_scrub_email():
    """邮箱 → [EMAIL]"""
    text = "联系作者 email: alice@example.com 获取数据"
    result = scrub(text)
    assert "alice@example.com" not in result, "邮箱应被替换"
    assert "[EMAIL]" in result, "应包含 [EMAIL] 占位符"


def test_scrub_multiple_emails():
    """多个邮箱全部替换"""
    text = "a@b.com and c@d.org"
    result = scrub(text)
    assert "a@b.com" not in result
    assert "c@d.org" not in result
    assert result.count("[EMAIL]") == 2


# ---- scrub: 电话 ----

def test_scrub_phone_with_context():
    """带"电话"上下文的号码 → [PHONE]"""
    text = "联系电话：13812345678"
    result = scrub(text)
    assert "13812345678" not in result
    assert "[PHONE]" in result


def test_scrub_phone_english_context():
    """英文电话上下文 → [PHONE]"""
    text = "Phone: +86-10-12345678"
    result = scrub(text)
    assert "12345678" not in result
    assert "[PHONE]" in result


# ---- scrub: ORCID ----

def test_scrub_orcid():
    """ORCID → [ORCID]"""
    text = "Author ORCID: 0000-0002-1825-0097"
    result = scrub(text)
    assert "0000-0002-1825-0097" not in result
    assert "[ORCID]" in result


# ---- scrub: 基金号 ----

def test_scrub_grant_number():
    """Grant No → [GRANT]"""
    text = "This work was supported by Grant No. 12345678"
    result = scrub(text)
    assert "12345678" not in result
    assert "[GRANT]" in result


def test_scrub_nsfc_grant():
    """NSFC 基金号 → [GRANT]"""
    text = "Funded by NSFC-62176001"
    result = scrub(text)
    assert "NSFC-62176001" not in result
    assert "[GRANT]" in result


# ---- scrub: 不误伤 ----

def test_scrub_normal_text_unchanged():
    """普通学术文本不应被修改"""
    text = "We propose a novel method for image segmentation using deep learning. The results show 15.3% improvement over baseline methods with p < 0.01."
    result = scrub(text)
    assert result == text, f"普通文本不应被修改，但结果不同：{result}"


def test_scrub_preserves_numbers():
    """统计数字不被替换"""
    text = "The accuracy was 95.7% with a sample size of 1024 participants."
    result = scrub(text)
    assert "95.7%" in result
    assert "1024" in result


# ---- scrub_with_stats ----

def test_scrub_with_stats_returns_structure():
    """返回含 text/redactions/total 的 dict"""
    r = scrub_with_stats("Email: test@example.com")
    assert "text" in r
    assert "redactions" in r
    assert "total" in r
    assert r["total"] >= 1
    assert "email" in r["redactions"]


def test_scrub_with_stats_no_redaction():
    """无需脱敏时 total=0"""
    r = scrub_with_stats("这是一段普通文本，没有任何敏感信息。")
    assert r["total"] == 0


# ---- scrub_review ----

def test_scrub_reviewer_id():
    """Reviewer #1 → [REVIEWER_1]"""
    text = "Reviewer #1: The paper is well written. Reviewer #2: Needs more experiments."
    result = scrub_review(text)
    assert "Reviewer #1" not in result
    assert "Reviewer #2" not in result
    assert "[REVIEWER_1]" in result
    assert "[REVIEWER_2]" in result


def test_scrub_reviewer_chinese():
    """审稿人 1 → [REVIEWER_1]"""
    text = "审稿人1：论文写得不错。"
    result = scrub_review(text)
    assert "审稿人1" not in result
    assert "[REVIEWER_1]" in result


# ---- scrub_payload ----

def test_scrub_payload_dict():
    """dict 中 text 字段的邮箱被替换"""
    payload = {"text": "Contact: foo@bar.com", "title": "Paper"}
    result = scrub_payload(payload)
    assert "foo@bar.com" not in result["text"]
    assert "[EMAIL]" in result["text"]
    assert result["title"] == "Paper", "非敏感字段不变"


def test_scrub_payload_nested():
    """嵌套 dict/list 递归脱敏"""
    payload = {"items": [{"content": "Email me at x@y.com"}]}
    result = scrub_payload(payload)
    assert "x@y.com" not in result["items"][0]["content"]


def test_scrub_payload_non_sensitive_key_untouched():
    """非敏感 key 不脱敏"""
    payload = {"title": "Email: admin@test.com", "text": "safe"}
    result = scrub_payload(payload)
    assert result["title"] == "Email: admin@test.com", "title 不在敏感 key 中"


# ---- looks_local_ip ----

def test_local_ip_localhost():
    assert looks_local_ip("127.0.0.1") is True


def test_local_ip_private():
    assert looks_local_ip("192.168.1.100") is True


def test_local_ip_link_local():
    assert looks_local_ip("169.254.1.1") is True


def test_public_ip_not_local():
    assert looks_local_ip("8.8.8.8") is False


def test_empty_ip_not_local():
    assert looks_local_ip("") is False


def test_invalid_ip_not_local():
    assert looks_local_ip("not-an-ip") is False


# ---- URL credential scrubbing ----

def test_scrub_url_credential():
    """URL 中的 user:pass@ 凭据被移除（邮箱模式先匹配，密码部分被替换）"""
    text = "Visit https://admin:secret123@api.example.com/data"
    result = scrub(text)
    assert "secret123" not in result, "密码不应出现在输出中"
    assert "api.example.com" not in result, "域名不应出现在输出中（被邮箱模式整体替换）"
