"""Agent-facing capability interface (stretch goal).

Approved artifacts become a catalog of typed tools. An AI agent discovers them by name,
calls one with typed arguments, and gets the structured ``ReplayResult`` back. The agent
never sees the UI, the steps or the locators — only the contract.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from .config import CONFIG, list_capabilities, load_policy, load_profile, load_tenant
from .llm import DEFAULT_MODEL
from .replay import ReplayOptions, replay
from .schema import Capability, ReplayResult, ReviewStatus, ValueType

_JSON_TYPES = {ValueType.integer: "string", ValueType.decimal: "string", ValueType.currency: "string",
               ValueType.boolean: "boolean", ValueType.string: "string", ValueType.date: "string", ValueType.enum: "string"}


def tool_name(cap_id: str) -> str:
    return cap_id.replace(".", "__")


def tenants() -> list[str]:
    return sorted(p.stem for p in (CONFIG / "tenants").glob("*.json"))


def as_tool(cap: Capability) -> dict[str, Any]:
    props: dict[str, Any] = {"tenant": {"type": "string", "enum": tenants(), "description": "Institution to run against."}}
    for p in cap.inputs:
        prop: dict[str, Any] = {"type": _JSON_TYPES[p.type], "description": f"{p.description} ({p.type.value})"}
        if p.pattern:
            prop["pattern"] = f"^{p.pattern}$"
        if p.enum:
            prop["enum"] = p.enum
        props[p.name] = prop
    outs = "; ".join(f"{o.name}: {o.type.value} - {o.description}" for o in cap.outputs) or "none"
    desc = (f"{cap.description}\nReturns outputs -> {outs}.\n"
            f"May instead return a business outcome: {', '.join(cap.declared_outcomes) or 'none'}.\n"
            f"Risk: {cap.max_risk.value}. Capability {cap.ref} ({cap.review.status.value}).")
    return {"name": tool_name(cap.id), "description": desc,
            "input_schema": {"type": "object", "properties": props, "required": ["tenant"] + [p.name for p in cap.inputs if p.required]}}


def catalog(include_drafts: bool = False) -> list[tuple[Capability, dict[str, Any]]]:
    out = []
    for cap, _ in list_capabilities():
        if cap.review.status == ReviewStatus.approved or (include_drafts and cap.review.status == ReviewStatus.draft):
            out.append((cap, as_tool(cap)))
    return out


async def invoke(name: str, args: dict[str, Any], opts: ReplayOptions | None = None, include_drafts: bool = False) -> ReplayResult:
    for cap, tool in catalog(include_drafts):
        if tool["name"] == name:
            args = dict(args)
            tenant = load_tenant(args.pop("tenant"))
            profile = load_profile(cap.app.profile)
            return await replay(cap, {k: str(v) for k, v in args.items()}, tenant, profile, load_policy(profile.irreversible_labels), opts)
    raise KeyError(f"no approved capability named {name}")


def result_for_agent(r: ReplayResult) -> dict[str, Any]:
    d = r.model_dump(exclude_none=True, by_alias=True, include={"status", "outputs", "outcome", "error", "warnings", "run_id"})
    if "error" in d:
        d["error"] = {k: d["error"][k] for k in ("code", "message", "step_id", "retryable") if k in d["error"]}
    return d


async def ask(question: str, *, model: str | None = None, max_turns: int = 4, log: list[dict[str, Any]] | None = None) -> str:
    """A tiny agent: the model sees only the capability catalog as tools and answers the question."""
    key = os.environ["ANTHROPIC_API_KEY"]
    tools = [t for _, t in catalog()]
    messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
    system = ("You are a credit-union service agent. You can only act on back-office systems through the provided capability tools. "
              "Call a tool when you need data; report business outcomes (e.g. MEMBER_NOT_FOUND) plainly. Answer concisely.")
    log = log if log is not None else []
    async with httpx.AsyncClient(timeout=120) as c:
        for _ in range(max_turns):
            r = await c.post("https://api.anthropic.com/v1/messages", headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                             json={"model": model or DEFAULT_MODEL, "max_tokens": 800, "system": system, "tools": tools, "messages": messages})
            r.raise_for_status()
            data = r.json()
            messages.append({"role": "assistant", "content": data["content"]})
            uses = [b for b in data["content"] if b["type"] == "tool_use"]
            log.append({"assistant": [b for b in data["content"]], "stop_reason": data["stop_reason"]})
            if not uses:
                return "".join(b.get("text", "") for b in data["content"] if b["type"] == "text")
            results = []
            for u in uses:
                res = await invoke(u["name"], u["input"])
                payload = result_for_agent(res)
                log.append({"tool_call": u["name"], "input": u["input"], "result_status": res.status.value, "evidence": res.evidence_dir})
                results.append({"type": "tool_result", "tool_use_id": u["id"], "content": json.dumps(payload)})
            messages.append({"role": "user", "content": results})
    return "(no final answer within turn limit)"
