"""Policy as data: lifespan, propagation and correction behaviour per memory kind.

setup_db.py writes DEFAULT_POLICY to the `policies` collection. The store uses DEFAULT_POLICY unless given another.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import KINDS

POLICY_ID = "mystery_v1"

# Permanent memories still get an expires_at, so one `expires_at > now` filter covers everything (rule 4).
FAR_FUTURE = datetime(9999, 12, 31, tzinfo=timezone.utc)


def utcnow() -> datetime:
    # BSON dates have millisecond precision; truncate so round-trips compare equal.
    now = datetime.now(timezone.utc)
    return now.replace(microsecond=(now.microsecond // 1000) * 1000)


class KindPolicy(BaseModel):
    ttl_seconds: int | None = None
    propagates: bool
    on_contradiction: Literal["correct", "new_lie"] | None = None
    confidence_decay: float | None = None


class Policy(BaseModel):
    model_config = ConfigDict(validate_by_name=True, validate_by_alias=True)

    id: str = Field(alias="_id")
    kinds: dict[str, KindPolicy]

    @model_validator(mode="after")
    def _all_kinds(self) -> "Policy":
        if set(self.kinds) != set(KINDS):
            raise ValueError(f"policy kinds {sorted(self.kinds)} must equal {sorted(KINDS)}")
        return self

    def to_doc(self) -> dict:
        return self.model_dump(by_alias=True)

    def expires_at(self, kind: str, now: datetime, rumour_ttl_override: int | None = None) -> datetime:
        ttl = self.kinds[kind].ttl_seconds
        if kind == "rumour" and rumour_ttl_override is not None:
            ttl = rumour_ttl_override
        return FAR_FUTURE if ttl is None else now + timedelta(seconds=ttl)


DEFAULT_POLICY = Policy.model_validate(
    {
        "_id": POLICY_ID,
        "kinds": {
            "witnessed": {"ttl_seconds": None, "propagates": True, "on_contradiction": "correct"},
            "rumour": {"ttl_seconds": 600, "propagates": True, "on_contradiction": "correct", "confidence_decay": 0.3},
            "cover_story": {"ttl_seconds": None, "propagates": True, "on_contradiction": "new_lie"},
            "evidence": {"ttl_seconds": None, "propagates": False},
            "interaction": {"ttl_seconds": None, "propagates": False},
            "finding": {"ttl_seconds": None, "propagates": False},
        },
    }
)
