from __future__ import annotations

import json
from typing import Any

import anthropic

from .base import LLMError, LLMProvider, ToolExecutor, ToolRunResult, ToolSpec

# Models that support the server-side refusal fallback ("default" routing by refusal category).
_FALLBACK_MODELS = ("claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5")
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.client = anthropic.Anthropic(api_key=self.api_key, max_retries=3)
        self._use_fallback = self.model.startswith(_FALLBACK_MODELS)

    # ---- helpers -------------------------------------------------------------------------
    @staticmethod
    def _to_blocks(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        blocks = []
        for p in parts:
            if p["type"] == "text":
                blocks.append({"type": "text", "text": p["text"]})
            elif p["type"] == "image":
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": p["media_type"], "data": p["data"]}})
            elif p["type"] == "pdf":
                blocks.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": p["data"]},
                               "title": p.get("name")})
        return blocks

    def _call(self, **kwargs) -> Any:
        params = dict(
            model=self.model,
            max_tokens=kwargs.pop("max_tokens", None) or self.max_tokens,
            thinking={"type": "adaptive"},
            output_config={"effort": self.effort},
            **kwargs,
        )
        try:
            if self._use_fallback:
                try:
                    with self.client.beta.messages.stream(
                        betas=[_FALLBACK_BETA], fallbacks="default", **params
                    ) as stream:
                        msg = stream.get_final_message()
                except anthropic.BadRequestError as e:
                    if "fallback" not in str(e).lower():
                        raise
                    self._use_fallback = False  # account/model does not accept it: run without
                    return self._call(**kwargs | {"max_tokens": params["max_tokens"]})
            else:
                with self.client.messages.stream(**params) as stream:
                    msg = stream.get_final_message()
        except anthropic.AuthenticationError as e:
            raise LLMError("Anthropic API key is invalid or missing - set it in Settings.") from e
        except anthropic.NotFoundError as e:
            raise LLMError(f"Model '{self.model}' not found for this API key.") from e
        except anthropic.RateLimitError as e:
            raise LLMError("Anthropic rate limit hit - retry shortly.") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Anthropic API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError("Could not reach the Anthropic API.") from e
        if msg.stop_reason == "refusal":
            raise LLMError("The model declined this request (refusal).")
        return msg

    @staticmethod
    def _text(msg) -> str:
        return "".join(b.text for b in msg.content if b.type == "text")

    # ---- interface -----------------------------------------------------------------------
    def generate(self, system, parts, max_tokens=None) -> str:
        msg = self._call(system=system, max_tokens=max_tokens,
                         messages=[{"role": "user", "content": self._to_blocks(parts)}])
        if msg.stop_reason == "max_tokens":
            raise LLMError("Model output was truncated (max_tokens). Increase max_tokens in settings.")
        return self._text(msg)

    def run_tools(self, system, history, tools: list[ToolSpec], execute: ToolExecutor, max_steps=10) -> ToolRunResult:
        tool_defs = [{"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in tools]
        messages: list[dict[str, Any]] = [{"role": m["role"], "content": m["content"]} for m in history]
        calls: list[dict[str, Any]] = []
        texts: list[str] = []
        for _ in range(max_steps):
            msg = self._call(system=system, messages=messages, tools=tool_defs, tool_choice={"type": "auto"})
            # Append the full content (thinking blocks included) so the history stays append-only.
            messages.append({"role": "assistant", "content": [
                b.model_dump(exclude_none=True) for b in msg.content
                if b.type in ("text", "thinking", "redacted_thinking", "tool_use")]})
            if t := self._text(msg):
                texts.append(t)
            if msg.stop_reason != "tool_use":
                break
            results = []
            for block in msg.content:
                if block.type != "tool_use":
                    continue
                inp = block.input if isinstance(block.input, dict) else json.loads(block.input or "{}")
                try:
                    out = execute(block.name, inp)
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": out})
                    calls.append({"tool": block.name, "input": inp})
                except Exception as e:  # report tool errors back to the model rather than failing the turn
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": f"Error: {e}", "is_error": True})
                    calls.append({"tool": block.name, "input": inp, "error": str(e)})
            messages.append({"role": "user", "content": results})
        return ToolRunResult(text="\n\n".join(texts).strip(), tool_calls=calls)
