"""Command line: python -m cua <command> ...

  discover   run the LLM agent on a goal and record a draft capability
  replay     deterministically replay a capability with inputs (no model)
  approve    mark a capability reviewed/approved (unattended replay requires it)
  catalog    list approved capabilities as agent tools
  invoke     call a capability by tool name with JSON args (what an agent does)
  ask        let a model answer a question using only the capability catalog
  operator-bot  scripted operator that handles supervisor-override interventions
  schemas    write JSON Schemas of all document types to schemas/
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx

from .config import CAPABILITIES, EVIDENCE, ROOT, load_capability, load_dotenv, load_policy, load_profile, load_tenant
from .schema import ReviewStatus, json_schemas, utcnow

MOCK = "http://127.0.0.1:8600"


def _kv(items: list[str]) -> dict[str, str]:
    out = {}
    for it in items or []:
        k, v = it.split("=", 1)
        out[k] = v
    return out


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def set_chaos(tenant: str, chaos: dict[str, Any] | None) -> None:
    """Test-harness only: configure (or reset) fault injection in the mock app."""
    try:
        httpx.post(f"{MOCK}/__chaos/{tenant}", json=chaos or {}, timeout=5).raise_for_status()
    except httpx.HTTPError:
        sys.exit(f"The CoreLink mock app is not reachable at {MOCK}. Start it first (in another terminal):\n"
                 f"    python -m mockapp.server")


async def _with_console(enabled: bool, coro_fn):
    console = None
    if enabled:
        from .operator import OperatorConsole

        console = await OperatorConsole().start()
        print(f"operator console: {console.url}", file=sys.stderr)
    try:
        return await coro_fn()
    finally:
        if console:
            await console.stop()


async def cmd_discover(a: argparse.Namespace) -> int:
    from .discovery import DeclaredParam, discover
    from .llm import ScriptedDecider, make_decider

    tenant = load_tenant(a.tenant)
    profile = load_profile(a.profile)
    policy = load_policy(profile.irreversible_labels)
    decider = ScriptedDecider(a.scripted) if a.scripted else make_decider(a.model)
    params = [DeclaredParam.parse(p) for p in a.param]
    if a.chaos:
        set_chaos(a.tenant, json.loads(a.chaos))

    async def run():
        return await discover(a.goal, tenant, profile, policy, decider, params, capability_id=a.capability_id,
                              max_steps=a.max_steps, timeout_s=a.timeout, vision=not a.no_vision, escalate=a.escalate,
                              headless=not a.headed, evidence_root=a.evidence_root)

    res = await _with_console(a.escalate, run)
    _print({"status": res.status, "reason": res.reason, "run_id": res.run_id, "evidence": res.evidence_dir,
            "capability": res.capability.ref if res.capability else None, "saved_to": res.capability_path,
            "steps": res.steps, "model": decider.model, "usage": res.usage})
    return 0 if res.status == "succeeded" else 1


async def cmd_replay(a: argparse.Namespace) -> int:
    from .replay import ReplayOptions, replay

    cap, _ = load_capability(a.capability)
    tenant = load_tenant(a.tenant)
    profile = load_profile(cap.app.profile)
    policy = load_policy(profile.irreversible_labels)
    set_chaos(a.tenant, json.loads(a.chaos) if a.chaos else None)
    opts = ReplayOptions(allow_draft=a.allow_draft, confirm_irreversible=a.confirm_irreversible, escalate=a.escalate,
                         escalation_timeout_s=a.escalation_timeout, headless=not a.headed, evidence_root=a.evidence_root, apply_overlays=not a.no_overlays)

    async def run():
        bot = None
        if a.escalate and a.operator_bot:
            from .operator import TOKEN
            from .operator_bot import run_bot

            bot = asyncio.create_task(run_bot("http://127.0.0.1:8700", TOKEN, max_wait_s=a.escalation_timeout))
        r = await replay(cap, _kv(a.input), tenant, profile, policy, opts)
        if bot:
            bot.cancel()
        return r

    r = await _with_console(a.escalate, run)
    # stdout is the caller's return channel: outputs are returned unmasked here, but never persisted unmasked
    _print(r.model_dump(by_alias=True, exclude_none=True))
    return {"succeeded": 0, "business_outcome": 0}.get(r.status.value, 2)


def cmd_approve(a: argparse.Namespace) -> int:
    cap, path = load_capability(a.capability)
    cap.review.status = ReviewStatus(a.status)
    cap.review.reviewed_by = a.by
    cap.review.reviewed_at = utcnow()
    cap.review.notes = a.notes
    Path(path).write_text(cap.dump() + "\n")
    print(f"{cap.ref} -> {cap.review.status.value} by {a.by}")
    return 0


def cmd_catalog(a: argparse.Namespace) -> int:
    from .catalog import catalog

    _print([t for _, t in catalog(include_drafts=a.drafts)])
    return 0


async def cmd_invoke(a: argparse.Namespace) -> int:
    from .catalog import invoke, result_for_agent

    r = await invoke(a.tool, json.loads(a.args))
    _print(result_for_agent(r) | {"evidence_dir": r.evidence_dir})
    return 0


async def cmd_ask(a: argparse.Namespace) -> int:
    from .catalog import ask
    from .redact import Redactor

    log: list[dict[str, Any]] = []
    answer = await ask(a.question, model=a.model, log=log)
    print(answer)
    if a.log:
        Path(a.log).parent.mkdir(parents=True, exist_ok=True)
        Path(a.log).write_text(json.dumps(Redactor(pii_patterns=load_profile("corelink-teller@1").pii_patterns).scrub({"question": a.question, "turns": log, "answer": answer}), indent=2, default=str))
    return 0


async def cmd_bot(a: argparse.Namespace) -> int:
    from .operator_bot import run_bot

    await run_bot(a.console, a.token, operator=a.operator, once=not a.forever)
    return 0


def cmd_schemas(a: argparse.Namespace) -> int:
    out = ROOT / "schemas"
    out.mkdir(exist_ok=True)
    for name, sch in json_schemas().items():
        (out / f"{name}.schema.json").write_text(json.dumps(sch, indent=2) + "\n")
        print(f"schemas/{name}.schema.json")
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    p = argparse.ArgumentParser(prog="cua", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", help="LLM-driven discovery run -> draft capability")
    d.add_argument("--tenant", default="acme")
    d.add_argument("--goal", required=True)
    d.add_argument("--param", action="append", default=[], help="name=value[:type[:sensitivity]], e.g. member_id=100234:integer:pii")
    d.add_argument("--capability-id")
    d.add_argument("--profile", default="corelink-teller@1")
    d.add_argument("--model")
    d.add_argument("--max-steps", type=int, default=25)
    d.add_argument("--timeout", type=float, default=600)
    d.add_argument("--no-vision", action="store_true", help="do not send screenshots to the model")
    d.add_argument("--escalate", action="store_true", help="enable the operator console for human handoff")
    d.add_argument("--headed", action="store_true")
    d.add_argument("--scripted", help="replay decisions from an earlier llm_transcript.jsonl (offline; not a real discovery)")
    d.add_argument("--chaos", help="test harness: JSON fault injection for the mock app")
    d.add_argument("--evidence-root", default=str(EVIDENCE / "runs"))

    r = sub.add_parser("replay", help="deterministic replay (no model)")
    r.add_argument("capability", help="path or capability id")
    r.add_argument("--tenant", default="acme")
    r.add_argument("--input", action="append", default=[], help="name=value")
    r.add_argument("--allow-draft", action="store_true")
    r.add_argument("--confirm-irreversible", action="store_true")
    r.add_argument("--escalate", action="store_true", help="route blocked states to a human via the operator console")
    r.add_argument("--operator-bot", action="store_true", help="demo: let the scripted operator handle the intervention")
    r.add_argument("--escalation-timeout", type=float, default=600)
    r.add_argument("--headed", action="store_true")
    r.add_argument("--no-overlays", action="store_true", help="ignore tenant overlays (run the shared base artifact)")
    r.add_argument("--chaos", help="test harness: JSON fault injection for the mock app, e.g. '{\"notice\": true}'")
    r.add_argument("--evidence-root", default=str(EVIDENCE / "runs"))

    ap = sub.add_parser("approve", help="review a capability")
    ap.add_argument("capability")
    ap.add_argument("--by", required=True)
    ap.add_argument("--notes")
    ap.add_argument("--status", default="approved", choices=[s.value for s in ReviewStatus])

    c = sub.add_parser("catalog", help="capabilities as agent tools")
    c.add_argument("--drafts", action="store_true")

    i = sub.add_parser("invoke", help="invoke a capability tool by name")
    i.add_argument("tool")
    i.add_argument("--args", required=True, help='JSON, e.g. {"tenant":"acme","member_id":"100234"}')

    k = sub.add_parser("ask", help="a model answers using only the capability catalog")
    k.add_argument("question")
    k.add_argument("--model")
    k.add_argument("--log")

    b = sub.add_parser("operator-bot", help="scripted operator for demos")
    b.add_argument("--console", default="http://127.0.0.1:8700")
    b.add_argument("--token", required=True)
    b.add_argument("--operator", default="supervisor.kim")
    b.add_argument("--forever", action="store_true")

    sub.add_parser("schemas", help="export JSON Schemas")

    a = p.parse_args(argv)
    handlers = {"discover": cmd_discover, "replay": cmd_replay, "approve": cmd_approve, "catalog": cmd_catalog,
                "invoke": cmd_invoke, "ask": cmd_ask, "operator-bot": cmd_bot, "schemas": cmd_schemas}
    h = handlers[a.cmd]
    res = h(a)
    if asyncio.iscoroutine(res):
        res = asyncio.run(res)
    return int(res or 0)
