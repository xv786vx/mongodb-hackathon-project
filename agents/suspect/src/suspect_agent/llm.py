"""Provider configuration. Constructed inside the Agent Engine entrypoint."""

import os
from langchain_openai import ChatOpenAI


def make_model():
    if os.environ.get("LLM_PROVIDER", "openai") != "openai":
        raise ValueError("LLM_PROVIDER must be openai for this deployment")
    model, key = os.environ.get("LLM_MODEL", "").strip(), os.environ.get("LLM_API_KEY", "").strip()
    if not model or not key:
        raise ValueError("LLM_MODEL and LLM_API_KEY are required")
    if key.startswith("<") or "your " in key.lower():
        raise ValueError("LLM_API_KEY contains a placeholder; configure a real provider key")
    # Do not set temperature: reasoning models such as gpt-5-mini don't accept arbitrary values.
    return ChatOpenAI(model=model, api_key=key, timeout=45, max_retries=1, max_completion_tokens=1800)
