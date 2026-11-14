"""Trace -> Discord text. Players never see the decision label (truth and lie must be indistinguishable)."""
from __future__ import annotations

import discord

from bot.engine_api import Trace

MAX_REPLY = 1900                      # Discord limit is 2,000; submit_reply caps too. This is the backstop.


def reply_embed(trace: Trace, suspect_name: str) -> discord.Embed:
    text = trace.reply if len(trace.reply) <= MAX_REPLY else trace.reply[:MAX_REPLY - 1] + "…"
    embed = discord.Embed(title=suspect_name, description=f"*\"{text}\"*", colour=0x3B3B58)
    embed.set_footer(text=trace.turn_id)
    return embed
