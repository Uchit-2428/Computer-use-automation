"""Model access for discovery. The only place a model is called.

``Decider`` is the seam: the discovery loop hands it a system prompt, a user turn
(text + optional screenshot) and tool definitions, and gets back exactly one tool call.

* ``AnthropicDecider`` — Claude via the Messages API (tool use, forced single tool call).
* ``ScriptedDecider``  — replays the decisions of an earlier *real* run from its
  transcript. Used for offline tests / demos without an API key; it is clearly labelled
  as such in evidence and never counts as the required genuine discovery run.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx

DEFAULT_MODEL = os.environ.get("CUA_MODEL", "claude-sonnet-4-5")


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]
    text: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    model: str = ""


class Decider(Protocol):
    model: str

    async def decide(self, system: str, text: str, image_jpeg: bytes | None, tools: list[dict[str, Any]]) -> ToolCall: ...


class AnthropicDecider:
    def __init__(self, model: str | None = None, api_key: str | None = None, max_tokens: int = 1024):
        self.model = model or DEFAULT_MODEL
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set (needed for discovery only; replay never calls a model)")
        self.max_tokens = max_tokens
        self.base = os.environ.get("CUA_ANTHROPIC_BASE_URL", "https://api.anthropic.com")
        self.client = httpx.AsyncClient(timeout=120)

    async def decide(self, system: str, text: str, image_jpeg: bytes | None, tools: list[dict[str, Any]]) -> ToolCall:
        content: list[dict[str, Any]] = []
        if image_jpeg:
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(image_jpeg).decode()}})
        content.append({"type": "text", "text": text})
        body = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "tools": tools,
            "tool_choice": {"type": "any", "disable_parallel_tool_use": True},
            "messages": [{"role": "user", "content": content}],
        }
        for attempt in range(4):
            r = await self.client.post(f"{self.base}/v1/messages", json=body, headers={
                "x-api-key": self.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
            if r.status_code in (429, 500, 502, 503, 529) and attempt < 3:
                import asyncio

                await asyncio.sleep(2 * (attempt + 1))
                continue
            if r.status_code != 200:
                raise RuntimeError(f"Anthropic API error {r.status_code}: {r.text[:300]}")
            break
        data = r.json()
        texts = [b["text"] for b in data["content"] if b["type"] == "text"]
        uses = [b for b in data["content"] if b["type"] == "tool_use"]
        if not uses:
            raise RuntimeError("model returned no tool call")
        return ToolCall(uses[0]["name"], uses[0]["input"], "\n".join(texts), data.get("usage", {}), data.get("model", self.model))


class ScriptedDecider:
    """Replays tool calls recorded in a previous discovery transcript (llm_transcript.jsonl)."""

    def __init__(self, transcript: str | Path):
        self.calls = [json.loads(line) for line in Path(transcript).read_text().splitlines() if line.strip()]
        self.calls = [c for c in self.calls if c.get("type") == "decision"]
        self.model = "scripted:" + (self.calls[0].get("model", "?") if self.calls else "?")
        self.i = 0

    async def decide(self, system: str, text: str, image_jpeg: bytes | None, tools: list[dict[str, Any]]) -> ToolCall:
        if self.i >= len(self.calls):
            return ToolCall("give_up", {"reasoning": "script exhausted", "reason": "script exhausted"})
        c = self.calls[self.i]
        self.i += 1
        return ToolCall(c["tool"], c["args"], c.get("text", ""), {}, self.model)
