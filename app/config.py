"""Persistent settings: LLM provider / model / effort / API keys and the publish webhook.

Settings live in config/settings.json (git-ignored). Every interface (web portal, REST API,
MCP server, CLI/skill) reads and writes the same file, so a change made in one place is
picked up by all others on the next call.
"""
from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.environ.get("HDX_CONFIG_DIR", ROOT / "config"))
SETTINGS_PATH = CONFIG_DIR / "settings.json"
DATA_DIR = Path(os.environ.get("HDX_DATA_DIR", ROOT / "data"))

EFFORT_LEVELS = ["low", "medium", "high", "xhigh", "max"]

# Provider catalogue shown in the portal. Model lists are suggestions; any model id is accepted.
PROVIDERS: dict[str, dict[str, Any]] = {
    "anthropic": {
        "label": "Anthropic (Claude)",
        "env_key": "ANTHROPIC_API_KEY",
        "default_model": "claude-opus-5-5",
        "models": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1"],
    },
    "openai": {
        "label": "OpenAI",
        "env_key": "OPENAI_API_KEY",
        "default_model": "gpt-5",
        "models": ["gpt-5", "gpt-5-mini", "o3"],
    },
}

DEFAULTS: dict[str, Any] = {
    "llm": {
        "provider": "anthropic",
        "model": "claude-opus-5-5",
        "effort": "medium",
        "max_tokens": 32000,
        "api_keys": {"anthropic": "", "openai": ""},
    },
    "webhook": {"url": "", "timeout_seconds": 20},
}

_lock = threading.Lock()


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_settings() -> dict[str, Any]:
    with _lock:
        if SETTINGS_PATH.exists():
            try:
                raw = json.loads(SETTINGS_PATH.read_text())
            except json.JSONDecodeError:
                raw = {}
        else:
            raw = {}
    return _deep_merge(DEFAULTS, raw)


def save_settings(settings: dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with _lock:
        tmp = SETTINGS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(settings, indent=2))
        os.chmod(tmp, 0o600)  # contains API keys
        tmp.replace(SETTINGS_PATH)


def update_settings(patch: dict[str, Any]) -> dict[str, Any]:
    """Apply a partial update. Empty/masked API keys in the patch leave the stored key unchanged."""
    current = load_settings()
    patch = copy.deepcopy(patch or {})
    keys = (patch.get("llm") or {}).get("api_keys")
    if isinstance(keys, dict):
        for prov, val in list(keys.items()):
            if val is None or val == "" or (isinstance(val, str) and "•" in val):
                keys.pop(prov)
    llm = patch.get("llm") or {}
    if "effort" in llm and llm["effort"] not in EFFORT_LEVELS:
        raise ValueError(f"effort must be one of {EFFORT_LEVELS}")
    if "provider" in llm and llm["provider"] not in PROVIDERS:
        raise ValueError(f"provider must be one of {list(PROVIDERS)}")
    hook = patch.get("webhook") or {}
    if hook.get("url") and not str(hook["url"]).startswith(("http://", "https://")):
        raise ValueError("webhook url must start with http:// or https://")
    merged = _deep_merge(current, patch)
    save_settings(merged)
    return merged


def get_api_key(provider: str, settings: dict[str, Any] | None = None) -> str | None:
    settings = settings or load_settings()
    key = (settings["llm"].get("api_keys") or {}).get(provider)
    if key:
        return key
    env = PROVIDERS.get(provider, {}).get("env_key")
    return os.environ.get(env) if env else None


def _mask(key: str | None) -> str:
    if not key:
        return ""
    return key[:7] + "•" * 8 + key[-4:] if len(key) > 12 else "•" * 8


def public_settings() -> dict[str, Any]:
    """Settings safe to show in a UI: API keys are masked, with a flag saying where each comes from."""
    s = load_settings()
    out = copy.deepcopy(s)
    out["llm"]["api_keys"] = {}
    out["llm"]["api_key_status"] = {}
    for prov, meta in PROVIDERS.items():
        stored = (s["llm"].get("api_keys") or {}).get(prov)
        env = os.environ.get(meta["env_key"])
        out["llm"]["api_keys"][prov] = _mask(stored or env)
        out["llm"]["api_key_status"][prov] = "config" if stored else ("env" if env else "missing")
    out["providers"] = PROVIDERS
    out["effort_levels"] = EFFORT_LEVELS
    out["settings_path"] = str(SETTINGS_PATH)
    return out
