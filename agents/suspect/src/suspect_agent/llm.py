"""Provider configuration. Constructed inside the Agent Engine entrypoint."""

import os
from langchain_openai import ChatOpenAI

# Providers reached through the OpenAI-compatible chat API. Gemini exposes one at this base URL.
BASE_URLS = {
    "openai": None,
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
}


def make_model():
    provider = os.environ.get("LLM_PROVIDER", "openai").strip().lower()
    if provider not in BASE_URLS:
        raise ValueError(f"LLM_PROVIDER must be one of {sorted(BASE_URLS)}")
    model, key = os.environ.get("LLM_MODEL", "").strip(), os.environ.get("LLM_API_KEY", "").strip()
    if not model or not key:
        raise ValueError("LLM_MODEL and LLM_API_KEY are required")
    if key.startswith("<") or "your " in key.lower():
        raise ValueError("LLM_API_KEY contains a placeholder; configure a real provider key")
    # Do not set temperature: reasoning models such as gpt-5-mini don't accept arbitrary values.
    return ChatOpenAI(model=model, api_key=key, base_url=BASE_URLS[provider],
                      timeout=45, max_retries=1, max_completion_tokens=1800)
