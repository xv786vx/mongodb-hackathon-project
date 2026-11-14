"""Discord entry point. Run: python -m bot.main"""
from __future__ import annotations

import logging

import discord
from discord import app_commands

from bot import config
from bot.fake_engine import FakeEngine
from bot.identity import Caller, Refusal, require_host, resolve_team, role_ids_of
from bot.render import clip, found_text
from bot.turns import run_turn
from bot.views import OfferButton

log = logging.getLogger("bot")
cfg = config.load()


def make_engine():
    if cfg.engine_mode == "fake":
        return FakeEngine()
    raise SystemExit(f"ENGINE_MODE={cfg.engine_mode} is not wired yet. Use ENGINE_MODE=fake.")


engine = make_engine()
OfferButton.cfg, OfferButton.engine = cfg, engine
round_no = 0          # TODO: ask person 3 where the round number should live; run_round needs it


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


# ---- helpers -----------------------------------------------------------------------------------
def caller_of(interaction: discord.Interaction) -> Caller:
    return resolve_team(cfg, role_ids_of(interaction), interaction.channel_id)


async def refuse(interaction: discord.Interaction, r: Refusal) -> None:
    await interaction.response.send_message(str(r), ephemeral=True)


def choices(pairs, current: str):
    cur = current.lower()
    return [app_commands.Choice(name=n, value=i) for i, n in pairs if cur in n.lower() or cur in i][:25]


async def ac_suspect(interaction, current: str):
    return choices([(s.id, s.name) for s in engine.case_info().suspects], current)


async def ac_room(interaction, current: str):                  # open rooms only; the locked room is not listed
    return choices([(r.id, r.name) for r in engine.case_info().open_rooms], current)


async def ac_weapon(interaction, current: str):
    return choices([(w.id, w.name) for w in engine.case_info().weapons], current)


async def ac_event(interaction, current: str):
    return choices([(e.id, e.name) for e in engine.case_info().events], current)


async def ac_findings(interaction, current: str):              # only evidence THIS team has found
    try:
        team = caller_of(interaction).team_id
    except Refusal:
        return []
    found = await engine.team_findings(team)
    return choices([(e.id, e.name) for e in found], current)


async def ac_all_evidence(interaction, current: str):          # host /discover only
    try:
        require_host(cfg, role_ids_of(interaction))
    except Refusal:
        return []
    return choices([(e.id, e.name) for e in engine.case_info().evidence], current)


async def post_to(channel_id: int | None, text: str) -> None:
    ch = bot.get_channel(channel_id) if channel_id else None
    if ch:
        await ch.send(clip(text))


# ---- player commands ---------------------------------------------------------------------------
@bot.tree.command(name="ping", description="Check that the bot sees your team")
async def ping(interaction: discord.Interaction) -> None:
    try:
        caller = caller_of(interaction)
    except Refusal as r:
        return await refuse(interaction, r)
    await interaction.response.send_message(f"pong. Team {caller.team_id}, engine={cfg.engine_mode}.")


@bot.tree.command(name="interrogate", description="Question a suspect")
@app_commands.autocomplete(suspect=ac_suspect)
async def interrogate(interaction: discord.Interaction, suspect: str, question: str) -> None:
    try:
        caller = caller_of(interaction)
    except Refusal as r:
        return await refuse(interaction, r)
    await run_turn(interaction, cfg, engine, caller, "interrogate", suspect, question)


@bot.tree.command(name="present", description="Show a suspect evidence your team has found")
@app_commands.autocomplete(evidence=ac_findings, suspect=ac_suspect)
async def present(interaction: discord.Interaction, evidence: str, suspect: str, question: str = "") -> None:
    try:
        caller = caller_of(interaction)
    except Refusal as r:
        return await refuse(interaction, r)
    await run_turn(interaction, cfg, engine, caller, "present", suspect, question, evidence_id=evidence)


@bot.tree.command(name="search", description="Search an open room")
@app_commands.autocomplete(room=ac_room)
async def search(interaction: discord.Interaction, room: str) -> None:
    try:
        caller = caller_of(interaction)
    except Refusal as r:
        return await refuse(interaction, r)
    await interaction.response.defer(thinking=True)
    result = await engine.search_room(caller.team_id, room)
    if not result.ok:
        return await interaction.followup.send(result.error or "You can't search there.", ephemeral=True)
    await interaction.followup.send(clip(found_text(result.found)))


@bot.tree.command(name="notebook", description="Look through your team's findings")
async def notebook(interaction: discord.Interaction, query: str = "") -> None:
    try:
        caller = caller_of(interaction)
    except Refusal as r:
        return await refuse(interaction, r)
    await interaction.response.defer(ephemeral=True)
    rows = await engine.notebook(caller.team_id, query)
    await interaction.followup.send(clip("\n".join(f"- {r}" for r in rows)) if rows else "Nothing in your notebook yet.",
                                    ephemeral=True)


@bot.tree.command(name="accuse", description="Make your team's accusation (one chance)")
@app_commands.autocomplete(suspect=ac_suspect, weapon=ac_weapon, room=ac_room)
async def accuse(interaction: discord.Interaction, suspect: str, weapon: str, room: str) -> None:
    try:
        caller = caller_of(interaction)
    except Refusal as r:
        return await refuse(interaction, r)
    await interaction.response.defer(ephemeral=True)
    result = await engine.accuse(caller.team_id, suspect, weapon, room)
    if result.status in ("solved", "eliminated"):
        await post_to(cfg.case_room_channel_id, result.message)   # never says which part was wrong
    await interaction.followup.send(result.message, ephemeral=True)


# ---- host commands -----------------------------------------------------------------------------
@bot.tree.command(name="event", description="Host: trigger a world event")
@app_commands.autocomplete(event_id=ac_event)
async def event(interaction: discord.Interaction, event_id: str) -> None:
    try:
        require_host(cfg, role_ids_of(interaction))
    except Refusal as r:
        return await refuse(interaction, r)
    await interaction.response.defer(ephemeral=True)
    try:
        result = await engine.trigger_event(event_id)
    except KeyError:
        return await interaction.followup.send("Unknown event.", ephemeral=True)
    for team in result.notify_teams:                          # only teams whose world held the memory
        await post_to(cfg.team_channels.get(team), result.notice)
    await interaction.followup.send(f"{event_id}: notified {list(result.notify_teams) or 'no teams'}.")


@bot.tree.command(name="discover", description="Host: record a discovery for one team (stages the demo)")
@app_commands.autocomplete(evidence=ac_all_evidence)
async def discover(interaction: discord.Interaction, team: str, evidence: str) -> None:
    try:
        require_host(cfg, role_ids_of(interaction))
    except Refusal as r:
        return await refuse(interaction, r)
    if team not in cfg.team_roles:
        return await interaction.response.send_message(f"Team must be one of {sorted(cfg.team_roles)}.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    try:
        found = await engine.discover(team, evidence)
    except KeyError:
        return await interaction.followup.send("Unknown evidence.", ephemeral=True)
    await interaction.followup.send(f"Team {team}: {found.evidence.name} ({'new' if found.new else 'already held'}).")


@bot.tree.command(name="round", description="Host: run gossip in every world")
async def round_cmd(interaction: discord.Interaction) -> None:
    global round_no
    try:
        require_host(cfg, role_ids_of(interaction))
    except Refusal as r:
        return await refuse(interaction, r)
    await interaction.response.defer(ephemeral=True)
    round_no += 1
    for team in sorted(cfg.team_roles):                       # once per world
        await engine.run_round(team, round_no)
    await post_to(cfg.case_room_channel_id, f"Round {round_no} begins.")   # never rumour text (rule 18)
    await interaction.followup.send(f"Round {round_no} run.")


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
