"""Host-only world events; the only production caller of cross-world supersession (rule 17).

- trigger_event (Person 3): what the bot's /event calls. Applies the event and its `also`
  changes, and returns the teams to notify and the notice.
- apply_event (Person 2): the minimal version, primary ref only, used by the agent tests.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from mystery_core.memory.models import Memory

from .case import Case, WorldEvent
from .context import TurnError
from .ports import GameStore


@dataclass
class EventResult:
    event: WorldEvent
    replaced: list[Memory] = field(default_factory=list)
    # Teams to tell, each in its own channel: only worlds that held the event's
    # primary ref, so the notice never reveals that memory to anyone else.
    notify_teams: list[str] = field(default_factory=list)


async def trigger_event(store: GameStore, case: Case, event_id: str) -> EventResult:
    """Apply a world event to every world. Safe to repeat: the store skips
    memories that are already superseded, so a rerun replaces nothing.

    The store writes one `supersede` audit event per replaced memory."""
    event = case.event(event_id)
    result = EventResult(event)
    for change in event.changes():
        replaced = await store.supersede_by_ref(case.id, change.ref, change.replacement,
                                                event_id=event.id, reason=f"event:{event.id}")
        result.replaced.extend(replaced)
        if change.ref == event.ref:
            result.notify_teams = sorted({m.team_id for m in replaced})
    return result


async def apply_event(store, case_id: str, event_id: str, *, is_host: bool):
    if not is_host:
        raise TurnError("host_required")
    case = await store.get_case(case_id)
    event = next((e for e in case.get("events", []) if e.get("id") == event_id), None) if case else None
    if event is None:
        raise TurnError("unknown_event")
    return await store.supersede_by_ref(case_id, event["ref"], event["replacement"], event_id, "host_event")
