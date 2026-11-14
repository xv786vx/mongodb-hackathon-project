"""In-memory implementation of MemoryStoreAPI for unit tests and stub development.

Same governance semantics as MemoryStore (scope, viewer, status, expiry, no text edits, no inherited
unlocks), with cosine similarity in Python instead of $vectorSearch. Not for production.

    store = FakeStore(cases={"case_test": case_dict})
    await store.seed_world("case_test", "A")
"""

from __future__ import annotations

import copy
from datetime import datetime
from typing import Any, Iterable

from . import ids as _ids
from .embeddings import Embedder, FakeEmbedder, cosine
from .models import AuditEvent, Decision, Memory, NewMemory, Offer, OfferStatus, Scope, Trace, validate_viewer
from .policy import DEFAULT_POLICY, Policy, utcnow
from .store import check_seed_memories
from .traces import TraceConflict, _check_turn_id, _completion_fields


class FakeStore:
    def __init__(
        self,
        cases: dict[str, dict[str, Any]] | None = None,
        embedder: Embedder | None = None,
        policy: Policy = DEFAULT_POLICY,
        rumour_ttl_seconds: int | None = None,
        dedup_threshold: float = 0.97,
    ):
        self.cases = cases or {}
        self.docs: dict[str, Memory] = {}  # includes embeddings
        self.audit: list[AuditEvent] = []
        self.offers: dict[str, Offer] = {}
        self.accusations: dict[tuple[str, str], dict[str, Any]] = {}
        self._embedder = embedder or FakeEmbedder()
        self._policy = policy
        self._rumour_ttl = rumour_ttl_seconds
        self._dedup_threshold = dedup_threshold
        self.now = utcnow  # override in tests to move time forward

    # --- helpers ------------------------------------------------------------------

    def _visible(self, m: Memory, scope: Scope, viewer: str, now: datetime) -> bool:
        return (
            m.case_id == scope.case_id
            and m.team_id == scope.team_id
            and viewer in m.known_by
            and m.status == "active"
            and m.expires_at > now
        )

    @staticmethod
    def _public(m: Memory, score: float | None = None) -> Memory:
        return m.model_copy(update={"embedding": None, "score": score})

    def _audit(self, type_: str, case_id: str, team_id: str | None, memory_ids: list[str], **kw: Any) -> None:
        self.audit.append(
            AuditEvent(
                _id=f"au_{len(self.audit) + 1}",
                type=type_,
                case_id=case_id,
                team_id=team_id,
                memory_ids=memory_ids,
                ts=self.now(),
                **kw,
            )
        )

    # --- reads ------------------------------------------------------------------

    async def retrieve(
        self,
        scope: Scope,
        viewer: str,
        query: str,
        k: int = 5,
        kinds: Iterable[str] | None = None,
        mask: Iterable[str] | None = None,
    ) -> list[Memory]:
        viewer = validate_viewer(viewer)
        now = self.now()
        kinds = set(kinds) if kinds else None
        mask = set(mask or [])
        qv = await self._embedder.embed_query(query)
        hits = [
            (cosine(qv, m.embedding or []), m)
            for m in self.docs.values()
            if self._visible(m, scope, viewer, now)
            and (kinds is None or m.kind in kinds)
            and m.id not in mask
        ]
        hits.sort(key=lambda h: h[0], reverse=True)
        return [self._public(m, s) for s, m in hits[:k]]

    async def fetch(self, scope: Scope, viewer: str, ids: Iterable[str]) -> list[Memory]:
        viewer = validate_viewer(viewer)
        now = self.now()
        return [
            self._public(self.docs[i])
            for i in ids
            if i in self.docs and self._visible(self.docs[i], scope, viewer, now)
        ]

    # --- writes -----------------------------------------------------------------

    async def write(self, scope: Scope, memories: list[NewMemory], *, dedup: bool = True) -> list[Memory]:
        now = self.now()
        out: list[Memory] = []
        for new in memories:
            emb = new.embedding or (await self._embedder.embed_documents([new.text]))[0]
            if new.id is None and dedup:
                dup = self._find_duplicate(scope, new, emb, now)
                if dup is not None:
                    dup.confidence = max(dup.confidence, new.confidence)
                    out.append(self._public(dup))
                    continue
            mid = new.id or _ids.random_memory_id()
            if mid in self.docs:
                existing = self.docs[mid]
                if existing.scope != scope:
                    raise ValueError(f"memory id {mid} already exists in another world")
                out.append(self._public(existing))
                continue
            m = Memory(
                _id=mid,
                case_id=scope.case_id,
                team_id=scope.team_id,
                text=new.text,
                known_by=new.known_by,
                kind=new.kind,
                source=new.source,
                confidence=new.confidence,
                unlocks=new.unlocks,
                status="active",
                created_at=now,
                expires_at=new.expires_at or self._policy.expires_at(new.kind, now, self._rumour_ttl),
                embedding=emb,
            )
            self.docs[mid] = m
            out.append(self._public(m))
        return out

    def _find_duplicate(self, scope: Scope, new: NewMemory, emb: list[float], now: datetime) -> Memory | None:
        for m in self.docs.values():
            if (
                self._visible(m, scope, new.known_by[0], now)
                and m.kind == new.kind
                and set(new.known_by) <= set(m.known_by)
                and cosine(emb, m.embedding or []) >= self._dedup_threshold
            ):
                return m
        return None

    async def seed_world(self, case_id: str, team_id: str) -> int:
        case = self.cases.get(case_id)
        if case is None:
            raise KeyError(f"case {case_id} not found")
        check_seed_memories(case_id, case.get("seed_memories", []))
        scope = Scope(case_id=case_id, team_id=team_id)
        before = len(self.docs)
        await self.write(
            scope,
            [
                NewMemory(
                    id=_ids.seed_memory_id(case_id, team_id, s["id"]),
                    text=s["text"],
                    known_by=s["known_by"],
                    kind=s["kind"],
                    source={"type": "seed", "ref": s["id"]},
                    confidence=s.get("confidence", 1.0),
                    unlocks=s.get("unlocks", []),
                    embedding=s.get("embedding") or None,
                )
                for s in case.get("seed_memories", [])
            ],
        )
        return len(self.docs) - before

    async def count_superseded(self, scope: Scope, viewer: str, kind: str = "cover_story") -> int:
        viewer = validate_viewer(viewer)
        return sum(
            1
            for m in self.docs.values()
            if m.scope == scope and viewer in m.known_by and m.kind == kind and m.status == "superseded"
        )

    @property
    def policy(self) -> Policy:
        return self._policy

    # --- engine reads (GameStore) -------------------------------------------------------

    async def find_by_ref(
        self, scope: Scope, viewer: str, ref: str, kinds: Iterable[str] | None = None
    ) -> list[Memory]:
        viewer, now, kinds = validate_viewer(viewer), self.now(), set(kinds) if kinds else None
        return [
            self._public(m)
            for m in self.docs.values()
            if self._visible(m, scope, viewer, now) and m.source.ref == ref and (kinds is None or m.kind in kinds)
        ]

    async def list_by_viewer(self, scope: Scope, viewer: str, kinds: Iterable[str] | None = None) -> list[Memory]:
        viewer, now, kinds = validate_viewer(viewer), self.now(), set(kinds) if kinds else None
        hits = [
            m for m in self.docs.values()
            if self._visible(m, scope, viewer, now) and (kinds is None or m.kind in kinds)
        ]
        return [self._public(m) for m in sorted(hits, key=lambda m: m.created_at)]

    # --- offers -----------------------------------------------------------------------

    async def get_offer(self, offer_id: str) -> Offer | None:
        o = self.offers.get(offer_id)
        return o.model_copy() if o else None

    async def insert_offer(self, offer: Offer) -> Offer:
        return self.offers.setdefault(offer.id, offer.model_copy()).model_copy()

    async def transition_offer(self, offer_id: str, from_status: OfferStatus, to_status: OfferStatus) -> bool:
        o = self.offers.get(offer_id)
        if o is None or o.status != from_status:
            return False
        o.status = to_status
        return True

    async def action_used(self, scope: Scope, action: str) -> bool:
        return any(
            o.case_id == scope.case_id and o.team_id == scope.team_id and o.action == action and o.status == "used"
            for o in self.offers.values()
        )

    # --- accusations / audit ------------------------------------------------------------

    async def record_accusation(self, scope: Scope, result: dict[str, Any]) -> bool:
        key = (scope.case_id, scope.team_id)
        if key in self.accusations:
            return False
        self.accusations[key] = copy.deepcopy(result)
        return True

    async def get_accusation(self, scope: Scope) -> dict[str, Any] | None:
        r = self.accusations.get((scope.case_id, scope.team_id))
        return copy.deepcopy(r) if r is not None else None

    async def log_audit(self, event: AuditEvent) -> None:
        if all(e.id != event.id for e in self.audit):
            self.audit.append(event)

    # --- cases ---------------------------------------------------------------------

    async def get_case(self, case_id: str) -> dict[str, Any] | None:
        case = self.cases.get(case_id)
        if case is None:
            return None
        out = copy.deepcopy({k: v for k, v in case.items() if k != "truth"})
        for s in out.get("seed_memories", []):
            s.pop("embedding", None)
        return out

    async def get_truth(self, case_id: str) -> dict[str, Any] | None:
        case = self.cases.get(case_id)
        return copy.deepcopy(case.get("truth")) if case else None

    # --- governance ---------------------------------------------------------------

    async def supersede(self, scope: Scope, old_id: str, new: NewMemory, reason: str) -> Memory:
        old = self.docs.get(old_id)
        if old is None or old.scope != scope:
            raise KeyError(f"memory {old_id} not found in this world")
        new_id = _ids.replacement_memory_id(old_id)
        if old.status == "superseded":
            if old.superseded_by == new_id and new_id in self.docs:
                return self._public(self.docs[new_id])  # retry
            raise ValueError(f"memory {old_id} is already superseded")
        emb = new.embedding or (await self._embedder.embed_documents([new.text]))[0]
        now = self.now()
        replacement = Memory(
            _id=new_id,
            case_id=old.case_id,
            team_id=old.team_id,
            text=new.text,
            known_by=new.known_by,
            kind=new.kind,
            source=new.source,
            confidence=new.confidence,
            unlocks=[],  # a lead dies with the memory that carried it
            status="active",
            supersedes=old_id,
            created_at=now,
            expires_at=new.expires_at or self._policy.expires_at(new.kind, now, self._rumour_ttl),
            embedding=emb,
        )
        self.docs[new_id] = replacement
        old.status = "superseded"
        old.superseded_by = new_id
        self._audit("supersede", old.case_id, old.team_id, [old_id, new_id], reason=reason)
        return self._public(replacement)

    async def supersede_by_ref(
        self, case_id: str, ref: str, replacement_text: str, event_id: str, reason: str
    ) -> list[Memory]:
        now = self.now()
        targets = [
            m
            for m in list(self.docs.values())
            if m.case_id == case_id and m.source.ref == ref and m.status == "active" and m.expires_at > now
        ]
        out = []
        for old in targets:
            out.append(
                await self.supersede(
                    old.scope,
                    old.id,
                    NewMemory(
                        text=replacement_text,
                        known_by=old.known_by,
                        kind=old.kind,
                        source={"type": "event", "ref": event_id},
                        confidence=old.confidence,
                    ),
                    reason,
                )
            )
        return out

    async def forget(self, scope: Scope, viewer: str | None = None) -> int:
        if viewer is not None:
            validate_viewer(viewer)
        doomed = [
            i
            for i, m in self.docs.items()
            if m.scope == scope and (viewer is None or viewer in m.known_by)
        ]
        for i in doomed:
            del self.docs[i]
        self._audit("forget", scope.case_id, scope.team_id, [], count=len(doomed))
        return len(doomed)


class FakeTraceStore:
    """In-memory implementation of TraceStoreAPI. Same state rules as TraceStore."""

    def __init__(self) -> None:
        self.traces: dict[str, Trace] = {}

    def _get(self, scope: Scope, turn_id: str) -> Trace | None:
        t = self.traces.get(_check_turn_id(turn_id))
        return t if t is not None and t.scope == scope else None

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
        existing = self.traces.get(_check_turn_id(turn_id))
        if existing is not None:
            if existing.scope != scope:
                raise TraceConflict(f"turn {turn_id} belongs to another world")
            return existing.model_copy()
        t = Trace(
            _id=turn_id, case_id=scope.case_id, team_id=scope.team_id, actor=validate_viewer(actor),
            command=command, input=input, engine_mode=engine_mode, replay_of=replay_of,
            masked=list(masked), ts=utcnow(),
        )
        self.traces[turn_id] = t
        return t.model_copy()

    async def get_trace(self, scope: Scope, turn_id: str) -> Trace | None:
        t = self._get(scope, turn_id)
        return t.model_copy() if t else None

    async def add_retrieved(self, scope: Scope, turn_id: str, memory_ids: Iterable[str]) -> None:
        t = self._get(scope, turn_id)
        if t is not None and t.status == "pending":
            t.retrieved += [i for i in memory_ids if i not in t.retrieved]

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
        t = self._get(scope, turn_id)
        if t is None:
            raise TraceConflict(f"turn {turn_id} not found; call begin_turn first")
        if t.status == "complete":
            return t.model_copy()
        if t.status != "pending":
            raise TraceConflict(f"turn {turn_id} is {t.status}")
        fields = _completion_fields(
            decision=decision, reply=reply, written=written, offer_id=offer_id, offer_label=offer_label,
            offer_rejected_reason=offer_rejected_reason, model=model, prompt_version=prompt_version,
            execution_id=execution_id,
        )
        self.traces[turn_id] = t = Trace.model_validate(t.to_doc() | fields)
        return t.model_copy()

    async def reject_turn(self, scope: Scope, turn_id: str, reason: str) -> Trace:
        t = self._get(scope, turn_id)
        if t is None:
            raise TraceConflict(f"turn {turn_id} not found")
        if t.status == "rejected":
            return t.model_copy()
        if t.status != "pending":
            raise TraceConflict(f"turn {turn_id} is {t.status}")
        t.status, t.rejected_reason = "rejected", reason
        return t.model_copy()

    async def set_voice(self, scope: Scope, turn_id: str, voice: dict[str, Any]) -> None:
        t = self._get(scope, turn_id)
        if t is not None:
            t.voice = voice
