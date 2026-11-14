"""The calls the bot makes into the game engine (placeholders until CONTRACTS.md is agreed).

mystery_core is synchronous, so the bot always calls these through asyncio.to_thread.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class Prepared:
    turn_id: str
    case_id: str
    team_id: str
    suspect_id: str
    command: str
    message: str = ""


@dataclass(frozen=True)
class Trace:
    turn_id: str
    status: str                      # pending | complete | rejected
    suspect_id: str
    reply: str = ""
    offer_id: str | None = None
    offer_label: str | None = None
    decision: str = ""               # kept for the host and replay. Never shown to players.
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class OfferResult:
    status: str                      # used | lead_gone | already_used | wrong_team
    text: str = ""


class Engine(Protocol):
    def suspects(self, case_id: str) -> list[tuple[str, str]]: ...       # (id, display name)
    def begin_turn(self, case_id: str, team_id: str, suspect_id: str, turn_id: str,
                   command: str, question: str) -> Prepared: ...
    def generate(self, prepared: Prepared) -> None: ...                  # writes the trace
    def get_trace(self, turn_id: str) -> Trace | None: ...
    def use_offer(self, case_id: str, team_id: str, offer_id: str) -> OfferResult: ...
    def trigger_event(self, case_id: str, event_id: str) -> list[str]: ...   # teams to notify
