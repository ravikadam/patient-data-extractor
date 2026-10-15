"""Provider registry: builds the configured LLM from persisted settings."""
from __future__ import annotations

from ..config import get_api_key, load_settings
from .base import LLMError, LLMProvider, ToolSpec, extract_json


def get_llm() -> LLMProvider:
    s = load_settings()["llm"]
    provider = s["provider"]
    key = get_api_key(provider)
    if not key:
        raise LLMError(f"No API key configured for provider '{provider}'. Add it in Settings "
                       f"(portal) or via `hdx config --api-key ...`.")
    if provider == "anthropic":
        from .anthropic_provider import AnthropicProvider as cls
    elif provider == "openai":
        from .openai_provider import OpenAIProvider as cls
    else:
        raise LLMError(f"Unknown provider '{provider}'")
    return cls(api_key=key, model=s["model"], effort=s["effort"], max_tokens=int(s.get("max_tokens") or 32000))


__all__ = ["get_llm", "LLMError", "LLMProvider", "ToolSpec", "extract_json"]
