import importlib
import inspect
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool

from mystery_core.engine.context import HEADER_NAMES, TurnContext, TurnError
from mystery_core.engine.turn import TurnService
from mystery_core.memory import FakeTraceStore


class ScriptedModel:
    def __init__(self, replies):
        self.replies, self.inputs = iter(replies), []

    def bind_tools(self, tools, **kwargs):
        self.tools = tools
        return self

    async def ainvoke(self, messages):
        self.inputs.append(list(messages))
        return next(self.replies)


def submitted(text="I was in the pantry.", decision="lie"):
    return AIMessage(content="", tool_calls=[{"name": "submit_reply", "args": {"decision": decision, "reply": text}, "id": "reply"}])


@pytest.fixture
def graph_module(monkeypatch):
    monkeypatch.setenv("RUNNER_MODE", "tool")
    from agent_engine_sdk_langgraph import App
    # App.run is mandatory in a deployment module but must not start a server in pytest.
    monkeypatch.setattr(App, "run", lambda self: None)
    return importlib.import_module("suspect_agent.graph")


@pytest.fixture
async def agent_runtime(store, team_a):
    traces = FakeTraceStore()
    turns = TurnService(store, traces, model="test")
    ctx = TurnContext(team_a, "butler", "t_agent", "interrogate")
    await traces.begin_turn(team_a, ctx.turn_id, ctx.viewer, ctx.command, "Where were you after midnight?")
    return SimpleNamespace(store=store, traces=traces, turns=turns), ctx


async def configured_graph(module, runtime, ctx, model, monkeypatch, finishes):
    async def get_runtime():
        return runtime
    headers = dict(zip(HEADER_NAMES, [ctx.scope.case_id, ctx.scope.team_id, ctx.suspect_id, ctx.turn_id, ctx.command]))
    monkeypatch.setattr(module, "get_runtime", get_runtime)
    monkeypatch.setattr(module, "get_current_custom_headers", lambda: headers)
    tools = [StructuredTool.from_function(coroutine=module.search_memories),
             StructuredTool.from_function(coroutine=module.submit_reply)]
    return module.create_graph(model, tools, runtime_provider=get_runtime, headers_provider=lambda: headers,
                               finish_session=lambda: finishes.append(True))


def test_tools_have_no_scope_parameters(graph_module):
    assert list(inspect.signature(graph_module.search_memories).parameters) == ["query"]
    assert list(inspect.signature(graph_module.submit_reply).parameters) == [
        "decision", "reply", "proposed_action", "supporting_memory_id"]


async def test_missing_header_fails_before_database_access(graph_module, monkeypatch):
    monkeypatch.setattr(graph_module, "get_current_custom_headers", lambda: {})
    async def forbidden():
        raise AssertionError("database must not be reached")
    monkeypatch.setattr(graph_module, "get_runtime", forbidden)
    with pytest.raises(TurnError):
        await graph_module.search_memories("query")
    with pytest.raises(TurnError):
        await graph_module.submit_reply("truth", "reply")


async def test_graph_discards_prior_chat_and_posts_only_validated_trace(graph_module, agent_runtime, monkeypatch):
    runtime, ctx = agent_runtime
    model, finishes = ScriptedModel([submitted()]), []
    graph = await configured_graph(graph_module, runtime, ctx, model, monkeypatch, finishes)
    state = await graph.ainvoke({"messages": [HumanMessage(content="STALE SECRET from previous team"),
                                             AIMessage(content="The gardener's old note unlocks the cellar.")]})
    assert "STALE SECRET" not in str(model.inputs)
    assert "old note unlocks" not in str(model.inputs)
    assert isinstance(model.inputs[0][-1], HumanMessage)
    assert not any(isinstance(m, (AIMessage, ToolMessage)) for m in model.inputs[0])
    trace = await runtime.traces.get_trace(ctx.scope, ctx.turn_id)
    assert trace.status == "complete" and trace.retrieved
    assert state["messages"][-1].content == trace.reply
    assert finishes == [True]


async def test_graph_allows_correcting_a_refused_confession(graph_module, agent_runtime, monkeypatch):
    runtime, ctx = agent_runtime
    model, finishes = ScriptedModel([submitted("I confess.", "crack"), submitted("I have nothing more to add.", "deflect")]), []
    graph = await configured_graph(graph_module, runtime, ctx, model, monkeypatch, finishes)
    await graph.ainvoke({"messages": []})
    trace = await runtime.traces.get_trace(ctx.scope, ctx.turn_id)
    assert trace.decision == "deflect" and "decision_not_allowed" in str(model.inputs[-1])


async def test_graph_preserves_model_tool_call_metadata(graph_module, agent_runtime, monkeypatch):
    runtime, ctx = agent_runtime
    search = AIMessage(content="", tool_calls=[{
        "name": "search_memories", "args": {"query": "pantry"}, "id": "gemini-call"}],
        additional_kwargs={"__gemini_function_call_thought_signatures__": {"gemini-call": "signature"}})
    model = ScriptedModel([search, submitted()])
    graph = await configured_graph(graph_module, runtime, ctx, model, monkeypatch, [])
    await graph.ainvoke({"messages": []})
    assert any(m is search for m in model.inputs[1])


async def test_graph_rejects_raw_unsubmitted_output(graph_module, agent_runtime, monkeypatch):
    runtime, ctx = agent_runtime
    model, finishes = ScriptedModel([AIMessage(content="UNVALIDATED") for _ in range(6)]), []
    graph = await configured_graph(graph_module, runtime, ctx, model, monkeypatch, finishes)
    result = await graph.ainvoke({"messages": []})
    trace = await runtime.traces.get_trace(ctx.scope, ctx.turn_id)
    assert trace.status == "rejected" and trace.written == []
    assert "UNVALIDATED" not in result["messages"][-1].content
    assert finishes == [True]


async def test_completed_retry_does_not_call_model(graph_module, agent_runtime, monkeypatch):
    runtime, ctx = agent_runtime
    from mystery_core.memory.models import ReplyProposal
    await runtime.turns.submit(ctx, ReplyProposal(decision="lie", reply="Saved reply."))
    model, finishes = ScriptedModel([]), []
    graph = await configured_graph(graph_module, runtime, ctx, model, monkeypatch, finishes)
    result = await graph.ainvoke({"messages": []})
    assert result["messages"][-1].content == "Saved reply." and model.inputs == []


async def test_local_fallback_uses_same_validation(agent_runtime):
    from suspect_agent.local import run_turn
    runtime, ctx = agent_runtime
    prepared = await runtime.turns.prepare(ctx)
    model = ScriptedModel([submitted("[laughs] Saved locally.")])
    result = await run_turn(prepared, runtime=runtime, model=model)
    assert result.reply == "Saved locally." and result.status == "complete" and result.retrieved
    assert isinstance(model.inputs[0][-1], HumanMessage)
    assert not any(isinstance(m, (AIMessage, ToolMessage)) for m in model.inputs[0])


async def test_hosted_client_header_body_and_service_account_contract(agent_runtime, monkeypatch):
    from suspect_agent.client import invoke
    runtime, ctx = agent_runtime
    prepared = await runtime.turns.prepare(ctx)
    for key, value in {"AGENT_ENGINE_PROJECT_ID": "project", "AGENT_ENGINE_WORKSPACE_ID": "workspace",
                        "AGENT_ENGINE_CLIENT_ID": "client", "AGENT_ENGINE_CLIENT_SECRET": "secret"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("AGENT_ENGINE_ACCESS_TOKEN", raising=False)
    requests = []
    def respond(request):
        requests.append(request)
        if request.url.path == "/api/v1/oauth/token":
            assert request.headers["Authorization"].startswith("Basic ")
            return httpx.Response(200, json={"access_token": "test-token"})
        assert request.headers["Authorization"] == "Bearer test-token"
        assert request.headers["X-Agent-Engine-Invoke-Timeout-Seconds"] == "60"
        assert "X-Session-ID" not in request.headers
        import json
        assert json.loads(request.content) == {"message": prepared.message}
        for name, value in ctx.custom_headers().items():
            assert request.headers[name] == value
        return httpx.Response(200, json={"status": "completed", "response": "DO NOT POST", "execution_id": "exec"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        assert await invoke(prepared, client=client) == {"status": "completed", "execution_id": "exec"}
    assert len(requests) == 2
