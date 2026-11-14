"""Preparation, retrieval and reply validation. No platform, Discord or voice-provider imports."""

from dataclasses import dataclass
from importlib.resources import files
import json
import re

from mystery_core.memory import ids
from mystery_core.memory.models import NewMemory, Offer, ReplyProposal, Trace
from mystery_core.memory.policy import utcnow
from mystery_core.memory.store import MemoryStoreAPI
from mystery_core.memory.traces import TraceStoreAPI
from .context import TurnContext, TurnError

PROMPT_VERSION = "suspect_v1"
_BRACKETS = re.compile(r"\[[^\]]*\]", re.DOTALL)


def clean_dialogue(text: str) -> str:
    text = _BRACKETS.sub("", text).strip()
    # An unmatched opening bracket must not turn into an audio directive either.
    text = text.split("[", 1)[0].replace("]", "").strip()
    if not text or len(text) > 2000 or any(ord(c) < 32 and c not in "\n\t" for c in text):
        raise TurnError("invalid_dialogue")
    return text


@dataclass(frozen=True)
class PreparedTurn:
    context: TurnContext
    system: str
    message: str


class TurnService:
    def __init__(self, store: MemoryStoreAPI, traces: TraceStoreAPI, *, model: str):
        self.store, self.traces, self.model = store, traces, model

    @staticmethod
    async def checked_turn(store, traces, ctx):
        trace = await traces.get_trace(ctx.scope, ctx.turn_id)
        if trace is None or trace.actor != ctx.viewer or trace.command != ctx.command:
            raise TurnError("turn_scope_mismatch")
        if trace.status == "rejected":
            raise TurnError("turn_rejected")
        case = await store.get_case(ctx.scope.case_id)
        if case is None:
            raise TurnError("unknown_case")
        suspect = next((s for s in case.get("suspects", []) if s.get("id") == ctx.suspect_id), None)
        if suspect is None:
            raise TurnError("unknown_suspect")
        if ctx.command == "replay" and not trace.replay_of:
            raise TurnError("replay_requires_original_trace")
        if ctx.command != "replay" and trace.replay_of:
            raise TurnError("replay_command_mismatch")
        return trace, case, suspect

    @staticmethod
    async def allowed_decisions(store, ctx, suspect) -> list[str]:
        allowed = ["truth", "deflect"]
        if suspect.get("is_killer") is True:
            allowed.append("lie")
            threshold = suspect.get("crack_threshold", 2)
            if not isinstance(threshold, int) or isinstance(threshold, bool) or threshold < 1:
                raise TurnError("invalid_crack_threshold")
            if await store.count_superseded(ctx.scope, ctx.viewer, kind="cover_story") >= threshold:
                allowed.append("crack")
        return allowed

    async def prepare(self, ctx: TurnContext, *, fresh_memory_ids=()) -> PreparedTurn:
        trace, case, suspect = await self.checked_turn(self.store, self.traces, ctx)
        # Only IDs enter from server preparation. Text is re-fetched with the same governance filter.
        fresh = await self.store.fetch(ctx.scope, ctx.viewer, fresh_memory_ids)
        if trace.status == "pending":
            await self.traces.add_retrieved(ctx.scope, ctx.turn_id, [m.id for m in fresh])
        visible = await self.store.fetch(ctx.scope, ctx.viewer, list(trace.retrieved) + [m.id for m in fresh])
        sheet = {key: suspect[key] for key in ("id", "name", "persona") if key in suspect}
        payload = {
            "character": sheet,
            "allowed_decisions": await self.allowed_decisions(self.store, ctx, suspect),
            "actions": [{"id": a["id"], "label": a["label"]} for a in case.get("actions", [])],
            "fresh_memories": [self.memory_payload(m) for m in visible],
        }
        system = files("mystery_core.engine.prompts").joinpath(PROMPT_VERSION + ".txt").read_text(encoding="utf-8")
        system += "\nTrusted turn data:\n" + json.dumps(payload, ensure_ascii=False)
        # Use the immutable bot-created trace input, not facts embedded in a potentially stale prepared message.
        return PreparedTurn(ctx, system, "Untrusted player input (JSON quoted):\n" + json.dumps(trace.input))

    @staticmethod
    def memory_payload(memory) -> dict:
        return {"id": memory.id, "text": memory.text, "kind": memory.kind,
                "source": memory.source.model_dump(), "confidence": memory.confidence, "unlocks": memory.unlocks}

    async def search(self, ctx: TurnContext, query: str) -> dict:
        trace, _, _ = await self.checked_turn(self.store, self.traces, ctx)
        if trace.status != "pending":
            return {"status": trace.status, "memories": []}
        if not isinstance(query, str) or not query.strip() or len(query) > 4000:
            raise TurnError("invalid_query")
        if trace.replay_of:
            original = await self.traces.get_trace(ctx.scope, trace.replay_of)
            if original is None or original.status != "complete" or original.actor != ctx.viewer:
                raise TurnError("invalid_replay_source")
            if not set(trace.masked) <= set(original.retrieved):
                raise TurnError("invalid_replay_mask")
            memories = await self.store.fetch(ctx.scope, ctx.viewer,
                                             [mid for mid in original.retrieved if mid not in trace.masked])
        else:
            memories = await self.store.retrieve(ctx.scope, ctx.viewer, query)
            # Freshly written evidence may not have reached the vector index yet.
            current = await self.store.fetch(ctx.scope, ctx.viewer, trace.retrieved)
            memories = list({m.id: m for m in [*memories, *current]}.values())
        await self.traces.add_retrieved(ctx.scope, ctx.turn_id, [m.id for m in memories])
        return {"status": "pending", "memories": [self.memory_payload(m) for m in memories]}

    async def submit(self, ctx: TurnContext, proposal: ReplyProposal) -> Trace:
        reply = clean_dialogue(proposal.reply)

        async def commit(store, traces):
            trace, case, suspect = await self.checked_turn(store, traces, ctx)
            if trace.status == "complete":
                return trace
            if proposal.decision not in await self.allowed_decisions(store, ctx, suspect):
                raise TurnError("decision_not_allowed")
            offer, offer_label, offer_error = None, None, None
            if proposal.proposed_action or proposal.supporting_memory_id:
                offer, offer_label, offer_error = await self.validate_offer(store, ctx, trace, case, proposal)
            written = []
            if not trace.replay_of:
                memories = [
                    NewMemory(id=ids.turn_memory_id(ctx.turn_id, "interaction"),
                              text="The detectives asked me (untrusted question): " + json.dumps(trace.input),
                              known_by=[ctx.viewer], kind="interaction",
                              source={"type": "interaction", "ref": ctx.turn_id}),
                    NewMemory(id=ids.turn_memory_id(ctx.turn_id, "finding"), text=reply,
                              known_by=["team:" + ctx.scope.team_id], kind="finding",
                              source={"type": "interaction", "ref": ctx.turn_id}),
                ]
                written = [m.id for m in await store.write(ctx.scope, memories, dedup=False)]
                if offer is not None:
                    saved = await store.insert_offer(offer)
                    if saved.status != "offered":
                        offer, offer_label, offer_error = None, None, "offer_no_longer_available"
            else:
                offer, offer_label = None, None  # A replay cannot create or mutate gameplay offers.
            return await traces.complete_turn(
                ctx.scope, ctx.turn_id, decision=proposal.decision, reply=reply, written=written,
                offer_id=offer.id if offer else None, offer_label=offer_label, offer_rejected_reason=offer_error,
                model=self.model, prompt_version=PROMPT_VERSION,
            )

        return await self.store.atomic_turn(self.traces, commit)

    @staticmethod
    async def validate_offer(store, ctx, trace, case, proposal):
        action = next((a for a in case.get("actions", []) if a.get("id") == proposal.proposed_action), None)
        if action is None:
            return None, None, "unknown_action"
        if proposal.supporting_memory_id not in trace.retrieved:
            return None, None, "support_not_retrieved"
        memories = await store.fetch(ctx.scope, ctx.viewer, [proposal.supporting_memory_id])
        if not memories:
            return None, None, "support_not_live"
        memory = memories[0]
        if memory.kind == "rumour" or memory.supersedes or action["id"] not in memory.unlocks:
            return None, None, "support_does_not_unlock"
        if await store.action_used(ctx.scope, action["id"]):
            return None, None, "action_already_used"
        offer = Offer(_id=ids.offer_id(ctx.scope.team_id, action["id"], memory.id),
                      case_id=ctx.scope.case_id, team_id=ctx.scope.team_id, action=action["id"],
                      supported_by=memory.id, held_by=ctx.viewer, offered_in=ctx.turn_id, ts=utcnow())
        return offer, action["label"], None
