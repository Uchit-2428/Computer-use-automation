"""Model access for discovery. The only place a model is called.

``Decider`` is the seam: the discovery loop hands it a system prompt, a user turn
(text + optional screenshot) and tool definitions, and gets back exactly one tool call.

* ``AnthropicDecider`` — Claude via the Messages API (tool use, forced single tool call).
* ``GeminiDecider``    — Gemini via the Generative Language API (function calling, mode ANY).
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
DEFAULT_GEMINI_MODEL = os.environ.get("CUA_GEMINI_MODEL", "gemini-3.6-flash")
GEMINI_BASE = os.environ.get("CUA_GEMINI_BASE_URL", "https://generativelanguage.googleapis.com")


def provider() -> str:
    """CUA_PROVIDER=anthropic|gemini, else whichever key is configured (Anthropic first)."""
    p = os.environ.get("CUA_PROVIDER")
    if p:
        return p
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        return "gemini"
    raise RuntimeError("no model key: set ANTHROPIC_API_KEY or GEMINI_API_KEY (needed for discovery only; replay never calls a model)")


def make_decider(model: str | None = None) -> "Decider":
    return GeminiDecider(model) if provider() == "gemini" else AnthropicDecider(model)


_GEMINI_SCHEMA_KEYS = {"type", "description", "properties", "required", "enum", "items", "nullable", "format"}


def gemini_schema(s: Any) -> Any:
    """JSON Schema -> the OpenAPI subset Gemini function declarations accept."""
    if isinstance(s, dict):
        out = {k: gemini_schema(v) if k in ("properties", "items") else v for k, v in s.items() if k in _GEMINI_SCHEMA_KEYS}
        if "properties" in out:
            out["properties"] = {k: gemini_schema(v) for k, v in s["properties"].items()}
        return out
    return s


def gemini_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"functionDeclarations": [{"name": t["name"], "description": t["description"], "parameters": gemini_schema(t["input_schema"])} for t in tools]}]


_RESOLVED_GEMINI: dict[str, str] = {}


async def _flash_models(client: httpx.AsyncClient, key: str) -> list[str]:
    """Available 'flash' models that support generateContent, newest first (models get retired)."""
    import re as _re

    r = await client.get(f"{GEMINI_BASE}/v1beta/models", params={"pageSize": 200}, headers={"x-goog-api-key": key})
    if r.status_code != 200:
        return []
    names = [m["name"].split("/", 1)[1] for m in r.json().get("models", [])
             if "generateContent" in m.get("supportedGenerationMethods", []) and "flash" in m["name"]
             and not any(x in m["name"] for x in ("image", "tts", "audio", "live", "exp", "embedding"))]

    def rank(n: str) -> tuple:
        v = tuple(int(x) for x in _re.findall(r"\d+", n)[:2])
        return ("lite" not in n, v, "preview" not in n)  # full flash models before lite ones

    return sorted(set(names), key=rank, reverse=True)


async def gemini_generate(client: httpx.AsyncClient, model: str, key: str, body: dict[str, Any]) -> dict[str, Any]:
    """generateContent with: patient backoff on 429/5xx (free tier), switch to the API-named
    replacement when a model is retired (404), and fail over to another flash model when one
    stays overloaded (503 'high demand')."""
    import asyncio
    import re as _re

    requested = model
    model = _RESOLVED_GEMINI.get(model, model)
    tried: set[str] = set()
    overloaded = 0
    for attempt in range(12):
        r = await client.post(f"{GEMINI_BASE}/v1beta/models/{model}:generateContent", json=body,
                              headers={"x-goog-api-key": key, "content-type": "application/json"})
        if r.status_code == 200:
            data = r.json()
            data.setdefault("modelVersion", model)
            return data
        tried.add(model)
        if r.status_code == 404 or (r.status_code == 503 and overloaded >= 2):
            m = _re.search(r"models/([a-z0-9.\-]+) for", r.text) if r.status_code == 404 else None
            alts = [m.group(1)] if m else [n for n in await _flash_models(client, key) if n not in tried]
            if alts:
                print(f"[gemini] {model} -> {alts[0]} ({r.status_code})", flush=True)
                _RESOLVED_GEMINI[requested] = alts[0]  # later calls in this run go straight to it
                model, overloaded = alts[0], 0
                continue
        if r.status_code in (429, 500, 502, 503, 504):
            overloaded += r.status_code == 503
            wait = min(60, 5 * (attempt + 1))
            print(f"[gemini] {r.status_code} from {model}; retrying in {wait}s", flush=True)
            await asyncio.sleep(wait)
            continue
        raise RuntimeError(f"Gemini API error {r.status_code}: {r.text[:300]}")
    raise RuntimeError("Gemini API: retries exhausted (service overloaded); try again in a few minutes")


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


class GeminiDecider:
    def __init__(self, model: str | None = None, api_key: str | None = None):
        self.model = model or DEFAULT_GEMINI_MODEL
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY is not set (needed for discovery only; replay never calls a model)")
        self.client = httpx.AsyncClient(timeout=120)

    async def decide(self, system: str, text: str, image_jpeg: bytes | None, tools: list[dict[str, Any]]) -> ToolCall:
        parts: list[dict[str, Any]] = []
        if image_jpeg:
            parts.append({"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(image_jpeg).decode()}})
        parts.append({"text": text})
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": parts}],
            "tools": gemini_tools(tools),
            "toolConfig": {"functionCallingConfig": {"mode": "ANY"}},
        }
        data = await gemini_generate(self.client, self.model, self.api_key, body)
        cparts = ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
        calls = [p["functionCall"] for p in cparts if "functionCall" in p]
        texts = [p["text"] for p in cparts if "text" in p]
        if not calls:
            raise RuntimeError(f"model returned no function call: {str(data)[:300]}")
        um = data.get("usageMetadata", {})
        usage = {"input_tokens": um.get("promptTokenCount", 0), "output_tokens": um.get("candidatesTokenCount", 0)}
        args = dict(calls[0].get("args") or {})
        if "element" in args and isinstance(args["element"], float):
            args["element"] = int(args["element"])
        return ToolCall(calls[0]["name"], args, "\n".join(texts), usage, data.get("modelVersion", self.model))


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
