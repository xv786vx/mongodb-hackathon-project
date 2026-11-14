"""Accusations. The only module that reads the case truth (AGENTS.md rule 5).

The truth is fetched from Atlas per accusation and never kept on a `Case`."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from mystery_core.memory.models import Scope
from mystery_core.memory.policy import utcnow

from .case import Case, Truth, check_truth
from .ports import GameStore

AccusationStatus = Literal["solved", "eliminated", "already_accused", "invalid"]


@dataclass
class AccusationResult:
    status: AccusationStatus
    message: str  # safe to post in the public channel


async def accuse(store: GameStore, case: Case, scope: Scope, who: str, weapon: str,
                 room: str) -> AccusationResult:
    """One accusation per team. A wrong guess knocks the team out without
    revealing which part was wrong."""
    try:
        suspect = case.suspect(who)
        weapon_name = next(w.name for w in case.weapons if w.id == weapon)
        room_name = case.room(room).name
    except (KeyError, StopIteration):
        return AccusationResult("invalid", "That accusation names something not in this case.")
    if await store.get_accusation(scope) is not None:
        return AccusationResult("already_accused", "Your team has already made its accusation.")

    doc = await store.get_truth(case.id)
    if doc is None:
        raise RuntimeError(f"case {case.id!r} has no truth in Atlas; cannot resolve accusations")
    truth = Truth.model_validate(doc)
    check_truth(case, truth)
    correct = (who, weapon, room) == (truth.killer, truth.weapon, truth.room)
    claim = f"{suspect.name}, with {weapon_name}, in {room_name}"
    record = {"case_id": scope.case_id, "team_id": scope.team_id, "who": who,
              "weapon": weapon, "room": room, "correct": correct,
              "ts": utcnow()}
    if not await store.record_accusation(scope, record):
        return AccusationResult("already_accused", "Your team has already made its accusation.")
    if correct:
        return AccusationResult("solved", f"Team {scope.team_id} accuses {claim}. Case solved!")
    return AccusationResult("eliminated",
                            f"Team {scope.team_id} accuses {claim}. Wrong. They are out of the case.")


async def is_eliminated(store: GameStore, scope: Scope) -> bool:
    record = await store.get_accusation(scope)
    return bool(record) and not record.get("correct", False)
