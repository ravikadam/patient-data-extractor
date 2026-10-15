"""Provider-neutral LLM interface.

Everything above this layer (extraction, chat agent, custom fields) talks to `LLMProvider`
only, so swapping provider / model / effort is a settings change, not a code change.

Content parts passed to providers are plain dicts:
    {"type": "text", "text": "..."}
    {"type": "image", "media_type": "image/png", "data": "<base64>"}
    {"type": "pdf", "data": "<base64>", "name": "file.pdf"}
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable


class LLMError(RuntimeError):
    pass


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class ToolRunResult:
    text: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


ToolExecutor = Callable[[str, dict[str, Any]], str]


class LLMProvider:
    name = "base"

    def __init__(self, api_key: str, model: str, effort: str, max_tokens: int):
        self.api_key = api_key
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens

    # Single-turn generation over multimodal content; returns the text answer.
    def generate(self, system: str, parts: list[dict[str, Any]], max_tokens: int | None = None) -> str:
        raise NotImplementedError

    # Multi-step tool loop for one user turn. `history` is [{"role": "user"|"assistant", "content": str}].
    def run_tools(
        self,
        system: str,
        history: list[dict[str, str]],
        tools: list[ToolSpec],
        execute: ToolExecutor,
        max_steps: int = 10,
    ) -> ToolRunResult:
        raise NotImplementedError

    def generate_json(self, system: str, parts: list[dict[str, Any]], max_tokens: int | None = None) -> Any:
        """Generate and parse a JSON answer, with one repair attempt if the first parse fails."""
        text = self.generate(system, parts, max_tokens)
        try:
            return extract_json(text)
        except ValueError:
            repair = [
                {"type": "text", "text": "The following was supposed to be a single valid JSON value but "
                 "failed to parse. Return ONLY the corrected JSON, nothing else.\n\n" + text}
            ]
            return extract_json(self.generate("You repair malformed JSON.", repair, max_tokens))


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json(text: str) -> Any:
    candidates = [m.group(1) for m in _FENCE.finditer(text)] + [text]
    decoder = json.JSONDecoder()
    for cand in candidates:
        for i, ch in enumerate(cand):
            if ch in "{[":
                try:
                    obj, _ = decoder.raw_decode(cand[i:])
                    return obj
                except json.JSONDecodeError:
                    continue
    raise ValueError("no JSON found in model output")
