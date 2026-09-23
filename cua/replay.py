"""Deterministic replay: the production execution path. No model in the loop.

For each step:
  1. resolve the target (ordered strategies; unique match required; polls until the
     step timeout, running the screen classifier on every poll)
  2. re-classify risk against the *live* element and gate it through policy
  3. act, verify input was accepted (read-back), settle
  4. wait for the step's postconditions, running the classifier on every poll
Finally verify the checkpoint and return typed outputs.

The screen classifier (``OutcomeRule`` list: capability rules, then product profile
rules) turns whatever the app shows into exactly one of:
  business     -> stop, return RunStatus.business_outcome (a legitimate answer)
  recoverable  -> perform the declared recovery (click / reload / re-auth) and continue
  failure      -> stop, RunStatus.failed with a debuggable ErrorInfo + evidence bundle
  escalate     -> pause, hand the live session to an operator, resume on hand-back
"""

from __future__ import annotations

import asyncio
import re
import secrets
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from .config import EVIDENCE, load_overlays, version_satisfies
from .control import SessionController
from .evidence import Evidence
from .policy import Policy, PolicyViolation
from .redact import Redactor
from .schema import (
    AppProfile, Capability, Click, ErrorInfo, Extract, Fill, InterventionRecord, Navigate, Outcome, OutcomeKind,
    OutcomeRule, ParamSpec, Press, RecoveryRecord, ReplayResult, ReviewStatus, Risk, RunStatus, Select, Sensitivity,
    Step, StepTrace, TenantConfig, TenantOverlay, ValueType, WaitFor,
)
from .surface.base import ResolveError
from .surface.web import WebSurface
from .templating import Renderer, SecretError

POLL_S = 0.25


# ============================================================ control-flow signals
class Stop(Exception):
    """Terminates the run with a final status."""

    def __init__(self, status: RunStatus, *, outcome: Outcome | None = None, error: ErrorInfo | None = None):
        super().__init__(error.message if error else (outcome.code if outcome else status.value))
        self.status, self.outcome, self.error = status, outcome, error


class Reauth(Exception):
    pass


class NeedsHuman(Exception):
    def __init__(self, kind: str, code: str, reason: str):
        super().__init__(reason)
        self.kind, self.code, self.reason = kind, code, reason


# ============================================================ options
@dataclass
class ReplayOptions:
    allow_draft: bool = False
    confirm_irreversible: bool = False
    escalate: bool = False  # hard failures / escalate-rules go to a human instead of failing
    escalation_timeout_s: float = 600
    max_reauth: int = 1
    headless: bool = True
    evidence_root: str = str(EVIDENCE / "runs")
    screenshot_every_step: bool = True
    apply_overlays: bool = True  # False = run the shared base artifact as-is (drift diagnosis)


# ============================================================ helpers
def validate_inputs(specs: list[ParamSpec], inputs: dict[str, Any]) -> list[str]:
    errs = []
    known = {p.name for p in specs}
    for k in inputs:
        if k not in known:
            errs.append(f"unknown input '{k}'")
    for p in specs:
        if p.name not in inputs or inputs[p.name] in (None, ""):
            if p.required:
                errs.append(f"missing required input '{p.name}'")
            continue
        v = str(inputs[p.name])
        if p.type == ValueType.integer and not re.fullmatch(r"-?\d+", v):
            errs.append(f"'{p.name}' must be an integer")
        if p.type in (ValueType.decimal, ValueType.currency) and not re.fullmatch(r"-?\d+(\.\d+)?", v):
            errs.append(f"'{p.name}' must be a decimal number")
        if p.pattern and not re.fullmatch(p.pattern, v):
            errs.append(f"'{p.name}' does not match {p.pattern}")
        if p.enum and v not in p.enum:
            errs.append(f"'{p.name}' must be one of {p.enum}")
    return errs


def normalise(raw: str, vtype: ValueType) -> Any:
    s = raw.strip()
    if vtype == ValueType.currency or vtype == ValueType.decimal:
        neg = s.startswith("(") or s.startswith("-")
        digits = re.sub(r"[^\d.]", "", s)
        try:
            d = Decimal(digits)
        except InvalidOperation:
            raise ValueError(f"not a number: {s!r}")
        d = -d if neg else d
        return f"{d:.2f}" if vtype == ValueType.currency else str(d)
    if vtype == ValueType.integer:
        return int(re.sub(r"[^\d-]", "", s))
    if vtype == ValueType.boolean:
        return s.lower() in ("y", "yes", "true", "1", "on")
    return s


def apply_overlays(cap: Capability, overlays: list[TenantOverlay]) -> tuple[Capability, list[str]]:
    if not overlays:
        return cap, []
    data = cap.model_dump(by_alias=True)
    applied = []
    for ov in overlays:
        for step in data["steps"]:
            if step["id"] in ov.target_overrides and "target" in step["action"]:
                step["action"]["target"] = ov.target_overrides[step["id"]].model_dump()
            if step["id"] in ov.expect_overrides:
                step["expect"] = [c.model_dump() for c in ov.expect_overrides[step["id"]]]
        if ov.checkpoint:
            data["checkpoint"] = ov.checkpoint.model_dump()
        data["outcomes"] = data["outcomes"] + [r.model_dump() for r in ov.extra_outcomes]
        applied.append(ov.id)
    return Capability.model_validate(data), applied


# ============================================================ executor
@dataclass
class Executor:
    surface: WebSurface
    policy: Policy
    profile: AppProfile
    tenant: TenantConfig
    render: Renderer
    evidence: Evidence
    redactor: Redactor
    controller: SessionController
    rules: list[OutcomeRule]
    opts: ReplayOptions
    label: str  # capability ref or goal, for context
    recoveries: list[RecoveryRecord] = field(default_factory=list)
    interventions: list[InterventionRecord] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    outputs: dict[str, Any] = field(default_factory=dict)
    irreversible_done: bool = False
    _attempts: dict[tuple[str, str], int] = field(default_factory=dict)

    # ---------------------------------------------------------------- classification
    async def classify(self, step: Step | None) -> bool:
        """Evaluate outcome rules against the live screen. Returns True if a recovery ran.
        Raises Stop / Reauth / NeedsHuman for the other kinds."""
        for rule in self.rules:
            if rule.after_steps and (step is None or step.id not in rule.after_steps):
                continue
            if not await self.surface.check(rule.when, self.render):
                continue
            msg = await self._message(rule)
            sid = step.id if step else None
            self.evidence.event("rule_matched", step=sid, code=rule.code, kind=rule.kind.value, message=msg)
            if rule.kind == OutcomeKind.recoverable:
                assert rule.recovery
                key = (sid or "-", rule.code)
                n = self._attempts.get(key, 0) + 1
                self._attempts[key] = n
                if n > rule.recovery.max_attempts:
                    raise Stop(RunStatus.failed, error=await self._err(
                        "RECOVERY_EXHAUSTED", f"{rule.code} persisted after {n - 1} recovery attempt(s)", step, expected="screen without " + rule.code, observed=msg))
                if rule.recovery.action == "reauthenticate":
                    self.recoveries.append(RecoveryRecord(step_id=sid, code=rule.code, action="reauthenticate", attempt=n, ok=True))
                    raise Reauth(rule.code)
                ok = await self._recover(rule)
                self.recoveries.append(RecoveryRecord(step_id=sid, code=rule.code, action=rule.recovery.action, attempt=n, ok=ok))
                return True
            if rule.kind == OutcomeKind.business:
                raise Stop(RunStatus.business_outcome, outcome=Outcome(code=rule.code, message=msg, step_id=sid))
            if rule.kind == OutcomeKind.escalate:
                raise NeedsHuman("escalation_rule", rule.code, f"{rule.code}: {rule.description}")
            if rule.kind == OutcomeKind.failure:
                raise Stop(RunStatus.failed, error=await self._err(rule.code, msg or rule.description, step, observed=msg))
        return False

    async def _message(self, rule: OutcomeRule) -> str | None:
        if not rule.message_regex:
            return None
        for text in (await self.surface.visible_text()).values():  # per frame: never stitch text across frames
            m = re.search(rule.message_regex, text, re.S)
            if m:
                return m.group(0).strip()[:300]
        return None

    async def _recover(self, rule: OutcomeRule) -> bool:
        r = rule.recovery
        assert r
        self.controller.assert_automation()
        try:
            if r.action == "click" and r.target:
                el = await self.surface.resolve(r.target, self.render)
                await self.surface.click(el)
            elif r.action == "reload":
                await asyncio.sleep(r.wait_ms / 1000)
                await self.surface.reload_frame(r.frame)
            elif r.action == "wait":
                await asyncio.sleep(r.wait_ms / 1000)
            await self.surface.settle()
            return True
        except Exception as e:
            self.evidence.event("recovery_error", code=rule.code, error=str(e))
            return False

    # ---------------------------------------------------------------- waiting primitives
    async def wait_resolve(self, step: Step, target) -> Any:
        deadline = time.monotonic() + step.timeout_ms / 1000
        last: ResolveError | None = None
        while True:
            await self.classify(step)
            try:
                return await self.surface.resolve(target, self.render)
            except ResolveError as e:
                last = e
            if time.monotonic() > deadline:
                break
            await asyncio.sleep(POLL_S)
        assert last
        loading = self.surface.is_loading()
        code = "TIMEOUT" if loading else last.code
        # near-miss diagnostics: what *is* on screen with the same role, in the same frame
        near: list[str] = []
        roles = {s.role for s in target.strategies if getattr(s, "kind", "") == "role_name"}
        try:
            obs = await self.surface.observe()
            fname = target.frame.name if target.frame else None
            near = [f"{e.role} '{e.name}'" for e in obs.by_gid.values() if e.role in roles and (fname is None or e.frame == fname)][:8]
        except Exception:
            pass
        raise Stop(RunStatus.failed, error=await self._err(
            code, f"{target.description}: {last}" + (" (host still loading)" if loading else ""), step,
            expected=f"exactly one match for {target.description}",
            observed=f"strategy results: {last.tried}" + (f"; same-role candidates on screen: {near}" if near else ""), retryable=loading))

    async def wait_expect(self, step: Step, timeout_ms: int | None = None, deadline: float | None = None) -> None:
        """Postconditions must hold within the step's time budget (measured from the action)."""
        if not step.expect:
            await self.classify(step)
            return
        deadline = deadline or time.monotonic() + (timeout_ms or step.timeout_ms) / 1000
        while True:
            await self.classify(step)
            if all([await self.surface.check(c, self.render) for c in step.expect]):
                return
            if time.monotonic() > deadline:
                break
            await asyncio.sleep(POLL_S)
        failed = [c.model_dump(exclude_none=True) for c in step.expect if not await self.surface.check(c, self.render)]
        loading = self.surface.is_loading()
        paths = await self.surface.frame_paths()
        raise Stop(RunStatus.failed, error=await self._err(
            "TIMEOUT" if loading else "UNEXPECTED_STATE",
            f"step '{step.id}' postcondition not met" + (" (host still loading)" if loading else ""), step,
            expected=str(failed), observed=f"frames={paths}", retryable=loading))

    # ---------------------------------------------------------------- one step
    async def run_step(self, step: Step, index: int) -> StepTrace:
        t0 = time.monotonic()
        a = step.action
        trace = StepTrace(step_id=step.id, index=index, status="ok")
        acted_at: float | None = None
        self.policy.check_action_type(a.type)
        self.evidence.event("step_start", step=step.id, index=index, intent=step.intent, action=a.type)
        if isinstance(a, Navigate):
            url = self.render(a.url)
            self.policy.check_url(url)
            self.controller.assert_automation()
            await self.surface.goto(url)
        elif isinstance(a, WaitFor):
            step = step.model_copy(update={"expect": [a.condition]})
        else:
            el = await self.wait_resolve(step, a.target)
            trace.strategy, trace.strategies_agreeing = el.strategy, el.agreeing
            first_kind = a.target.strategies[0].kind
            if el.strategy != first_kind:
                self.warnings.append(f"step {step.id}: primary strategy '{first_kind}' failed, resolved via fallback '{el.strategy}' (possible UI drift)")
            if el.disagreeing:
                self.warnings.append(f"step {step.id}: {el.disagreeing} strategy(ies) point at a different element (possible UI drift)")
            live_risk = self.policy.classify(a.type, el.info.get("role"), el.info.get("name"))
            risk = max(step.risk, live_risk, key=lambda r: r.rank)
            decision = self.policy.gate(risk, mode="replay", confirmed=self.opts.confirm_irreversible)
            self.evidence.event("policy", step=step.id, risk=risk.value, allowed=decision.allowed, reason=decision.reason)
            if not decision.allowed:
                raise Stop(RunStatus.failed, error=await self._err(
                    "CONFIRMATION_REQUIRED" if decision.needs_confirmation else "POLICY_BLOCKED", decision.reason, step,
                    expected="authorised action", observed=f"{el.info.get('role')} '{el.info.get('name')}' classified {risk.value}"))
            self.controller.assert_automation()
            acted_at = time.monotonic()
            if isinstance(a, Click):
                await self.surface.click(el)
                if risk == Risk.irreversible:
                    self.irreversible_done = True
            elif isinstance(a, Fill):
                value = self.render(a.value)
                await self.surface.fill(el, value)
                if not self.render.uses_secret(a.value):
                    back = await self.surface.read(el)
                    if back != value:
                        raise Stop(RunStatus.failed, error=await self._err(
                            "INPUT_NOT_ACCEPTED", f"field '{a.target.description}' did not accept the value", step,
                            expected=self.redactor.text(value), observed=self.redactor.text(back)))
            elif isinstance(a, Select):
                await self.surface.select(el, self.render(a.option))
            elif isinstance(a, Press):
                await self.surface.press(a.key, el)
            elif isinstance(a, Extract):
                await self._extract(step, a, el)
            await self.surface.settle(min(8000, step.timeout_ms))
        await self.wait_expect(step, deadline=(acted_at + step.timeout_ms / 1000) if acted_at else None)
        trace.duration_ms = int((time.monotonic() - t0) * 1000)
        self.evidence.event("step_ok", step=step.id, strategy=trace.strategy, agreeing=trace.strategies_agreeing, ms=trace.duration_ms)
        if self.opts.screenshot_every_step:
            await self.evidence.screenshot(self.surface, step.id)
        return trace

    async def _extract(self, step: Step, a: Extract, el: Any) -> None:
        spec = next(o for o in self.outputs_spec if o.name == a.output)
        raw = await self.surface.read(el)
        try:
            val = normalise(raw, spec.type)
            if spec.pattern and not re.fullmatch(spec.pattern, str(val)):
                raise ValueError(f"does not match {spec.pattern}")
        except ValueError as e:
            raise Stop(RunStatus.failed, error=await self._err(
                "EXTRACTION_FAILED", f"output '{a.output}': {e}", step, expected=f"{spec.type.value} matching {spec.pattern}", observed=self.redactor.text(raw)))
        self.redactor.register(val, spec.sensitivity)
        self.redactor.register(raw, spec.sensitivity)
        self.outputs[a.output] = val
        self.evidence.event("extracted", step=step.id, output=a.output, value=self.redactor.mask(str(val), spec.sensitivity))

    outputs_spec: list = field(default_factory=list)

    # ---------------------------------------------------------------- auth
    async def authenticate(self) -> None:
        for i, s in enumerate(self.profile.auth.steps):
            await self.run_step(s, i)
        deadline = time.monotonic() + 10
        while not all([await self.surface.check(c, self.render) for c in self.profile.auth.success]):
            await self.classify(None)
            if time.monotonic() > deadline:
                raise Stop(RunStatus.failed, error=await self._err("AUTH_FAILED", "sign-on did not reach the application", None))
            await asyncio.sleep(POLL_S)
        self.evidence.event("authenticated", tenant=self.tenant.id)

    # ---------------------------------------------------------------- escalation
    async def escalate(self, kind: str, code: str, reason: str, step: Step | None) -> None:
        paths = await self.surface.frame_paths()
        iv = await self.controller.escalate(
            kind, reason,
            {"run": self.label, "tenant": self.tenant.id, "code": code, "step_id": step.id if step else None,
             "step_intent": step.intent if step else None, "frames": paths},
            self.opts.escalation_timeout_s)
        self.interventions.append(InterventionRecord(
            id=iv.id, reason=reason, kind=kind, step_id=step.id if step else None, operator=iv.operator,
            resolution=iv.status, human_actions=iv.human_actions, waited_ms=int((time.monotonic() - iv._t0) * 1000)))
        if iv.status == "aborted":
            raise Stop(RunStatus.failed, error=await self._err("ESCALATION_ABORTED", f"operator aborted: {iv.resolution_note or ''}", step))
        if iv.status == "timed_out":
            raise Stop(RunStatus.failed, error=await self._err("ESCALATION_TIMEOUT", "no operator took the session in time", step, retryable=True))
        await self.surface.settle()

    # ---------------------------------------------------------------- errors
    async def _err(self, code: str, message: str, step: Step | None, *, expected: str | None = None,
                   observed: str | None = None, retryable: bool = False) -> ErrorInfo:
        obs_text = None
        try:
            obs_text = (await self.surface.observe()).render()
        except Exception:
            pass
        bundle = await self.evidence.failure_bundle(self.surface, code.lower(), obs_text)
        return ErrorInfo(code=code, message=self.redactor.text(message), step_id=step.id if step else None,
                         step_index=None, expected=expected, observed=self.redactor.text(observed) if observed else None,
                         retryable=retryable, evidence=bundle)


# ============================================================ entry point
async def replay(cap: Capability, inputs: dict[str, Any], tenant: TenantConfig, profile: AppProfile, policy: Policy,
                 opts: ReplayOptions | None = None) -> ReplayResult:
    opts = opts or ReplayOptions()
    run_id = f"replay-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"
    t0 = time.monotonic()
    redactor = Redactor(mask_currency=not policy.cfg.persist_currency_in_evidence, pii_patterns=profile.pii_patterns)
    for p in cap.inputs:
        if p.name in inputs:
            redactor.register(inputs[p.name], p.sensitivity)
    evidence = Evidence(opts.evidence_root, run_id, redactor)
    result = ReplayResult(run_id=run_id, capability=cap.ref, tenant=tenant.id, status=RunStatus.rejected, evidence_dir=str(evidence.dir))

    def finish(r: ReplayResult) -> ReplayResult:
        r.duration_ms = int((time.monotonic() - t0) * 1000)
        persisted = r.model_dump(by_alias=True, exclude_none=True)
        persisted["outputs"] = {k: redactor.mask(str(v), next((o.sensitivity for o in cap.outputs if o.name == k), Sensitivity.internal)) for k, v in r.outputs.items()}
        evidence.write_json("result.json", persisted)
        evidence.event("run_end", status=r.status.value, outcome=r.outcome.code if r.outcome else None, error=r.error.code if r.error else None)
        evidence.close()
        return r

    evidence.event("run_start", capability=cap.ref, tenant=tenant.id, inputs={k: redactor.text(str(v)) for k, v in inputs.items()},
                   review=cap.review.status.value, max_risk=cap.max_risk.value)

    # ---------------------------------------------------------------- pre-flight (no UI touched)
    def reject(code: str, message: str) -> ReplayResult:
        result.error = ErrorInfo(code=code, message=message)
        return finish(result)

    if cap.review.status != ReviewStatus.approved and policy.cfg.require_approved_for_replay and not opts.allow_draft:
        return reject("NOT_APPROVED", f"capability is '{cap.review.status.value}'; unattended replay requires 'approved'")
    if cap.review.status == ReviewStatus.deprecated:
        return reject("DEPRECATED", "capability is deprecated")
    if tenant.product != cap.app.product:
        return reject("PRODUCT_MISMATCH", f"tenant runs {tenant.product}, capability targets {cap.app.product}")
    if not version_satisfies(tenant.app_version, cap.app.product_versions):
        return reject("VERSION_UNSUPPORTED", f"tenant runs {tenant.app_version}; capability supports {cap.app.product_versions}")
    errs = validate_inputs(cap.inputs, inputs)
    if errs:
        return reject("INVALID_INPUT", "; ".join(errs))
    try:
        for s in cap.steps:
            policy.check_action_type(s.action.type)
    except PolicyViolation as e:
        return reject(e.code, str(e))
    if cap.max_risk == Risk.irreversible:
        d = policy.gate(Risk.irreversible, mode="replay", confirmed=opts.confirm_irreversible)
        if not d.allowed:
            return reject("CONFIRMATION_REQUIRED" if d.needs_confirmation else "POLICY_BLOCKED", d.reason)
    overlays = load_overlays(tenant, cap) if opts.apply_overlays else []
    cap, applied = apply_overlays(cap, overlays)
    if applied:
        evidence.event("overlays_applied", overlays=applied)
    render = Renderer(inputs, tenant)

    # ---------------------------------------------------------------- execution
    surface = WebSurface(policy, headless=opts.headless)
    await surface.start()
    controller = SessionController(surface, evidence, redactor, run_id)
    evidence.event("session", session=controller.session_id)
    ex = Executor(surface, policy, profile, tenant, render, evidence, redactor, controller,
                  rules=list(cap.outcomes) + list(profile.rules), opts=opts, label=cap.ref, outputs_spec=list(cap.outputs))
    reauths = 0
    try:
        await ex.authenticate()
        i = 0
        await surface.goto(render(cap.entry.url))
        while i < len(cap.steps):
            step = cap.steps[i]
            try:
                result.steps.append((await ex.run_step(step, i)).model_copy(update={"index": i}))
                i += 1
            except Reauth:
                reauths += 1
                if ex.irreversible_done:
                    raise Stop(RunStatus.failed, error=await ex._err("SESSION_LOST", "session expired after an irreversible step; not restarting", step))
                if reauths > opts.max_reauth:
                    raise Stop(RunStatus.failed, error=await ex._err("SESSION_LOST", "session kept expiring", step, retryable=True))
                evidence.event("restart", reason="session expired; re-authenticating and restarting from step 0 (no irreversible step executed)")
                ex.outputs.clear()
                result.steps.clear()
                await ex.authenticate()
                await surface.goto(render(cap.entry.url))
                i = 0
            except (NeedsHuman, Stop) as sig:
                if isinstance(sig, Stop) and (sig.status != RunStatus.failed or not opts.escalate):
                    raise
                if isinstance(sig, NeedsHuman) and not opts.escalate:
                    raise Stop(RunStatus.failed, error=await ex._err("HUMAN_REQUIRED", sig.reason, step))
                kind, code, reason = (sig.kind, sig.code, sig.reason) if isinstance(sig, NeedsHuman) else (
                    "unrecoverable", sig.error.code if sig.error else "FAILED", str(sig))
                if sum(1 for r in ex.interventions if r.step_id == step.id) >= 2:
                    raise Stop(RunStatus.failed, error=await ex._err("ESCALATION_LOOP", "step still blocked after two human interventions", step))
                result.steps.append(StepTrace(step_id=step.id, index=i, status="human", note=code))
                await ex.escalate(kind, code, reason, step)
                # resume: if the human completed this step's intent, its postcondition now holds
                try:
                    await ex.wait_expect(step, timeout_ms=3000)
                    evidence.event("resumed", step=step.id, how="postcondition satisfied after handoff")
                    i += 1
                except Stop:
                    evidence.event("resumed", step=step.id, how="retrying step after handoff")
        # ---------------------------------------------------------------- checkpoint
        deadline = time.monotonic() + 5
        while not all([await surface.check(c, render) for c in cap.checkpoint.all_of]):
            await ex.classify(None)
            if time.monotonic() > deadline:
                raise Stop(RunStatus.failed, error=await ex._err("CHECKPOINT_FAILED", cap.checkpoint.description, None,
                                                                 expected=str([c.model_dump(exclude_none=True) for c in cap.checkpoint.all_of]),
                                                                 observed=str(await surface.frame_paths())))
            await asyncio.sleep(POLL_S)
        missing = [o.name for o in cap.outputs if o.name not in ex.outputs]
        if cap.checkpoint.require_outputs and missing:
            raise Stop(RunStatus.failed, error=await ex._err("CHECKPOINT_FAILED", f"outputs not extracted: {missing}", None))
        evidence.event("checkpoint_ok", description=cap.checkpoint.description)
        await evidence.screenshot(surface, "checkpoint")
        result.status = RunStatus.succeeded
        result.outputs = dict(ex.outputs)
    except Stop as s:
        result.status, result.outcome, result.error = s.status, s.outcome, s.error
        if s.outcome and s.outcome.code not in cap.declared_outcomes:
            s.outcome.declared = False
            ex.warnings.append(f"business outcome {s.outcome.code} is not declared by the capability")
        if s.outcome:
            await evidence.screenshot(surface, f"outcome-{s.outcome.code.lower()}")
    except PolicyViolation as e:
        result.status = RunStatus.failed
        result.error = await ex._err(e.code, str(e), None)
    except SecretError as e:
        result.status = RunStatus.failed
        result.error = ErrorInfo(code="SECRET_UNAVAILABLE", message=str(e))
    except Exception as e:  # engine bug: still return a structured result
        result.status = RunStatus.failed
        result.error = await ex._err("INTERNAL", f"{type(e).__name__}: {e}", None)
    finally:
        if surface.blocked_requests:
            ex.warnings.append(f"{len(surface.blocked_requests)} request(s) blocked by allowlist")
            evidence.event("blocked_requests", urls=surface.blocked_requests)
        result.recoveries, result.interventions, result.warnings = ex.recoveries, ex.interventions, ex.warnings
        controller.close()
        await surface.close()
    if result.error and result.error.step_id is not None:
        idx = next((k for k, s in enumerate(cap.steps) if s.id == result.error.step_id), None)
        result.error.step_index = idx
    return finish(result)
