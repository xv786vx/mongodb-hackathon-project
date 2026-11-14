"""Builds the turn message the suspect agent receives (ARCHITECTURE.md section 6).

Trusted part first (character sheet, allowed decisions, fresh memories, recent
turns), untrusted player text last in a quoted block (AGENTS.md rule 10).

`prepare_interrogate` and `prepare_present` run every deterministic step of a
turn before the agent is invoked (AGENTS.md rule 11). The bot (or local runner)
then invokes the agent with `TurnPrep.message`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from mystery_core.memory import ids
from mystery_core.memory.models import Decision, Memory, NewMemory, Scope, Source, team_viewer

from . import prompts
from .case import Case, Suspect
from .contradiction import apply_contradiction, find_contradiction
from .ports import LLM, GameStore
from .rules import DECISION_GUIDE, allowed_decisions

PROMPT_VERSION = prompts.TURN
MAX_PLAYER_CHARS = 600
RECENT_TURNS = 3
LOW_CONFIDENCE = 0.6

# Removing runs of angle brackets is enough to stop player text forming the
# <<<DETECTIVES / DETECTIVES>>> markers that delimit the quoted block.
_MARKER = re.compile(r"<{2,}|>{2,}")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


@dataclass
class TurnPrep:
    ok: bool
    message: str = ""
    allowed: list[Decision] = field(default_factory=list)
    prompt_version: str = PROMPT_VERSION
    fresh: list[Memory] = field(default_factory=list)  # pass to the trace as "written"
    superseded: list[tuple[str, str]] = field(default_factory=list)  # (old_id, new_id)
    error: str | None = None  # shown to the player when ok is False


def sanitize_player_text(text: str) -> str:
    """Untrusted input: cap it and make sure it cannot close the quoted block."""
    text = _CONTROL.sub(" ", text or "")
    text = _MARKER.sub("", text)
    text = text.strip()[:MAX_PLAYER_CHARS]
    return text or "(the detectives say nothing)"


def format_memory(m: Memory) -> str:
    """One line per memory, shared by the turn message and search_memories."""
    notes = []
    if m.kind == "rumour":
        notes.append("hearsay")
    if m.confidence < LOW_CONFIDENCE:
        notes.append("you are not sure about this")
    line = f"[{m.id}] {m.text}"
    if notes:
        line += f" ({'; '.join(notes)})"
    for action in m.unlocks:
        line += f" [lead: {action}]"
    return line


def format_memories(memories: Iterable[Memory]) -> str:
    lines = [format_memory(m) for m in memories]
    return "\n".join(lines) if lines else "(nothing)"


def _field(turn: Any, name: str) -> str:
    """Recent turns are Trace models; plain dicts are accepted too."""
    value = turn.get(name) if isinstance(turn, dict) else getattr(turn, name, None)
    return str(value or "")


def _format_recent(recent_turns: Iterable[Any]) -> str:
    """Oldest first. Pass completed traces for this team and suspect only."""
    turns = list(recent_turns)[-RECENT_TURNS:]
    if not turns:
        return "(this is your first conversation with these detectives)"
    out = []
    for t in turns:
        q = sanitize_player_text(_field(t, "input"))
        out.append(f"Detectives: {q}\nYou: {_field(t, 'reply')}")
    return "\n".join(out)


def _guilt(suspect: Suspect, allowed: list[Decision]) -> str:
    if not suspect.is_killer:
        return ("You did not kill anyone. You may still have things you would "
                "rather keep to yourself.")
    note = ("You are the killer. Your cover story is a deliberate lie that you "
            "maintain. Never confess")
    if "crack" in allowed:
        note += (" unless you choose to crack: your story has fallen apart, "
                 "and cracking is now open to you.")
        if suspect.confession:
            # Only reachable once the gate has passed (rule 13); never a searchable memory.
            note += ("\nWhat really happened (say this only if you choose crack): "
                     + suspect.confession)
        return note
    return note + ". Cracking is not an option this turn."


def build_turn_message(case: Case, suspect_id: str, allowed: list[Decision],
                       player_text: str, *, fresh: Iterable[Memory] = (),
                       fresh_note: str = "", recent_turns: Iterable[Any] = (),
                       command: str = "interrogate") -> str:
    suspect = case.suspect(suspect_id)
    fresh_lines = format_memories(fresh) if fresh else "(nothing new)"
    if fresh_note:
        fresh_lines = f"{fresh_note}\n{fresh_lines}"
    command_note = ""
    if command == "present":
        command_note = ("\nThe detectives have just shown you evidence (see \"Just happened\"). "
                        "React to it in character.\n")
    return prompts.render(
        PROMPT_VERSION,
        name=suspect.name,
        role=suspect.role,
        title=case.title,
        victim=case.victim,
        persona=suspect.persona,
        guilt=_guilt(suspect, allowed),
        decisions=", ".join(allowed),
        decision_guide="\n".join(f"   - {d}: {DECISION_GUIDE[d]}" for d in allowed),
        command_note=command_note,
        fresh=fresh_lines,
        recent=_format_recent(recent_turns),
        player=sanitize_player_text(player_text),
    )


def _check_suspect(case: Case, suspect_id: str) -> str | None:
    try:
        case.suspect(suspect_id)
    except KeyError:
        return f"There is no suspect called {suspect_id!r} in this case."
    return None


async def prepare_interrogate(store: GameStore, case: Case, scope: Scope, suspect_id: str,
                              question: str, recent_turns: Iterable[Any] = ()) -> TurnPrep:
    if err := _check_suspect(case, suspect_id):
        return TurnPrep(ok=False, error=err)
    allowed = await allowed_decisions(store, case, scope, suspect_id)
    message = build_turn_message(case, suspect_id, allowed, question,
                                 recent_turns=recent_turns, command="interrogate")
    return TurnPrep(ok=True, message=message, allowed=allowed)


async def prepare_present(store: GameStore, case: Case, scope: Scope, suspect_id: str,
                          evidence_id: str, question: str = "", recent_turns: Iterable[Any] = (),
                          llm: LLM | None = None) -> TurnPrep:
    """/present: evidence write, contradiction check and supersession, then the message.

    Safe to repeat: the evidence memory has a deterministic ID, and the
    contradiction check runs only the first time this evidence reaches this
    suspect in this world, so the same item can't break a story twice.
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

    # 2. The suspect now remembers being shown it, with the evidence's leads.
    shown = await store.write(scope, [NewMemory(
        id=ids.evidence_memory_id(scope.case_id, scope.team_id, suspect_id, evidence.id),
        text=f"The detectives showed me {evidence.name[0].lower()}{evidence.name[1:]}: {evidence.text}",
        known_by=[suspect.viewer],
        kind="evidence",
        source=Source(type="evidence", ref=evidence.id),
        unlocks=list(evidence.unlocks),
    )])
    fresh: list[Memory] = [m for m in shown if m.status == "active"]
    superseded: list[tuple[str, str]] = []
    note = ""

    # 3-4. Contradiction check, then the policy's on_contradiction.
    if not already_shown:
        belief = await find_contradiction(store, scope, suspect, evidence, llm=llm)
        if belief is not None:
            replacement = await apply_contradiction(store, case, scope, suspect, evidence, belief,
                                                    llm=llm)
            if replacement is not None:
                superseded.append((belief.id, replacement.id))
                fresh.append(replacement)
                if belief.kind == "cover_story":
                    note = (f"This evidence breaks your cover story (\"{belief.text}\"). "
                            "Your new story, which you will now maintain, is below.")
                else:
                    note = (f"This evidence shows you were wrong to believe "
                            f"\"{belief.text}\" What you believe now is below.")
    else:
        note = "You have been shown this before."

    # 5. Continue as /interrogate, with the fresh memories in the trusted part.
    allowed = await allowed_decisions(store, case, scope, suspect_id)
    message = build_turn_message(case, suspect_id, allowed,
                                 question or f"What do you make of {evidence.name.lower()}?",
                                 fresh=fresh, fresh_note=note, recent_turns=recent_turns,
                                 command="present")
    return TurnPrep(ok=True, message=message, allowed=allowed, fresh=fresh, superseded=superseded)
