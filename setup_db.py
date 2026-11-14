"""Create collections, validator, indexes and the policy document. Idempotent: safe to run repeatedly.

    python setup_db.py                 # uses MONGODB_URI and DB_NAME from .env
    python setup_db.py --db mystery_test
    python setup_db.py --no-wait       # don't wait for the vector index to become READY
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "agents" / "suspect" / "src"))

from dotenv import load_dotenv
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.operations import SearchIndexModel

from mystery_core.memory.config import (
    ACCUSATIONS,
    AUDIT,
    CASES,
    MEMORIES,
    OFFERS,
    POLICIES,
    TRACES,
    VECTOR_INDEX,
    WORKER_STATE,
)
from mystery_core.memory.models import KINDS, MEMORY_STATUSES, SOURCE_TYPES
from mystery_core.memory.policy import DEFAULT_POLICY

VIEWER_PATTERN = r"^(suspect|team):[A-Za-z0-9_-]+$"


def memory_schema(dims: int) -> dict:
    """$jsonSchema for `memories`. Keep in sync with ARCHITECTURE.md section 3 and models.Memory.
    If an insert fails this, fix the caller. Do not loosen the validator."""
    return {
        "bsonType": "object",
        "required": [
            "_id", "case_id", "team_id", "text", "embedding", "known_by",
            "kind", "source", "status", "created_at", "expires_at",
        ],
        "properties": {
            "_id": {"bsonType": "string", "pattern": "^m_"},
            "case_id": {"bsonType": "string", "minLength": 1},
            "team_id": {"bsonType": "string", "minLength": 1},
            "text": {"bsonType": "string", "minLength": 1},
            "embedding": {
                "bsonType": "array",
                "minItems": dims,
                "maxItems": dims,
                "items": {"bsonType": "double"},
            },
            "known_by": {
                "bsonType": "array",
                "minItems": 1,
                "items": {"bsonType": "string", "pattern": VIEWER_PATTERN},
            },
            "kind": {"enum": list(KINDS)},
            "source": {
                "bsonType": "object",
                "required": ["type"],
                "properties": {
                    "type": {"enum": list(SOURCE_TYPES)},
                    "ref": {"bsonType": ["string", "null"]},
                },
            },
            "confidence": {"bsonType": ["double", "int"], "minimum": 0, "maximum": 1},
            "unlocks": {"bsonType": "array", "items": {"bsonType": "string"}},
            "status": {"enum": list(MEMORY_STATUSES)},
            "supersedes": {"bsonType": ["string", "null"]},
            "superseded_by": {"bsonType": ["string", "null"]},
            "created_at": {"bsonType": "date"},
            "expires_at": {"bsonType": "date"},
        },
    }


def vector_index_definition(dims: int) -> dict:
    filters = ["case_id", "team_id", "known_by", "status", "kind", "expires_at"]
    return {
        "fields": [
            {"type": "vector", "path": "embedding", "numDimensions": dims, "similarity": "cosine"},
            *({"type": "filter", "path": p} for p in filters),
        ]
    }


def ensure_memories(db: Database, dims: int) -> None:
    opts = {
        "validator": {"$jsonSchema": memory_schema(dims)},
        "validationLevel": "strict",
        "validationAction": "error",
        "changeStreamPreAndPostImages": {"enabled": True},
    }
    if MEMORIES in db.list_collection_names():
        db.command("collMod", MEMORIES, **opts)
        print(f"  {MEMORIES}: updated validator and pre-images")
    else:
        db.create_collection(MEMORIES, **opts)
        print(f"  {MEMORIES}: created")


def ensure_plain(db: Database, names: list[str]) -> None:
    existing = set(db.list_collection_names())
    for name in names:
        if name not in existing:
            db.create_collection(name)
            print(f"  {name}: created")


# Index options that matter when deciding whether an existing index is "the same".
_COMPARED_OPTIONS = ("expireAfterSeconds", "unique", "sparse", "partialFilterExpression")


def ensure_index(coll: Collection, keys: list[tuple[str, int]], name: str, **opts) -> None:
    """Create the index, replacing any existing index on the same keys that has a different name or options.

    MongoDB refuses a second index on identical keys, so an index created by hand (e.g. `expires_at_1` without TTL
    from Data Explorer) would otherwise make setup fail."""
    wanted_key = dict(keys)
    for existing in coll.list_indexes():
        if existing["name"] == "_id_" or dict(existing["key"]) != wanted_key:
            continue
        same_opts = all(existing.get(o) == opts.get(o) for o in _COMPARED_OPTIONS)
        if existing["name"] == name and same_opts:
            return
        coll.drop_index(existing["name"])
        print(f"  {coll.name}: dropped index {existing['name']} (replaced by {name})")
    coll.create_index(keys, name=name, **opts)


def ensure_indexes(db: Database) -> None:
    mem = db[MEMORIES]
    ensure_index(mem, [("expires_at", ASCENDING)], "ttl_expires_at", expireAfterSeconds=0)
    ensure_index(
        mem,
        [("case_id", ASCENDING), ("team_id", ASCENDING), ("known_by", ASCENDING), ("status", ASCENDING)],
        "scope_viewer_status",
    )
    ensure_index(mem, [("case_id", ASCENDING), ("source.ref", ASCENDING), ("status", ASCENDING)], "case_ref_status")
    ensure_index(db[TRACES], [("case_id", ASCENDING), ("team_id", ASCENDING), ("ts", DESCENDING)], "scope_ts")
    ensure_index(
        db[OFFERS],
        [("case_id", ASCENDING), ("team_id", ASCENDING), ("action", ASCENDING), ("status", ASCENDING)],
        "scope_action_status",
    )
    ensure_index(db[AUDIT], [("case_id", ASCENDING), ("ts", DESCENDING)], "case_ts")
    print("  regular + TTL indexes: ok")


def _field_set(defn: dict) -> set:
    return {tuple(sorted(f.items())) for f in defn.get("fields", [])}


def ensure_vector_index(db: Database, dims: int, wait: bool, timeout_s: int = 600) -> None:
    mem = db[MEMORIES]
    definition = vector_index_definition(dims)
    existing = list(mem.list_search_indexes(VECTOR_INDEX))
    if not existing:
        mem.create_search_index(SearchIndexModel(definition=definition, name=VECTOR_INDEX, type="vectorSearch"))
        print(f"  {VECTOR_INDEX}: creating")
    elif _field_set(existing[0].get("latestDefinition", {})) != _field_set(definition):
        mem.update_search_index(VECTOR_INDEX, definition)
        print(f"  {VECTOR_INDEX}: definition changed, updating (re-seed if numDimensions changed)")
    else:
        print(f"  {VECTOR_INDEX}: definition up to date")

    if not wait:
        return
    deadline = time.monotonic() + timeout_s
    while True:
        idx = list(mem.list_search_indexes(VECTOR_INDEX))[0]
        status, queryable = idx.get("status"), idx.get("queryable")
        if status == "READY" and queryable:
            print(f"  {VECTOR_INDEX}: READY")
            return
        if status == "FAILED":
            raise RuntimeError(f"{VECTOR_INDEX} failed to build: {idx}")
        if time.monotonic() > deadline:
            raise TimeoutError(f"{VECTOR_INDEX} still {status} after {timeout_s}s")
        print(f"  {VECTOR_INDEX}: {status}, waiting...")
        time.sleep(10)


def main() -> None:
    load_dotenv(Path(__file__).parent / ".env")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=os.environ.get("DB_NAME"), help="database name (default: DB_NAME)")
    ap.add_argument("--no-wait", action="store_true", help="don't wait for the vector index")
    args = ap.parse_args()
    if not args.db:
        sys.exit("DB_NAME is not set and --db was not given")
    dims = int(os.environ.get("EMBEDDING_DIMS", "1024"))

    client = MongoClient(os.environ["MONGODB_URI"], tz_aware=True, appname="mystery-setup")
    db = client[args.db]
    print(f"Setting up {args.db} (EMBEDDING_DIMS={dims})")
    ensure_memories(db, dims)
    ensure_plain(db, [CASES, POLICIES, TRACES, OFFERS, AUDIT, WORKER_STATE, ACCUSATIONS])
    ensure_indexes(db)
    db[POLICIES].replace_one({"_id": DEFAULT_POLICY.id}, DEFAULT_POLICY.to_doc(), upsert=True)
    print(f"  policy {DEFAULT_POLICY.id}: upserted")
    ensure_vector_index(db, dims, wait=not args.no_wait)
    print("Done.")


if __name__ == "__main__":
    main()
