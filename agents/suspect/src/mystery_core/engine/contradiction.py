"""Contradiction check and correction for /present (ARCHITECTURE.md section 7).

Runs deterministically before the agent is invoked (AGENTS.md rule 11).

Two ways a contradiction is found:
1. Declared in the case (`evidence.contradicts`): reliable, used for the demo.
2. Otherwise, if an LLM is supplied, ask it to pick a contradicted belief from
   the suspect's own scoped retrieval. Its answer is only accepted if it names
   one of those candidates, so the model cannot reach outside the scope.

What happens next comes from the policy's `on_contradiction` for the belief's kind:
- `correct`: supersede with a corrected belief (wrong beliefs get fixed).
- `new_lie`: supersede the cover story with a new lie (the killer adapts).
"""
from __future__ import annotations

import json
import logging
import re

from pydantic import BaseModel, ValidationError

from mystery_core.memory.models import Memory, NewMemory, Scope, Source

from . import prompts
from .case import Case, Evidence, Suspect
from .ports import LLM, GameStore
from .rules import cover_breaks

log = logging.getLogger(__name__)

BELIEF_KINDS = ["witnessed", "rumour", "cover_story"]
MAX_TEXT = 400


class Verdict(BaseModel):
    contradicts: bool
    memory_id: str | None = None


def _clean(text: str | None) -> str | None:
    if not text:
        return None
    text = re.sub(r"\[[^\]]*\]", "", text).strip().strip('"').strip()
    return text[:MAX_TEXT] or None


def _parse_verdict(raw: str) -> Verdict | None:
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        return Verdict.model_validate(json.loads(match.group(0)))
    except (json.JSONDecodeError, ValidationError):
        return None


def _is_cover_ref(case: Case, ref: str) -> bool:
    try:
        return case.seed(ref).kind == "cover_story"
    except KeyError:
        return False


async def already_applied(store: GameStore, scope: Scope, suspect: Suspect,
                          evidence: Evidence) -> bool:
    """True if this evidence already replaced one of the suspect's beliefs in this world.

    Every replacement made by apply_contradiction carries source.ref = the evidence id,
    so a /present retried after the supersede (but before the evidence memory was
    written) does not break the story a second time."""
    return bool(await store.find_by_ref(scope, suspect.viewer, evidence.id,
                                        kinds=["witnessed", "cover_story"]))


async def find_contradiction(store: GameStore, case: Case, scope: Scope, suspect: Suspect,
                             evidence: Evidence, llm: LLM | None = None) -> Memory | None:
    """Return the suspect's active belief this evidence contradicts, if any."""
    viewer = suspect.viewer
    if evidence.contradicts is not None:
        for ref in evidence.contradicts:
            hits = await store.find_by_ref(scope, viewer, ref, kinds=BELIEF_KINDS)
            if hits:
                return hits[0]
            # Once broken, the killer's story is a fabricated replacement tagged with the
            # evidence that broke it, not the seed ref. He holds exactly one active cover
            # story, so find it by kind.
            if suspect.is_killer and _is_cover_ref(case, ref):
                covers = await store.list_by_viewer(scope, viewer, kinds=["cover_story"])
                if covers:
                    return covers[-1]
        return None

    if llm is None:
        return None
    candidates = await store.retrieve(scope, viewer, evidence.text, k=5, kinds=BELIEF_KINDS)
    if not candidates:
        return None
    beliefs = "\n".join(f"{m.id}: {m.text}" for m in candidates)
    raw = await llm(prompts.render(prompts.CONTRADICTION, name=suspect.name,
                                   evidence=evidence.text, beliefs=beliefs))
    verdict = _parse_verdict(raw)
    if verdict is None or not verdict.contradicts:
        return None
    by_id = {m.id: m for m in candidates}
    if verdict.memory_id not in by_id:
        log.warning("contradiction check named a memory outside the candidates: %s",
                    verdict.memory_id)
        return None
    return by_id[verdict.memory_id]


async def apply_contradiction(store: GameStore, case: Case, scope: Scope, suspect: Suspect,
                              evidence: Evidence, belief: Memory,
                              llm: LLM | None = None) -> Memory | None:
    """Supersede `belief` according to the store's policy. Returns the replacement."""
    kind_policy = store.policy.kinds.get(belief.kind)
    rule = kind_policy.on_contradiction if kind_policy else None
    declared_map = evidence.contradicts or {}
    if belief.source.ref in declared_map:
        declared = _clean(declared_map[belief.source.ref])
    elif belief.kind == "cover_story":
        # A replacement lie: use the text declared against the original cover story.
        declared = next((_clean(t) for r, t in declared_map.items() if _is_cover_ref(case, r)), None)
    else:
        declared = None
    reason = f"contradicted by {evidence.id}"

    if rule == "correct":
        text = declared
        if text is None and llm is not None:
            text = _clean(await llm(prompts.render(prompts.CORRECTION, name=suspect.name,
                                                   belief=belief.text, evidence=evidence.text)))
        if text is None:
            text = f"I was wrong to believe that {belief.text[0].lower()}{belief.text[1:]}"
        new = NewMemory(text=text, known_by=[suspect.viewer], kind="witnessed",
                        source=Source(type="evidence", ref=evidence.id), confidence=1.0)

    elif rule == "new_lie":
        text = declared
        if text is None and llm is not None:
            text = _clean(await llm(prompts.render(prompts.NEW_LIE, name=suspect.name,
                                                   belief=belief.text, evidence=evidence.text)))
        if text is None:
            breaks = await cover_breaks(store, case, scope, suspect.id)
            lies = suspect.fallback_lies
            text = lies[min(breaks, len(lies) - 1)] if lies else (
                "I have told you everything I know. I was where I said I was.")
        # Tagged with the evidence that forced it, so a retry can tell this evidence was
        # already applied (already_applied). find_contradiction finds it by kind later.
        new = NewMemory(text=text, known_by=[suspect.viewer], kind="cover_story",
                        source=Source(type="fabricated", ref=evidence.id),
                        confidence=1.0)
    else:
        return None

    return await store.supersede(scope, belief.id, new, reason=reason)
