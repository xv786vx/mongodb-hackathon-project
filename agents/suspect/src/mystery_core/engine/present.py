"""Game-rule preparation for /interrogate and /present (Person 3).

Runs every deterministic step of a turn before the agent is invoked (AGENTS.md rule 11).
The prompt itself is built by Person 2's `TurnService.prepare` (turn.py) from the trace,
so these functions return only what the bot needs: ok/error and the fresh memory ids to
pass as `TurnService.prepare(ctx, fresh_memory_ids=...)` and to the trace's `written`.

Re-exported from `mystery_core.engine.turn`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from mystery_core.memory import ids
from mystery_core.memory.models import Memory, NewMemory, Scope, Source, team_viewer

from .case import Case
from .contradiction import already_applied, apply_contradiction, find_contradiction
from .ports import LLM, GameStore

# The hosted agent ignores the invoke message and rebuilds the prompt from the trace.
AGENT_MESSAGE = "Take this turn. Your instructions and the detectives' words come from the trace."


def _prompt_version() -> str:
    from .turn import PROMPT_VERSION  # late import: turn.py re-exports this module
    return PROMPT_VERSION


@dataclass
class TurnPrep:
    ok: bool
    message: str = AGENT_MESSAGE
    prompt_version: str = field(default_factory=_prompt_version)
    fresh: list[Memory] = field(default_factory=list)  # ids -> TurnService.prepare and trace "written"
    superseded: list[tuple[str, str]] = field(default_factory=list)  # (old_id, new_id)
    error: str | None = None  # shown to the player when ok is False


def _check_suspect(case: Case, suspect_id: str) -> str | None:
    try:
        case.suspect(suspect_id)
    except KeyError:
        return f"There is no suspect called {suspect_id!r} in this case."
    return None


async def prepare_interrogate(store: GameStore, case: Case, scope: Scope, suspect_id: str,
                              question: str, recent_turns: Iterable[Any] = ()) -> TurnPrep:
    """/interrogate has no deterministic writes; only the suspect is checked."""
    if err := _check_suspect(case, suspect_id):
        return TurnPrep(ok=False, error=err)
    return TurnPrep(ok=True)


async def prepare_present(store: GameStore, case: Case, scope: Scope, suspect_id: str,
                          evidence_id: str, question: str = "", recent_turns: Iterable[Any] = (),
                          llm: LLM | None = None) -> TurnPrep:
    """/present: finding check, contradiction and supersession, then the evidence memory.

    Safe to repeat. The evidence memory has a deterministic id and is written last, and
    every replacement carries the evidence id, so a retry (even after a crash between the
    supersede and the evidence write) applies the contradiction exactly once.
    """
    if err := _check_suspect(case, suspect_id):
        return TurnPrep(ok=False, error=err)
    try:
        evidence = case.evidence_item(evidence_id)
    except KeyError:
        return TurnPrep(ok=False, error=f"There is no evidence called {evidence_id!r}.")
    suspect = case.suspect(suspect_id)

    # 1. The team must hold an active finding for this evidence.
    if not await store.find_by_ref(scope, team_viewer(scope.team_id), evidence.id, kinds=["finding"]):
        return TurnPrep(ok=False, error=f"Your team hasn't found {evidence.name.lower()}.")

    already_shown = bool(await store.find_by_ref(scope, suspect.viewer, evidence.id, kinds=["evidence"]))
    fresh: list[Memory] = []
    superseded: list[tuple[str, str]] = []

    # 2-3. Contradiction check, then the policy's on_contradiction.
    if not already_shown and not await already_applied(store, scope, suspect, evidence):
        belief = await find_contradiction(store, case, scope, suspect, evidence, llm=llm)
        if belief is not None:
            replacement = await apply_contradiction(store, case, scope, suspect, evidence, belief, llm=llm)
            if replacement is not None:
                superseded.append((belief.id, replacement.id))
                fresh.append(replacement)

    # 4. Last: the suspect now remembers being shown it, with the evidence's leads.
    shown = await store.write(scope, [NewMemory(
        id=ids.evidence_memory_id(scope.case_id, scope.team_id, suspect_id, evidence.id),
        text=f"The detectives showed me {evidence.name[0].lower()}{evidence.name[1:]}: {evidence.text}",
        known_by=[suspect.viewer],
        kind="evidence",
        source=Source(type="evidence", ref=evidence.id),
        unlocks=list(evidence.unlocks),
    )], dedup=False)
    fresh = [m for m in shown if m.status == "active"] + fresh
    return TurnPrep(ok=True, fresh=fresh, superseded=superseded)
