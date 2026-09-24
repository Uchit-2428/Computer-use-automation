"""LLM-driven discovery: observe -> decide -> act until the goal is met, then compile
the run into a draft ``Capability``.

What the model sees each turn (stateless request, bounded tokens):
  goal (parameter values replaced by placeholders), parameter names/types, a short
  history of prior actions and their effects, the current screen as an element list
  (``[id] role "name"`` across all frames) and a screenshot with secrets masked.

What the model never sees: secret values (sign-on happens deterministically via the
app profile before the model is involved; password fields render as ••••) and raw
parameter values (it types ``{{member_id}}``; the executor substitutes).

Every model action passes the same policy gate as replay; irreversible actions are
blocked (or escalated for approval) and the refusal is fed back to the model.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import CAPABILITIES, EVIDENCE
from .control import SessionController
from .evidence import Evidence
from .llm import Decider
from .policy import Policy
from .recorder import RecordedStep, derive_expect, derive_target, step_id
from .redact import Redactor
from .replay import Executor, ReplayOptions, Stop, normalise, Reauth, NeedsHuman
from .schema import (
    AppProfile, AppRef, Capability, Checkpoint, Entry, FrameScope, OutcomeKind, OutputSpec, ParamSpec, Provenance,
    Risk, Sensitivity, Step, SurfaceKind, TenantConfig, TextVisible, TitleIs, UrlMatches, ValueType,
)
from .surface.base import Observation, Resolved
from .surface.web import WebSurface
from .templating import Renderer, Templater

SYSTEM_PROMPT = """You are the discovery agent of a computer-use automation system for bank and credit-union back-office software.
You operate a LIVE legacy application (framesets, table layouts, no ids) through tools. Your successful run is recorded and turned
into a deterministic, reusable automation, so take the most direct, repeatable path to the goal.

Each turn you get: the goal, the parameters, a history of what you did, the current screen as an element list across all frames
(format: [id] role "accessible name" (column "...")) and a screenshot. Call exactly ONE tool per turn.

Rules:
- Refer to elements by their [id] from the CURRENT element list only.
- Parameters are given as placeholders. When typing a parameter value, type the placeholder exactly, e.g. {{member_id}}.
  Never invent or guess values. Values of parameters appear on screen as their placeholder.
- Do not explore unrelated screens. Use the application's own navigation (menus, links, buttons).
- NEVER perform irreversible actions (confirming, posting, approving, transferring, deleting). If the goal is to reach a
  confirmation/review screen, stop there. The system will block irreversible clicks anyway.
- To read data the goal asks for, call `extract` on the element that holds the value itself (e.g. the table cell with the
  amount), with a snake_case output_name. Extract every requested value before finishing.
- When the goal is achieved, call `finish`. success_evidence must be a short piece of visible, stable text proving you are
  on the right screen (e.g. the screen title) - not a data value.
- Read error messages on screen. If you are blocked (supervisor approval, permission problem, an error you cannot fix,
  or you are unsure how to proceed safely) call `request_human` with a clear reason. If the goal is impossible, call `give_up`.
- Keep `reasoning` to one or two sentences. `intent` is a short imperative description of the step for a human reviewer.
"""

_R = {"reasoning": {"type": "string", "description": "One or two sentences: why this action."}}
_EL = {"element": {"type": "integer", "description": "Element [id] from the current screen."}}
_INT = {"intent": {"type": "string", "description": "Short imperative description of the step for reviewers, e.g. 'Open Member Inquiry'."}}

TOOLS: list[dict[str, Any]] = [
    {"name": "click", "description": "Click a link, button or control.",
     "input_schema": {"type": "object", "properties": {**_R, **_EL, **_INT}, "required": ["reasoning", "element", "intent"]}},
    {"name": "type_text", "description": "Replace the content of a text field. Use parameter placeholders like {{member_id}} for parameter values.",
     "input_schema": {"type": "object", "properties": {**_R, **_EL, "text": {"type": "string"}, **_INT}, "required": ["reasoning", "element", "text", "intent"]}},
    {"name": "select_option", "description": "Choose an option (by its visible text) in a drop-down.",
     "input_schema": {"type": "object", "properties": {**_R, **_EL, "option": {"type": "string"}, **_INT}, "required": ["reasoning", "element", "option", "intent"]}},
    {"name": "press_key", "description": "Press a key (e.g. Enter, Tab), optionally focused on an element.",
     "input_schema": {"type": "object", "properties": {**_R, "key": {"type": "string"}, "element": {"type": "integer"}, **_INT}, "required": ["reasoning", "key", "intent"]}},
    {"name": "extract", "description": "Read a value the goal asks for from an element and return it as a named, typed output.",
     "input_schema": {"type": "object", "properties": {**_R, **_EL,
        "output_name": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"},
        "value_type": {"type": "string", "enum": ["string", "integer", "decimal", "currency", "date"]},
        "description": {"type": "string", "description": "What the value means, for the calling agent."},
        "sensitivity": {"type": "string", "enum": ["public", "internal", "pii", "financial"]}},
        "required": ["reasoning", "element", "output_name", "value_type", "description", "sensitivity"]}},
    {"name": "finish", "description": "The goal is achieved (and all requested values extracted).",
     "input_schema": {"type": "object", "properties": {**_R,
        "success_evidence": {"type": "string", "description": "Short stable visible text proving success (e.g. screen title)."},
        "capability_id": {"type": "string", "description": "Dotted snake_case name for this reusable capability, e.g. member.get_savings_balance"},
        "title": {"type": "string"},
        "description": {"type": "string", "description": "One or two sentences telling a calling AI agent what this capability does and returns."}},
        "required": ["reasoning", "success_evidence", "capability_id", "title", "description"]}},
    {"name": "request_human", "description": "Ask a human operator to take over the live session (you are blocked or unsure).",
     "input_schema": {"type": "object", "properties": {**_R, "reason": {"type": "string"}}, "required": ["reasoning", "reason"]}},
    {"name": "give_up", "description": "The goal cannot be achieved.",
     "input_schema": {"type": "object", "properties": {**_R, "reason": {"type": "string"}}, "required": ["reasoning", "reason"]}},
]


@dataclass
class DeclaredParam:
    name: str
    value: str
    type: ValueType = ValueType.string
    sensitivity: Sensitivity = Sensitivity.internal
    description: str = ""
    pattern: str | None = None

    @classmethod
    def parse(cls, spec: str) -> "DeclaredParam":
        """'member_id=100234:integer:pii' or 'member_id=100234'"""
        name, rest = spec.split("=", 1)
        parts = rest.split(":")
        p = cls(name=name.strip(), value=parts[0])
        if len(parts) > 1 and parts[1]:
            p.type = ValueType(parts[1])
        if len(parts) > 2 and parts[2]:
            p.sensitivity = Sensitivity(parts[2])
        if p.type == ValueType.integer and not p.pattern:
            p.pattern = r"\d{1,10}"
        return p


@dataclass
class DiscoveryResult:
    status: str  # succeeded | failed
    run_id: str
    evidence_dir: str
    reason: str = ""
    capability: Capability | None = None
    capability_path: str | None = None
    steps: int = 0
    usage: dict[str, int] = field(default_factory=dict)


class _ModelView:
    """Placeholders for the model: '100234' -> '{{member_id}}' (short form)."""

    def __init__(self, params: list[DeclaredParam]):
        self.params = [p for p in params if len(p.value) >= 2]

    def hide(self, s: str) -> str:
        for p in sorted(self.params, key=lambda p: len(p.value), reverse=True):
            s = s.replace(p.value, "{{%s}}" % p.name)
        return s

    def fill(self, s: str) -> tuple[str, str]:
        """model text -> (concrete value, recorded template)"""
        templ = re.sub(r"\{\{\s*(?:inputs\.)?([a-z][a-z0-9_]*)\s*\}\}", r"{{inputs.\1}}", s)
        conc = templ
        for p in self.params:
            conc = conc.replace("{{inputs.%s}}" % p.name, p.value)
            if templ.strip() == p.value:  # model typed the literal value: template it anyway
                templ = "{{inputs.%s}}" % p.name
        if "{{inputs." in conc:
            raise ValueError(f"unknown parameter placeholder in {s!r}")
        return conc, templ


async def discover(goal: str, tenant: TenantConfig, profile: AppProfile, policy: Policy, decider: Decider,
                   params: list[DeclaredParam], *, capability_id: str | None = None, max_steps: int = 30,
                   timeout_s: float = 600, vision: bool = True, escalate: bool = False, escalation_timeout_s: float = 600,
                   headless: bool = True, evidence_root: str | None = None, save_dir: Path = CAPABILITIES) -> DiscoveryResult:
    run_id = f"discovery-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"
    redactor = Redactor(mask_currency=not policy.cfg.persist_currency_in_evidence, pii_patterns=profile.pii_patterns)
    for p in params:
        redactor.register(p.value, p.sensitivity)
    ev = Evidence(evidence_root or EVIDENCE / "runs", run_id, redactor)
    transcript = (ev.dir / "llm_transcript.jsonl").open("a", encoding="utf-8")
    view = _ModelView(params)
    tmpl = Templater({p.name: p.value for p in params}, tenant)
    goal_for_model = view.hide(goal)
    res = DiscoveryResult(status="failed", run_id=run_id, evidence_dir=str(ev.dir))
    usage = {"input_tokens": 0, "output_tokens": 0, "calls": 0}
    models_used: list[str] = []  # the provider may fail over between models mid-run
    ev.event("discovery_start", goal=goal_for_model, tenant=tenant.id, model=decider.model,
             params=[{"name": p.name, "type": p.type.value, "sensitivity": p.sensitivity.value} for p in params])

    surface = WebSurface(policy, headless=headless)
    await surface.start()
    controller = SessionController(surface, ev, redactor, run_id)
    render = Renderer({p.name: p.value for p in params}, tenant)
    ex = Executor(surface, policy, profile, tenant, render, ev, redactor, controller, rules=list(profile.rules),
                  opts=ReplayOptions(escalation_timeout_s=escalation_timeout_s), label=f"discovery: {goal_for_model}")

    recorded: list[RecordedStep] = []
    outputs: list[OutputSpec] = []
    taken: set[str] = set()
    history: list[str] = []
    human_actions = 0
    last_sigs: list[tuple[str, str]] = []
    consecutive_errors = 0
    t_end = time.monotonic() + timeout_s
    finish_args: dict[str, Any] | None = None
    entry_url = ""
    start_obs: Observation | None = None

    async def human(reason: str, kind: str) -> str:
        nonlocal human_actions
        if not escalate:
            raise Stop_("escalation needed but no operator channel is enabled (--escalate): " + reason)
        iv = await controller.escalate(kind, reason, {"goal": goal_for_model, "step": len(recorded)}, escalation_timeout_s)
        human_actions += len(iv.human_actions)
        if iv.status != "resolved":
            raise Stop_(f"escalation {iv.status}")
        await surface.settle()
        did = "; ".join(f"{a.get('type')} {a.get('role') or ''} '{a.get('name') or ''}'" for a in iv.human_actions if a.get("via") == "dom") or "no recorded actions"
        return f"HUMAN OPERATOR ({iv.operator}) handled it and handed control back. They did: {did}. Note: {iv.resolution_note or '-'}"

    try:
        await ex.authenticate()
        await surface.goto(render("{{tenant.base_url}}/main"))
        entry_url = "{{tenant.base_url}}/main"
        start_obs = await surface.observe()
        for i in range(max_steps):
            if time.monotonic() > t_end:
                res.reason = "timeout"
                break
            obs = await surface.observe()
            screen = view.hide(obs.render())
            img = await surface.screenshot_bytes(redactor.sensitive_values(), False) if vision else None
            outs = ", ".join(o.name for o in outputs) or "none"
            params_txt = "\n".join(f"- {{{{{p.name}}}}}: {p.type.value}{' - ' + p.description if p.description else ''}" for p in params) or "- (none)"
            prompt = (f"GOAL: {goal_for_model}\n\nPARAMETERS (type the placeholder; the system substitutes the real value):\n{params_txt}\n\n"
                      f"OUTPUTS EXTRACTED SO FAR: {outs}\n\nSTEP {i + 1} of max {max_steps}\n\nHISTORY:\n"
                      + ("\n".join(history[-14:]) or "(start)") + f"\n\nCURRENT SCREEN:\n{screen}")
            call = await decider.decide(SYSTEM_PROMPT, prompt, img, TOOLS)
            if call.model and call.model not in models_used:
                models_used.append(call.model)
            usage["calls"] += 1
            for k in ("input_tokens", "output_tokens"):
                usage[k] += int(call.usage.get(k, 0) or 0)
            rec = {"type": "decision", "step": i + 1, "model": call.model, "tool": call.name, "args": call.args, "text": call.text, "usage": call.usage}
            transcript.write(json.dumps(redactor.scrub(rec)) + "\n")
            transcript.flush()
            ev.event("decision", step=i + 1, tool=call.name, args=call.args, screen_sig=obs.signature())
            a = call.args
            name = call.name
            feedback = ""
            try:
                if name in ("click", "type_text", "select_option", "press_key", "extract"):
                    el = obs.by_gid.get(int(a["element"])) if a.get("element") is not None else None
                    if name != "press_key" and el is None:
                        raise ValueError(f"element [{a.get('element')}] is not on the current screen")
                    atype = {"click": "click", "type_text": "fill", "select_option": "select", "press_key": "press", "extract": "extract"}[name]
                    policy.check_action_type(atype)
                    risk = policy.classify(atype, el.role if el else None, el.name if el else a.get("key"))
                    gate = policy.gate(risk, mode="discovery")
                    ev.event("policy", step=i + 1, action=atype, risk=risk.value, allowed=gate.allowed, reason=gate.reason)
                    if not gate.allowed:
                        if gate.needs_confirmation and escalate:
                            feedback = await human(f"approval needed for irreversible action on '{el.name if el else ''}'", "approval")
                        else:
                            feedback = f"BLOCKED BY POLICY: {gate.reason}. Do not attempt irreversible actions; finish if the goal only requires reaching this screen."
                        history.append(f"{i + 1}. {name} [{a.get('element')}] -> {feedback}")
                        consecutive_errors += 1
                        continue
                    target = None
                    notes: list[str] = []
                    if el is not None:
                        target, notes = await derive_target(surface, el, "extract" if name == "extract" else "act", tmpl,
                                                            description=_describe(el, name, a))
                        handle = await surface.handle_for_gid(el.gid)
                        resolved = Resolved(el.frame, "gid", 0, 0, el.raw, handle)
                    controller.assert_automation()
                    action: dict[str, Any]
                    if name == "click":
                        await surface.click(resolved)
                        action = {"type": "click", "target": target}
                    elif name == "type_text":
                        conc, templ = view.fill(str(a["text"]))
                        await surface.fill(resolved, conc)
                        action = {"type": "fill", "target": target, "value": templ}
                    elif name == "select_option":
                        conc, templ = view.fill(str(a["option"]))
                        await surface.select(resolved, conc)
                        action = {"type": "select", "target": target, "option": templ}
                    elif name == "press_key":
                        await surface.press(str(a["key"]), resolved if el else None)
                        action = {"type": "press", "key": str(a["key"]), **({"target": target} if target else {})}
                    else:  # extract
                        raw = await surface.read(resolved)
                        vtype = ValueType(a["value_type"])
                        val = normalise(raw, vtype)
                        sens = Sensitivity(a.get("sensitivity", "internal"))
                        oname = a["output_name"]
                        if any(o.name == oname for o in outputs):
                            raise ValueError(f"output '{oname}' already extracted")
                        pattern = {ValueType.currency: r"-?\d+\.\d{2}", ValueType.integer: r"-?\d+", ValueType.decimal: r"-?\d+(\.\d+)?"}.get(vtype)
                        outputs.append(OutputSpec(name=oname, type=vtype, description=a.get("description", ""), sensitivity=sens, pattern=pattern))
                        redactor.register(str(val), sens)
                        action = {"type": "extract", "target": target, "output": oname}
                        feedback = f"extracted {oname} = {val!r} (from {raw!r})"
                    await surface.settle()
                    after = await surface.observe()
                    expect = derive_expect(obs, after, tmpl) if name != "extract" else []
                    sid = step_id({"click": "click", "type_text": "enter", "select_option": "select", "press_key": "press", "extract": "read"}[name],
                                  a.get("output_name") or (el.name if el else a.get("key", "")), taken)
                    recorded.append(RecordedStep(sid, a.get("intent") or a.get("description") or name, action, risk.value, expect, notes=notes))
                    ev.event("action", step=i + 1, step_id=sid, action=atype, element=f"{el.role} '{el.name}'" if el else None,
                             strategies=[s.kind for s in target.strategies] if target else [], expect=[c.model_dump(exclude_none=True) for c in expect], notes=notes)
                    changed = [f"{f.name}: '{f.title}' {f.path}" for f in after.frames if f.name != "_top" and any(
                        b.name == f.name and (b.path != f.path or b.title != f.title) for b in obs.frames)]
                    matched = await _matched_rules(ex, profile)
                    feedback = feedback or ("ok" + (f"; frame changed -> {view.hide(', '.join(changed))}" if changed else "; no navigation"))
                    if matched:
                        feedback += f"; screen shows known condition(s): {', '.join(matched)}"
                    consecutive_errors = 0
                    await ev.screenshot(surface, f"d{i + 1:02d}-{sid}")
                    sig = (obs.signature(), f"{name}:{a.get('element')}")
                    last_sigs.append(sig)
                    if len(last_sigs) >= 3 and len(set(last_sigs[-3:])) == 1:
                        feedback += "; STUCK: same action on the same screen three times"
                        history.append(f"{i + 1}. {name} -> {feedback}")
                        history.append(f"   -> {await human('stuck: repeated the same action three times without progress', 'stuck')}")
                        continue
                elif name == "finish":
                    finish_args = a
                    ev.event("finish", args=a)
                    break
                elif name == "request_human":
                    feedback = await human(str(a.get("reason")), "stuck")
                elif name == "give_up":
                    res.reason = f"model gave up: {a.get('reason')}"
                    break
                else:
                    raise ValueError(f"unknown tool {name}")
            except (Stop_, Stop) as e:
                res.reason = str(e)
                break
            except (Reauth, NeedsHuman) as e:
                feedback = f"ERROR: {type(e).__name__}: {e}"
                consecutive_errors += 1
            except Exception as e:  # tool-level error: tell the model, let it adapt
                feedback = f"ERROR: {type(e).__name__}: {str(e)[:200]}"
                consecutive_errors += 1
                ev.event("tool_error", step=i + 1, error=str(e))
            history.append(f"{i + 1}. {name} [{a.get('element', '')}] {a.get('intent', '')} -> {feedback}")
            if consecutive_errors >= 3:
                history.append(f"   -> {await human('three consecutive failed actions', 'stuck')}")
                consecutive_errors = 0
        else:
            res.reason = f"max steps ({max_steps}) reached"

        if finish_args is not None:
            cap = await _compile(finish_args, capability_id, goal, tenant, profile, params, recorded, outputs, start_obs, surface, tmpl,
                                 run_id, ", ".join(models_used) or decider.model, human_actions, entry_url, view)
            path = _save(cap, save_dir)
            ev.write_json("capability.json", json.loads(cap.dump()))
            res.status, res.capability, res.capability_path = "succeeded", cap, str(path)
            ev.event("capability_saved", id=cap.ref, path=str(path), steps=len(cap.steps))
            await ev.screenshot(surface, "final")
    except (Stop_, Stop) as e:
        res.reason = str(e)
    except Exception as e:
        res.reason = f"{type(e).__name__}: {e}"
        ev.event("discovery_error", error=res.reason)
        try:
            await ev.failure_bundle(surface, "discovery", (await surface.observe()).render())
        except Exception:
            pass
    finally:
        res.steps = len(recorded)
        res.usage = usage
        ev.event("discovery_end", status=res.status, reason=res.reason, steps=res.steps, usage=usage)
        ev.write_json("discovery_result.json", {"status": res.status, "reason": res.reason, "run_id": run_id, "steps": res.steps,
                                                "capability": res.capability.ref if res.capability else None, "usage": usage, "model": decider.model,
                                                "models_used": models_used})
        transcript.close()
        ev.close()
        controller.close()
        await surface.close()
    return res


class Stop_(Exception):
    pass


def _describe(el: Any, tool: str, a: dict[str, Any]) -> str:
    kind = {"textbox": "field", "combobox": "drop-down", "button": "button", "link": "link"}.get(el.role, el.role)
    if tool == "extract":
        col = (el.raw.get("table") or {}).get("column")
        return f"value cell for {a.get('output_name')}" + (f" (column '{col}')" if col else "")
    return f"{el.name or el.text[:40]} {kind}".strip()


async def _matched_rules(ex: Executor, profile: AppProfile) -> list[str]:
    out = []
    for r in profile.rules:
        try:
            if await ex.surface.check(r.when, ex.render):
                out.append(f"{r.code} ({r.kind.value})")
        except Exception:
            pass
    return out


async def _compile(fa: dict[str, Any], capability_id: str | None, goal: str, tenant: TenantConfig, profile: AppProfile,
                   params: list[DeclaredParam], recorded: list[RecordedStep], outputs: list[OutputSpec],
                   start_obs: Observation | None, surface: WebSurface, tmpl: Templater, run_id: str, model: str,
                   human_actions: int, entry_url: str, view: _ModelView) -> Capability:
    if not recorded:
        raise RuntimeError("finished without any recorded step")
    final = await surface.observe()
    # checkpoint: final state of every frame that changed during the run + the model's evidence text
    conds: list[Any] = []
    start = {f.name: f for f in (start_obs.frames if start_obs else [])}
    for f in final.frames:
        s = start.get(f.name)
        if f.name == "_top" or (s and s.path == f.path and s.title == f.title):
            continue
        conds.append(UrlMatches(pattern=tmpl.path_regex(f.path), frame=FrameScope(name=f.name)))
        if f.title:
            conds.append(TitleIs(title=tmpl.text(f.title), frame=FrameScope(name=f.name)))
    evid = tmpl.text(view.fill(str(fa.get("success_evidence", "")))[0]).strip()
    if evid and not re.search(r"\$|\d{3,}", evid) and await surface.check(TextVisible(text=evid), Renderer({p.name: p.value for p in params}, tenant)):
        conds.append(TextVisible(text=evid))
    if not conds:
        conds.append(TextVisible(text=evid or final.frames[-1].title))
    steps = [Step(id=r.id, intent=r.intent, action=r.action, risk=Risk(r.risk), expect=r.expect, source=r.source) for r in recorded]  # type: ignore[arg-type]
    cid = capability_id or re.sub(r"[^a-z0-9_.\-]", "_", str(fa.get("capability_id", "capability")).lower())
    return Capability(
        id=cid,
        version="1.0.0",
        title=str(fa.get("title") or cid),
        description=str(fa.get("description") or goal),
        app=AppRef(product=tenant.product, profile=f"{profile.id}@{profile.version.split('.')[0]}",
                   product_versions=f">={'.'.join(tenant.app_version.split('.')[:2])} <{int(tenant.app_version.split('.')[0]) + 1}",
                   surface=SurfaceKind.legacy_web),
        entry=Entry(url=entry_url, requires_session=True),
        inputs=[ParamSpec(name=p.name, type=p.type, description=p.description or p.name.replace("_", " "), sensitivity=p.sensitivity,
                          pattern=p.pattern, example=None) for p in params],
        outputs=outputs,
        steps=steps,
        checkpoint=Checkpoint(description=f"Reached the target screen: {evid or 'final screen'}", all_of=conds, require_outputs=bool(outputs)),
        outcomes=[],
        declared_outcomes=[r.code for r in profile.rules if r.kind == OutcomeKind.business],
        provenance=Provenance(model=model, discovery_run_id=run_id, goal=view.hide(goal), tenant=tenant.id,
                              app_version=tenant.app_version, human_steps=human_actions),
    )


def _save(cap: Capability, save_dir: Path) -> Path:
    save_dir.mkdir(parents=True, exist_ok=True)
    path = save_dir / f"{cap.id}.json"
    if path.exists():  # never silently overwrite a reviewed artifact: bump minor, reset review
        prev = Capability.model_validate_json(path.read_text())
        major, minor, _ = (int(x) for x in prev.version.split("."))
        cap.version = f"{major}.{minor + 1}.0"
        (save_dir / "history").mkdir(exist_ok=True)
        (save_dir / "history" / f"{prev.id}@{prev.version}.json").write_text(prev.dump())
    path.write_text(cap.dump() + "\n")
    return path
