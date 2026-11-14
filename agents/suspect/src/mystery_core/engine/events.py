"""Host-only world events; the only production caller of cross-world supersession."""

from .context import TurnError


async def apply_event(store, case_id: str, event_id: str, *, is_host: bool):
    if not is_host:
        raise TurnError("host_required")
    case = await store.get_case(case_id)
    event = next((e for e in case.get("events", []) if e.get("id") == event_id), None) if case else None
    if event is None:
        raise TurnError("unknown_event")
    return await store.supersede_by_ref(case_id, event["ref"], event["replacement"], event_id, "host_event")
