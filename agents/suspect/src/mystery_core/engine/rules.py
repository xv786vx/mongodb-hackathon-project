"""Decisions and the confession gate (AGENTS.md rule 13)."""
from __future__ import annotations

from mystery_core.memory.models import Decision, Scope

from .case import Case
from .ports import GameStore

DECISION_GUIDE: dict[str, str] = {
    "truth": "answer honestly from what you remember",
    "deflect": "avoid the question, change the subject, or give a guarded non-answer",
    "lie": "say something you know is false to protect yourself or someone else",
    "crack": "break down and admit what you did",
}


async def cover_breaks(store: GameStore, case: Case, scope: Scope, suspect_id: str) -> int:
    """How many times this suspect's cover story has been superseded in this world."""
    suspect = case.suspect(suspect_id)
    if not suspect.is_killer:
        return 0
    return await store.count_superseded(scope, suspect.viewer, "cover_story")


async def allowed_decisions(store: GameStore, case: Case, scope: Scope,
                            suspect_id: str) -> list[Decision]:
    """Recomputed from the database every time; never cached, never from model output.

    Everyone may answer truthfully, deflect or lie. Only the killer can crack,
    and only once their cover story has been broken `crack_threshold` times in
    this team's world.
    """
    suspect = case.suspect(suspect_id)
    allowed: list[Decision] = ["truth", "deflect", "lie"]
    if suspect.is_killer and await cover_breaks(store, case, scope, suspect_id) >= suspect.crack_threshold:
        allowed.append("crack")
    return allowed


async def check_decision(store: GameStore, case: Case, scope: Scope, suspect_id: str,
                         decision: str) -> str | None:
    """For submit_reply. Returns None if allowed, or an error the model can act on."""
    allowed = await allowed_decisions(store, case, scope, suspect_id)
    if decision in allowed:
        return None
    return (f"Decision {decision!r} is not allowed for this turn. "
            f"Choose one of: {', '.join(allowed)}.")
