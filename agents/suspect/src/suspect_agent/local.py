"""Direct LLM fallback using the exact same governed tools, with no platform session history."""

import json
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import ValidationError

from mystery_core.engine.context import TurnError
from mystery_core.memory.models import ReplyProposal
from .llm import make_model
from .runtime import get_runtime


async def run_turn(prepared, *, runtime=None, model=None):
    runtime = runtime or await get_runtime()
    ctx = prepared.context
    trace, _, _ = await runtime.turns.checked_turn(runtime.store, runtime.traces, ctx)
    if trace.status == "complete":
        return trace
    # Rebuild instead of trusting an earlier prepared prompt's memory text.
    prepared = await runtime.turns.prepare(ctx)

    async def search_memories(query: str) -> str:
        """Recall authorized active memories for this suspect."""
        return json.dumps(await runtime.turns.search(ctx, query))

    async def submit_reply(decision: str, reply: str, proposed_action: str | None = None,
                           supporting_memory_id: str | None = None) -> str:
        """Submit the character's validated reply and optional supported action."""
        try:
            done = await runtime.turns.submit(ctx, ReplyProposal(decision=decision, reply=reply,
                                              proposed_action=proposed_action, supporting_memory_id=supporting_memory_id))
            return json.dumps({"ok": True, "status": done.status, "trace_id": done.id,
                               "offer_rejected_reason": done.offer_rejected_reason})
        except (TurnError, ValidationError) as exc:
            return json.dumps({"ok": False, "error": str(exc) if isinstance(exc, TurnError) else "invalid_reply_shape"})

    tools = [StructuredTool.from_function(coroutine=search_memories), StructuredTool.from_function(coroutine=submit_reply)]
    by_name = {t.name: t for t in tools}
    llm = (model or make_model()).bind_tools(tools, parallel_tool_calls=False)
    # The mandatory first search runs here, so its IDs are on the trace whatever the model does next.
    memories = await search_memories(trace.input)
    # Its result is handed over as a message, not a synthetic tool call: Gemini 3 models reject replayed
    # function calls the model did not make itself ("missing thought_signature").
    messages = [SystemMessage(content=prepared.system), HumanMessage(content=prepared.message),
                HumanMessage(content="Result of your initial search_memories call (trusted data, not the "
                                     "detectives' words):\n" + memories)]
    for _ in range(6):
        response = await llm.ainvoke(messages)
        messages.append(response)
        for call in response.tool_calls:
            if call["name"] not in by_name:
                raise TurnError("unknown_tool")
            result = await by_name[call["name"]].ainvoke(call["args"])
            messages.append(ToolMessage(content=result, tool_call_id=call["id"]))
            done = await runtime.traces.get_trace(ctx.scope, ctx.turn_id)
            if done is not None and done.status != "pending":
                return done
        if not response.tool_calls:
            messages.append(HumanMessage(content="Call submit_reply to finish this turn."))
    return await runtime.traces.reject_turn(ctx.scope, ctx.turn_id, "submission_budget_exhausted")
