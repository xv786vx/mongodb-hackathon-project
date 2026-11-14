"""Reads .env once. Config comes from environment variables only."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv


def _pairs(raw: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for part in filter(None, (p.strip() for p in raw.split(","))):
        key, _, val = part.partition(":")
        if key and val.strip().isdigit() and int(val) != 0:
            out[key.strip()] = int(val)
    return out


def _int(raw: str | None) -> int | None:
    return int(raw) if raw and raw.strip().isdigit() else None


@dataclass(frozen=True)
class Settings:
    token: str = ""
    guild_id: int | None = None
    engine_mode: str = "fake"
    case_id: str = "case_manor"
    team_roles: dict[str, int] = field(default_factory=dict)      # team_id -> role id
    team_channels: dict[str, int] = field(default_factory=dict)   # team_id -> channel id
    host_role_id: int | None = None
    host_channel_id: int | None = None
    case_room_channel_id: int | None = None

    @property
    def team_for_role(self) -> dict[int, str]:
        return {rid: team for team, rid in self.team_roles.items()}

    def missing(self) -> list[str]:
        """Names of settings the bot cannot run without."""
        gaps = []
        if not self.token:
            gaps.append("DISCORD_TOKEN")
        if self.guild_id is None:
            gaps.append("DISCORD_GUILD_ID")
        if not self.team_roles:
            gaps.append("TEAM_ROLES")
        if not self.team_channels:
            gaps.append("TEAM_CHANNELS")
        if self.host_role_id is None:
            gaps.append("HOST_ROLE_ID")
        if self.engine_mode not in ("fake", "local", "hosted"):
            gaps.append("ENGINE_MODE (fake|local|hosted)")
        return gaps


def load() -> Settings:
    load_dotenv()
    e = os.environ.get
    return Settings(
        token=e("DISCORD_TOKEN", "").strip(),
        guild_id=_int(e("DISCORD_GUILD_ID")),
        engine_mode=e("ENGINE_MODE", "fake").strip().lower(),
        case_id=e("CASE_ID", "case_manor").strip(),
        team_roles=_pairs(e("TEAM_ROLES", "")),
        team_channels=_pairs(e("TEAM_CHANNELS", "")),
        host_role_id=_int(e("HOST_ROLE_ID")),
        host_channel_id=_int(e("HOST_CHANNEL_ID")),
        case_room_channel_id=_int(e("CASE_ROOM_CHANNEL_ID")),
    )
