"""Leads, offers, actions, room searches and discoveries (ARCHITECTURE.md section 7).

The model proposes; this module decides. An offer exists only if every check
passes, and the supporting memory is fetched again, scoped, on every click
(AGENTS.md rule 15). All writes use deterministic IDs from
`mystery_core.memory.ids` (rule 16).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from mystery_core.memory import ids
from mystery_core.memory.models import AuditEvent, Memory, NewMemory, Offer, Scope, Source, team_viewer
from mystery_core.memory.policy import utcnow

from .case import Case, Evidence
from .ports import GameStore

# ---------------------------------------------------------------- discoveries


@dataclass
class Discovery:
    evidence: Evidence
    memory: Memory | None
    new: bool  # False: the team already had it
    withdrawn: bool = False  # True: found before, since retracted by a world event


async def discover(store: GameStore, case: Case, scope: Scope, evidence_id: str) -> Discovery:
    """Record that a team found an evidence item. Safe to repeat.

    Used by /search, by taking an action, and by the host's /discover.
    """
    evidence = case.evidence_item(evidence_id)
    viewer = team_viewer(scope.team_id)
    existing = await store.find_by_ref(scope, viewer, evidence.id, kinds=["finding"])
    if existing:
        return Discovery(evidence, existing[0], new=False)
    [memory] = await store.write(scope, [NewMemory(
        id=ids.finding_id(scope.case_id, scope.team_id, evidence.id),
        text=f"{evidence.name}: {evidence.text}",
        known_by=[viewer],
        kind="finding",
        source=Source(type="evidence", ref=evidence.id),
    )])
    if memory.status != "active":
        # The deterministic ID already exists and was superseded by a world
        # event: searching the room again must not bring the lead back.
        return Discovery(evidence, None, new=False, withdrawn=True)
    return Discovery(evidence, memory, new=True)


@dataclass
class SearchResult:
    ok: bool
    room: str
    found: list[Discovery] = field(default_factory=list)
    error: str | None = None


async def search_room(store: GameStore, case: Case, scope: Scope, room_id: str) -> SearchResult:
    """/search <room>: open rooms only. Closed rooms are reached through actions."""
    try:
        room = case.room(room_id)
    except KeyError:
        return SearchResult(False, room_id, error=f"There is no room called {room_id!r}.")
    if not room.open:
        return SearchResult(False, room.id, error=f"{room.name.capitalize()} is locked.")
    found = [await discover(store, case, scope, e.id) for e in case.evidence if e.room == room.id]
    return SearchResult(True, room.id, found=found)


async def notebook(store: GameStore, scope: Scope, query: str, k: int = 10) -> list[Memory]:
    """/notebook: the team's own findings, nothing else."""
    return await store.retrieve(scope, team_viewer(scope.team_id), query, k=k, kinds=["finding"])


async def team_findings(store: GameStore, case: Case, scope: Scope) -> list[Evidence]:
    """Evidence this team has found and still holds, in case order.

    For /present autocomplete: never list evidence a team hasn't found.
    Findings withdrawn by a world event are excluded (their replacement's
    source is the event, not the evidence).
    """
    mems = await store.list_by_viewer(scope, team_viewer(scope.team_id), kinds=["finding"])
    held = {m.source.ref for m in mems if m.source.type == "evidence"}
    return [e for e in case.evidence if e.id in held]


# ---------------------------------------------------------------- offers


@dataclass
class OfferDecision:
    offer: Offer | None
    # Why a proposal was dropped. Pass to traces.complete_turn(offer_rejected_reason=...).
    reason: str | None = None


async def validate_offer(store: GameStore, case: Case, scope: Scope, suspect_id: str,
                         turn_id: str, retrieved: list[str], proposed_action: str | None,
                         supporting_memory_id: str | None) -> OfferDecision:
    """Called from submit_reply. The reply is accepted either way; only the
    offer is dropped if a check fails."""
    if not proposed_action:
        return OfferDecision(None)
    # 1. The action exists in this case.
    try:
        action = case.action(proposed_action)
    except KeyError:
        return OfferDecision(None, f"unknown action {proposed_action!r}")
    if not supporting_memory_id:
        return OfferDecision(None, "no supporting memory named")
    # 2. The supporting memory was retrieved in this turn.
    if supporting_memory_id not in retrieved:
        return OfferDecision(None, "supporting memory was not retrieved this turn")
    # 3. A scoped fetch still returns it: active, unexpired, this world, this suspect.
    holder = case.suspect(suspect_id).viewer
    fetched = await store.fetch(scope, holder, [supporting_memory_id])
    if not fetched:
        return OfferDecision(None, "supporting memory is not live in this world")
    # 4. It actually carries the lead.
    if action.id not in fetched[0].unlocks:
        return OfferDecision(None, "supporting memory does not unlock this action")
    # 5. The team has not already taken it.
    if await store.action_used(scope, action.id):
        return OfferDecision(None, "action already used by this team")

    offer = await store.insert_offer(Offer(
        id=ids.offer_id(scope.team_id, action.id, supporting_memory_id),
        case_id=scope.case_id,
        team_id=scope.team_id,
        action=action.id,
        supported_by=supporting_memory_id,
        held_by=holder,
        offered_in=turn_id,
        ts=utcnow(),
    ))
    return OfferDecision(offer)


ClickStatus = Literal["used", "rejected", "already_used", "forbidden", "not_found"]


@dataclass
class ClickResult:
    status: ClickStatus
    message: str
    offer: Offer | None = None
    found: list[Discovery] = field(default_factory=list)


async def use_offer(store: GameStore, case: Case, offer_id: str, clicker_team_id: str) -> ClickResult:
    """Button click. `clicker_team_id` comes from the clicker's Discord role,
    not from whoever ran the original command."""
    offer = await store.get_offer(offer_id)
    if offer is None or offer.case_id != case.id:
        return ClickResult("not_found", "That lead no longer exists.")
    # 1. Only the team the offer belongs to.
    if offer.team_id != clicker_team_id:
        return ClickResult("forbidden", "That lead belongs to another team.")
    if offer.status == "used":
        return ClickResult("already_used", "Your team has already followed that lead.", offer)
    if offer.status == "rejected":
        return ClickResult("rejected", "That lead no longer holds.", offer)

    scope = Scope(case_id=offer.case_id, team_id=offer.team_id)
    action = case.action(offer.action)

    # 2. Fetch the supporting memory again, scoped. Never trust the offer alone.
    fetched = await store.fetch(scope, offer.held_by, [offer.supported_by])
    if not fetched or action.id not in fetched[0].unlocks:
        if await store.transition_offer(offer.id, "offered", "rejected"):
            await store.log_audit(AuditEvent.model_validate({
                "_id": f"au_offer_rejected_{offer.id}",
                "type": "offer_rejected",
                "case_id": offer.case_id,
                "team_id": offer.team_id,
                "memory_ids": [offer.supported_by],
                "reason": f"offer {offer.id}: supporting memory no longer live",
                "ts": utcnow(),
            }))
        return ClickResult("rejected", "That lead no longer holds.", offer)

    # 3. One atomic flip. Only the click that wins performs the action.
    if not await store.transition_offer(offer.id, "offered", "used"):
        return ClickResult("already_used", "Your team has already followed that lead.", offer)
    found = [await discover(store, case, scope, ev) for ev in action.reveals]
    return ClickResult("used", f"You {action.label[0].lower()}{action.label[1:]}.", offer, found)
