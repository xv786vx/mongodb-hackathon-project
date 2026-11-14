"""Load a case, embed its seed memories once, and seed teams' worlds.

    python -m scripts.seed cases/manor.json                  # load case only
    python -m scripts.seed cases/manor.json --teams A B      # and seed worlds for teams A and B

Embeddings are stored on the case's seed_memories, so seed_world copies them instead of re-embedding.
Re-running is safe: the case is replaced, already-seeded memories are skipped.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from dotenv import load_dotenv
from pymongo import AsyncMongoClient

from mystery_core.memory.config import CASES, Settings
from mystery_core.memory.embeddings import embedder_from_settings
from mystery_core.memory.models import KINDS, validate_viewer
from mystery_core.memory.store import MemoryStore


def validate_case(case: dict) -> None:
    if not str(case.get("_id", "")).startswith("case_"):
        raise ValueError("case _id must start with 'case_'")
    seen = set()
    for s in case.get("seed_memories", []):
        sid = s.get("id")
        if not sid:
            raise ValueError(f"seed memory without id: {s.get('text', '')[:40]!r}")
        if sid in seen:
            raise ValueError(f"duplicate seed memory id {sid}")
        seen.add(sid)
        if s.get("kind") not in KINDS:
            raise ValueError(f"seed {sid}: kind {s.get('kind')!r} not in {KINDS}")
        if not s.get("known_by"):
            raise ValueError(f"seed {sid}: known_by is empty")
        for v in s["known_by"]:
            validate_viewer(v)


async def main() -> None:
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("case_file", type=Path)
    ap.add_argument("--teams", nargs="*", default=[], help="team ids whose worlds to seed")
    args = ap.parse_args()

    settings = Settings.from_env()
    case = json.loads(args.case_file.read_text())
    validate_case(case)

    embedder = embedder_from_settings(settings)
    seeds = case.get("seed_memories", [])
    vectors = await embedder.embed_documents([s["text"] for s in seeds])
    for s, v in zip(seeds, vectors):
        s["embedding"] = v
    print(f"Embedded {len(seeds)} seed memories with {settings.embedding_model} ({settings.embedding_dims} dims)")

    client = AsyncMongoClient(settings.mongodb_uri, tz_aware=True, appname="mystery-seed")
    try:
        db = client[settings.db_name]
        await db[CASES].replace_one({"_id": case["_id"]}, case, upsert=True)
        print(f"Loaded {case['_id']} into {settings.db_name}.{CASES}")

        store = MemoryStore.from_settings(settings, client=client, embedder=embedder)
        for team in args.teams:
            n = await store.seed_world(case["_id"], team)
            print(f"Seeded world {case['_id']}/{team}: {n} new memories")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
