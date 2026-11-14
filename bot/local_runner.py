"""ENGINE_MODE=local fallback. Same tools, preparation and validation; direct model calls."""

async def run(prepared, *, runtime=None, model=None):
    # Avoid importing graph (and its hosted app.run) into the standalone bot process.
    from suspect_agent.local import run_turn
    return await run_turn(prepared, runtime=runtime, model=model)
