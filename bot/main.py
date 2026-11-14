"""Discord entry point. Run: python -m bot.main"""
from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands

from bot import config
from bot.fake_engine import FakeEngine
from bot.identity import Refusal, require_host, resolve_team, role_ids_of
from bot.turns import run_turn
from bot.views import OfferButton

log = logging.getLogger("bot")
cfg = config.load()


def make_engine():
    if cfg.engine_mode == "fake":
        return FakeEngine()
    raise SystemExit(f"ENGINE_MODE={cfg.engine_mode} is not wired yet (phase 1). Use ENGINE_MODE=fake.")


engine = make_engine()
OfferButton.cfg, OfferButton.engine = cfg, engine


class Bot(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=discord.Intents.default())   # slash commands need no privileged intents
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        self.add_dynamic_items(OfferButton)                   # must run before connecting
        guild = discord.Object(id=cfg.guild_id)
        self.tree.copy_global_to(guild=guild)                 # guild sync: changes show up immediately
        await self.tree.sync(guild=guild)


bot = Bot()


async def refuse(interaction: discord.Interaction, r: Refusal) -> None:
    await interaction.response.send_message(str(r), ephemeral=True)


async def suspect_choices(interaction: discord.Interaction, current: str):
    names = await asyncio.to_thread(engine.suspects, cfg.case_id)
    cur = current.lower()
    return [app_commands.Choice(name=n, value=i) for i, n in names if cur in n.lower() or cur in i][:25]


@bot.tree.command(name="ping", description="Check that the bot sees your team")
async def ping(interaction: discord.Interaction) -> None:
    try:
        caller = resolve_team(cfg, role_ids_of(interaction), interaction.channel_id)
    except Refusal as r:
        return await refuse(interaction, r)
    await interaction.response.send_message(f"pong. Team {caller.team_id}, engine={cfg.engine_mode}.")


@bot.tree.command(name="interrogate", description="Question a suspect")
@app_commands.autocomplete(suspect=suspect_choices)
async def interrogate(interaction: discord.Interaction, suspect: str, question: str) -> None:
    try:
        caller = resolve_team(cfg, role_ids_of(interaction), interaction.channel_id)
    except Refusal as r:
        return await refuse(interaction, r)
    await run_turn(interaction, cfg, engine, caller, "interrogate", suspect, question)


@bot.tree.command(name="event", description="Host: trigger a world event")
async def event(interaction: discord.Interaction, event_id: str) -> None:
    try:
        require_host(cfg, role_ids_of(interaction))
    except Refusal as r:
        return await refuse(interaction, r)
    await interaction.response.defer(ephemeral=True)
    teams = await asyncio.to_thread(engine.trigger_event, cfg.case_id, event_id)
    for team in teams:                                       # only teams whose world held the memory
        ch = bot.get_channel(cfg.team_channels[team])
        if ch:
            await ch.send("Word has reached you: a witness has changed his story.")
    await interaction.followup.send(f"Event {event_id}: notified {teams or 'no teams'}.")


@bot.event
async def on_ready() -> None:
    log.info("online as %s (engine=%s)", bot.user, cfg.engine_mode)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    if gaps := cfg.missing():
        raise SystemExit("Missing in .env: " + ", ".join(gaps) + "\nTip: python -m bot.discover_ids")
    bot.run(cfg.token)


if __name__ == "__main__":
    main()
