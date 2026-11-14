"""Embed a case's seed memories and seed teams' worlds.

    # Atlas is the source of truth (case edited in Data Explorer): only sets seed_memories.$.embedding
    python -m scripts.seed --case case_manor --teams A B

    # Load a case from a JSON file (refuses to overwrite an existing case unless --replace)
    python -m scripts.seed --file cases/manor.json --teams A B
    python -m scripts.seed --file cases/manor.json --replace

    --reembed   recompute embeddings even for seeds that already have one (after changing EMBEDDING_MODEL)

Re-running is safe: already-seeded memories are skipped (deterministic ids).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from dotenv import load_dotenv
from pymongo import AsyncMongoClient

from mystery_core.memory.config import CASES, Settings
from mystery_core.memory.embeddings import embedder_from_settings
from mystery_core.memory.store import MemoryStore, check_seed_memories


async def embed_in_place(db, embedder, case_id: str, reembed: bool) -> int:
    """Set seed_memories.$.embedding for each seed (matched by id). Touches no other field of the case."""
    case = await db[CASES].find_one({"_id": case_id}, {"seed_memories": 1})
    if case is None:
        sys.exit(f"case {case_id} not found in {db.name}.{CASES}")
    seeds = case.get("seed_memories", [])
    check_seed_memories(case_id, seeds)
    todo = [s for s in seeds if reembed or not s.get("embedding")]
    if not todo:
        return 0
    vectors = await embedder.embed_documents([s["text"] for s in todo])
    for s, v in zip(todo, vectors):
        await db[CASES].update_one(
            {"_id": case_id, "seed_memories.id": s["id"]}, {"$set": {"seed_memories.$.embedding": v}}
        )
    return len(todo)


async def main() -> None:
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--case", help="case _id already in Atlas")
    src.add_argument("--file", type=Path, help="case JSON file to load")
    ap.add_argument("--replace", action="store_true", help="with --file: overwrite an existing case document")
    ap.add_argument("--reembed", action="store_true", help="recompute existing embeddings")
    ap.add_argument("--teams", nargs="*", default=[], help="team ids whose worlds to seed")
    args = ap.parse_args()

    settings = Settings.from_env()
    embedder = embedder_from_settings(settings)
    client = AsyncMongoClient(settings.mongodb_uri, tz_aware=True, appname="mystery-seed")
    try:
        db = client[settings.db_name]
        if args.file:
            case = json.loads(args.file.read_text())
            case_id = case.get("_id", "")
            if not str(case_id).startswith("case_"):
                sys.exit("case _id must start with 'case_'")
            check_seed_memories(case_id, case.get("seed_memories", []))
            if await db[CASES].find_one({"_id": case_id}, {"_id": 1}) and not args.replace:
                sys.exit(
                    f"{case_id} already exists in {db.name}.{CASES}. Use --case {case_id} to embed in place, "
                    "or --replace to overwrite it (loses edits made in Atlas)."
                )
            await db[CASES].replace_one({"_id": case_id}, case, upsert=True)
            print(f"Loaded {case_id} from {args.file}")
        else:
            case_id = args.case

        n = await embed_in_place(db, embedder, case_id, args.reembed)
        print(f"Embedded {n} seed memories with {settings.embedding_model} ({settings.embedding_dims} dims)")

        store = await MemoryStore.open(settings, client=client, embedder=embedder)
        for team in args.teams:
            print(f"Seeded world {case_id}/{team}: {await store.seed_world(case_id, team)} new memories")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
