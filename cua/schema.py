"""Typed data model for the whole system.

Three families of documents live here:

1. ``Capability``   — the recorded, reviewable, versioned artifact an AI agent invokes.
2. ``AppProfile``   — product-level knowledge shared by every capability and every
                      tenant running the same vendor product (auth procedure, known
                      interstitials, error/business message classifiers).
3. ``TenantConfig`` / ``TenantOverlay`` — per-institution configuration and the
                      *minimal* per-tenant overrides applied on top of a shared capability.

plus the replay result contract (``ReplayResult``) returned to callers.

Design notes (see REPORT.md §2):
* Steps say *what* to act on as a ``Target`` — an ordered list of independent locator
  strategies expressed in surface-neutral vocabulary (role + accessible name, visible
  label, table coordinates, form-field attribute). Only the surface driver knows how to
  turn those into a DOM node / UIA element / AX element.
* Values are templates (``{{inputs.member_id}}``, ``{{secrets.operator_password}}``);
  concrete values never appear in the artifact.
* Every step carries a postcondition (``expect``) so replay *verifies* instead of assuming.
* Outcome rules classify screens into business / recoverable / failure / escalate —
  the error taxonomy is data, not code.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

CAPABILITY_SCHEMA = "cua.capability/v1"
PROFILE_SCHEMA = "cua.app-profile/v1"
OVERLAY_SCHEMA = "cua.tenant-overlay/v1"
RESULT_SCHEMA = "cua.replay-result/v1"

TEMPLATE_RE = re.compile(r"\{\{\s*(inputs|secrets|tenant)\.([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ============================================================ enums
class Sensitivity(str, Enum):
    """Drives redaction. ``secret`` values never reach logs, artifacts or the LLM."""

    public = "public"
    internal = "internal"
    pii = "pii"  # masked to last 4 in persisted evidence
    financial = "financial"  # replaced by a keyed fingerprint in persisted evidence
    secret = "secret"  # never persisted, never shown to a model


class ValueType(str, Enum):
    string = "string"
    integer = "integer"
    decimal = "decimal"
    currency = "currency"  # returned as a normalised decimal string, e.g. "12480.55"
    date = "date"
    enum = "enum"
    boolean = "boolean"


class Risk(str, Enum):
    read_only = "read_only"  # navigation, reading
    reversible = "reversible"  # typing into a form, opening a screen
    irreversible = "irreversible"  # commits a business transaction

    @property
    def rank(self) -> int:
        return {"read_only": 0, "reversible": 1, "irreversible": 2}[self.value]


class OutcomeKind(str, Enum):
    success = "success"
    business = "business"  # legitimate answer the caller must handle ("no such member")
    recoverable = "recoverable"  # handled inside replay (dismiss notice, retry, re-auth)
    failure = "failure"  # hard failure: stop, surface debuggable error
    escalate = "escalate"  # needs a human: pause, hand the live session to an operator


class ReviewStatus(str, Enum):
    draft = "draft"
    approved = "approved"
    deprecated = "deprecated"


class SurfaceKind(str, Enum):
    web = "web"
    legacy_web = "legacy_web"
    desktop = "desktop"


# ============================================================ contract: inputs & outputs
class ParamSpec(Model):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    type: ValueType = ValueType.string
    description: str
    required: bool = True
    pattern: str | None = Field(None, description="Regex the value must fully match (pre-flight validation).")
    enum: list[str] | None = None
    sensitivity: Sensitivity = Sensitivity.internal
    example: str | None = Field(None, description="Synthetic example only; never real data.")


class OutputSpec(Model):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    type: ValueType = ValueType.string
    description: str
    sensitivity: Sensitivity = Sensitivity.internal
    pattern: str | None = Field(None, description="Regex the *normalised* value must fully match.")


# ============================================================ targeting
class FrameScope(Model):
    """Where on the surface to look. For web: a frame by name or URL; for desktop: a window."""

    name: str | None = None
    url_pattern: str | None = None


class _StrategyBase(Model):
    nth: int | None = Field(None, description="0-based index among matches. Discouraged; flagged as brittle.")
    note: str | None = None


class RoleName(_StrategyBase):
    """Semantic role + accessible name. Maps 1:1 onto ARIA, Windows UIA (ControlType+Name) and macOS AX."""

    kind: Literal["role_name"] = "role_name"
    role: str
    name: str = Field(description="Normalised accessible name; may be a template.")


class LabelValue(_StrategyBase):
    """The value cell next to a visible label cell (``Name | DOE, JANE``). Robust for legacy detail screens."""

    kind: Literal["label_value"] = "label_value"
    label: str


class TableCell(_StrategyBase):
    """A cell addressed by column header + a key cell in the same row. Robust to row order / count."""

    kind: Literal["table_cell"] = "table_cell"
    column: str
    row_key: str = Field(description="Text of another cell in the same row (may be a template).")
    interactive: bool = Field(False, description="Act on the first interactive descendant of the cell.")


class Attribute(_StrategyBase):
    """Stable markup attributes, e.g. a server-side form field name (``name=P_MBR``)."""

    kind: Literal["attribute"] = "attribute"
    tag: str
    attrs: dict[str, str]


class TextMatch(_StrategyBase):
    kind: Literal["text"] = "text"
    text: str
    role: str | None = None


class XPath(_StrategyBase):
    """Structural path. Last resort — breaks on any layout change; kept for diagnostics."""

    kind: Literal["xpath"] = "xpath"
    xpath: str


Strategy = Annotated[Union[RoleName, LabelValue, TableCell, Attribute, TextMatch, XPath], Field(discriminator="kind")]


class Target(Model):
    description: str = Field(description="Human-readable name of the control, for reviewers and error messages.")
    frame: FrameScope | None = None
    strategies: list[Strategy] = Field(min_length=1, description="Tried in order; the first that resolves to exactly one element wins.")
    validated: list[str] = Field(default_factory=list, description="Strategy kinds verified unique at record time.")
    visual_hint: dict[str, float] | None = Field(None, description="Bounding box at record time. Debug/operator aid only; never used to act.")


# ============================================================ conditions
class TextVisible(Model):
    kind: Literal["text_visible"] = "text_visible"
    text: str = Field(description="Literal (case/space-insensitive) unless regex=true. May be a template.")
    regex: bool = False
    frame: FrameScope | None = None


class ElementPresent(Model):
    kind: Literal["element_present"] = "element_present"
    target: Target


class UrlMatches(Model):
    kind: Literal["url_matches"] = "url_matches"
    pattern: str = Field(description="Regex over the frame's path+query (templates allowed).")
    frame: FrameScope | None = None


class TitleIs(Model):
    kind: Literal["title_is"] = "title_is"
    title: str
    frame: FrameScope | None = None


Condition = Annotated[Union[TextVisible, ElementPresent, UrlMatches, TitleIs], Field(discriminator="kind")]


# ============================================================ actions
class Click(Model):
    type: Literal["click"] = "click"
    target: Target


class Fill(Model):
    type: Literal["fill"] = "fill"
    target: Target
    value: str = Field(description="Template, e.g. '{{inputs.member_id}}'. Literals allowed for constants.")


class Select(Model):
    type: Literal["select"] = "select"
    target: Target
    option: str


class Press(Model):
    type: Literal["press"] = "press"
    key: str
    target: Target | None = None


class Navigate(Model):
    type: Literal["navigate"] = "navigate"
    url: str = Field(description="Template relative to tenant base_url, e.g. '{{tenant.base_url}}/main'.")


class Extract(Model):
    type: Literal["extract"] = "extract"
    target: Target
    output: str


class WaitFor(Model):
    type: Literal["wait_for"] = "wait_for"
    condition: Condition


Action = Annotated[Union[Click, Fill, Select, Press, Navigate, Extract, WaitFor], Field(discriminator="type")]
ACTION_TYPES = ["click", "fill", "select", "press", "navigate", "extract", "wait_for"]


class Step(Model):
    id: str = Field(pattern=r"^[a-z0-9_]+$")
    intent: str = Field(description="Why this step exists, in plain language (from the discovery agent / reviewer).")
    action: Action
    risk: Risk = Risk.reversible
    expect: list[Condition] = Field(default_factory=list, description="Postconditions that must hold after the action (all).")
    timeout_ms: int = Field(10_000, ge=100, le=120_000)
    source: Literal["agent", "human", "profile", "reviewer"] = "agent"


# ============================================================ outcomes & recovery
class Recovery(Model):
    """What replay does when a *recoverable* rule matches."""

    action: Literal["click", "reload", "wait", "reauthenticate"]
    target: Target | None = None
    frame: FrameScope | None = Field(None, description="Frame to reload (reload action).")
    wait_ms: int = 1000
    max_attempts: int = 2


class OutcomeRule(Model):
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    kind: OutcomeKind
    description: str
    when: Condition
    message_regex: str | None = Field(None, description="Regex whose first match (on visible text) becomes the outcome message.")
    recovery: Recovery | None = None
    after_steps: list[str] | None = Field(None, description="Only evaluate after these step ids (None = any step).")

    @model_validator(mode="after")
    def _recovery_only_for_recoverable(self) -> "OutcomeRule":
        if self.kind == OutcomeKind.recoverable and self.recovery is None:
            raise ValueError(f"recoverable rule {self.code} needs a recovery")
        return self


class Checkpoint(Model):
    description: str
    all_of: list[Condition] = Field(min_length=1)
    require_outputs: bool = True


# ============================================================ capability (the artifact)
class AppRef(Model):
    product: str = Field(description="Vendor product family, shared across tenants, e.g. 'corelink-teller'.")
    profile: str = Field(description="AppProfile id@major, e.g. 'corelink-teller@1'.")
    product_versions: str = Field(description="Product versions this capability is known to work on, e.g. '>=4.2 <5'.")
    surface: SurfaceKind = SurfaceKind.legacy_web


class Entry(Model):
    url: str = Field(description="Template, e.g. '{{tenant.base_url}}/main'.")
    requires_session: bool = True


class Provenance(Model):
    method: Literal["llm_discovery", "manual", "derived"] = "llm_discovery"
    recorded_at: str = Field(default_factory=utcnow)
    model: str | None = None
    discovery_run_id: str | None = None
    goal: str = Field(description="Goal as given, with parameter values replaced by placeholders.")
    tenant: str
    app_version: str | None = None
    human_steps: int = 0


class Review(Model):
    status: ReviewStatus = ReviewStatus.draft
    reviewed_by: str | None = None
    reviewed_at: str | None = None
    notes: str | None = None


class Capability(Model):
    schema_: Literal["cua.capability/v1"] = Field(CAPABILITY_SCHEMA, alias="schema")
    id: str = Field(pattern=r"^[a-z0-9_.\-]+$", description="Stable name agents invoke, e.g. 'member.get_savings_balance'.")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    title: str
    description: str = Field(description="What the capability does, written for the calling agent.")
    app: AppRef
    entry: Entry
    inputs: list[ParamSpec] = Field(default_factory=list)
    outputs: list[OutputSpec] = Field(default_factory=list)
    steps: list[Step] = Field(min_length=1)
    checkpoint: Checkpoint
    outcomes: list[OutcomeRule] = Field(default_factory=list, description="Capability-specific rules, evaluated before profile rules.")
    declared_outcomes: list[str] = Field(default_factory=list, description="Business outcome codes a caller must be ready to handle.")
    max_risk: Risk = Field(Risk.read_only, description="Highest risk of any step (computed).")
    provenance: Provenance
    review: Review = Field(default_factory=Review)

    @model_validator(mode="after")
    def _check(self) -> "Capability":
        ids = [s.id for s in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("step ids must be unique")
        in_names = {p.name for p in self.inputs}
        out_names = {o.name for o in self.outputs}
        for s in self.steps:
            blob = s.model_dump_json()
            for scope, name in TEMPLATE_RE.findall(blob):
                if scope == "inputs" and name not in in_names:
                    raise ValueError(f"step {s.id} references undeclared input '{name}'")
            if isinstance(s.action, Extract) and s.action.output not in out_names:
                raise ValueError(f"step {s.id} extracts undeclared output '{s.action.output}'")
        extracted = {s.action.output for s in self.steps if isinstance(s.action, Extract)}
        missing = out_names - extracted
        if missing:
            raise ValueError(f"outputs never extracted: {sorted(missing)}")
        self.max_risk = max((s.risk for s in self.steps), key=lambda r: r.rank)
        return self

    def dump(self) -> str:
        return self.model_dump_json(by_alias=True, indent=2, exclude_none=True)

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"


# ============================================================ app profile (per vendor product)
class AuthProcedure(Model):
    steps: list[Step]
    success: list[Condition]


class AppProfile(Model):
    schema_: Literal["cua.app-profile/v1"] = Field(PROFILE_SCHEMA, alias="schema")
    id: str
    version: str
    product: str
    description: str
    auth: AuthProcedure
    rules: list[OutcomeRule] = Field(description="Interstitials, session expiry, app errors, business messages, escalations.")
    irreversible_labels: list[str] = Field(default_factory=list, description="Control names that commit transactions in this product.")
    pii_patterns: list[str] = Field(default_factory=list, description="Regexes for PII this product displays (e.g. member names); masked in logs and screenshots.")


# ============================================================ tenants
class TenantConfig(Model):
    id: str
    display_name: str
    base_url: str
    product: str
    app_version: str
    secrets: dict[str, str] = Field(description="Secret name -> reference (env:VAR). Values are resolved at action time only.")
    overlays: list[str] = Field(default_factory=list)
    data_classification: Literal["synthetic", "production"] = "synthetic"


class TenantOverlay(Model):
    """Minimal, reviewable per-tenant specialisation of a shared capability."""

    schema_: Literal["cua.tenant-overlay/v1"] = Field(OVERLAY_SCHEMA, alias="schema")
    id: str
    tenant: str
    capability: str
    capability_versions: str = Field(description="Major version this overlay applies to, e.g. '1.x'.")
    reason: str
    target_overrides: dict[str, Target] = Field(default_factory=dict, description="step id -> replacement Target")
    expect_overrides: dict[str, list[Condition]] = Field(default_factory=dict)
    checkpoint: Checkpoint | None = None
    extra_outcomes: list[OutcomeRule] = Field(default_factory=list)


# ============================================================ replay result contract
class RunStatus(str, Enum):
    succeeded = "succeeded"
    business_outcome = "business_outcome"
    failed = "failed"
    rejected = "rejected"  # refused before touching the UI (bad input, policy, not approved)


class ErrorInfo(Model):
    code: str
    message: str
    step_id: str | None = None
    step_index: int | None = None
    expected: str | None = None
    observed: str | None = None
    retryable: bool = False
    evidence: list[str] = Field(default_factory=list)


class Outcome(Model):
    code: str
    message: str | None = None
    step_id: str | None = None
    declared: bool = True


class RecoveryRecord(Model):
    step_id: str | None
    code: str
    action: str
    attempt: int
    ok: bool


class InterventionRecord(Model):
    id: str
    reason: str
    kind: str
    step_id: str | None
    operator: str | None = None
    resolution: str | None = None
    human_actions: list[dict[str, Any]] = Field(default_factory=list)
    waited_ms: int = 0


class StepTrace(Model):
    step_id: str
    index: int
    status: Literal["ok", "failed", "skipped", "human"]
    strategy: str | None = None
    strategies_agreeing: int | None = None
    duration_ms: int = 0
    note: str | None = None


class ReplayResult(Model):
    schema_: Literal["cua.replay-result/v1"] = Field(RESULT_SCHEMA, alias="schema")
    run_id: str
    capability: str
    tenant: str
    status: RunStatus
    outputs: dict[str, Any] = Field(default_factory=dict)
    outcome: Outcome | None = None
    error: ErrorInfo | None = None
    recoveries: list[RecoveryRecord] = Field(default_factory=list)
    interventions: list[InterventionRecord] = Field(default_factory=list)
    steps: list[StepTrace] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    started_at: str = Field(default_factory=utcnow)
    duration_ms: int = 0
    evidence_dir: str | None = None


def json_schemas() -> dict[str, dict]:
    return {
        "capability": Capability.model_json_schema(by_alias=True),
        "app_profile": AppProfile.model_json_schema(by_alias=True),
        "tenant_overlay": TenantOverlay.model_json_schema(by_alias=True),
        "replay_result": ReplayResult.model_json_schema(by_alias=True),
    }

