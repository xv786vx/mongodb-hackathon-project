"""ID helpers. Anything a retry could create twice gets a deterministic id (rule 16).

Prefixes: m_* memories, t_* traces/turns, a_* offers, case_* cases.
"""

from __future__ import annotations

import hashlib
import uuid


def _short_hash(*parts: str) -> str:
    return hashlib.sha1("\x1f".join(parts).encode()).hexdigest()[:12]


def turn_id(discord_interaction_id: int | str) -> str:
    """A retried Discord command maps to the same trace."""
    return f"t_{discord_interaction_id}"


def random_memory_id() -> str:
    """Only for memories a retry cannot duplicate (the turn's trace already guards them)."""
    return f"m_{uuid.uuid4().hex[:16]}"


def seed_memory_id(case_id: str, team_id: str, seed_id: str) -> str:
    """One copy of a seed memory per world. Readable on purpose, for Data Explorer during the demo."""
    return f"m_{case_id.removeprefix('case_')}_{team_id}_{seed_id}"


def finding_id(case_id: str, team_id: str, evidence_id: str) -> str:
    """A team discovers a piece of evidence at most once."""
    return f"m_find_{case_id.removeprefix('case_')}_{team_id}_{evidence_id}"


def evidence_memory_id(case_id: str, team_id: str, suspect_id: str, evidence_id: str) -> str:
    """A suspect is shown a piece of evidence by a team at most once."""
    return f"m_ev_{case_id.removeprefix('case_')}_{team_id}_{suspect_id}_{evidence_id}"


def turn_memory_id(turn: str, label: str) -> str:
    """A memory written during a turn (interaction, finding). Same turn + label -> same id."""
    return f"m_{_short_hash(turn, label)}"


def replacement_memory_id(old_id: str) -> str:
    """The memory that supersedes old_id. A memory is superseded at most once, so this is stable."""
    return f"m_r_{_short_hash(old_id)}"


def offer_id(team_id: str, action: str, memory_id: str) -> str:
    return f"a_{team_id}_{action}_{memory_id}"
