import pytest

from app.db import knowledge
from app.training import review


def _draft(title="Brand performance decomposition", body="Decompose sales into market, share, price/mix."):
    return knowledge.create_draft("playbook", title, body, "brand_analytics", "deck_upload", "x.pdf")


def test_draft_is_not_retrievable():
    _draft()
    assert knowledge.search_methods("brand performance") == []


def test_approve_requires_anonymization_confirmation():
    method_id = _draft()
    with pytest.raises(ValueError):
        knowledge.approve(method_id, "trainer-1")


def test_approved_method_is_retrievable():
    method_id = _draft()
    knowledge.mark_anonymized(method_id, True)
    knowledge.approve(method_id, "trainer-1")
    results = knowledge.search_methods("brand performance decline")
    assert [r["method_id"] for r in results] == [method_id]


def test_reject_removes_from_index():
    method_id = _draft()
    knowledge.mark_anonymized(method_id, True)
    knowledge.approve(method_id, "trainer-1")
    knowledge.reject(method_id, "trainer-1")
    assert knowledge.search_methods("brand performance") == []


def test_approved_methods_are_immutable():
    method_id = _draft()
    knowledge.mark_anonymized(method_id, True)
    knowledge.approve(method_id, "trainer-1")
    with pytest.raises(ValueError):
        knowledge.update_draft(method_id, body_md="tampered")


def test_regex_scan_flags_identifiers():
    method_id = _draft(body="Contact jane@pfizer.com about the $4.2M Northeast deal.")
    flags = review.scan(method_id, use_llm=False)
    assert any("email" in f for f in flags)
    assert any("figure" in f for f in flags)
    assert knowledge.get_method(method_id)["status"] == "pending_review"


def test_punctuation_query_does_not_crash():
    assert knowledge.search_methods("what?! (about) 'this'") == []
