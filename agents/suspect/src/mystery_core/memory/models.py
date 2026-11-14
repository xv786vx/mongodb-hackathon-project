"""Shared document shapes. ARCHITECTURE.md section 3 is the source of truth; keep them in sync.

Changing a model here is a contract change: update CONTRACTS.md and tell the team.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator

Kind = Literal["witnessed", "rumour", "cover_story", "evidence", "interaction", "finding"]
SourceType = Literal["seed", "told_by", "evidence", "interaction", "fabricated", "event"]
MemoryStatus = Literal["active", "superseded"]
Decision = Literal["truth", "lie", "deflect", "crack"]
TraceStatus = Literal["pending", "complete", "rejected"]
OfferStatus = Literal["offered", "used", "rejected"]
AuditType = Literal["supersede", "forget", "expire", "leak_test", "offer_rejected"]

KINDS: tuple[str, ...] = get_args(Kind)
SOURCE_TYPES: tuple[str, ...] = get_args(SourceType)
MEMORY_STATUSES: tuple[str, ...] = get_args(MemoryStatus)

# --- viewers ---------------------------------------------------------------

VIEWER_RE = re.compile(r"^(suspect|team):[A-Za-z0-9_-]+$")


def validate_viewer(viewer: str) -> str:
    """Viewers are 'suspect:<id>' or 'team:<id>'. Anything else is rejected."""
    if not isinstance(viewer, str) or not VIEWER_RE.match(viewer):
        raise ValueError(f"invalid viewer {viewer!r}; expected 'suspect:<id>' or 'team:<id>'")
    return viewer


def suspect_viewer(suspect_id: str) -> str:
    return validate_viewer(f"suspect:{suspect_id}")


def team_viewer(team_id: str) -> str:
    return validate_viewer(f"team:{team_id}")


# --- base -------------------------------------------------------------------


class _Doc(BaseModel):
    """A MongoDB document whose `id` field is stored as `_id`."""

    model_config = ConfigDict(validate_by_name=True, validate_by_alias=True)

    id: str = Field(alias="_id")

    def to_doc(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True)


# --- scope -------------------------------------------------------------------


class Scope(BaseModel):
    """One world: (case, team). Always built from trusted input (Discord role, headers), never model output."""

    model_config = ConfigDict(frozen=True)

    case_id: str = Field(min_length=1)
    team_id: str = Field(min_length=1)


# --- memories ------------------------------------------------------------------


class Source(BaseModel):
    type: SourceType
    ref: str | None = None


class NewMemory(BaseModel):
    """What callers pass to store.write(). Scope comes separately; status, links and dates are set by the store."""

    text: str = Field(min_length=1)
    known_by: list[str] = Field(min_length=1)
    kind: Kind
    source: Source
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    unlocks: list[str] = Field(default_factory=list)
    # Pass a deterministic id (see ids.py) for anything a retry could create twice (rule 16).
    id: str | None = None
    # Override the policy lifespan. Must be timezone-aware.
    expires_at: datetime | None = None
    # Precomputed embedding (seed copies). Normally left None and computed by the store.
    embedding: list[float] | None = Field(default=None, repr=False)

    @field_validator("known_by")
    @classmethod
    def _viewers(cls, v: list[str]) -> list[str]:
        return [validate_viewer(x) for x in v]


class Memory(_Doc):
    case_id: str
    team_id: str
    text: str
    known_by: list[str]
    kind: Kind
    source: Source
    confidence: float
    unlocks: list[str] = Field(default_factory=list)
    status: MemoryStatus
    supersedes: str | None = None
    superseded_by: str | None = None
    created_at: datetime
    expires_at: datetime
    # Not returned by retrieve/fetch (projected out). Present only on documents being inserted.
    embedding: list[float] | None = Field(default=None, repr=False)
    # Vector search score; set by retrieve only, never stored.
    score: float | None = None

    @property
    def scope(self) -> Scope:
        return Scope(case_id=self.case_id, team_id=self.team_id)

    def to_doc(self) -> dict[str, Any]:
        d = self.model_dump(by_alias=True, exclude={"score"})
        if d.get("embedding") is None:
            d.pop("embedding", None)
        return d


# --- traces, offers, audit -----------------------------------------------------


class Trace(_Doc):
    """One turn. _id is the turn id 't_<discord interaction id>'. The bot posts only `complete` traces."""

    status: TraceStatus = "pending"
    case_id: str
    team_id: str
    actor: str  # 'suspect:<id>'
    command: str
    input: str
    retrieved: list[str] = Field(default_factory=list)
    decision: Decision | None = None
    reply: str | None = None
    written: list[str] = Field(default_factory=list)
    offer_id: str | None = None
    offer_rejected_reason: str | None = None
    engine_mode: Literal["hosted", "local"] | None = None
    execution_id: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    voice: dict[str, Any] | None = None
    replay_of: str | None = None
    masked: list[str] = Field(default_factory=list)
    ts: datetime


class Offer(_Doc):
    """_id is offer_id(team, action, supporting memory). status flips offered -> used in one atomic update."""

    case_id: str
    team_id: str
    action: str
    supported_by: str
    offered_in: str
    status: OfferStatus = "offered"
    ts: datetime


class AuditEvent(_Doc):
    """Governance events. IDs only, never memory text."""

    type: AuditType
    case_id: str
    team_id: str | None = None
    memory_ids: list[str] = Field(default_factory=list)
    count: int | None = None
    reason: str | None = None
    ts: datetime
