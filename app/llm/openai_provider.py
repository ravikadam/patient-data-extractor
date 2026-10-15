from __future__ import annotations

import json
from typing import Any

from .base import LLMError, LLMProvider, ToolExecutor, ToolRunResult, ToolSpec

# Our effort scale -> OpenAI reasoning_effort
_EFFORT = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        try:
            import openai
        except ImportError as e:  # pragma: no cover
            raise LLMError("The 'openai' package is not installed.") from e
        self._openai = openai
        self.client = openai.OpenAI(api_key=self.api_key, max_retries=3)

    @staticmethod
    def _to_content(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for p in parts:
            if p["type"] == "text":
                out.append({"type": "text", "text": p["text"]})
            elif p["type"] == "image":
                out.append({"type": "image_url", "image_url": {"url": f"data:{p['media_type']};base64,{p['data']}"}})
            elif p["type"] == "pdf":
                out.append({"type": "file", "file": {"filename": p.get("name") or "document.pdf",
                                                     "file_data": f"data:application/pdf;base64,{p['data']}"}})
        return out

    def _call(self, messages, tools=None, max_tokens=None):
        params: dict[str, Any] = dict(model=self.model, messages=messages,
                                      max_completion_tokens=max_tokens or self.max_tokens)
        if self.model.startswith(("o", "gpt-5")):
            params["reasoning_effort"] = _EFFORT.get(self.effort, "medium")
        if tools:
            params["tools"] = tools
        oa = self._openai
        try:
            return self.client.chat.completions.create(**params)
        except oa.AuthenticationError as e:
            raise LLMError("OpenAI API key is invalid or missing - set it in Settings.") from e
        except oa.NotFoundError as e:
            raise LLMError(f"Model '{self.model}' not found for this API key.") from e
        except oa.APIStatusError as e:
            raise LLMError(f"OpenAI API error {e.status_code}: {e.message}") from e
        except oa.APIConnectionError as e:
            raise LLMError("Could not reach the OpenAI API.") from e

    def generate(self, system, parts, max_tokens=None) -> str:
        resp = self._call([{"role": "system", "content": system},
                           {"role": "user", "content": self._to_content(parts)}], max_tokens=max_tokens)
        choice = resp.choices[0]
        if choice.finish_reason == "length":
            raise LLMError("Model output was truncated (max_tokens). Increase max_tokens in settings.")
        return choice.message.content or ""

    def run_tools(self, system, history, tools: list[ToolSpec], execute: ToolExecutor, max_steps=10) -> ToolRunResult:
        tool_defs = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                       "parameters": t.input_schema}} for t in tools]
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}] + [dict(m) for m in history]
        calls, texts = [], []
        for _ in range(max_steps):
            resp = self._call(messages, tools=tool_defs)
            m = resp.choices[0].message
            messages.append(m.model_dump(exclude_none=True))
            if m.content:
                texts.append(m.content)
            if not m.tool_calls:
                break
            for tc in m.tool_calls:
                try:
                    inp = json.loads(tc.function.arguments or "{}")
                    out = execute(tc.function.name, inp)
                    calls.append({"tool": tc.function.name, "input": inp})
                except Exception as e:
                    out = f"Error: {e}"
                    calls.append({"tool": tc.function.name, "error": str(e)})
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": out})
        return ToolRunResult(text="\n\n".join(texts).strip(), tool_calls=calls)
