"""One suspect turn, the same path for every engine mode. Posts only from a complete trace (rule 12)."""
from __future__ import annotations

import asyncio
import logging

import discord

from bot.config import Settings
from bot.engine_api import Engine
from bot.identity import Caller
from bot.render import reply_embed
from bot.views import offer_view

log = logging.getLogger("bot.turns")
_locks: dict[tuple[str, str], asyncio.Lock] = {}


def _lock(team_id: str, suspect_id: str) -> asyncio.Lock:
    return _locks.setdefault((team_id, suspect_id), asyncio.Lock())


async def run_turn(interaction: discord.Interaction, cfg: Settings, engine: Engine, caller: Caller,
                   command: str, suspect_id: str, question: str) -> None:
    """Identity must already be resolved (so refusals can be ephemeral) before this defers."""
    await interaction.response.defer(thinking=True)
    names = dict(await asyncio.to_thread(engine.suspects, cfg.case_id))
    if suspect_id not in names:
        await interaction.followup.send("No such suspect.", ephemeral=True)
        return

    turn_id = f"t_{interaction.id}"                              # rule 16
    async with _lock(caller.team_id, suspect_id):               # one turn at a time per (team, suspect)
        prepared = await asyncio.to_thread(engine.begin_turn, cfg.case_id, caller.team_id, suspect_id,
                                           turn_id, command, question)
        trace = await asyncio.to_thread(engine.get_trace, turn_id)
        if trace is None or trace.status != "complete":
            # hosted mode: invoke.py goes here. fake/local: the engine generates in-process.
            await asyncio.to_thread(engine.generate, prepared)
            trace = await asyncio.to_thread(engine.get_trace, turn_id)

    if trace is None or trace.status != "complete":
        log.warning("turn %s not complete", turn_id)
        await interaction.followup.send("The suspect says nothing. Try asking again.")
        return

    kwargs = {"embed": reply_embed(trace, names[suspect_id])}
    if trace.offer_id:
        kwargs["view"] = offer_view(trace.offer_id, trace.offer_label or "Take this lead")
    await interaction.followup.send(**kwargs)
    # Phase 2: attach the voice clip here with edit_original_response(attachments=[...]).
