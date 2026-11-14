"""In-memory implementation of MemoryStoreAPI for unit tests and stub development.

Same governance semantics as MemoryStore (scope, viewer, status, expiry, no text edits, no inherited
unlocks), with cosine similarity in Python instead of $vectorSearch. Not for production.

    store = FakeStore(cases={"case_test": case_dict})
    await store.seed_world("case_test", "A")
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from . import ids as _ids
from .embeddings import Embedder, FakeEmbedder, cosine
from .models import AuditEvent, Memory, NewMemory, Scope, validate_viewer
from .policy import DEFAULT_POLICY, Policy, utcnow


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
