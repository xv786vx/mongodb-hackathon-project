"""World events (host only). The one place that calls supersede_by_ref
(AGENTS.md rule 17)."""
from __future__ import annotations

from dataclasses import dataclass, field

from mystery_core.memory.models import Memory

from .case import Case, WorldEvent
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
