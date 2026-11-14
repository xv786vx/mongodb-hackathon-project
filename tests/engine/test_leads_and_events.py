"""Leads, offers, world events: the core demo as tests (AGENTS.md "Demo path")."""
from conftest import all_in
from mystery_core.engine.actions import (discover, notebook, search_room,
                                         team_findings, use_offer, validate_offer)
from mystery_core.engine.events import trigger_event
from mystery_core.engine.turn import prepare_present
from mystery_core.memory import ids


async def butler_lead(store, scope, query="Where were you after midnight? the note cellar"):
    """Simulate search_memories for the butler and return (retrieved ids, lead memory)."""
    hits = await store.retrieve(scope, "suspect:butler", query, k=5)
    lead = next((m for m in hits if "search_cellar" in m.unlocks), None)
    return [m.id for m in hits], lead


async def stage(store, case, A):
    """Demo setup: Team A found the note and showed it to the butler."""
    await discover(store, case, A, "ev_note")
    assert (await prepare_present(store, case, A, "butler", "ev_note")).ok


async def test_core_demo(store, case, A, B):
    await stage(store, case, A)

    # 1. Same question, different outcome: only Team A's world supports the lead.
    retrieved_a, lead_a = await butler_lead(store, A)
    retrieved_b, lead_b = await butler_lead(store, B)
    assert lead_a is not None and lead_b is None
    offer_a = (await validate_offer(store, case, A, "butler", "t_1", retrieved_a,
                             "search_cellar", lead_a.id)).offer
    assert offer_a is not None and offer_a.team_id == "A"
    # Team B's model proposing the action (even naming A's memory) gets nothing.
    refused = await validate_offer(store, case, B, "butler", "t_2", retrieved_b + [lead_a.id],
                             "search_cellar", lead_a.id)
    assert refused.offer is None and "not live" in refused.reason

    # 2. The witness recants: the memory is superseded in every world that held it.
    result = await trigger_event(store, case, "gardener_recants")
    assert result.notify_teams == ["A"]  # B never held the note, so B is not told
    assert store.docs[lead_a.id].status == "superseded"
    assert all(m.unlocks == [] for m in result.replaced)
    # The gardener's own memory changed in both worlds.
    assert {m.team_id for m in result.replaced if "suspect:gardener" in m.known_by} == {"A", "B"}

    # 3. The old button is refused; a fresh question no longer offers it.
    click = await use_offer(store, case, offer_a.id, "A")
    assert click.status == "rejected"
    assert store.offers[offer_a.id].status == "rejected"
    assert any(e.type == "offer_rejected" for e in store.audit)
    retrieved_a2, lead_a2 = await butler_lead(store, A)
    assert lead_a2 is None
    # Even if the model names the dead memory, the offer is dropped.
    dead = await validate_offer(store, case, A, "butler", "t_3", retrieved_a2 + [lead_a.id],
                          "search_cellar", lead_a.id)
    assert dead.offer is None

    # The evidence cannot be presented again to bring the lead back...
    again = await prepare_present(store, case, A, "butler", "ev_note")
    assert not again.ok
    # ...nor re-found by searching the greenhouse.
    search = await search_room(store, case, A, "greenhouse")
    assert search.found[0].withdrawn and search.found[0].memory is None

    # Running the event again changes nothing.
    assert (await trigger_event(store, case, "gardener_recants")).replaced == []


async def test_click_takes_action_once_and_only_for_owner(store, case, A):
    await stage(store, case, A)
    retrieved, lead = await butler_lead(store, A)
    offer = (await validate_offer(store, case, A, "butler", "t_1", retrieved, "search_cellar", lead.id)).offer

    assert (await use_offer(store, case, offer.id, "B")).status == "forbidden"
    first = await use_offer(store, case, offer.id, "A")
    assert first.status == "used"
    assert [d.evidence.id for d in first.found] == ["ev_glove"] and first.found[0].new
    assert (await use_offer(store, case, offer.id, "A")).status == "already_used"
    assert len([m for m in all_in(store, A) if m.source.ref == "ev_glove"]) == 1

    # Already used: a new proposal is not offered again.
    again = await validate_offer(store, case, A, "butler", "t_2", retrieved, "search_cellar", lead.id)
    assert again.offer is None and "already used" in again.reason


async def test_offer_checks(store, case, A):
    await stage(store, case, A)
    retrieved, lead = await butler_lead(store, A)
    other = next(i for i in retrieved if i != lead.id)

    assert (await validate_offer(store, case, A, "butler", "t", retrieved, None, None)).offer is None
    assert "unknown action" in (await validate_offer(store, case, A, "butler", "t", retrieved,
                                              "dig_up_garden", lead.id)).reason
    assert "not retrieved" in (await validate_offer(store, case, A, "butler", "t", [other],
                                             "search_cellar", lead.id)).reason
    assert "does not unlock" in (await validate_offer(store, case, A, "butler", "t", retrieved,
                                               "search_cellar", other)).reason
    # Another suspect cannot "propose" the butler's memory.
    assert "not live" in (await validate_offer(store, case, A, "cook", "t", retrieved,
                                        "search_cellar", lead.id)).reason
    # Retries create one offer.
    o1 = (await validate_offer(store, case, A, "butler", "t", retrieved, "search_cellar", lead.id)).offer
    o2 = (await validate_offer(store, case, A, "butler", "t", retrieved, "search_cellar", lead.id)).offer
    assert o1.id == o2.id and len(store.offers) == 1


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
