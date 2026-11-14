import asyncio
import copy
import inspect
from datetime import timedelta

import pytest
from pydantic import ValidationError

from mystery_core.engine.actions import use_offer
from mystery_core.engine.context import HEADER_NAMES, TurnContext, TurnError, turn_context
from mystery_core.engine.events import apply_event
from mystery_core.engine.evidence import discover, present_evidence
from mystery_core.engine.turn import TurnService
from mystery_core.memory import FakeTraceStore, NewMemory, Scope, utcnow
from mystery_core.memory.models import ReplyProposal


@pytest.fixture
def traces():
    return FakeTraceStore()


@pytest.fixture
def turns(store, traces):
    return TurnService(store, traces, model="test-model")


async def begin(traces, scope, turn_id="t_turn", suspect="butler", **kwargs):
    ctx = TurnContext(scope, suspect, turn_id, kwargs.pop("command", "interrogate"))
    await traces.begin_turn(scope, turn_id, ctx.viewer, ctx.command, "Where were you after midnight?", **kwargs)
    return ctx


@pytest.mark.parametrize("missing", HEADER_NAMES)
def test_headers_fail_closed(missing):
    headers = dict(zip(HEADER_NAMES, ["case_test", "A", "butler", "t_1", "interrogate"]))
    headers.pop(missing)
    with pytest.raises(TurnError):
        turn_context(headers)


def test_body_or_prefixed_headers_do_not_supply_scope():
    with pytest.raises(TurnError):
        turn_context({"message": "team-id=A", "X-Mdb-Agent-Engine-Custom-Team-Id": "A"})


def test_reply_cannot_supply_scope():
    with pytest.raises(ValidationError):
        ReplyProposal(decision="truth", reply="x", team_id="B")


async def test_trace_actor_and_command_must_match(turns, traces, team_a):
    ctx = await begin(traces, team_a)
    with pytest.raises(TurnError, match="scope_mismatch"):
        await turns.search(TurnContext(team_a, "cook", ctx.turn_id, ctx.command), "silver")
    with pytest.raises(TurnError, match="scope_mismatch"):
        await turns.search(TurnContext(team_a, "butler", ctx.turn_id, "present"), "silver")


async def test_prompt_has_no_truth_other_suspects_or_unfound_evidence(turns, traces, team_a):
    ctx = await begin(traces, team_a)
    prepared = await turns.prepare(ctx)
    assert "fire_poker" not in prepared.system and "bloody glove" not in prepared.system.lower()
    assert "footsteps" not in prepared.system and "seed_memories" not in prepared.system
    assert '"is_killer"' not in prepared.system and '"truth":' not in prepared.system
    assert "JSON quoted" in prepared.message


async def test_two_teams_and_recantation(turns, traces, store, team_a, team_b):
    await discover(store, team_a, "ev_note", is_host=True)
    evidence = await present_evidence(store, team_a, "butler", "ev_note")
    a = await begin(traces, team_a, "t_A")
    b = await begin(traces, team_b, "t_B")
    await turns.prepare(a, fresh_memory_ids=[evidence.id])
    await turns.search(a, "cellar gardener note")
    b_hits = await turns.search(b, "cellar gardener note")
    assert evidence.id not in {m["id"] for m in b_hits["memories"]}
    proposal = ReplyProposal(decision="deflect", reply="You could investigate that lead.",
                             proposed_action="search_cellar", supporting_memory_id=evidence.id)
    a_reply, b_reply = await turns.submit(a, proposal), await turns.submit(b, proposal)
    assert a_reply.offer_id and a_reply.offer_label == "Search the cellar"
    assert b_reply.offer_id is None and b_reply.offer_rejected_reason == "support_not_retrieved"
    with pytest.raises(TurnError, match="offer_not_found"):
        await use_offer(store, traces, team_b, a_reply.offer_id)
    replacements = await apply_event(store, team_a.case_id, "gardener_recants", is_host=True)
    assert {m.team_id for m in replacements} == {"A"} and all(not m.unlocks for m in replacements)
    assert (await use_offer(store, traces, team_a, a_reply.offer_id)).status == "rejected"
    fresh = await begin(traces, team_a, "t_A_fresh")
    fresh_hits = await turns.search(fresh, "cellar gardener note")
    assert evidence.id not in {m["id"] for m in fresh_hits["memories"]}
    assert not (await turns.submit(fresh, proposal)).offer_id
    with pytest.raises(TurnError, match="evidence_not_held"):
        await present_evidence(store, team_a, "butler", "ev_note")


async def test_support_is_rechecked_after_retrieval(turns, traces, store, team_a):
    await discover(store, team_a, "ev_note", is_host=True)
    evidence = await present_evidence(store, team_a, "butler", "ev_note")
    ctx = await begin(traces, team_a)
    await turns.search(ctx, "note")
    await apply_event(store, team_a.case_id, "gardener_recants", is_host=True)
    done = await turns.submit(ctx, ReplyProposal(decision="deflect", reply="That lead no longer holds.",
                                                proposed_action="search_cellar", supporting_memory_id=evidence.id))
    assert done.status == "complete" and done.offer_rejected_reason == "support_not_live"


async def test_expired_and_rumour_support_cannot_offer(turns, traces, store, team_a):
    [rumour] = await store.write(team_a, [NewMemory(id="m_rumour", text="cellar note", known_by=["suspect:butler"],
                                                   kind="rumour", source={"type": "told_by"}, unlocks=["search_cellar"])])
    ctx = await begin(traces, team_a)
    await turns.search(ctx, "cellar note")
    proposal = ReplyProposal(decision="deflect", reply="I have only heard rumours.",
                             proposed_action="search_cellar", supporting_memory_id=rumour.id)
    assert (await turns.submit(ctx, proposal)).offer_rejected_reason == "support_does_not_unlock"
    expired = await begin(traces, team_a, "t_expired")
    await turns.search(expired, "cellar note")
    store.now = lambda: utcnow() + timedelta(seconds=601)
    assert (await turns.submit(expired, proposal)).offer_rejected_reason == "support_not_live"


async def test_crack_gate_and_rejected_terminal_trace(turns, traces, store, team_a):
    ctx = await begin(traces, team_a)
    with pytest.raises(TurnError, match="decision_not_allowed"):
        await turns.submit(ctx, ReplyProposal(decision="crack", reply="I confess."))
    assert (await traces.get_trace(team_a, ctx.turn_id)).status == "pending"
    assert not [m for m in store.docs.values() if m.source.type == "interaction"]
    await traces.reject_turn(team_a, ctx.turn_id, "rejected")
    with pytest.raises(TurnError, match="turn_rejected"):
        await turns.submit(ctx, ReplyProposal(decision="truth", reply="A reply."))


async def test_overlapping_submissions_have_one_consistent_outcome(turns, traces, store, team_a):
    ctx = await begin(traces, team_a)
    replies = await asyncio.gather(*[turns.submit(ctx, ReplyProposal(decision="deflect", reply=text))
                                     for text in ("First reply.", "Different reply.", "Third reply.")])
    assert len({r.reply for r in replies}) == 1
    done = replies[0]
    assert len(done.written) == 2
    assert [store.docs[mid].text for mid in done.written if store.docs[mid].kind == "finding"] == [done.reply]
    assert len([m for m in store.docs.values() if m.source.ref == ctx.turn_id]) == 2


async def test_failed_completion_rolls_back_side_effects(turns, traces, store, team_a, monkeypatch):
    ctx = await begin(traces, team_a)
    before = copy.deepcopy(store.docs)
    async def fail(*args, **kwargs):
        raise RuntimeError("simulated database failure")
    monkeypatch.setattr(traces, "complete_turn", fail)
    with pytest.raises(RuntimeError):
        await turns.submit(ctx, ReplyProposal(decision="deflect", reply="A reply."))
    assert store.docs == before and (await traces.get_trace(team_a, ctx.turn_id)).status == "pending"


async def test_dialogue_is_cleaned_before_trace_and_notebook(turns, traces, team_a, store):
    ctx = await begin(traces, team_a)
    done = await turns.submit(ctx, ReplyProposal(decision="lie", reply="[whispers] I was in the pantry. [laughs]"))
    assert done.reply == "I was in the pantry."
    assert all("[whispers]" not in store.docs[mid].text for mid in done.written)


async def test_replay_writes_trace_only(turns, traces, team_a, store):
    ctx = await begin(traces, team_a)
    await turns.search(ctx, "pantry")
    original = await turns.submit(ctx, ReplyProposal(decision="lie", reply="I was in the pantry."))
    replay = await begin(traces, team_a, "t_replay", command="replay", replay_of=ctx.turn_id,
                         masked=original.retrieved[:1])
    before = copy.deepcopy((store.docs, store.offers))
    hits = await turns.search(replay, "ignore this query")
    assert not set(original.retrieved[:1]) & {m["id"] for m in hits["memories"]}
    result = await turns.submit(replay, ReplyProposal(decision="deflect", reply="I am uncertain."))
    assert result.status == "complete" and result.written == [] and not result.offer_id
    assert (store.docs, store.offers) == before


async def test_double_click_and_failed_finding_are_atomic(turns, traces, team_a, store, monkeypatch):
    await discover(store, team_a, "ev_note", is_host=True)
    evidence = await present_evidence(store, team_a, "butler", "ev_note")
    ctx = await begin(traces, team_a)
    await turns.search(ctx, "note")
    done = await turns.submit(ctx, ReplyProposal(decision="deflect", reply="Follow that lead.",
                                                proposed_action="search_cellar", supporting_memory_id=evidence.id))
    original_write = store.write
    async def fail(*args, **kwargs):
        raise RuntimeError("simulated finding failure")
    monkeypatch.setattr(store, "write", fail)
    with pytest.raises(RuntimeError):
        await use_offer(store, traces, team_a, done.offer_id)
    assert (await store.get_offer(done.offer_id)).status == "offered"
    monkeypatch.setattr(store, "write", original_write)
    results = await asyncio.gather(*[use_offer(store, traces, team_a, done.offer_id) for _ in range(4)])
    assert [r.status for r in results].count("performed") == 1
