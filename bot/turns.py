"""One suspect turn, the same path for every engine mode. Posts only from a complete trace (rule 12)."""
from __future__ import annotations

import asyncio
import io
import logging

import discord

from bot.config import Settings
from bot.engine_api import Engine
from bot import invoke as ae
from bot import tts
from bot.identity import Caller
from bot.render import reply_embed
from bot.views import offer_view
from mystery_core.voice import settings_for

log = logging.getLogger("bot.turns")


async def generate(cfg: Settings, engine: Engine, prepared) -> None:
    """Run the agent for one prepared turn. The trace is the output, never the return value."""
    if cfg.engine_mode != "hosted":
        await engine.generate(prepared)         # fake / local
        return
    try:
        await ae.invoke(cfg, prepared)
    except ae.InvokeError as e:
        if not e.retryable:
            raise
        log.warning("turn %s: %s; retrying once with the same turn id", prepared.turn_id, e)
        await ae.invoke(cfg, prepared)                              # same turn id: submit_reply is idempotent
_locks: dict[tuple[str, str], asyncio.Lock] = {}


def default_question(engine: Engine, command: str, question: str, evidence_id: str | None) -> str:
    """/present's question is optional, but the agent's memory search needs a non-empty input."""
    if question.strip() or command != "present":
        return question
    names = {e.id: e.name for e in engine.case_info().evidence}
    return f"What can you tell us about {names.get(evidence_id or '', 'this evidence')}?"


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
    if await engine.is_eliminated(caller.team_id):
        await interaction.followup.send("Your team has been knocked out and can no longer question suspects.",
                                        ephemeral=True)
        return

    question = default_question(engine, command, question, evidence_id)
    turn_id = f"t_{interaction.id}"                              # rule 16
    async with _lock(caller.team_id, suspect_id):               # one turn at a time per (team, suspect)
        recent = await engine.recent_turns(caller.team_id, suspect_id)
        if command == "present":                                 # correction runs before the agent (rule 11)
            prep = await engine.prepare_present(caller.team_id, suspect_id,
                                           evidence_id or "", question, recent)
        else:
            prep = await engine.prepare_interrogate(caller.team_id, suspect_id,
                                           question, recent)
        if not prep.ok:                                          # do not invoke the agent
            await interaction.followup.send(prep.error or "That can't be done right now.", ephemeral=True)
            return

        prepared = await engine.begin_turn(caller.team_id, suspect_id, turn_id, command,
                                           question, prep)
        trace = await engine.get_trace(caller.team_id, turn_id)
        if trace is None or trace.status != "complete":
            try:
                await generate(cfg, engine, prepared)
            except ae.InvokeError as e:
                log.warning("turn %s failed: %s", turn_id, e)      # the trace decides what happens next
            trace = await engine.get_trace(caller.team_id, turn_id)

    if trace is None or trace.status != "complete":
        log.warning("turn %s not complete", turn_id)
        await interaction.followup.send("The suspect says nothing. Try asking again.")
        return

    kwargs = {"embed": reply_embed(trace, names[suspect_id])}
    if trace.offer_id:
        kwargs["view"] = offer_view(trace.offer_id, trace.offer_label or "Take this lead")
    message = await interaction.followup.send(wait=True, **kwargs)      # text first
    await attach_clip(cfg, engine, caller, trace, suspect_id, message)  # then the clip


def voice_id_for(cfg: Settings, engine: Engine, suspect_id: str) -> str | None:
    for s in engine.case_info().suspects:
        if s.id == suspect_id and s.voice_id:
            return s.voice_id                                   # the case document wins
    return cfg.voice_ids.get(suspect_id)                         # local testing fallback


async def attach_clip(cfg, engine, caller, trace, suspect_id, message) -> None:
    """One clip per suspect reply, attached to the message in the team's private channel (rules 18, 20).

    Any failure is logged and swallowed: a voice problem never blocks or retracts the text.
    """
    voice_id = voice_id_for(cfg, engine, suspect_id)
    if not (cfg.elevenlabs_api_key and voice_id and trace.decision):
        return
    try:
        confidence, breaks = await engine.voice_inputs(caller.team_id, trace)
        settings = settings_for(trace.decision, confidence, breaks, cfg.voice_model)
        audio = await tts.speak(cfg, voice_id, trace.reply, trace.decision, settings)
        await message.edit(attachments=[discord.File(io.BytesIO(audio), filename="reply.mp3")])
        await engine.record_voice(caller.team_id, trace.turn_id, settings.to_trace(voice_id))
    except Exception as e:                                       # noqa: BLE001 - voice must never break a turn
        log.warning("turn %s: no clip (%s)", trace.turn_id, e.__class__.__name__)
