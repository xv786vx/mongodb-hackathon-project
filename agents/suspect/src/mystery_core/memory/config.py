"""Configuration from environment variables only. Callers load .env themselves (scripts, bot)."""

from __future__ import annotations

import os
from dataclasses import dataclass

# Collection and index names, in one place.
MEMORIES = "memories"
CASES = "cases"
POLICIES = "policies"
TRACES = "traces"
OFFERS = "offers"
AUDIT = "audit"
WORKER_STATE = "worker_state"
ACCUSATIONS = "accusations"
VECTOR_INDEX = "memory_index"


@dataclass(frozen=True)
class Settings:
    mongodb_uri: str
    db_name: str
    embedding_model: str = "voyage-3.5"
    embedding_dims: int = 1024
    embedding_api_key: str | None = None
    rumour_ttl_seconds: int | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        rumour = os.environ.get("RUMOUR_TTL_SECONDS")
        return cls(
            mongodb_uri=os.environ["MONGODB_URI"],
            db_name=os.environ["DB_NAME"],
            embedding_model=os.environ.get("EMBEDDING_MODEL", "voyage-3.5"),
            embedding_dims=int(os.environ.get("EMBEDDING_DIMS", "1024")),
            embedding_api_key=os.environ.get("EMBEDDING_API_KEY") or None,
            rumour_ttl_seconds=int(rumour) if rumour else None,
        )
