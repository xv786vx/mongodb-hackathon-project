"""Leads, offers, world events: the core demo as tests (AGENTS.md "Demo path").

Offers are created by Person 2's TurnService.submit and clicked through Person 2's
use_offer; the game rules (present, events, search) are Person 3's."""
import pytest

from conftest import all_in, begin
from mystery_core.engine.actions import discover, notebook, search_room, team_findings, use_offer
from mystery_core.engine.context import TurnError
from mystery_core.engine.events import trigger_event
from mystery_core.engine.turn import prepare_present
from mystery_core.memory import ids
from mystery_core.memory.models import ReplyProposal

QUESTION = "Where were you after midnight? the note cellar"


async def butler_turn(turns, traces, scope, turn_id, fresh=(), supporting=None):
    """One butler turn as the agent runs it: prepare, search, submit proposing the cellar."""
    ctx = await begin(traces, scope, "butler", turn_id, question=QUESTION)
    await turns.prepare(ctx, fresh_memory_ids=[m.id for m in fresh])
    found = await turns.search(ctx, QUESTION)
    lead = next((m["id"] for m in found["memories"] if "search_cellar" in m["unlocks"]), None)
    proposal = ReplyProposal(decision="deflect", reply="Perhaps the cellar.",
                             proposed_action="search_cellar", supporting_memory_id=supporting or lead or "m_none")
    return await turns.submit(ctx, proposal), lead


async def stage(store, case, A):
    """Demo setup: Team A found the note and showed it to the butler."""
    await discover(store, case, A, "ev_note")
    prep = await prepare_present(store, case, A, "butler", "ev_note")
    assert prep.ok
    return prep.fresh


async def test_core_demo(store, case, turns, traces, A, B):
    fresh = await stage(store, case, A)

    # 1. Same question, different outcome: only Team A's world supports the lead.
    trace_a, lead_a = await butler_turn(turns, traces, A, "t_1", fresh)
    assert lead_a is not None and trace_a.offer_id is not None
    # Team B's model proposing the action, even naming A's memory, gets nothing.
    trace_b, lead_b = await butler_turn(turns, traces, B, "t_2", supporting=lead_a)
    assert lead_b is None and trace_b.offer_id is None and trace_b.offer_rejected_reason

    # 2. The witness recants: the memory is superseded in every world that held it.
    result = await trigger_event(store, case, "gardener_recants")
    assert result.notify_teams == ["A"]  # B never held the note, so B is not told
    assert store.docs[lead_a].status == "superseded"
    assert all(m.unlocks == [] for m in result.replaced)
    assert {m.team_id for m in result.replaced if "suspect:gardener" in m.known_by} == {"A", "B"}

    # 3. The old button is refused; a fresh question no longer offers it.
    click = await use_offer(store, traces, A, trace_a.offer_id)
    assert click.status == "rejected" and click.message == "That lead no longer holds."
    assert store.offers[trace_a.offer_id].status == "rejected"
    assert any(e.type == "offer_rejected" for e in store.audit)
    trace_a2, lead_a2 = await butler_turn(turns, traces, A, "t_3", supporting=lead_a)
    assert lead_a2 is None and trace_a2.offer_id is None

    # The evidence cannot be presented again to bring the lead back...
    assert not (await prepare_present(store, case, A, "butler", "ev_note")).ok
    # ...nor re-found by searching the greenhouse.
    search = await search_room(store, case, A, "greenhouse")
    assert search.found[0].withdrawn and search.found[0].memory is None

    # Running the event again changes nothing.
    assert (await trigger_event(store, case, "gardener_recants")).replaced == []


async def test_click_takes_action_once_and_only_for_owner(store, case, turns, traces, A, B):
    fresh = await stage(store, case, A)
    trace, _ = await butler_turn(turns, traces, A, "t_1", fresh)

    with pytest.raises(TurnError, match="offer_not_found"):
        await use_offer(store, traces, B, trace.offer_id)
    first = await use_offer(store, traces, A, trace.offer_id)
    assert first.status == "performed" and first.revealed == ("ev_glove",)
    assert first.message == "You search the cellar."
    assert (await use_offer(store, traces, A, trace.offer_id)).status == "used"
    assert len([m for m in all_in(store, A) if m.source.ref == "ev_glove"]) == 1
    # The click writes the same finding text as /search and /discover.
    assert store.docs[first.finding_id].text.startswith(case.evidence_item("ev_glove").name + ": ")

    # Already used: a new proposal is not offered again.
    again, _ = await butler_turn(turns, traces, A, "t_2")
    assert again.offer_id is None and again.offer_rejected_reason == "action_already_used"


async def test_present_is_retry_safe(store, case, A):
    await stage(store, case, A)
    await prepare_present(store, case, A, "butler", "ev_note")
    ev_id = ids.evidence_memory_id(A.case_id, A.team_id, "butler", "ev_note")
    assert sum(1 for m in all_in(store, A) if m.id == ev_id) == 1
    assert store.docs[ev_id].unlocks == ["search_cellar"]


async def test_search(store, case, A, B):
    assert not (await search_room(store, case, A, "wine_cellar")).ok
    assert not (await search_room(store, case, A, "attic")).ok
    first = await search_room(store, case, A, "office")
    assert first.ok and {d.evidence.id for d in first.found} == {"ev_poker", "ev_letter"}
    assert all(d.new for d in first.found)
    assert not any(d.new for d in (await search_room(store, case, A, "office")).found)
    assert store.docs[ids.finding_id(A.case_id, A.team_id, "ev_poker")].known_by == ["team:A"]
    # Findings live in the team's notebook only.
    assert await notebook(store, A, "fire poker office")
    assert await notebook(store, B, "fire poker office") == []
    assert all(m.kind != "finding" for m in await store.retrieve(A, "suspect:butler", "fire poker"))


async def test_team_findings_lists_only_what_the_team_holds(store, case, A, B):
    assert await team_findings(store, case, A) == []
    await discover(store, case, A, "ev_note")
    await discover(store, case, A, "ev_ticket")
    assert [e.id for e in await team_findings(store, case, A)] == ["ev_note", "ev_ticket"]
    assert await team_findings(store, case, B) == []
    # A world event withdraws the note, so it can no longer be presented.
    await trigger_event(store, case, "gardener_recants")
    assert [e.id for e in await team_findings(store, case, A)] == ["ev_ticket"]
