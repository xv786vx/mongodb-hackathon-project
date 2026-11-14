"""Governance semantics, run against FakeStore. The same cases run against Atlas in test_isolation.py (Phase 2)."""

from datetime import timedelta

from mystery_core.memory import NewMemory, Scope, utcnow
from mystery_core.memory.ids import finding_id
from mystery_core.memory.store import retrieve_pipeline

COOK, BUTLER = "suspect:cook", "suspect:butler"


async def test_seed_world_is_idempotent(store, team_a):
    assert await store.seed_world("case_test", "A") == 0
    hits = await store.retrieve(team_a, COOK, "footsteps on the stairs", k=10)
    assert len(hits) == 2


async def test_suspect_cannot_read_another_suspects_memories(store, team_a):
    ids_ = {m.id for m in await store.retrieve(team_a, BUTLER, "footsteps cellar stairs midnight", k=10)}
    assert ids_ == {"m_test_A_s_butler_alibi"}


async def test_team_b_cannot_see_team_a_findings(store, team_a, team_b):
    await store.write(team_a, [NewMemory(
        id=finding_id("case_test", "A", "ev_note"), text="The gardener's note about the cellar",
        known_by=["team:A"], kind="finding", source={"type": "evidence", "ref": "ev_note"})])
    assert await store.retrieve(team_a, "team:A", "gardener note", k=5)
    assert await store.retrieve(team_b, "team:B", "gardener note", k=5) == []
    assert await store.retrieve(team_b, "team:A", "gardener note", k=5) == []  # wrong viewer for this world


async def test_correction_in_one_world_does_not_touch_another(store, team_a, team_b):
    await store.supersede(team_a, "m_test_A_s_butler_alibi", NewMemory(
        text="I stepped out of the pantry briefly.", known_by=[BUTLER], kind="cover_story",
        source={"type": "fabricated"}), reason="test")
    a = await store.retrieve(team_a, BUTLER, "where were you", k=5)
    b = await store.retrieve(team_b, BUTLER, "where were you", k=5)
    assert [m.text for m in a] == ["I stepped out of the pantry briefly."]
    assert [m.id for m in b] == ["m_test_B_s_butler_alibi"]


async def test_superseded_excluded_and_unlocks_not_inherited(store, team_a):
    [ev] = await store.write(team_a, [NewMemory(
        text="Shown the gardener's note", known_by=[BUTLER], kind="evidence",
        source={"type": "evidence", "ref": "ev_note"}, unlocks=["search_cellar"])])
    new = await store.supersede(team_a, ev.id, NewMemory(
        text="Gardener recanted", known_by=[BUTLER], kind="evidence", source={"type": "event", "ref": "x"},
        unlocks=["search_cellar"]), reason="test")
    assert new.unlocks == [] and new.supersedes == ev.id
    assert await store.fetch(team_a, BUTLER, [ev.id]) == []
    assert ev.id not in {m.id for m in await store.retrieve(team_a, BUTLER, "gardener note", k=10)}


async def test_supersede_is_retry_safe(store, team_a):
    new = NewMemory(text="new", known_by=[BUTLER], kind="cover_story", source={"type": "fabricated"})
    first = await store.supersede(team_a, "m_test_A_s_butler_alibi", new, reason="r")
    again = await store.supersede(team_a, "m_test_A_s_butler_alibi", new, reason="r")
    assert first.id == again.id


async def test_expired_excluded_before_delete(store, team_a):
    [r] = await store.write(team_a, [NewMemory(
        text="Heard the cook argue with the butler", known_by=[BUTLER], kind="rumour",
        source={"type": "told_by", "ref": "cook"})])
    assert await store.fetch(team_a, BUTLER, [r.id])
    later = utcnow() + timedelta(seconds=601)
    store.now = lambda: later
    assert await store.fetch(team_a, BUTLER, [r.id]) == []
    assert r.id in store.docs  # still stored; only the filter hides it


async def test_deterministic_write_twice_creates_one(store, team_a):
    m = NewMemory(id=finding_id("case_test", "A", "ev_note"), text="note", known_by=["team:A"],
                  kind="finding", source={"type": "evidence", "ref": "ev_note"})
    first = await store.write(team_a, [m])
    second = await store.write(team_a, [m])
    assert first[0].id == second[0].id
    assert sum(1 for d in store.docs.values() if d.kind == "finding") == 1


async def test_supersede_by_ref_hits_only_worlds_holding_it(store, team_a, team_b):
    await store.write(team_a, [NewMemory(
        text="Shown the gardener's note", known_by=[BUTLER], kind="evidence",
        source={"type": "evidence", "ref": "ev_note"}, unlocks=["search_cellar"])])
    out = await store.supersede_by_ref("case_test", "ev_note", "The gardener saw no one.", "gardener_recants", "event")
    assert {m.team_id for m in out} == {"A"}
    assert all(m.unlocks == [] and m.source.type == "event" for m in out)


def test_retrieve_pipeline_filters_in_both_stages():
    """Rule 2: $vectorSearch and $match both carry the full governance filter."""
    now = utcnow()
    p = retrieve_pipeline(Scope(case_id="c", team_id="A"), COOK, [0.0] * 4, now, k=3, mask=["m_x"])
    vs, match = p[0]["$vectorSearch"]["filter"], p[1]["$match"]
    for f in (vs, match):
        assert f["case_id"] == "c" and f["team_id"] == "A" and f["known_by"] == COOK
        assert f["status"] == "active" and f["expires_at"] == {"$gt": now}
    assert match["_id"] == {"$nin": ["m_x"]} and "_id" not in vs


async def test_get_case_hides_truth_and_embeddings(store):
    store.cases["case_test"]["seed_memories"][0]["embedding"] = [0.1] * 4
    case = await store.get_case("case_test")
    assert "truth" not in case
    assert all("embedding" not in s for s in case["seed_memories"])
    assert case["actions"][0]["label"] == "Search the cellar"
    case["title"] = "mutated"  # callers get a copy
    assert store.cases["case_test"]["title"] != "mutated"
    assert await store.get_case("case_nope") is None


async def test_get_truth_only_returns_truth(store):
    assert await store.get_truth("case_test") == {"killer": "butler", "weapon": "candlestick", "room": "library"}
    assert await store.get_truth("case_nope") is None
