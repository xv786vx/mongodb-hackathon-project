"""Engine results -> Discord text. Players never see the decision label (truth and lie must be indistinguishable)."""
from __future__ import annotations

import discord

from bot.engine_api import Found, Trace

MAX_REPLY = 1900                      # Discord limit is 2,000; submit_reply caps too. This is the backstop.


def clip(text: str, limit: int = MAX_REPLY) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def reply_embed(trace: Trace, suspect_name: str) -> discord.Embed:
    embed = discord.Embed(title=suspect_name, description=f"*\"{clip(trace.reply)}\"*", colour=0x3B3B58)
    embed.set_footer(text=trace.turn_id)
    return embed


def found_text(found: tuple[Found, ...]) -> str:
    """Items the team actually gained. Already-held or withdrawn evidence reads as 'nothing new'."""
    gained = [f for f in found if f.new and not f.withdrawn]
    if not gained:
        return "Nothing new here."
    return "\n".join(f"**{f.evidence.name}**: {f.evidence.text}" for f in gained)
