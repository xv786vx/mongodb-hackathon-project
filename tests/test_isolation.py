"""The isolation and governance claims, against real Atlas ($vectorSearch, transactions, TTL filter).

    pytest -m atlas tests/test_isolation.py

Uses DB mystery_test (override with TEST_DB_NAME) and FakeEmbedder, so no embedding API calls.
Each test gets its own case_id and cleans up after itself.
"""

import asyncio
import os
import uuid
from datetime import timedelta

import pytest
from dotenv import load_dotenv

from mystery_core.memory import FakeEmbedder, MemoryStore, NewMemory, Offer, Scope, utcnow
from mystery_core.memory.ids import evidence_memory_id, finding_id

pytestmark = pytest.mark.atlas

BUTLER, COOK = "suspect:butler", "suspect:cook"
ALIBI = "I was polishing the silver in the pantry from eleven until two. I never left the pantry."


async def eventually(fn, check, timeout=60.0):
    """Search index lag: poll until check(result) holds."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        result = await fn()
        if check(result):
            return result
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"condition not met within {timeout}s; last result: {result}")
        await asyncio.sleep(1)


@pytest.fixture
async def world():
    from pymongo import AsyncMongoClient

    load_dotenv()
    client = AsyncMongoClient(os.environ["MONGODB_URI"], tz_aware=True)
    db = client[os.environ.get("TEST_DB_NAME", "mystery_test")]
    emb = FakeEmbedder()
    store = MemoryStore(db, emb)
    await store.load_policy()
    case_id = f"case_iso{uuid.uuid4().hex[:8]}"
    seeds = [
        {"id": "s_alibi", "kind": "cover_story", "known_by": [BUTLER], "text": ALIBI},
        {"id": "s_steps", "kind": "witnessed", "known_by": [COOK],
         "text": "Just after midnight I heard heavy footsteps going down the cellar stairs."},
    ]
    for s, v in zip(seeds, await emb.embed_documents([s["text"] for s in seeds])):
        s["embedding"] = v
    await db.cases.insert_one({"_id": case_id, "truth": {"killer": "butler"}, "seed_memories": seeds})
    a, b = Scope(case_id=case_id, team_id="A"), Scope(case_id=case_id, team_id="B")
    assert await store.seed_world(case_id, "A") == 2
    assert await store.seed_world(case_id, "B") == 2
    for scope in (a, b):
        await eventually(lambda: store.retrieve(scope, BUTLER, "pantry silver"), lambda r: len(r) == 1)
    yield store, db, a, b
    for scope in (a, b):
        await store.forget(scope)
    await db.cases.delete_one({"_id": case_id})
    await db.offers.delete_many({"case_id": case_id})
    await db.audit.delete_many({"case_id": case_id})
    await client.close()


async def test_team_b_cannot_retrieve_team_a_findings(world):
    store, _, a, b = world
    [f] = await store.write(a, [NewMemory(id=finding_id(a.case_id, "A", "ev_note"), known_by=["team:A"], kind="finding",
                                          text="The gardener's note says the butler went into the cellar.",
                                          source={"type": "evidence", "ref": "ev_note"})])
    await eventually(lambda: store.retrieve(a, "team:A", "gardener note cellar"), lambda r: [m.id for m in r] == [f.id])
    assert await store.retrieve(b, "team:B", "gardener note cellar") == []
    assert await store.retrieve(b, "team:A", "gardener note cellar") == []  # A's viewer, B's world
    assert await store.fetch(b, "team:B", [f.id]) == []


async def test_suspect_cannot_retrieve_another_suspects_memories(world):
    store, _, a, _ = world
    hits = await store.retrieve(a, BUTLER, "footsteps going down the cellar stairs", k=10)
    assert {m.id for m in hits} == {f"m_{a.case_id.removeprefix('case_')}_A_s_alibi"}


async def test_correction_in_team_a_leaves_team_b_belief(world):
    store, _, a, b = world
    old_a = (await store.retrieve(a, BUTLER, "pantry silver"))[0]
    new = await store.supersede(a, old_a.id, NewMemory(
        text="I did step out of the pantry, but only to fetch more polish.", known_by=[BUTLER],
        kind="cover_story", source={"type": "fabricated"}), reason="contradicted by ev_note")
    # superseded memory excluded immediately (the $match re-check), before the index catches up
    assert old_a.id not in {m.id for m in await store.retrieve(a, BUTLER, "pantry silver", k=10)}
    assert await store.fetch(a, BUTLER, [old_a.id]) == []
    await eventually(lambda: store.retrieve(a, BUTLER, "pantry polish"), lambda r: new.id in {m.id for m in r})
    # Team B's butler still holds the original story
    assert [m.text for m in await store.retrieve(b, BUTLER, "pantry silver")] == [ALIBI]
    assert await store.count_superseded(a, BUTLER) == 1 and await store.count_superseded(b, BUTLER) == 0


async def test_supersede_is_retry_safe(world):
    store, _, a, _ = world
    old = (await store.retrieve(a, BUTLER, "pantry"))[0]
    lie = NewMemory(text="A new lie.", known_by=[BUTLER], kind="cover_story", source={"type": "fabricated"})
    r1 = await store.supersede(a, old.id, lie, reason="r")
    r2 = await store.supersede(a, old.id, lie, reason="r")
    assert r1.id == r2.id
    with pytest.raises(KeyError):
        await store.supersede(a, "m_does_not_exist", lie, reason="r")


async def test_world_event_hits_only_holders_and_kills_the_lead(world):
    store, _, a, b = world
    ev_id = evidence_memory_id(a.case_id, "A", "butler", "ev_note")
    [ev] = await store.write(a, [NewMemory(id=ev_id, known_by=[BUTLER], kind="evidence", unlocks=["search_cellar"],
                                           text="The detectives showed me the gardener's note.",
                                           source={"type": "evidence", "ref": "ev_note"})])
    await store.write(a, [NewMemory(id=finding_id(a.case_id, "A", "ev_note"), known_by=["team:A"], kind="finding",
                                    text="The gardener's note.", source={"type": "evidence", "ref": "ev_note"})])
    out = await store.supersede_by_ref(a.case_id, "ev_note", "The gardener now says he saw no one that night.",
                                       "gardener_recants", "host event")
    assert {m.team_id for m in out} == {"A"} and len(out) == 2  # suspect's evidence + team's finding
    assert all(m.unlocks == [] and m.source.type == "event" and m.source.ref == "gardener_recants" for m in out)
    assert await store.fetch(a, BUTLER, [ev.id]) == []  # the old lead no longer holds
    assert await store.find_by_ref(a, "team:A", "ev_note") == []  # cannot present it again
    again = await store.supersede_by_ref(a.case_id, "ev_note", "x", "gardener_recants", "repeat")
    assert sorted(m.id for m in again) == sorted(m.id for m in out)  # a retried /event still names the teams
    assert all(m.text != "x" for m in again)  # and writes nothing new


async def test_expired_excluded_before_ttl_delete(world):
    store, db, a, _ = world
    [r] = await store.write(a, [NewMemory(text="Heard the cook argue with someone.", known_by=[BUTLER], kind="rumour",
                                          source={"type": "told_by", "ref": "cook"},
                                          expires_at=utcnow() + timedelta(seconds=2))], dedup=False)
    assert await store.fetch(a, BUTLER, [r.id])
    await asyncio.sleep(3)
    assert await store.fetch(a, BUTLER, [r.id]) == []
    assert await db.memories.find_one({"_id": r.id}, {"_id": 1}) is not None  # still stored; filter hides it


async def test_offer_double_click_performs_once(world):
    store, _, a, b = world
    o = Offer(_id=f"a_A_search_cellar_m_x_{a.case_id}", case_id=a.case_id, team_id="A", action="search_cellar",
              supported_by="m_x", held_by=BUTLER, offered_in="t_1", ts=utcnow())
    assert (await store.insert_offer(o)).id == (await store.insert_offer(o)).id
    results = await asyncio.gather(*[store.transition_offer(o.id, "offered", "used") for _ in range(5)])
    assert results.count(True) == 1
    assert await store.action_used(a, "search_cellar") and not await store.action_used(b, "search_cellar")


async def test_policy_loaded_from_database(world):
    store, *_ = world
    assert store.policy.id == "mystery_v1" and store.policy.kinds["rumour"].ttl_seconds == 600
