"""Evidence handoff used by /present and by the scoped integration smoke test."""

from mystery_core.memory import ids
from mystery_core.memory.models import NewMemory, Scope, suspect_viewer
from .context import TurnError


async def discover(store, scope: Scope, evidence_id: str, *, is_host: bool):
    """Host staging command; normal room-search rules belong to the game engine."""
    if not is_host:
        raise TurnError("host_required")
    case = await store.get_case(scope.case_id)
    evidence = next((e for e in case.get("evidence", []) if e.get("id") == evidence_id), None) if case else None
    if evidence is None:
        raise TurnError("unknown_evidence")
    return (await store.write(scope, [NewMemory(
        id=ids.finding_id(scope.case_id, scope.team_id, evidence_id), text=evidence["text"],
        known_by=["team:" + scope.team_id], kind="finding", source={"type": "evidence", "ref": evidence_id},
    )], dedup=False))[0]


async def present_evidence(store, scope: Scope, suspect_id: str, evidence_id: str):
    """Authorize from the live notebook. No write-then-vector-search, and no inactive retry result."""
    case = await store.get_case(scope.case_id)
    if case is None or not any(s.get("id") == suspect_id for s in case.get("suspects", [])):
        raise TurnError("unknown_suspect")
    evidence = next((e for e in case.get("evidence", []) if e.get("id") == evidence_id), None)
    if evidence is None or not await store.find_by_ref(scope, "team:" + scope.team_id, evidence_id, kinds=["finding"]):
        raise TurnError("evidence_not_held")
    viewer = suspect_viewer(suspect_id)
    mid = ids.evidence_memory_id(scope.case_id, scope.team_id, suspect_id, evidence_id)
    await store.write(scope, [NewMemory(id=mid, text=evidence["text"], known_by=[viewer], kind="evidence",
                                      source={"type": "evidence", "ref": evidence_id},
                                      unlocks=evidence.get("unlocks", []))], dedup=False)
    live = await store.fetch(scope, viewer, [mid])
    if not live:
        raise TurnError("evidence_no_longer_live")
    return live[0]
