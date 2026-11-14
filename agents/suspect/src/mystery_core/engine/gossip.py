"""Gossip between suspects, one world at a time (`/round`).

The bot calls `run_round` for each team's world. A rumour is a copy of the
speaker's memory, held only by the listener, with decayed confidence, the
rumour TTL (set by the store from the policy in Atlas), and never any `unlocks`
(AGENTS.md rule 15).
"""
from __future__ import annotations

import random

from mystery_core.memory.models import Memory, NewMemory, Scope, Source
from mystery_core.memory.policy import Policy

from .case import Case, Suspect
from .ports import GameStore

MIN_RUMOUR_CONFIDENCE = 0.2


def _pairs(case: Case, rng: random.Random, n: int) -> list[tuple[Suspect, Suspect]]:
    candidates = [(a, b) for a in case.suspects for b in case.suspects if a.id != b.id]
    rng.shuffle(candidates)
    return candidates[:n]


def _speakable(policy: Policy, speaker: Suspect) -> list[str]:
    kinds = [k for k, rule in policy.kinds.items() if rule.propagates]
    if speaker.is_killer:
        # The killer only ever repeats his cover story; he never gossips the truth.
        return [k for k in kinds if k == "cover_story"]
    return [k for k in kinds if k != "cover_story"]


async def run_round(store: GameStore, case: Case, scope: Scope, round_no: int) -> list[Memory]:
    """Spread one rumour per selected pair in this world. Deterministic per
    (case, team, round), so a retried round makes the same choices and the
    store's dedup absorbs the repeat."""
    policy = store.policy
    decay = policy.kinds["rumour"].confidence_decay or 0.3
    rng = random.Random(f"{case.id}:{scope.team_id}:{round_no}")
    topics = case.gossip.topics or [case.victim]

    new: list[NewMemory] = []
    for speaker, listener in _pairs(case, rng, case.gossip.pairs_per_round):
        topic = rng.choice(topics)
        kinds = _speakable(policy, speaker)
        if not kinds:
            continue
        for m in await store.retrieve(scope, speaker.viewer, topic, k=1, kinds=kinds):
            if listener.viewer in m.known_by:
                continue
            confidence = round(m.confidence * (1 - decay), 3)
            if confidence < MIN_RUMOUR_CONFIDENCE:
                continue
            new.append(NewMemory(
                text=f"{speaker.name} told me: \"{m.text}\"",
                known_by=[listener.viewer],
                kind="rumour",
                source=Source(type="told_by", ref=speaker.id),
                confidence=confidence,
                unlocks=[],
            ))
    return await store.write(scope, new) if new else []
