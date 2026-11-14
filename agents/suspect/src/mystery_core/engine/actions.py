"""Discoveries, room searches, the notebook, and the offer click.

- discover / search_room / notebook / team_findings: Person 3 (game rules).
- use_offer: Person 2. The click-time check; consuming the offer and writing the finding
  happen in one transaction (store.atomic_turn). Never expose it as an agent tool.
  Offers are created and validated in TurnService.submit (turn.py).

All writes use deterministic ids from `mystery_core.memory.ids` (rule 16), and the
supporting memory is fetched again, scoped, on every click (rule 15).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from mystery_core.memory import ids
from mystery_core.memory.models import AuditEvent, Memory, NewMemory, Scope, Source, team_viewer
from mystery_core.memory.policy import utcnow

from .case import Case, Evidence
from .context import TurnError
from .ports import GameStore


def finding_text(name: str | None, text: str) -> str:
    """One format for a finding, whichever path writes it (search, /discover, a click), so the
    same deterministic id never carries two different texts."""
    return f"{name}: {text}" if name else text


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
        text=finding_text(evidence.name, evidence.text),
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


# ---------------------------------------------------------------- offer click (Person 2)

CLICK_MESSAGES = {
    "performed": "You {label}.",
    "rejected": "That lead no longer holds.",
    "used": "Your team has already followed that lead.",
}


@dataclass(frozen=True)
class ActionResult:
    status: str  # performed | rejected | used
    finding_id: str | None = None
    # Added by Person 3: ready-to-post text, and every evidence id the action revealed.
    message: str = ""
    revealed: tuple[str, ...] = field(default_factory=tuple)


async def use_offer(store, traces, scope: Scope, offer_id: str) -> ActionResult:
    """scope MUST come from the current clicker's Discord role, never from the button or old author.

    Raises TurnError("offer_not_found") for an unknown offer or another team's offer."""
    async def commit(tx, _):
        offer = await tx.get_offer(offer_id)
        if offer is None or (offer.case_id, offer.team_id) != (scope.case_id, scope.team_id):
            raise TurnError("offer_not_found")
        if offer.status != "offered":
            return _result(offer.status)
        memories = await tx.fetch(scope, offer.held_by, [offer.supported_by])
        if (not memories or offer.action not in memories[0].unlocks
                or memories[0].kind == "rumour" or memories[0].supersedes):
            await tx.transition_offer(offer.id, "offered", "rejected")
            await tx.log_audit(AuditEvent(_id="au_reject_" + offer.id, type="offer_rejected",
                                        case_id=scope.case_id, team_id=scope.team_id,
                                        memory_ids=[offer.supported_by], reason="support_not_live", ts=utcnow()))
            return _result("rejected")
        if await tx.action_used(scope, offer.action):
            return _result("used")
        case = await tx.get_case(scope.case_id)
        action = next((a for a in case.get("actions", []) if a.get("id") == offer.action), None) if case else None
        # Fixed by Person 3: the case schema's `reveals` is a list (a single id is still accepted).
        reveals = (action or {}).get("reveals") or []
        reveals = [reveals] if isinstance(reveals, str) else list(reveals)
        by_id = {e.get("id"): e for e in (case or {}).get("evidence", [])}
        revealed = [by_id[r] for r in reveals if r in by_id]
        if not revealed or len(revealed) != len(reveals):
            raise TurnError("invalid_action_definition")
        if not await tx.transition_offer(offer.id, "offered", "used"):
            return _result("used")
        written = await tx.write(scope, [NewMemory(
            id=ids.finding_id(scope.case_id, scope.team_id, ev["id"]),
            text=finding_text(ev.get("name"), ev["text"]),
            known_by=[team_viewer(scope.team_id)], kind="finding",
            source={"type": "evidence", "ref": ev["id"]},
        ) for ev in revealed], dedup=False)
        label = action.get("label") or offer.action
        return ActionResult("performed", written[0].id,
                            CLICK_MESSAGES["performed"].format(label=label[0].lower() + label[1:]),
                            tuple(ev["id"] for ev in revealed))

    # Consuming the offer and writing its finding either both commit or both roll back.
    return await store.atomic_turn(traces, commit)


def _result(status: str) -> ActionResult:
    return ActionResult(status, message=CLICK_MESSAGES.get(status, ""))
