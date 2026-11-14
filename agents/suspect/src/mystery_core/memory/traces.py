"""Turn records. Every turn writes one trace (rule 14); the bot posts only `complete` traces (rule 12).

Lifecycle:  bot: begin_turn -> agent tools: add_retrieved ... complete_turn | reject_turn -> bot: get_trace, set_voice

Every call takes a Scope, and a trace from another world behaves as if it does not exist.
Turn ids come from the Discord interaction id (ids.turn_id), so a retried command finds the same trace.
"""

from __future__ import annotations

from typing import Any, Iterable, Protocol

from pymongo import ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from .config import TRACES
from .models import Decision, Scope, Trace, validate_viewer
from .policy import utcnow


class TraceConflict(Exception):
    """The trace exists but not in a state that allows this change (e.g. completing a rejected turn)."""


class TraceStoreAPI(Protocol):
    async def begin_turn(
        self,
        scope: Scope,
        turn_id: str,
        actor: str,
        command: str,
        input: str,
        *,
        engine_mode: str | None = None,
        replay_of: str | None = None,
        masked: Iterable[str] = (),
    ) -> Trace: ...

    async def get_trace(self, scope: Scope, turn_id: str) -> Trace | None: ...

    async def add_retrieved(self, scope: Scope, turn_id: str, memory_ids: Iterable[str]) -> None: ...

    async def complete_turn(
        self,
        scope: Scope,
        turn_id: str,
        *,
        decision: Decision,
        reply: str,
        written: Iterable[str] = (),
        offer_id: str | None = None,
        offer_label: str | None = None,
        offer_rejected_reason: str | None = None,
        model: str | None = None,
        prompt_version: str | None = None,
        execution_id: str | None = None,
    ) -> Trace: ...

    async def reject_turn(self, scope: Scope, turn_id: str, reason: str) -> Trace: ...

    async def set_voice(self, scope: Scope, turn_id: str, voice: dict[str, Any]) -> None: ...


def _check_turn_id(turn_id: str) -> str:
    if not turn_id.startswith("t_"):
        raise ValueError(f"turn id {turn_id!r} must start with 't_'")
    return turn_id


def _completion_fields(**kw: Any) -> dict[str, Any]:
    fields = {k: v for k, v in kw.items() if v is not None}
    fields["written"] = list(kw.get("written") or [])
    fields["status"] = "complete"
    return fields


class TraceStore:
    def __init__(self, db: AsyncDatabase):
        self._traces = db[TRACES]

    def _key(self, scope: Scope, turn_id: str) -> dict[str, Any]:
        return {"_id": _check_turn_id(turn_id), "case_id": scope.case_id, "team_id": scope.team_id}

    async def begin_turn(
        self,
        scope: Scope,
        turn_id: str,
        actor: str,
        command: str,
        input: str,
        *,
        engine_mode: str | None = None,
        replay_of: str | None = None,
        masked: Iterable[str] = (),
    ) -> Trace:
        """Find or create the pending trace. A retry returns the existing trace unchanged (input is never overwritten)."""
        new = Trace(
            _id=_check_turn_id(turn_id),
            case_id=scope.case_id,
            team_id=scope.team_id,
            actor=validate_viewer(actor),
            command=command,
            input=input,
            engine_mode=engine_mode,
            replay_of=replay_of,
            masked=list(masked),
            ts=utcnow(),
        ).to_doc()
        try:
            doc = await self._traces.find_one_and_update(
                self._key(scope, turn_id),
                {"$setOnInsert": {k: v for k, v in new.items() if k not in ("_id", "case_id", "team_id")}},
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
        except DuplicateKeyError:
            # The id exists with a different scope: another world's turn. Never reveal or reuse it.
            raise TraceConflict(f"turn {turn_id} belongs to another world") from None
        return Trace.model_validate(doc)

    async def get_trace(self, scope: Scope, turn_id: str) -> Trace | None:
        doc = await self._traces.find_one(self._key(scope, turn_id))
        return Trace.model_validate(doc) if doc else None

    async def add_retrieved(self, scope: Scope, turn_id: str, memory_ids: Iterable[str]) -> None:
        """Record what search_memories returned. Only while pending; idempotent."""
        ids_ = list(memory_ids)
        if ids_:
            await self._traces.update_one(
                self._key(scope, turn_id) | {"status": "pending"},
                {"$addToSet": {"retrieved": {"$each": ids_}}},
            )

    async def complete_turn(
        self,
        scope: Scope,
        turn_id: str,
        *,
        decision: Decision,
        reply: str,
        written: Iterable[str] = (),
        offer_id: str | None = None,
        offer_label: str | None = None,
        offer_rejected_reason: str | None = None,
        model: str | None = None,
        prompt_version: str | None = None,
        execution_id: str | None = None,
    ) -> Trace:
        """pending -> complete. If already complete, returns the stored trace and changes nothing."""
        fields = _completion_fields(
            decision=decision, reply=reply, written=written, offer_id=offer_id, offer_label=offer_label,
            offer_rejected_reason=offer_rejected_reason, model=model, prompt_version=prompt_version,
            execution_id=execution_id,
        )
        doc = await self._traces.find_one_and_update(
            self._key(scope, turn_id) | {"status": "pending"},
            {"$set": fields},
            return_document=ReturnDocument.AFTER,
        )
        if doc:
            return Trace.model_validate(doc)
        existing = await self.get_trace(scope, turn_id)
        if existing is None:
            raise TraceConflict(f"turn {turn_id} not found; call begin_turn first")
        if existing.status == "complete":
            return existing
        raise TraceConflict(f"turn {turn_id} is {existing.status}")

    async def reject_turn(self, scope: Scope, turn_id: str, reason: str) -> Trace:
        """pending -> rejected. A rejected trace is never posted."""
        doc = await self._traces.find_one_and_update(
            self._key(scope, turn_id) | {"status": "pending"},
            {"$set": {"status": "rejected", "rejected_reason": reason}},
            return_document=ReturnDocument.AFTER,
        )
        if doc:
            return Trace.model_validate(doc)
        existing = await self.get_trace(scope, turn_id)
        if existing is None:
            raise TraceConflict(f"turn {turn_id} not found")
        if existing.status == "rejected":
            return existing
        raise TraceConflict(f"turn {turn_id} is {existing.status}")

    async def set_voice(self, scope: Scope, turn_id: str, voice: dict[str, Any]) -> None:
        """Record the voice settings used for the clip (bot, after posting)."""
        await self._traces.update_one(self._key(scope, turn_id), {"$set": {"voice": voice}})
