"""The only module that touches the `memories` collection (rule 1).

Every read is scoped by (case_id, team_id) and one viewer, and filtered to active, unexpired memories,
both inside $vectorSearch and again in a $match against the live documents (rule 2).
Text is never edited; corrections go through supersede() (rule 3). Every write sets expires_at (rule 4).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Iterable, Protocol

from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import BulkWriteError, DuplicateKeyError

from . import ids
from .config import CASES, MEMORIES, VECTOR_INDEX, Settings
from .embeddings import Embedder, embedder_from_settings
from .models import Memory, NewMemory, Scope, validate_viewer
from .policy import DEFAULT_POLICY, Policy, utcnow

log = logging.getLogger(__name__)

DUPLICATE_KEY = 11000


class MemoryStoreAPI(Protocol):
    """The contract. MemoryStore (Atlas) and FakeStore (in-memory) both implement it."""

    async def retrieve(
        self,
        scope: Scope,
        viewer: str,
        query: str,
        k: int = 5,
        kinds: Iterable[str] | None = None,
        mask: Iterable[str] | None = None,
    ) -> list[Memory]: ...

    async def fetch(self, scope: Scope, viewer: str, ids: Iterable[str]) -> list[Memory]: ...

    async def write(self, scope: Scope, memories: list[NewMemory], *, dedup: bool = True) -> list[Memory]: ...

    async def supersede(self, scope: Scope, old_id: str, new: NewMemory, reason: str) -> Memory: ...

    async def supersede_by_ref(
        self, case_id: str, ref: str, replacement_text: str, event_id: str, reason: str
    ) -> list[Memory]: ...

    async def forget(self, scope: Scope, viewer: str | None = None) -> int: ...

    async def seed_world(self, case_id: str, team_id: str) -> int: ...


def scope_filter(scope: Scope, viewer: str, now: datetime) -> dict[str, Any]:
    """The governance filter. Used by every read; never build a memories query without it."""
    return {
        "case_id": scope.case_id,
        "team_id": scope.team_id,
        "known_by": validate_viewer(viewer),
        "status": "active",
        "expires_at": {"$gt": now},
    }


def retrieve_pipeline(
    scope: Scope,
    viewer: str,
    query_vector: list[float],
    now: datetime,
    k: int = 5,
    kinds: Iterable[str] | None = None,
    mask: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    flt = scope_filter(scope, viewer, now)
    if kinds:
        flt["kind"] = {"$in": list(kinds)}
    match = dict(flt)
    mask = list(mask or [])
    if mask:
        # _id is not a vector index filter field, so masking happens in $match; over-fetch to compensate.
        match["_id"] = {"$nin": mask}
    limit = k + len(mask)
    return [
        {
            "$vectorSearch": {
                "index": VECTOR_INDEX,
                "path": "embedding",
                "queryVector": query_vector,
                "numCandidates": min(10_000, max(100, limit * 20)),
                "limit": limit,
                "filter": flt,
            }
        },
        # The vector index is eventually consistent; re-check against the live documents.
        {"$match": match},
        {"$limit": k},
        {"$addFields": {"score": {"$meta": "vectorSearchScore"}}},
        {"$project": {"embedding": 0}},
    ]


class MemoryStore:
    """Atlas-backed store. Construct with MemoryStore.from_settings(Settings.from_env())."""

    def __init__(
        self,
        db: AsyncDatabase,
        embedder: Embedder,
        policy: Policy = DEFAULT_POLICY,
        rumour_ttl_seconds: int | None = None,
        dedup_threshold: float = 0.97,
    ):
        self._db = db
        self._mem = db[MEMORIES]
        self._embedder = embedder
        self._policy = policy
        self._rumour_ttl = rumour_ttl_seconds
        self._dedup_threshold = dedup_threshold

    @classmethod
    def from_settings(
        cls, settings: Settings, client: AsyncMongoClient | None = None, embedder: Embedder | None = None
    ) -> "MemoryStore":
        client = client or AsyncMongoClient(settings.mongodb_uri, tz_aware=True, appname="mystery")
        return cls(
            client[settings.db_name],
            embedder or embedder_from_settings(settings),
            rumour_ttl_seconds=settings.rumour_ttl_seconds,
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
        qv = await self._embedder.embed_query(query)
        pipeline = retrieve_pipeline(scope, viewer, qv, utcnow(), k, kinds, mask)
        cursor = await self._mem.aggregate(pipeline)
        docs = await cursor.to_list()
        log.debug("retrieve %s/%s %s -> %s", scope.case_id, scope.team_id, viewer, [d["_id"] for d in docs])
        return [Memory.model_validate(d) for d in docs]

    async def fetch(self, scope: Scope, viewer: str, ids: Iterable[str]) -> list[Memory]:
        """By id, with the same governance filter. A superseded, expired or out-of-scope id is simply absent."""
        wanted = list(ids)
        if not wanted:
            return []
        flt = scope_filter(scope, viewer, utcnow()) | {"_id": {"$in": wanted}}
        docs = await self._mem.find(flt, {"embedding": 0}).to_list()
        by_id = {d["_id"]: d for d in docs}
        return [Memory.model_validate(by_id[i]) for i in wanted if i in by_id]

    # --- writes -----------------------------------------------------------------

    async def write(self, scope: Scope, memories: list[NewMemory], *, dedup: bool = True) -> list[Memory]:
        """Insert memories into one world. Returns full documents (without embeddings).

        Pass the returned memories into the next prompt directly; never write-then-retrieve (index lag).
        A memory with a deterministic `id` that already exists in this scope is returned as-is (retry-safe).
        Without an id, a near-duplicate held by the same viewers with the same kind gets its confidence
        raised instead of a second insert.
        """
        now = utcnow()
        missing = [m.text for m in memories if m.embedding is None]
        vectors = iter(await self._embedder.embed_documents(missing) if missing else [])

        out: list[Memory] = []
        for new in memories:
            embedding = new.embedding if new.embedding is not None else next(vectors)

            if new.id is None and dedup:
                dup = await self._find_duplicate(scope, new, embedding, now)
                if dup is not None:
                    conf = max(dup.confidence, new.confidence)
                    if conf != dup.confidence:
                        await self._mem.update_one({"_id": dup.id}, {"$set": {"confidence": conf}})
                    out.append(dup.model_copy(update={"confidence": conf, "score": None}))
                    continue

            mem = Memory(
                _id=new.id or ids.random_memory_id(),
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
                embedding=embedding,
            )
            try:
                await self._mem.insert_one(mem.to_doc())
            except DuplicateKeyError:
                existing = await self._mem.find_one(
                    {"_id": mem.id, "case_id": scope.case_id, "team_id": scope.team_id}, {"embedding": 0}
                )
                if existing is None:
                    raise ValueError(f"memory id {mem.id} already exists in another world") from None
                out.append(Memory.model_validate(existing))
                continue
            out.append(mem.model_copy(update={"embedding": None}))
        log.info("write %s/%s -> %s", scope.case_id, scope.team_id, [m.id for m in out])
        return out

    async def _find_duplicate(
        self, scope: Scope, new: NewMemory, embedding: list[float], now: datetime
    ) -> Memory | None:
        flt = scope_filter(scope, new.known_by[0], now) | {"kind": new.kind}
        pipeline = [
            {
                "$vectorSearch": {
                    "index": VECTOR_INDEX,
                    "path": "embedding",
                    "queryVector": embedding,
                    "numCandidates": 50,
                    "limit": 3,
                    "filter": flt,
                }
            },
            {"$match": flt | {"known_by": {"$all": new.known_by}}},
            {"$addFields": {"score": {"$meta": "vectorSearchScore"}}},
            {"$project": {"embedding": 0}},
        ]
        cursor = await self._mem.aggregate(pipeline)
        for d in await cursor.to_list():
            if d["score"] >= self._dedup_threshold:
                return Memory.model_validate(d)
        return None

    async def seed_world(self, case_id: str, team_id: str) -> int:
        """Copy the case's seed memories into one team's world. Embeddings are copied, not recomputed.

        Idempotent: ids are deterministic, so re-seeding inserts only what is missing. Returns the number inserted.
        """
        case = await self._db[CASES].find_one({"_id": case_id}, {"seed_memories": 1})
        if case is None:
            raise KeyError(f"case {case_id} not found; run scripts.seed first")
        now = utcnow()
        docs = []
        for s in case.get("seed_memories", []):
            if not s.get("id"):
                raise ValueError(f"seed memory without id in {case_id}: every seed memory needs an 'id'")
            if not s.get("embedding"):
                raise ValueError(f"seed memory {s['id']} has no embedding; run scripts.seed")
            kind = s["kind"]
            docs.append(
                Memory(
                    _id=ids.seed_memory_id(case_id, team_id, s["id"]),
                    case_id=case_id,
                    team_id=team_id,
                    text=s["text"],
                    known_by=[validate_viewer(v) for v in s["known_by"]],
                    kind=kind,
                    source={"type": "seed", "ref": s["id"]},
                    confidence=s.get("confidence", 1.0),
                    unlocks=s.get("unlocks", []),
                    status="active",
                    created_at=now,
                    expires_at=self._policy.expires_at(kind, now, self._rumour_ttl),
                    embedding=s["embedding"],
                ).to_doc()
            )
        if not docs:
            return 0
        try:
            res = await self._mem.insert_many(docs, ordered=False)
            inserted = len(res.inserted_ids)
        except BulkWriteError as e:
            if any(err["code"] != DUPLICATE_KEY for err in e.details["writeErrors"]):
                raise
            inserted = e.details["nInserted"]
        log.info("seed_world %s/%s inserted %d of %d", case_id, team_id, inserted, len(docs))
        return inserted

    # --- governance (Phase 2/3) ---------------------------------------------------

    async def supersede(self, scope: Scope, old_id: str, new: NewMemory, reason: str) -> Memory:
        """Replace an active memory in this world. One transaction:
        insert `new` (id = ids.replacement_memory_id(old_id), supersedes=old_id, unlocks never inherited),
        mark old status=superseded + superseded_by, write an audit event. Repeat call returns the same replacement.
        """
        raise NotImplementedError("Phase 2: supersede")

    async def supersede_by_ref(
        self, case_id: str, ref: str, replacement_text: str, event_id: str, reason: str
    ) -> list[Memory]:
        """World events only (rule 17). Supersede every active memory with source.ref == ref in every world.
        Replacements get source={type: event, ref: event_id} and no unlocks. Returns replacements (one per world/memory).
        """
        raise NotImplementedError("Phase 2: supersede_by_ref")

    async def forget(self, scope: Scope, viewer: str | None = None) -> int:
        """Hard-delete a world's memories (or one viewer's). Writes one audit event with the count."""
        raise NotImplementedError("Phase 3: forget")
