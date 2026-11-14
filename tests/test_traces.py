"""Trace lifecycle. Runs against FakeTraceStore always, and against Atlas (mystery_test) with `pytest -m atlas`."""

import os
from datetime import timedelta

import pytest
from dotenv import load_dotenv

from mystery_core.memory import FakeTraceStore, NewMemory, Scope, TraceConflict, TraceStore, utcnow
from mystery_core.memory.store import expiry_notice_from_change

A = Scope(case_id="case_test", team_id="A")
B = Scope(case_id="case_test", team_id="B")


@pytest.fixture(params=["fake", pytest.param("atlas", marks=pytest.mark.atlas)])
async def traces(request):
    if request.param == "fake":
        yield FakeTraceStore()
        return
    from pymongo import AsyncMongoClient

    load_dotenv()
    client = AsyncMongoClient(os.environ["MONGODB_URI"], tz_aware=True)
    db = client[os.environ.get("TEST_DB_NAME", "mystery_test")]
    await db.traces.delete_many({"_id": {"$regex": "^t_test_"}})
    yield TraceStore(db)
    await db.traces.delete_many({"_id": {"$regex": "^t_test_"}})
    await client.close()


async def test_begin_turn_is_find_or_create(traces):
    t1 = await traces.begin_turn(A, "t_test_1", "suspect:butler", "interrogate", "Where were you?")
    t2 = await traces.begin_turn(A, "t_test_1", "suspect:butler", "interrogate", "DIFFERENT TEXT on retry")
    assert t1.status == t2.status == "pending"
    assert t2.input == "Where were you?"  # retry never overwrites
    assert t2.suspect_id == "butler"


async def test_get_trace_is_scoped(traces):
    await traces.begin_turn(A, "t_test_2", "suspect:butler", "interrogate", "q")
    assert (await traces.get_trace(A, "t_test_2")) is not None
    assert (await traces.get_trace(B, "t_test_2")) is None  # another team's trace looks absent
    assert (await traces.get_trace(A, "t_test_missing")) is None


async def test_other_world_cannot_claim_turn_id(traces):
    await traces.begin_turn(A, "t_test_3", "suspect:butler", "interrogate", "q")
    with pytest.raises(TraceConflict):
        await traces.begin_turn(B, "t_test_3", "suspect:butler", "interrogate", "q")


async def test_complete_turn_is_idempotent(traces):
    await traces.begin_turn(A, "t_test_4", "suspect:butler", "interrogate", "q")
    await traces.add_retrieved(A, "t_test_4", ["m_1", "m_2"])
    await traces.add_retrieved(A, "t_test_4", ["m_2", "m_3"])
    done = await traces.complete_turn(A, "t_test_4", decision="deflect", reply="I was in the pantry.",
                                      written=["m_9"], offer_id="a_A_search_cellar_m_1",
                                      offer_label="Search the cellar")
    assert done.status == "complete" and done.retrieved == ["m_1", "m_2", "m_3"]
    assert done.offer_label == "Search the cellar"
    again = await traces.complete_turn(A, "t_test_4", decision="truth", reply="changed")
    assert again.reply == "I was in the pantry." and again.decision == "deflect"
    await traces.add_retrieved(A, "t_test_4", ["m_late"])  # ignored once complete
    assert "m_late" not in (await traces.get_trace(A, "t_test_4")).retrieved


async def test_rejected_turn_cannot_be_completed(traces):
    await traces.begin_turn(A, "t_test_5", "suspect:butler", "interrogate", "q")
    r = await traces.reject_turn(A, "t_test_5", "crack below threshold")
    assert r.status == "rejected" and r.rejected_reason == "crack below threshold"
    with pytest.raises(TraceConflict):
        await traces.complete_turn(A, "t_test_5", decision="crack", reply="I confess")


async def test_complete_requires_begin(traces):
    with pytest.raises(TraceConflict):
        await traces.complete_turn(A, "t_test_6", decision="truth", reply="x")


async def test_set_voice(traces):
    await traces.begin_turn(A, "t_test_7", "suspect:butler", "interrogate", "q")
    await traces.set_voice(A, "t_test_7", {"voice_id": "v", "stability": 0.8})
    assert (await traces.get_trace(A, "t_test_7")).voice == {"voice_id": "v", "stability": 0.8}


def test_turn_ids_must_be_prefixed():
    with pytest.raises(ValueError):
        FakeTraceStore()._get(A, "1234")


# --- count_superseded and expiry notices ----------------------------------------


async def test_count_superseded(store, team_a, team_b):
    lie = NewMemory(text="A new lie", known_by=["suspect:butler"], kind="cover_story", source={"type": "fabricated"})
    assert await store.count_superseded(team_a, "suspect:butler") == 0
    new = await store.supersede(team_a, "m_test_A_s_butler_alibi", lie, reason="contradiction")
    await store.supersede(team_a, new.id, lie, reason="contradiction")
    assert await store.count_superseded(team_a, "suspect:butler") == 2
    assert await store.count_superseded(team_b, "suspect:butler") == 0
    assert await store.count_superseded(team_a, "suspect:cook") == 0


def _delete_event(expires_at):
    return {
        "operationType": "delete",
        "documentKey": {"_id": "m_r1"},
        "fullDocumentBeforeChange": {
            "_id": "m_r1", "case_id": "case_test", "team_id": "A", "kind": "rumour",
            "text": "SECRET rumour text", "expires_at": expires_at,
        },
    }


def test_expiry_notice_has_ids_only():
    now = utcnow()
    n = expiry_notice_from_change(_delete_event(now - timedelta(seconds=5)), now)
    assert n is not None and (n.team_id, n.memory_id, n.kind) == ("A", "m_r1", "rumour")
    assert "SECRET" not in n.model_dump_json()


def test_forget_delete_is_not_an_expiry():
    now = utcnow()
    assert expiry_notice_from_change(_delete_event(now + timedelta(days=1)), now) is None
    assert expiry_notice_from_change({"operationType": "delete"}, now) is None  # no pre-image
    assert expiry_notice_from_change({"operationType": "insert"}, now) is None
