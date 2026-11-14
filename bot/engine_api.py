"""What the bot needs from the game engine.

Shapes follow person 3's entry points (mystery_core.engine) and person 1's traces. Everything is
async (decided with person 3): the bot awaits each call directly. `case_info` stays synchronous because the
adapter loads the case once at startup. The real adapter (not written yet) maps each method onto the engine
functions listed beside it. FakeEngine implements all of it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class Suspect:
    id: str
    name: str
    role: str = ""
    voice_id: str | None = None


@dataclass(frozen=True)
class Named:
    id: str
    name: str


@dataclass(frozen=True)
class CaseInfo:
    title: str
    intro: str
    suspects: tuple[Suspect, ...]
    open_rooms: tuple[Named, ...]
    weapons: tuple[Named, ...]
    events: tuple[Named, ...]
    evidence: tuple[Named, ...]          # host-only (/discover). Players only ever get team_findings.


@dataclass(frozen=True)
class Evidence:
    id: str
    name: str
    text: str = ""


@dataclass(frozen=True)
class Found:
    evidence: Evidence
    new: bool = True                     # False: the team already had it
    withdrawn: bool = False              # True: a world event retracted it


@dataclass(frozen=True)
class TurnPrep:                          # turn.prepare_interrogate / prepare_present
    ok: bool
    error: str = ""
    message: str = ""                    # the invoke message
    prompt_version: str = ""
    fresh: tuple[str, ...] = ()          # memory ids written for this turn; go on the trace's `written`


@dataclass(frozen=True)
class Prepared:                          # what the bot hands to the agent runner
    turn_id: str
    case_id: str
    team_id: str
    suspect_id: str
    command: str
    message: str = ""
    prompt_version: str = ""
    fresh: tuple[str, ...] = ()
    native: Any = None                   # person 2's PreparedTurn (has .context), for the local runner


@dataclass(frozen=True)
class Trace:                             # person 1's Trace, reduced to what the bot reads
    turn_id: str
    status: str                          # pending | complete | rejected
    suspect_id: str
    reply: str = ""
    offer_id: str | None = None
    offer_label: str | None = None
    decision: str = ""                   # for the host and replay. Never shown to players.


@dataclass(frozen=True)
class OfferResult:                       # actions.use_offer -> ClickResult
    status: str                          # used | rejected | already_used | forbidden | not_found
    message: str = ""
    found: tuple[Found, ...] = ()


@dataclass(frozen=True)
class EventResult:                       # events.trigger_event
    notify_teams: tuple[str, ...]
    notice: str


@dataclass(frozen=True)
class SearchResult:                      # actions.search_room
    ok: bool
    room: str = ""
    found: tuple[Found, ...] = ()
    error: str = ""


@dataclass(frozen=True)
class AccusationResult:                  # game.accuse
    status: str                          # solved | eliminated | already_accused | invalid
    message: str = ""                    # safe to post publicly


class Engine(Protocol):
    def case_info(self) -> CaseInfo: ...                                      # load_case, once
    async def team_findings(self, team_id: str) -> list[Evidence]: ...              # actions.team_findings
    async def recent_turns(self, team_id: str, suspect_id: str) -> list[dict]: ...  # traces: {input, reply}
    async def prepare_interrogate(self, team_id: str, suspect_id: str, question: str,
                            recent: list[dict]) -> TurnPrep: ...
    async def prepare_present(self, team_id: str, suspect_id: str, evidence_id: str, question: str,
                        recent: list[dict]) -> TurnPrep: ...                  # always llm=None
    async def begin_turn(self, team_id: str, suspect_id: str, turn_id: str, command: str, question: str,
                   prep: TurnPrep) -> Prepared: ...                           # traces.begin_turn
    async def generate(self, prepared: Prepared) -> None: ...                       # local runner or invoke.py
    async def get_trace(self, team_id: str, turn_id: str) -> Trace | None: ...      # traces.get_trace
    async def use_offer(self, team_id: str, offer_id: str) -> OfferResult: ...
    async def trigger_event(self, event_id: str) -> EventResult: ...
    async def search_room(self, team_id: str, room_id: str) -> SearchResult: ...
    async def discover(self, team_id: str, evidence_id: str) -> Found: ...
    async def notebook(self, team_id: str, query: str) -> list[str]: ...
    async def accuse(self, team_id: str, who: str, weapon: str, room: str) -> AccusationResult: ...
    async def run_round(self, team_id: str, round_no: int) -> int: ...              # post nothing about content
    async def is_eliminated(self, team_id: str) -> bool: ...
    async def voice_inputs(self, team_id: str, trace: "Trace") -> tuple[float, int]: ...   # (mean confidence, cover breaks)
    async def record_voice(self, team_id: str, turn_id: str, voice: dict) -> None: ...      # traces.set_voice
