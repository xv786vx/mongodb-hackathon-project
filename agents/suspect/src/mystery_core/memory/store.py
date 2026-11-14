"""The only module that touches the `memories` collection (rule 1).

Every read is scoped by (case_id, team_id) and one viewer, and filtered to active, unexpired memories,
both inside $vectorSearch and again in a $match against the live documents (rule 2).
Text is never edited; corrections go through supersede() (rule 3). Every write sets expires_at (rule 4).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Awaitable, Callable, Iterable, Protocol

from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import BulkWriteError, DuplicateKeyError

from . import ids
from .config import (
    ACCUSATIONS,
    AUDIT,
    CASES,
    MEMORIES,
    OFFERS,
    POLICIES,
    VECTOR_INDEX,
    WORKER_STATE,
    Settings,
)
from .embeddings import Embedder, embedder_from_settings
from .models import KINDS, AuditEvent, ExpiryNotice, Memory, NewMemory, Offer, OfferStatus, Scope, validate_viewer
from .policy import DEFAULT_POLICY, POLICY_ID, Policy, utcnow

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

    async def count_superseded(self, scope: Scope, viewer: str, kind: str = "cover_story") -> int: ...

    async def get_case(self, case_id: str) -> dict[str, Any] | None: ...

    async def get_truth(self, case_id: str) -> dict[str, Any] | None: ...

    async def find_by_ref(
        self, scope: Scope, viewer: str, ref: str, kinds: Iterable[str] | None = None
    ) -> list[Memory]: ...

    async def list_by_viewer(self, scope: Scope, viewer: str, kinds: Iterable[str] | None = None) -> list[Memory]: ...

    async def get_offer(self, offer_id: str) -> Offer | None: ...

    async def insert_offer(self, offer: Offer) -> Offer: ...

    async def transition_offer(self, offer_id: str, from_status: OfferStatus, to_status: OfferStatus) -> bool: ...

    async def action_used(self, scope: Scope, action: str) -> bool: ...

    async def record_accusation(self, scope: Scope, result: dict[str, Any]) -> bool: ...

    async def get_accusation(self, scope: Scope) -> dict[str, Any] | None: ...

    async def log_audit(self, event: AuditEvent) -> None: ...

    @property
    def policy(self) -> Policy: ...


def check_seed_memories(case_id: str, seeds: list[dict[str, Any]]) -> None:
    """Every seed memory needs id, text, kind (a known kind) and known_by (valid viewers). Fail loudly, never default."""
    problems = []
    seen: set[str] = set()
    for n, s in enumerate(seeds):
        label = s.get("id") or f"seed_memories[{n}]"
        if not s.get("id"):
            problems.append(f"{label}: missing id")
        elif s["id"] in seen:
            problems.append(f"{label}: duplicate id")
        else:
            seen.add(s["id"])
        if not s.get("text"):
            problems.append(f"{label}: missing text")
        if s.get("kind") not in KINDS:
            problems.append(f"{label}: kind {s.get('kind')!r} is not one of {list(KINDS)}")
        if not s.get("known_by"):
            problems.append(f"{label}: known_by is empty")
        else:
            for v in s["known_by"]:
                try:
                    validate_viewer(v)
                except ValueError as e:
                    problems.append(f"{label}: {e}")
    if problems:
        raise ValueError(f"case {case_id} has invalid seed memories:\n  " + "\n  ".join(problems))


# Never leaves the database through get_case: truth (rule 5) and seed embeddings (large, useless to callers).
CASE_HIDDEN_FIELDS = {"truth": 0, "seed_memories.embedding": 0}

ExpiryCallback = Callable[[ExpiryNotice], Awaitable[None]]


def expiry_notice_from_change(change: dict[str, Any], now: datetime) -> ExpiryNotice | None:
    """Turn a change-stream delete event into a notice, or None if it wasn't an expiry.

    forget() also deletes; only documents whose expires_at has passed count as expired.
    Needs the pre-image (changeStreamPreAndPostImages, set by setup_db.py)."""
    pre = change.get("fullDocumentBeforeChange")
    if change.get("operationType") != "delete" or not pre:
        return None
    expires_at = pre.get("expires_at")
    if expires_at is None or expires_at > now:
        return None
    return ExpiryNotice(
        case_id=pre["case_id"], team_id=pre["team_id"], memory_id=pre["_id"], kind=pre["kind"]
    )


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
        """Sync constructor; uses DEFAULT_POLICY until load_policy() is awaited. Prefer `await MemoryStore.open(...)`."""
        client = client or AsyncMongoClient(settings.mongodb_uri, tz_aware=True, appname="mystery")
        return cls(
            client[settings.db_name],
            embedder or embedder_from_settings(settings),
            rumour_ttl_seconds=settings.rumour_ttl_seconds,
        )

    @classmethod
    async def open(
        cls, settings: Settings, client: AsyncMongoClient | None = None, embedder: Embedder | None = None
    ) -> "MemoryStore":
        """Construct and load the policy from the `policies` collection (policy as data)."""
        store = cls.from_settings(settings, client, embedder)
        await store.load_policy()
        return store

    @property
    def policy(self) -> Policy:
        return self._policy

    async def load_policy(self, policy_id: str = POLICY_ID) -> Policy:
        """Read the policy document. Falls back to DEFAULT_POLICY (with a warning) if it is missing."""
        doc = await self._db[POLICIES].find_one({"_id": policy_id})
        if doc is None:
            log.warning("policy %s not found in %s; using DEFAULT_POLICY (run setup_db.py)", policy_id, self._db.name)
            self._policy = DEFAULT_POLICY
        else:
            self._policy = Policy.model_validate(doc)
        return self._policy

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
        seeds = case.get("seed_memories", [])
        check_seed_memories(case_id, seeds)
        missing = [s["id"] for s in seeds if not s.get("embedding")]
        if missing:
            raise ValueError(f"seed memories {missing} have no embedding; run scripts.seed --embed-only {case_id}")
        now = utcnow()
        docs = []
        for s in seeds:
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

    async def count_superseded(self, scope: Scope, viewer: str, kind: str = "cover_story") -> int:
        """How many of this viewer's memories of `kind` have been superseded in this world.
        Used by the crack gate (rule 13) and by voice settings."""
        return await self._mem.count_documents(
            {
                "case_id": scope.case_id,
                "team_id": scope.team_id,
                "known_by": validate_viewer(viewer),
                "kind": kind,
                "status": "superseded",
            }
        )

    # --- cases ---------------------------------------------------------------------

    async def get_case(self, case_id: str) -> dict[str, Any] | None:
        """The case document without `truth` and without seed embeddings. None if unknown.
        Safe to cache per case_id; use this everywhere except engine/game.py."""
        return await self._db[CASES].find_one({"_id": case_id}, CASE_HIDDEN_FIELDS)

    async def get_truth(self, case_id: str) -> dict[str, Any] | None:
        """{killer, weapon, room} or None. Only engine/game.py may call this, to check an accusation (rule 5).
        Never put the result in a prompt, trace, log or message."""
        doc = await self._db[CASES].find_one({"_id": case_id}, {"truth": 1})
        return doc.get("truth") if doc else None

    # --- expiry notices (runs inside the bot process) -------------------------------

    async def watch_expiries(self, on_expired: ExpiryCallback, worker_id: str = "expiry_notices") -> None:
        """Run forever: for each TTL-expired memory, write an audit event and call on_expired(notice).

        Resumes from the stored token after a restart. The bot runs it as a task:
            asyncio.create_task(store.watch_expiries(post_notice))
        """
        state = self._db[WORKER_STATE]
        saved = await state.find_one({"_id": worker_id})
        pipeline = [{"$match": {"operationType": "delete"}}]
        async with await self._mem.watch(
            pipeline,
            full_document_before_change="whenAvailable",
            resume_after=saved["token"] if saved else None,
        ) as stream:
            async for change in stream:
                notice = expiry_notice_from_change(change, utcnow())
                if notice is not None:
                    # Deterministic id + $setOnInsert: replaying an event after a crash writes one audit row.
                    await self._db[AUDIT].update_one(
                        {"_id": f"au_expire_{notice.memory_id}"},
                        {
                            "$setOnInsert": {
                                "type": "expire",
                                "case_id": notice.case_id,
                                "team_id": notice.team_id,
                                "memory_ids": [notice.memory_id],
                                "ts": utcnow(),
                            }
                        },
                        upsert=True,
                    )
                    try:
                        await on_expired(notice)
                    except Exception:
                        log.exception("on_expired failed for %s", notice.memory_id)
                await state.replace_one(
                    {"_id": worker_id}, {"_id": worker_id, "token": stream.resume_token}, upsert=True
                )

    # --- governance ------------------------------------------------------------------

    async def supersede(self, scope: Scope, old_id: str, new: NewMemory, reason: str) -> Memory:
        """Replace an active memory in this world, in one transaction:
        mark old superseded (+ superseded_by), insert the replacement (supersedes=old_id, NO unlocks), write audit.

        Retry-safe: the replacement id is ids.replacement_memory_id(old_id), so a repeat returns the same replacement.
        Raises KeyError if old_id is not in this world, ValueError if it is already superseded by something else.
        """
        if new.embedding is None:
            new = new.model_copy(update={"embedding": (await self._embedder.embed_documents([new.text]))[0]})
        return await self._supersede(scope, old_id, new, reason)

    async def _supersede(self, scope: Scope, old_id: str, new: NewMemory, reason: str) -> Memory:
        new_id = ids.replacement_memory_id(old_id)
        now = utcnow()
        replacement = Memory(
            _id=new_id,
            case_id=scope.case_id,
            team_id=scope.team_id,
            text=new.text,
            known_by=new.known_by,
            kind=new.kind,
            source=new.source,
            confidence=new.confidence,
            unlocks=[],  # a lead dies with the memory that carried it (rule 15)
            status="active",
            supersedes=old_id,
            created_at=now,
            expires_at=new.expires_at or self._policy.expires_at(new.kind, now, self._rumour_ttl),
            embedding=new.embedding,
        )

        async def txn(session) -> bool:
            res = await self._mem.update_one(
                {"_id": old_id, "case_id": scope.case_id, "team_id": scope.team_id, "status": "active"},
                {"$set": {"status": "superseded", "superseded_by": new_id}},
                session=session,
            )
            if res.matched_count == 0:
                return False
            await self._mem.insert_one(replacement.to_doc(), session=session)
            await self._db[AUDIT].insert_one(
                AuditEvent(
                    _id=f"au_sup_{old_id}", type="supersede", case_id=scope.case_id, team_id=scope.team_id,
                    memory_ids=[old_id, new_id], reason=reason, ts=now,
                ).to_doc(),
                session=session,
            )
            return True

        async with self._db.client.start_session() as session:
            done = await session.with_transaction(txn)

        if not done:
            old = await self._mem.find_one(
                {"_id": old_id, "case_id": scope.case_id, "team_id": scope.team_id}, {"embedding": 0}
            )
            if old is None:
                raise KeyError(f"memory {old_id} not found in this world")
            if old.get("superseded_by") == new_id:
                existing = await self._mem.find_one({"_id": new_id}, {"embedding": 0})
                if existing is not None:
                    return Memory.model_validate(existing)  # retry
            raise ValueError(f"memory {old_id} is already {old.get('status')}")
        log.info("supersede %s/%s %s -> %s", scope.case_id, scope.team_id, old_id, new_id)
        return replacement.model_copy(update={"embedding": None})

    async def supersede_by_ref(
        self, case_id: str, ref: str, replacement_text: str, event_id: str, reason: str
    ) -> list[Memory]:
        """World events only; called from engine/events.py by a host command (rule 17).

        Supersedes every active memory in the case whose source.ref == ref, in every world that holds one.
        Replacements get source={type: event, ref: event_id} and no unlocks. Worlds already superseded are skipped,
        so a repeat returns []. Returns the replacements; their team_ids are the teams to notify.
        """
        targets = await self._mem.find(
            {"case_id": case_id, "source.ref": ref, "status": "active", "expires_at": {"$gt": utcnow()}},
            {"embedding": 0},
        ).to_list()
        if not targets:
            return []
        [embedding] = await self._embedder.embed_documents([replacement_text])
        out = []
        for doc in targets:
            old = Memory.model_validate(doc)
            new = NewMemory(
                text=replacement_text,
                known_by=old.known_by,
                kind=old.kind,
                source={"type": "event", "ref": event_id},
                confidence=old.confidence,
                embedding=embedding,
            )
            try:
                out.append(await self._supersede(old.scope, old.id, new, reason))
            except ValueError:
                continue  # superseded concurrently by something else; skip that world
        log.info("supersede_by_ref %s ref=%s event=%s -> %d", case_id, ref, event_id, len(out))
        return out

    async def forget(self, scope: Scope, viewer: str | None = None) -> int:
        """Hard-delete a world's memories (or only those one viewer holds). Writes one audit event with the count."""
        flt: dict[str, Any] = {"case_id": scope.case_id, "team_id": scope.team_id}
        if viewer is not None:
            flt["known_by"] = validate_viewer(viewer)
        res = await self._mem.delete_many(flt)
        await self.log_audit(
            AuditEvent(
                _id=f"au_forget_{ids.random_memory_id()[2:]}", type="forget", case_id=scope.case_id,
                team_id=scope.team_id, count=res.deleted_count, reason=viewer, ts=utcnow(),
            )
        )
        return res.deleted_count

    # --- engine reads (GameStore) -------------------------------------------------------

    async def find_by_ref(
        self, scope: Scope, viewer: str, ref: str, kinds: Iterable[str] | None = None
    ) -> list[Memory]:
        """Active memories in this world, held by viewer, whose source.ref == ref (e.g. the team's finding for ev_note)."""
        flt = scope_filter(scope, viewer, utcnow()) | {"source.ref": ref}
        if kinds:
            flt["kind"] = {"$in": list(kinds)}
        return [Memory.model_validate(d) for d in await self._mem.find(flt, {"embedding": 0}).to_list()]

    async def list_by_viewer(self, scope: Scope, viewer: str, kinds: Iterable[str] | None = None) -> list[Memory]:
        """All active memories this viewer holds in this world, oldest first (e.g. /present autocomplete)."""
        flt = scope_filter(scope, viewer, utcnow())
        if kinds:
            flt["kind"] = {"$in": list(kinds)}
        cursor = self._mem.find(flt, {"embedding": 0}).sort("created_at", 1)
        return [Memory.model_validate(d) for d in await cursor.to_list()]

    # --- offers -----------------------------------------------------------------------

    async def get_offer(self, offer_id: str) -> Offer | None:
        doc = await self._db[OFFERS].find_one({"_id": offer_id})
        return Offer.model_validate(doc) if doc else None

    async def insert_offer(self, offer: Offer) -> Offer:
        """Insert, or return the existing offer with the same _id (ids.offer_id is deterministic)."""
        try:
            await self._db[OFFERS].insert_one(offer.to_doc())
            return offer
        except DuplicateKeyError:
            existing = await self.get_offer(offer.id)
            assert existing is not None
            return existing

    async def transition_offer(self, offer_id: str, from_status: OfferStatus, to_status: OfferStatus) -> bool:
        """One atomic update. True only for the call that made the change (a double click performs the action once)."""
        res = await self._db[OFFERS].update_one(
            {"_id": offer_id, "status": from_status}, {"$set": {"status": to_status, "updated_at": utcnow()}}
        )
        return res.modified_count == 1

    async def action_used(self, scope: Scope, action: str) -> bool:
        return (
            await self._db[OFFERS].find_one(
                {"case_id": scope.case_id, "team_id": scope.team_id, "action": action, "status": "used"},
                {"_id": 1},
            )
            is not None
        )

    # --- accusations --------------------------------------------------------------------

    async def record_accusation(self, scope: Scope, result: dict[str, Any]) -> bool:
        """One accusation per team per case. True if this call recorded it, False if the team already accused."""
        try:
            await self._db[ACCUSATIONS].insert_one(
                {
                    "_id": f"acc_{scope.case_id}_{scope.team_id}",
                    "case_id": scope.case_id,
                    "team_id": scope.team_id,
                    "result": result,
                    "ts": utcnow(),
                }
            )
            return True
        except DuplicateKeyError:
            return False

    async def get_accusation(self, scope: Scope) -> dict[str, Any] | None:
        doc = await self._db[ACCUSATIONS].find_one({"_id": f"acc_{scope.case_id}_{scope.team_id}"})
        return doc.get("result") if doc else None

    # --- audit --------------------------------------------------------------------------

    async def log_audit(self, event: AuditEvent) -> None:
        """IDs only, never memory text. Idempotent on event.id."""
        doc = event.to_doc()
        await self._db[AUDIT].update_one({"_id": doc.pop("_id")}, {"$setOnInsert": doc}, upsert=True)
