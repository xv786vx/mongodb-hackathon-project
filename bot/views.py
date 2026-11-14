"""Persistent offer buttons. One DynamicItem matches every offer:<id>, including after a restart."""
from __future__ import annotations

import discord

from bot.config import Settings
from bot.engine_api import Engine
from bot.identity import Refusal, resolve_team, role_ids_of
from bot.real_engine import EngineNotReady
from bot.render import clip, found_text

PREFIX = "offer:"


def offer_view(offer_id: str, label: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(OfferButton(offer_id, label))
    return view


class OfferButton(discord.ui.DynamicItem[discord.ui.Button], template=r"offer:(?P<offer_id>[\w:-]+)"):
    # Set once at startup by main.py.
    cfg: Settings
    engine: Engine

    def __init__(self, offer_id: str, label: str = "Take this lead") -> None:
        super().__init__(discord.ui.Button(label=label, style=discord.ButtonStyle.primary,
                                           custom_id=f"{PREFIX}{offer_id}"))
        self.offer_id = offer_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["offer_id"])

    async def callback(self, interaction: discord.Interaction) -> None:
        # The CLICKER's role decides, not whoever ran the original command.
        try:
            caller = resolve_team(self.cfg, role_ids_of(interaction), interaction.channel_id)
        except Refusal as r:
            await interaction.response.send_message(str(r), ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        try:
            result = await self.engine.use_offer(caller.team_id, self.offer_id)
        except EngineNotReady as e:
            await interaction.followup.send(f"That part of the game isn't connected yet ({e}).", ephemeral=True)
            return
        if result.status == "forbidden":
            await interaction.followup.send(result.message, ephemeral=True)
            return
        text = result.message
        if result.status == "used" and result.found:
            text += "\n" + found_text(result.found)
        await interaction.followup.send(clip(text))
