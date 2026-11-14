"""The bot's click-time authorization. Never expose this function as an agent tool."""

from dataclasses import dataclass

from mystery_core.memory import ids
from mystery_core.memory.models import AuditEvent, NewMemory, Scope
from mystery_core.memory.policy import utcnow
from .context import TurnError


@dataclass(frozen=True)
class ActionResult:
    status: str
    finding_id: str | None = None


async def use_offer(store, traces, scope: Scope, offer_id: str) -> ActionResult:
    """scope MUST come from the current clicker's Discord role, never from the button or old author."""
    async def commit(tx, _):
        offer = await tx.get_offer(offer_id)
        if offer is None or (offer.case_id, offer.team_id) != (scope.case_id, scope.team_id):
            raise TurnError("offer_not_found")
        if offer.status != "offered":
            return ActionResult(offer.status)
        memories = await tx.fetch(scope, offer.held_by, [offer.supported_by])
        if (not memories or offer.action not in memories[0].unlocks
                or memories[0].kind == "rumour" or memories[0].supersedes):
            await tx.transition_offer(offer.id, "offered", "rejected")
            await tx.log_audit(AuditEvent(_id="au_reject_" + offer.id, type="offer_rejected",
                                        case_id=scope.case_id, team_id=scope.team_id,
                                        memory_ids=[offer.supported_by], reason="support_not_live", ts=utcnow()))
            return ActionResult("rejected")
        if await tx.action_used(scope, offer.action):
            return ActionResult("used")
        case = await tx.get_case(scope.case_id)
        action = next((a for a in case.get("actions", []) if a.get("id") == offer.action), None) if case else None
        evidence = next((e for e in case.get("evidence", []) if action and e.get("id") == action.get("reveals")), None) if case else None
        if evidence is None:
            raise TurnError("invalid_action_definition")
        if not await tx.transition_offer(offer.id, "offered", "used"):
            return ActionResult("used")
        mid = ids.finding_id(scope.case_id, scope.team_id, evidence["id"])
        await tx.write(scope, [NewMemory(id=mid, text=evidence["text"], known_by=["team:" + scope.team_id],
                                        kind="finding", source={"type": "evidence", "ref": evidence["id"]})], dedup=False)
        return ActionResult("performed", mid)

    # Consuming the offer and writing its finding either both commit or both roll back.
    return await store.atomic_turn(traces, commit)
