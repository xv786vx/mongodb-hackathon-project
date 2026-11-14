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


def _str_pairs(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in filter(None, (p.strip() for p in raw.split(","))):
        key, _, val = part.partition(":")
        if key.strip() and val.strip():
            out[key.strip()] = val.strip()
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
    ae_project_id: str = ""
    ae_workspace_id: str = ""
    ae_client_id: str = ""           # service account credentials. Live in the bot's .env only (rule 9).
    ae_client_secret: str = ""
    ae_api_key: str = ""             # optional static bearer token, used only if no client credentials are set
    ae_base_url: str = "https://agentengine.mongodb.com"
    elevenlabs_api_key: str = ""
    voice_model: str = "eleven_flash_v2_5"
    voice_ids: dict[str, str] = field(default_factory=dict)   # suspect id -> voice id; the case's voice_id wins

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
        if self.engine_mode == "hosted":
            gaps += [n for n, v in (("AGENT_ENGINE_PROJECT_ID", self.ae_project_id),
                                    ("AGENT_ENGINE_WORKSPACE_ID", self.ae_workspace_id)) if not v]
            if not (self.ae_client_id and self.ae_client_secret) and not self.ae_api_key:
                gaps.append("AGENT_ENGINE_CLIENT_ID + AGENT_ENGINE_CLIENT_SECRET")
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
        ae_project_id=e("AGENT_ENGINE_PROJECT_ID", "").strip(),
        ae_workspace_id=e("AGENT_ENGINE_WORKSPACE_ID", "").strip(),
        elevenlabs_api_key=e("ELEVENLABS_API_KEY", "").strip(),
        voice_model=e("VOICE_MODEL", "eleven_flash_v2_5").strip(),
        voice_ids=_str_pairs(e("VOICE_IDS", "")),
        ae_client_id=e("AGENT_ENGINE_CLIENT_ID", "").strip(),
        ae_client_secret=e("AGENT_ENGINE_CLIENT_SECRET", "").strip(),
        ae_api_key=e("AGENT_ENGINE_API_KEY", "").strip(),
    )
