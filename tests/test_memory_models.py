from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from mystery_core.memory import DEFAULT_POLICY, FAR_FUTURE, Memory, NewMemory, Scope, validate_viewer
from mystery_core.memory import ids


@pytest.mark.parametrize("v", ["suspect:butler", "team:A", "team:team_2"])
def test_valid_viewers(v):
    assert validate_viewer(v) == v


@pytest.mark.parametrize("v", ["butler", "suspect:", "admin:x", "team:A; drop", "suspect:*", ""])
def test_invalid_viewers(v):
    with pytest.raises(ValueError):
        validate_viewer(v)


def test_new_memory_requires_viewer():
    with pytest.raises(ValidationError):
        NewMemory(text="x", known_by=[], kind="witnessed", source={"type": "seed"})
    with pytest.raises(ValidationError):
        NewMemory(text="x", known_by=["everyone"], kind="witnessed", source={"type": "seed"})


def test_memory_roundtrips_id_as_underscore_id():
    now = datetime.now(timezone.utc)
    m = Memory(
        _id="m_1", case_id="case_test", team_id="A", text="t", known_by=["suspect:cook"], kind="witnessed",
        source={"type": "seed", "ref": "s1"}, confidence=1.0, status="active", created_at=now, expires_at=FAR_FUTURE,
        score=0.9,
    )
    doc = m.to_doc()
    assert doc["_id"] == "m_1" and "id" not in doc
    assert "score" not in doc and "embedding" not in doc
    assert Memory.model_validate(doc).id == "m_1"


def test_scope_is_immutable():
    s = Scope(case_id="case_test", team_id="A")
    with pytest.raises(ValidationError):
        s.team_id = "B"


def test_policy_lifespans():
    now = datetime(2026, 11, 14, tzinfo=timezone.utc)
    assert DEFAULT_POLICY.expires_at("witnessed", now) == FAR_FUTURE
    assert DEFAULT_POLICY.expires_at("rumour", now) == now + timedelta(seconds=600)
    assert DEFAULT_POLICY.expires_at("rumour", now, rumour_ttl_override=30) == now + timedelta(seconds=30)
    assert FAR_FUTURE.tzinfo is not None


def test_deterministic_ids():
    assert ids.finding_id("case_manor", "A", "ev_note") == ids.finding_id("case_manor", "A", "ev_note")
    assert ids.finding_id("case_manor", "A", "ev_note") != ids.finding_id("case_manor", "B", "ev_note")
    assert ids.turn_id(123) == "t_123"
    assert ids.offer_id("A", "search_cellar", "m_9") == "a_A_search_cellar_m_9"
    assert ids.replacement_memory_id("m_1") == ids.replacement_memory_id("m_1")
    assert all(i.startswith("m_") for i in [ids.random_memory_id(), ids.seed_memory_id("case_x", "A", "s1")])
