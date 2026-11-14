"""Case model and consistency checks.

The case lives in Atlas (`mystery.cases`), not in the repo. The engine loads it
through the store with `load_case(store, case_id)`. The document shape is in
CONTRACTS.md ("Case JSON shape").

`truth` is never part of a `Case`: the store projects it out, and the model
refuses a document that contains it. Only `engine/game.py` fetches the truth,
separately, to resolve an accusation (AGENTS.md rule 5).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field, model_validator

if TYPE_CHECKING:
    from .ports import GameStore

SeedKind = Literal["witnessed", "cover_story"]


class Truth(BaseModel):
    killer: str
    weapon: str
    room: str


class Suspect(BaseModel):
    id: str
    name: str
    role: str
    persona: str
    is_killer: bool = False
    crack_threshold: int = 2
    voice_id: str | None = None
    # Killer only: new lies used, in order, when the cover story is contradicted
    # and no LLM is available (or the LLM output is unusable).
    fallback_lies: list[str] = Field(default_factory=list)
    # Killer only: what really happened, in the first person. Not a memory, so search
    # can never return it; the engine adds it to the turn message only once crack is
    # allowed (rule 13).
    confession: str | None = None

    @property
    def viewer(self) -> str:
        return f"suspect:{self.id}"


class Room(BaseModel):
    id: str
    name: str
    open: bool = True


class Weapon(BaseModel):
    id: str
    name: str


class Evidence(BaseModel):
    id: str
    name: str
    room: str
    text: str
    unlocks: list[str] = Field(default_factory=list)
    # Deterministic contradictions: seed memory id -> replacement text.
    # A null replacement means "let the policy decide" (an LLM-written
    # correction, or the killer's next fallback lie).
    # Missing (None) means "no declared contradictions; ask the LLM checker".
    # An empty dict means "contradicts nothing", and the LLM is not asked.
    contradicts: dict[str, str | None] | None = None


class Action(BaseModel):
    id: str
    label: str
    reveals: list[str]
    opens: str | None = None  # closed room this action reaches


class EventChange(BaseModel):
    ref: str
    replacement: str


class WorldEvent(BaseModel):
    id: str
    label: str
    ref: str
    replacement: str
    notice: str  # what affected teams are told in their channel
    also: list[EventChange] = Field(default_factory=list)

    def changes(self) -> list[EventChange]:
        return [EventChange(ref=self.ref, replacement=self.replacement), *self.also]


class SeedMemory(BaseModel):
    id: str
    text: str
    known_by: list[str] = Field(min_length=1)
    kind: SeedKind  # required: Person 1's seed script rejects a seed without it
    confidence: float = 1.0
    unlocks: list[str] = Field(default_factory=list)


class Gossip(BaseModel):
    topics: list[str] = Field(default_factory=list)
    pairs_per_round: int = 2


class Case(BaseModel):
    id: str = Field(alias="_id")
    title: str
    intro: str
    victim: str
    suspects: list[Suspect]
    rooms: list[Room]
    weapons: list[Weapon]
    evidence: list[Evidence]
    actions: list[Action] = Field(default_factory=list)
    events: list[WorldEvent] = Field(default_factory=list)
    seed_memories: list[SeedMemory]
    gossip: Gossip = Field(default_factory=Gossip)
    status: Literal["active", "closed"] = "active"

    model_config = {"populate_by_name": True}

    @model_validator(mode="before")
    @classmethod
    def _no_truth(cls, data: Any) -> Any:
        if isinstance(data, dict) and "truth" in data:
            raise ValueError("case document includes `truth`; load cases with the truth "
                             "projected out (store.get_case)")
        return data

    # ---- lookups -------------------------------------------------------
    def suspect(self, suspect_id: str) -> Suspect:
        for s in self.suspects:
            if s.id == suspect_id:
                return s
        raise KeyError(f"unknown suspect {suspect_id!r}")

    def room(self, room_id: str) -> Room:
        for r in self.rooms:
            if r.id == room_id:
                return r
        raise KeyError(f"unknown room {room_id!r}")

    def evidence_item(self, evidence_id: str) -> Evidence:
        for e in self.evidence:
            if e.id == evidence_id:
                return e
        raise KeyError(f"unknown evidence {evidence_id!r}")

    def action(self, action_id: str) -> Action:
        for a in self.actions:
            if a.id == action_id:
                return a
        raise KeyError(f"unknown action {action_id!r}")

    def event(self, event_id: str) -> WorldEvent:
        for ev in self.events:
            if ev.id == event_id:
                return ev
        raise KeyError(f"unknown event {event_id!r}")

    def seed(self, seed_id: str) -> SeedMemory:
        for m in self.seed_memories:
            if m.id == seed_id:
                return m
        raise KeyError(f"unknown seed memory {seed_id!r}")

    def killer_suspect(self) -> Suspect | None:
        return next((s for s in self.suspects if s.is_killer), None)

    # ---- consistency ---------------------------------------------------
    @model_validator(mode="after")
    def _check(self) -> "Case":
        errors: list[str] = []

        def unique(label: str, ids: list[str]) -> set[str]:
            dupes = {i for i in ids if ids.count(i) > 1}
            if dupes:
                errors.append(f"duplicate {label} ids: {sorted(dupes)}")
            return set(ids)

        suspects = unique("suspect", [s.id for s in self.suspects])
        rooms = unique("room", [r.id for r in self.rooms])
        weapons = unique("weapon", [w.id for w in self.weapons])
        evidence = unique("evidence", [e.id for e in self.evidence])
        actions = unique("action", [a.id for a in self.actions])
        seeds = unique("seed memory", [m.id for m in self.seed_memories])
        unique("event", [e.id for e in self.events])
        unique("ref", list(evidence) + list(seeds))  # refs must be unambiguous

        killers = [s.id for s in self.suspects if s.is_killer]
        if len(killers) != 1:
            errors.append(f"exactly one suspect must be the killer, got {killers}")
        for s in self.suspects:
            if s.confession and not s.is_killer:
                errors.append(f"suspect {s.id}: only the killer may have a confession")

        for e in self.evidence:
            if e.room not in rooms:
                errors.append(f"evidence {e.id}: unknown room {e.room!r}")
            for a in e.unlocks:
                if a not in actions:
                    errors.append(f"evidence {e.id}: unlocks unknown action {a!r}")
            for ref in (e.contradicts or {}):
                if ref not in seeds:
                    errors.append(f"evidence {e.id}: contradicts unknown seed {ref!r}")

        opened = set()
        for a in self.actions:
            for r in a.reveals:
                if r not in evidence:
                    errors.append(f"action {a.id}: reveals unknown evidence {r!r}")
            if a.opens is not None:
                if a.opens not in rooms:
                    errors.append(f"action {a.id}: opens unknown room {a.opens!r}")
                else:
                    opened.add(a.opens)
            supported = any(a.id in x.unlocks for x in [*self.evidence, *self.seed_memories])
            if not supported:
                errors.append(f"action {a.id}: no evidence or seed memory unlocks it")

        for r in self.rooms:
            if not r.open and r.id not in opened:
                errors.append(f"closed room {r.id} is not reachable through any action")

        viewers = {f"suspect:{s}" for s in suspects}
        for m in self.seed_memories:
            for v in m.known_by:
                if v not in viewers:
                    errors.append(f"seed {m.id}: known_by unknown viewer {v!r}")
            for a in m.unlocks:
                if a not in actions:
                    errors.append(f"seed {m.id}: unlocks unknown action {a!r}")
            if m.kind == "cover_story":
                holders = [v.removeprefix("suspect:") for v in m.known_by]
                if holders != killers:
                    errors.append(f"seed {m.id}: a cover story must be held only by the killer")

        for ev in self.events:
            for ch in ev.changes():
                if ch.ref not in evidence and ch.ref not in seeds:
                    errors.append(f"event {ev.id}: unknown ref {ch.ref!r}")

        if errors:
            raise ValueError("invalid case file:\n  " + "\n  ".join(errors))
        return self


class CaseNotFound(LookupError):
    pass


def case_from_doc(doc: dict) -> Case:
    """Build a Case from a `cases` document (without `truth`)."""
    return Case.model_validate(doc)


async def load_case(store: "GameStore", case_id: str) -> Case:
    """Load and validate a case from Atlas. Fails loudly on a broken document.
    The case is read-only during play, so callers may cache the result per case_id."""
    doc = await store.get_case(case_id)
    if doc is None:
        raise CaseNotFound(f"no case {case_id!r} in the cases collection")
    return case_from_doc(doc)


def check_truth(case: Case, truth: Truth) -> None:
    """Consistency check between a case and its truth. Called by game.py only."""
    errors = []
    killer = case.killer_suspect()
    if killer is None or truth.killer != killer.id:
        errors.append("truth.killer does not match the suspect flagged is_killer")
    if truth.weapon not in {w.id for w in case.weapons}:
        errors.append(f"truth.weapon {truth.weapon!r} is not a weapon")
    if truth.room not in {r.id for r in case.rooms}:
        errors.append(f"truth.room {truth.room!r} is not a room")
    if errors:
        raise ValueError("case truth is inconsistent: " + "; ".join(errors))
