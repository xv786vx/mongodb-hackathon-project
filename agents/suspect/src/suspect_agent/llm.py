"""Provider configuration. Constructed inside the Agent Engine entrypoint."""

import os
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI


def provider_name():
    provider = os.environ.get("LLM_PROVIDER", "openai").strip().lower()
    if provider not in {"openai", "gemini"}:
        raise ValueError("LLM_PROVIDER must be openai or gemini")
    return provider


def make_model():
    provider = provider_name()
    model, key = os.environ.get("LLM_MODEL", "").strip(), os.environ.get("LLM_API_KEY", "").strip()
    if not model or not key:
        raise ValueError("LLM_MODEL and LLM_API_KEY are required")
    if key.startswith("<") or "your " in key.lower():
        raise ValueError("LLM_API_KEY contains a placeholder; configure a real provider key")
    if provider == "gemini":
        return ChatGoogleGenerativeAI(model=model, api_key=key, vertexai=False,
                                      timeout=45, max_retries=1, max_tokens=1800)
    if model.startswith("gemini-"):
        raise ValueError("Gemini models require LLM_PROVIDER=gemini and a Google API key")
    # Do not set temperature: reasoning models such as gpt-5-mini don't accept arbitrary values.
    return ChatOpenAI(model=model, api_key=key, timeout=45, max_retries=1, max_completion_tokens=1800)


def bind_model_tools(model, tools):
    # Gemini does not accept OpenAI's parallel_tool_calls request field.
    if provider_name() == "gemini":
        return model.bind_tools(tools)
    return model.bind_tools(tools, parallel_tool_calls=False)
