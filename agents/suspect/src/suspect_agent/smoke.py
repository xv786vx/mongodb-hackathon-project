"""Run a scoped local or hosted smoke turn. Prints identifiers, never credentials or private clues."""

import argparse
import asyncio
import json
import uuid
from dotenv import load_dotenv

from mystery_core.engine.context import TurnContext
from mystery_core.memory.models import Scope
from .runtime import Runtime


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--mode", choices=["local", "hosted"], default="hosted")
    parser.add_argument("--case", default="case_manor")
    parser.add_argument("--team", default="A")
    parser.add_argument("--suspect", default="butler")
    parser.add_argument("--turn-id", default="t_smoke_" + uuid.uuid4().hex)
    parser.add_argument("--question", default="Where were you after midnight?")
    args = parser.parse_args()
    load_dotenv(args.env_file)
    runtime = await Runtime.open()
    try:
        ctx = TurnContext(Scope(case_id=args.case, team_id=args.team), args.suspect, args.turn_id, "interrogate")
        await runtime.traces.begin_turn(ctx.scope, ctx.turn_id, ctx.viewer, ctx.command, args.question, engine_mode=args.mode)
        prepared = await runtime.turns.prepare(ctx)
        if args.mode == "hosted":
            from .client import invoke
            await invoke(prepared)
        else:
            from .local import run_turn
            await run_turn(prepared, runtime=runtime)
        trace = await runtime.traces.get_trace(ctx.scope, ctx.turn_id)
        if trace is None or trace.status != "complete" or not trace.retrieved:
            raise RuntimeError("smoke failed: expected a complete scoped trace with retrieved clue IDs")
        print(json.dumps({"trace_id": trace.id, "status": trace.status, "retrieved": trace.retrieved,
                          "written": trace.written, "offer_id": trace.offer_id}))
    finally:
        await runtime.client.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        # Provider exceptions can echo part of a key or request payload. Keep terminal/log output generic.
        print("Smoke test failed:", type(exc).__name__, "(check configuration and the scoped trace)")
        raise SystemExit(1) from None
