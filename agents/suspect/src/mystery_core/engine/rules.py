"""Cover-story breaks (AGENTS.md rule 13).

Allowed decisions and the crack gate live in Person 2's `TurnService.allowed_decisions`
(turn.py), which `submit_reply` re-checks. This module only counts breaks, which the
contradiction step uses to choose the killer's next fallback lie.
"""
from __future__ import annotations

from mystery_core.memory.models import Scope

from .case import Case
from .ports import GameStore


async def cover_breaks(store: GameStore, case: Case, scope: Scope, suspect_id: str) -> int:
    """How many times this suspect's cover story has been superseded in this world."""
    suspect = case.suspect(suspect_id)
    if not suspect.is_killer:
        return 0
    return await store.count_superseded(scope, suspect.viewer, "cover_story")
