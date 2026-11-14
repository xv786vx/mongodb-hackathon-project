"""One suspect turn, the same path for every engine mode. Posts only from a complete trace (rule 12)."""
from __future__ import annotations

import asyncio
import logging

import discord

from bot.config import Settings
from bot.engine_api import Engine
from bot import invoke as ae
from bot.identity import Caller
from bot.render import reply_embed
from bot.views import offer_view

log = logging.getLogger("bot.turns")


async def generate(cfg: Settings, engine: Engine, prepared) -> None:
    """Run the agent for one prepared turn. The trace is the output, never the return value."""
    if cfg.engine_mode != "hosted":
        await asyncio.to_thread(engine.generate, prepared)         # fake / local
        return
    try:
        await ae.invoke(cfg, prepared)
    except ae.InvokeError as e:
        if not e.retryable:
            raise
        log.warning("turn %s: %s; retrying once with the same turn id", prepared.turn_id, e)
        await ae.invoke(cfg, prepared)                              # same turn id: submit_reply is idempotent
_locks: dict[tuple[str, str], asyncio.Lock] = {}


def _lock(team_id: str, suspect_id: str) -> asyncio.Lock:
    return _locks.setdefault((team_id, suspect_id), asyncio.Lock())


async def run_turn(interaction: discord.Interaction, cfg: Settings, engine: Engine, caller: Caller,
                   command: str, suspect_id: str, question: str, evidence_id: str | None = None) -> None:
    """Identity must already be resolved (so refusals can be ephemeral) before this defers."""
    await interaction.response.defer(thinking=True)
    names = {s.id: s.name for s in engine.case_info().suspects}
    if suspect_id not in names:
        await interaction.followup.send("No such suspect.", ephemeral=True)
        return
    if await asyncio.to_thread(engine.is_eliminated, caller.team_id):
        await interaction.followup.send("Your team has been knocked out and can no longer question suspects.",
                                        ephemeral=True)
        return

    turn_id = f"t_{interaction.id}"                              # rule 16
    async with _lock(caller.team_id, suspect_id):               # one turn at a time per (team, suspect)
        recent = await asyncio.to_thread(engine.recent_turns, caller.team_id, suspect_id)
        if command == "present":                                 # correction runs before the agent (rule 11)
            prep = await asyncio.to_thread(engine.prepare_present, caller.team_id, suspect_id,
                                           evidence_id or "", question, recent)
        else:
            prep = await asyncio.to_thread(engine.prepare_interrogate, caller.team_id, suspect_id,
                                           question, recent)
        if not prep.ok:                                          # do not invoke the agent
            await interaction.followup.send(prep.error or "That can't be done right now.", ephemeral=True)
            return

        prepared = await asyncio.to_thread(engine.begin_turn, caller.team_id, suspect_id, turn_id, command,
                                           question, prep)
        trace = await asyncio.to_thread(engine.get_trace, caller.team_id, turn_id)
        if trace is None or trace.status != "complete":
            try:
                await generate(cfg, engine, prepared)
            except ae.InvokeError as e:
                log.warning("turn %s failed: %s", turn_id, e)      # the trace decides what happens next
            trace = await asyncio.to_thread(engine.get_trace, caller.team_id, turn_id)

    if trace is None or trace.status != "complete":
        log.warning("turn %s not complete", turn_id)
        await interaction.followup.send("The suspect says nothing. Try asking again.")
        return

    kwargs = {"embed": reply_embed(trace, names[suspect_id])}
    if trace.offer_id:
        kwargs["view"] = offer_view(trace.offer_id, trace.offer_label or "Take this lead")
    await interaction.followup.send(**kwargs)
    # Phase 2: attach the voice clip here with edit_original_response(attachments=[...]).
