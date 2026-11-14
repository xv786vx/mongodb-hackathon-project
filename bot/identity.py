"""Who is asking. team_id comes from the member's Discord role (rule 7), never from input."""
from __future__ import annotations

from dataclasses import dataclass

from bot.config import Settings


class Refusal(Exception):
    """A user-facing refusal. The message is safe to show (ephemerally)."""


class NotOnATeam(Refusal):
    pass


class WrongChannel(Refusal):
    pass


class NotHost(Refusal):
    pass


@dataclass(frozen=True)
class Caller:
    team_id: str
    channel_id: int


def resolve_team(cfg: Settings, role_ids: set[int], channel_id: int | None) -> Caller:
    teams = {cfg.team_for_role[r] for r in role_ids if r in cfg.team_for_role}
    if len(teams) != 1:
        raise NotOnATeam("You need exactly one team role to do that.")
    team_id = teams.pop()
    # The channel never decides the team. It only keeps replies out of public channels (rule 18).
    if channel_id != cfg.team_channels.get(team_id):
        raise WrongChannel("Use your team's private channel for that.")
    return Caller(team_id, channel_id)


def require_host(cfg: Settings, role_ids: set[int]) -> None:
    if cfg.host_role_id is None or cfg.host_role_id not in role_ids:
        raise NotHost("Host only.")


def role_ids_of(interaction) -> set[int]:
    return {r.id for r in getattr(interaction.user, "roles", [])}
