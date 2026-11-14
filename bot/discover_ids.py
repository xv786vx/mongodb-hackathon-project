"""Prints the guild, role and channel IDs to paste into .env. Run: python -m bot.discover_ids"""
from __future__ import annotations

import discord

from bot import config

cfg = config.load()
client = discord.Client(intents=discord.Intents.default())


@client.event
async def on_ready() -> None:
    for g in client.guilds:
        print(f"\nDISCORD_GUILD_ID={g.id}   # {g.name}")
        for role in g.roles:
            print(f"  role     {role.name:<16} {role.id}")
        for ch in g.channels:
            if isinstance(ch, discord.TextChannel):
                print(f"  channel  #{ch.name:<15} {ch.id}")
    await client.close()


if __name__ == "__main__":
    if not cfg.token:
        raise SystemExit("Set DISCORD_TOKEN in .env first.")
    client.run(cfg.token)
