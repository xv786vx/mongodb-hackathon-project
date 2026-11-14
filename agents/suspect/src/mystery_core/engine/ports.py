"""What the game engine needs from the memory layer.

The engine never touches MongoDB directly (AGENTS.md rule 1). Every function
takes a `store`: Person 1's `MemoryStore` (Atlas) or `FakeStore` (in-memory),
both in `mystery_core.memory`. The store is async, so every engine function
that uses it is `async def`.

This is the subset of Person 1's `MemoryStoreAPI` (mystery_core/memory/store.py)
that the engine calls. Keep the two in step.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Iterable, Protocol

from mystery_core.memory.models import AuditEvent, Memory, NewMemory, Offer, Scope
from mystery_core.memory.policy import Policy

# An LLM call: prompt in, raw text out. Optional; the demo path passes None.
LLM = Callable[[str], Awaitable[str]]


class GameStore(Protocol):
    @property
    def policy(self) -> Policy:
        """The policy loaded from the `policies` collection (policy as data)."""
        ...

    # --- memories ---
    async def retrieve(self, scope: Scope, viewer: str, query: str, k: int = 5,
                       kinds: Iterable[str] | None = None,
                       mask: Iterable[str] | None = None) -> list[Memory]: ...

    async def fetch(self, scope: Scope, viewer: str, ids: Iterable[str]) -> list[Memory]: ...

    async def find_by_ref(self, scope: Scope, viewer: str, ref: str,
                          kinds: Iterable[str] | None = None) -> list[Memory]: ...

    async def list_by_viewer(self, scope: Scope, viewer: str,
                             kinds: Iterable[str] | None = None) -> list[Memory]: ...

    async def write(self, scope: Scope, memories: list[NewMemory], *,
                    dedup: bool = True) -> list[Memory]: ...

    async def supersede(self, scope: Scope, old_id: str, new: NewMemory, reason: str) -> Memory: ...

    async def supersede_by_ref(self, case_id: str, ref: str, replacement_text: str,
                               event_id: str, reason: str) -> list[Memory]: ...

    async def count_superseded(self, scope: Scope, viewer: str, kind: str = "cover_story") -> int: ...

    # --- cases ---
    async def get_case(self, case_id: str) -> dict[str, Any] | None: ...

    async def get_truth(self, case_id: str) -> dict[str, Any] | None: ...

    # --- offers ---
    async def get_offer(self, offer_id: str) -> Offer | None: ...

    async def insert_offer(self, offer: Offer) -> Offer: ...

    async def transition_offer(self, offer_id: str, from_status: str, to_status: str) -> bool: ...

    async def action_used(self, scope: Scope, action: str) -> bool: ...

    # --- accusations and audit ---
    async def record_accusation(self, scope: Scope, result: dict) -> bool: ...

    async def get_accusation(self, scope: Scope) -> dict | None: ...

    async def log_audit(self, event: AuditEvent) -> None: ...
