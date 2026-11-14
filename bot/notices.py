"""Expiry notices: "a rumour has faded", posted only to the team whose world held the memory (rule 18).

Person 1's expiry worker calls on_expired(ExpiryNotice) with IDs and a kind only. We write the wording here and never
include memory text: a rumour belongs to the suspect, so its text must not reach the team.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from bot.config import Settings
from mystery_core.memory import ExpiryNotice

log = logging.getLogger("bot.notices")

# What the team is told, by memory kind. Deliberately vague: it says that something faded, never what it was.
WORDING = {
    "rumour": "A rumour has faded.",
}
DEFAULT_WORDING = "Something you heard has faded from memory."

Poster = Callable[[int, str], Awaitable[None]]


def notice_text(notice: ExpiryNotice) -> str:
    return WORDING.get(str(notice.kind), DEFAULT_WORDING)


def make_on_expired(cfg: Settings, post: Poster, case_id: str | None = None) -> Callable[[ExpiryNotice], Awaitable[None]]:
    """Build the callback for store.watch_expiries. `post(channel_id, text)` sends to a channel."""

    async def on_expired(notice: ExpiryNotice) -> None:
        if case_id and notice.case_id != case_id:
            return                                              # another case's memory
        channel = cfg.team_channels.get(notice.team_id)
        if channel is None:
            log.warning("expiry for unknown team %s (memory %s)", notice.team_id, notice.memory_id)
            return
        try:
            await post(channel, notice_text(notice))
        except Exception as e:                                  # noqa: BLE001 - a failed post must not stop the watcher
            log.warning("expiry notice for %s failed (%s)", notice.memory_id, e.__class__.__name__)

    return on_expired
