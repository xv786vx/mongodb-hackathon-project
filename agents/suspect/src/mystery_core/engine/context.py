"""Trusted identity, read from bot-set headers rather than model-controlled arguments."""

from dataclasses import dataclass
from collections.abc import Mapping
import re

from mystery_core.memory.models import Scope, suspect_viewer

_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
HEADER_NAMES = ("case-id", "team-id", "suspect-id", "turn-id", "command")


class TurnError(ValueError):
    """A safe, nonsecret error code that can be returned to a model or caller."""


@dataclass(frozen=True)
class TurnContext:
    scope: Scope
    suspect_id: str
    turn_id: str
    command: str

    @property
    def viewer(self) -> str:
        return suspect_viewer(self.suspect_id)

    def custom_headers(self) -> dict[str, str]:
        values = (self.scope.case_id, self.scope.team_id, self.suspect_id, self.turn_id, self.command)
        return {"X-Mdb-Agent-Engine-Custom-" + name: value for name, value in zip(HEADER_NAMES, values)}


def turn_context(headers: Mapping[str, str]) -> TurnContext:
    # The platform already lowercases and strips the transport prefix. Do not accept scope from a body.
    values = []
    for name in HEADER_NAMES:
        value = headers.get(name)
        if not isinstance(value, str) or not _ID.fullmatch(value):
            raise TurnError("missing_or_invalid_" + name)
        values.append(value)
    case, team, suspect, turn, command = values
    if not case.startswith("case_") or not turn.startswith("t_"):
        raise TurnError("invalid_case_or_turn_id")
    if command not in ("interrogate", "present", "replay"):
        raise TurnError("invalid_command")
    return TurnContext(Scope(case_id=case, team_id=team), suspect, turn, command)
