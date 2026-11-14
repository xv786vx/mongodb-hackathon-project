"""Provider configuration and the Gemini metadata boundary; no external requests."""

from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage

from suspect_agent import llm


@pytest.fixture(autouse=True)
def config(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "gemini-3.5-flash-lite")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_PROVIDER", "gemini")


def test_gemini_uses_developer_api_and_generic_key(monkeypatch):
    factory = Mock()
    monkeypatch.setattr(llm, "ChatGoogleGenerativeAI", factory)
    assert llm.make_model() is factory.return_value
    kwargs = factory.call_args.kwargs
    assert kwargs == dict(model="gemini-3.5-flash-lite", api_key="test-key", vertexai=False,
                          timeout=45, max_retries=1, max_tokens=1800)


def test_gemini_constructor_accepts_settings():
    model = llm.make_model()
    assert model.model == "gemini-3.5-flash-lite"
    assert model.max_output_tokens == 1800


def test_openai_remains_supported(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_MODEL", "gpt-5-mini")
    factory = Mock()
    monkeypatch.setattr(llm, "ChatOpenAI", factory)
    llm.make_model()
    assert factory.call_args.kwargs["max_completion_tokens"] == 1800
    model = Mock()
    llm.bind_model_tools(model, ["tool"])
    model.bind_tools.assert_called_once_with(["tool"], parallel_tool_calls=False)


def test_gemini_does_not_receive_openai_tool_options():
    model = Mock()
    llm.bind_model_tools(model, ["tool"])
    model.bind_tools.assert_called_once_with(["tool"])


@pytest.mark.parametrize("provider,model,key", [
    ("unknown", "model", "test-key"),
    ("openai", "gemini-3.5-flash-lite", "test-key"),
    ("gemini", "", "test-key"),
    ("gemini", "gemini-3.5-flash-lite", ""),
    ("gemini", "gemini-3.5-flash-lite", "<your API key>"),
])
def test_invalid_configuration_fails_without_echoing_key(monkeypatch, provider, model, key):
    monkeypatch.setenv("LLM_PROVIDER", provider)
    monkeypatch.setenv("LLM_MODEL", model)
    monkeypatch.setenv("LLM_API_KEY", key)
    with pytest.raises(ValueError) as exc:
        llm.make_model()
    if key:
        assert key not in str(exc.value)


def test_agent_engine_message_roundtrip_preserves_gemini_signature():
    from agent_engine_sdk_langgraph.messages import lc_to_platform_message, platform_to_lc_message
    original = AIMessage(content="", tool_calls=[{
        "name": "search_memories", "args": {"query": "test"}, "id": "call-1"}],
        additional_kwargs={"__gemini_function_call_thought_signatures__": {"call-1": "signature"}})
    restored = platform_to_lc_message(lc_to_platform_message(original))
    assert restored.additional_kwargs == original.additional_kwargs
    assert restored.tool_calls == original.tool_calls
