"""Milestone check: a paraphrased question retrieves a seeded memory, through the governed retrieve().

    python -m scripts.try_retrieve case_test A suspect:cook "did you hear anything on the stairs?"
"""

from __future__ import annotations

import argparse
import asyncio

from dotenv import load_dotenv

from mystery_core.memory import MemoryStore, Scope, Settings


async def main() -> None:
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("case_id")
    ap.add_argument("team_id")
    ap.add_argument("viewer", help="suspect:<id> or team:<id>")
    ap.add_argument("query")
    ap.add_argument("-k", type=int, default=5)
    args = ap.parse_args()

    store = await MemoryStore.open(Settings.from_env())
    hits = await store.retrieve(Scope(case_id=args.case_id, team_id=args.team_id), args.viewer, args.query, k=args.k)
    if not hits:
        print("No memories returned. (Is the vector index READY? Was this world seeded?)")
    for m in hits:
        print(f"{m.score:.3f}  {m.id}  [{m.kind}]  {m.text}")


if __name__ == "__main__":
    asyncio.run(main())
