# Design report

The through-line is **the model discovers, the artifact becomes a capability, and deterministic
replay is how agents invoke it.** I put most of the depth into the artifact contract, the replay
error taxonomy, and the safety/handoff model. I kept breadth thin but real: one web surface, a
mock operator UI, and multi-tenancy as a data model plus one demonstrated overlay.

## 1. Architecture

A single Python process (asyncio + Playwright) with hard internal seams:

| layer | module | responsibility |
|---|---|---|
| Surface | `surface/base.py`, `web.py`, `resolver.js` | Perceive and act: `observe`, `resolve(Target)`, `click/fill/select/read`, `check(Condition)`, masked screenshots |
| Discovery | `discovery.py`, `llm.py`, `recorder.py` | LLM loop; turns the run into a draft `Capability` |
| Replay | `replay.py` | Deterministic executor + screen classifier + recovery + result contract |
| Policy / redaction | `policy.py`, `redact.py` | Allowlist, risk gating, sensitivity-driven masking |
| Control | `control.py`, `operator.py` | Single-owner session lease, interventions, operator console |
| Contract | `schema.py`, `config/`, `capabilities/` | Typed documents (pydantic → JSON Schema) |

**Key decisions**

- **Perception is our own accessibility model, not selectors.** `resolver.js` is injected into
  every frame. It computes role and accessible name the way an a11y tree does, plus the legacy
  idioms a real a11y tree misses: the label is the previous table cell, a `<b>` in
  `<font size=4>` is a heading, and table header and row context are attached. The *same code*
  names elements for the model, derives locators at record time, and resolves them at replay, so
  record and replay cannot disagree. It needs no ids, test ids or clean DOM, and the vocabulary
  (role + name + table/label context) maps directly onto Windows UIA and macOS AX (§4).
- **The model sees an element list plus a screenshot and acts by element id.** It never emits
  selectors or coordinates. Coordinates are fine for a model but useless as a replay locator.
  Choosing *how to find it again* is the recorder's job, and the recorder validates its choices
  against the live page.
- **Stateless model turns.** Each request carries the goal, parameters, a compact history and the
  current screen, so tokens stay bounded regardless of run length (about 3k input tokens per
  step in the evidence). Providers sit behind a one-method `Decider` seam: Gemini (function calling,
  mode ANY) and Claude (forced single tool use). A scripted decider can replay an earlier run's
  decisions offline. The evidence runs used free-tier Gemini Flash. The Gemini client handles
  retired models (404) and sustained overload (503) by failing over to another Flash model, and
  the transcript records which model answered each call.
- **Single process, no queue.** The browser session, the replay loop and the operator console
  share one event loop, and that is what makes a same-session handoff simple and correct. §5 covers
  how this splits into services.

## 2. Artifact schema

`cua.capability/v1` ([`schemas/capability.schema.json`](schemas/capability.schema.json), example
[`capabilities/member.get_savings_balance.json`](capabilities/member.get_savings_balance.json)).
It is shaped as a **contract first, step list second**:

- **Identity and lifecycle:** `id` (the name agents call), semver `version`, `review`
  (`draft → approved → deprecated`, reviewer, notes), and `provenance` (model, discovery run id,
  goal *with values replaced by placeholders*, tenant, app version, count of human steps).
  Re-discovery bumps the minor version and archives the old file. It never overwrites a reviewed
  artifact.
- **Contract for the caller:** typed `inputs` (`ParamSpec`: type, regex, enum, **sensitivity**),
  typed `outputs` (`OutputSpec`: type, normalised pattern, sensitivity), `declared_outcomes` (the
  business results a caller must handle, e.g. `MEMBER_NOT_FOUND`), `max_risk` (computed), and
  `checkpoint` (conditions that must all hold, plus "all outputs extracted"). `catalog.py` turns
  exactly this into a tool definition, so an agent needs nothing else.
- **Binding to the product, not the tenant:** `app.product`, `product_versions` range and
  `profile` (the product's `AppProfile`), with `entry.url = {{tenant.base_url}}/main`. Tenancy
  only enters through templates and overlays.
- **Steps:** `id`, reviewer-readable `intent`, a typed `action` (`click | fill | select | press |
  navigate | extract | wait_for`), `risk`, `expect` postconditions, `timeout_ms` and `source`
  (agent / human / profile). Values are templates (`{{inputs.member_id}}`,
  `{{secrets.operator_password}}`). Concrete PII and secrets never appear; a test asserts this.
- **Targets:** a human `description`, a `frame` scope, and an **ordered list of independent
  strategies**:
  - `role_name`: role + accessible name. Robust to layout; breaks on relabelling.
  - `attribute`: server form-field name such as `P_MBR`. Legacy apps keep these stable across
    skins, which proved useful across tenants.
  - `table_cell`: column header + a row key, often a template (`row_key: {{inputs.member_id}}`).
    Robust to row order and count.
  - `label_value`: the cell next to a visible label.
  - `xpath`: kept for diagnostics only. Replay never acts on it while any semantic strategy
    exists, because a structural path that still resolves after drift is exactly how you click
    the wrong button.

  Every strategy is **validated at record time**: it must resolve uniquely to the chosen element
  or it is dropped. `validated` records which ones passed. Row keys that are data (a member's name
  in a results row) are rejected as locators. Only parameters or stable labels qualify.
- **Outcome rules** (`OutcomeRule`: `code`, `kind`, `when`, `message_regex`, `recovery`) express
  the error taxonomy *as data*. Product-wide rules live in the `AppProfile`: session expiry, the
  system notice, host errors, "no member found", and supervisor override. Capability-local rules
  can be added and are evaluated first.

Why this shape: a reviewer can read `intent`, `description` and `expect` without knowing
Playwright. An agent reads only the contract. The engine gets enough redundancy (several
strategies plus postconditions) to both *act* and *notice when it shouldn't*.

## 3. Determinism & error handling

**Determinism.** There is no model in the loop. Each step: (1) poll-resolve the target until the
step timeout, where the first *unique* strategy wins and ambiguity is an error, never "pick the
first"; (2) re-classify risk against the **live** element and gate it; (3) act, then read back
filled values (`INPUT_NOT_ACCEPTED` if the app rewrote them); (4) wait for `expect`. That means
route pattern (canonicalised, e.g. `^{{tenant.base_path}}/work/member/{{inputs.member_id}}`),
frame title and new headings, all within the step's time budget measured from the action.
Waiting is event-based: in-flight document requests are tracked and DOM stability is required, so
there are no sleeps. Final `checkpoint` plus output typing (`"$12,480.55"` → `"12480.55"`,
pattern-checked) complete the run. The discovery-time value and the replayed value share the same
HMAC fingerprint in evidence, which shows determinism without persisting the balance.

**Runtime conditions.** On *every poll* of steps (1) and (4), the screen classifier evaluates the
outcome rules. The result contract has four statuses and never conflates them:

| status | meaning | examples in evidence |
|---|---|---|
| `succeeded` | checkpoint verified, typed outputs | happy paths |
| `business_outcome` | a legitimate answer, with `code` and the host's message | `MEMBER_NOT_FOUND`, `PERMISSION_DENIED`, `VALIDATION_ERROR` |
| `failed` | hard failure: `code`, `step_id`/`step_index`, `expected`, `observed`, `retryable`, evidence bundle (masked screenshot, redacted DOM per frame, the observation) | `APP_ERROR`, `TARGET_NOT_FOUND`, `TIMEOUT`, `UNEXPECTED_STATE`, `HUMAN_REQUIRED` |
| `rejected` | refused before touching the UI | `INVALID_INPUT`, `NOT_APPROVED`, `CONFIRMATION_REQUIRED`, `VERSION_UNSUPPORTED` |

**Recoverable** conditions are handled inside the run and listed in `recoveries[]`. A known
interstitial is dismissed. A transient 503 triggers a frame reload. Session expiry triggers
re-authentication through the profile's auth procedure and a restart from step 0, but **only if no
irreversible step has executed** (otherwise `SESSION_LOST`). Recoveries are bounded per step and
rule (`RECOVERY_EXHAUSTED`). A slow host is distinguished from a wrong screen: timing out *while a
navigation is in flight* is `TIMEOUT` (retryable); otherwise it's `UNEXPECTED_STATE`.

**Drift (secondary).** Resolving through a fallback strategy, or strategies disagreeing, emits a
warning. On `TARGET_NOT_FOUND` the error lists same-role candidates on screen (e.g. "button
'Search'"), which is exactly what a reviewer needs to write an overlay.

## 4. Heterogeneity & multi-tenant

**Surface seam.** Everything above `Surface` speaks in frames/windows, `role + name`,
`Target` strategies and `Condition`s. A surface must implement `observe`, `resolve`, the actions,
`read`, `check`, and masked `screenshot`.

- *Legacy web* is what's implemented: framesets, tables, no ids.
- *Desktop (Windows):* a `UIASurface` maps `role_name` to ControlType + Name, `attribute` to
  AutomationId/ClassName, `table_cell` to the Grid/Table patterns, and `FrameScope` to the
  top-level window. The artifact schema is unchanged; only `app.surface` differs.
- *No accessibility at all (Citrix, terminal emulators):* a `VisionSurface` implements `resolve`
  with OCR/grounding over screenshots, and `visual_hint` becomes a real strategy there. It is less
  deterministic, so those artifacts would carry lower confidence and stricter checkpoints.

Artifacts don't change across surfaces. Only the driver does.

**Multi-tenant reuse** uses three layers:

1. **`AppProfile` per vendor product and major version:** auth procedure, interstitials,
   error/business classifiers, irreversible labels, PII patterns. Shared by every capability and
   every tenant on that product.
2. **`Capability` per product:** recorded once on any tenant and bound to `product_versions`, not
   to a tenant. Tenant specifics are templates (`{{tenant.base_url}}`, `{{tenant.base_path}}`),
   and routes are canonicalised at record time.
3. **`TenantOverlay`:** a minimal, reviewable diff keyed by step id (replacement targets,
   postconditions, checkpoint, extra outcome rules), scoped to a capability major version.

Demonstrated: Bayside runs 4.3.0 with a relabelled field, button and column. The base artifact
fails on the relabelled button with `TARGET_NOT_FOUND` at `click_inquire`, naming the
candidate `button 'Search'`. It warns that the member field survived via its `attribute` fallback. With a
two-entry overlay, the same artifact succeeds.

**Drift management** at scale: run each approved capability on a schedule against a canary
account per tenant, and feed fallback-resolution warnings and strategy disagreement into per-tenant
health. A tenant upgrade that breaks the `product_versions` range is refused up front
(`VERSION_UNSUPPORTED`) rather than half-run. Overlays are the only per-tenant artifact, so the
count of overlays per capability becomes a direct signal that the base artifact should absorb the
variation, for example by adding a second `role_name` strategy.

## 5. Escalation & handoff

**Detecting "stuck".** In discovery: the model calls `request_human`, the same action repeats on
an unchanged screen three times (screen signature), three consecutive failed actions occur, or an
irreversible action needs approval (when policy is `escalate` rather than `block`). In replay:
`escalate`-kind rules (supervisor override) fire, or, with `--escalate`, any hard failure does.

**Control model** (`control.py`). Each live session has exactly one owner:
`automation → awaiting_operator → human:<operator> → automation`. The automation must hold the
lease to act (`assert_automation()` runs before every action), so the two can never drive
concurrently. Only the operator who claimed a session can send input or release it. The
`Intervention` carries the capability or goal, step id and intent, the rule code and reason, frame
routes and a masked screenshot. It goes to notifiers (console output in this repo; pager or queue
in production).

**Taking control** of the *same* session: the operator console runs in the process that owns the
browser. It streams screenshots and forwards clicks, typing and keys to the same Playwright page,
so cookies, frames and host session all persist. Every click and change in any frame is captured
by an in-page listener while a human holds the lease. It is described in the recorder's
vocabulary (role, name, field, table context) and redacted: password fields become `[SECRET]`.
The record lands in `interventions[].human_actions` and the event log.

**Handing back:** `release(resume|abort)`. On resume, replay re-checks the blocked step's
postcondition. If the human completed it, execution continues at the next step; otherwise the
step is retried once. The evidence shows supervisor.kim entering the override and the run
finishing with the balance. Timeouts and aborts map to `ESCALATION_TIMEOUT` and
`ESCALATION_ABORTED`. Human steps are recorded as evidence and counted in provenance, **not**
silently baked into the artifact; a reviewer decides whether they belong.

The console is a deliberate mock (screenshot polling, token auth). In production the browser runs
in a worker exposing CDP, the console streams via screencast/WebRTC, and a broker owns the lease
table, operator identity (SSO) and per-tenant entitlements. The lease semantics and API stay the
same.

## 6. Safety

- **Allowlist** (`config/policy.json`): origins, path prefixes and action types. It is enforced
  at the network layer (every browser request is routed through it and off-list requests are
  aborted), at every navigation, and at every action. The mock's fault-injection endpoint sits
  outside the allowlist, and a test proves an artifact cannot reach it.
- **Risk classes:** `read_only`, `reversible` and `irreversible`, classified from the live
  control's role and name (policy patterns plus the product's `irreversible_labels`). Replay takes
  the max of the artifact's label and the live classification, so a tampered artifact can't
  downgrade risk. Discovery **blocks** irreversible actions and tells the model why. In the
  evidence run the model stopped on the review screen by itself; the block is exercised by
  `test_discovery_blocks_irreversible_click`. Replay refuses to
  *start* an irreversible capability unless the caller passes explicit confirmation (scenario
  `rejected-irreversible-unconfirmed`). It also never
  restarts after an irreversible step. I chose confirmation over flat blocking because some
  capabilities legitimately commit, but only with an approved artifact plus explicit caller intent.
- **Data handling:** secrets are `env:` references resolved at action time. They never enter the
  artifact, logs or the model, and sign-on runs from the profile before the model is involved.
  Parameter values are shown to the model as placeholders. Persisted evidence masks PII to its last
  4 characters and replaces financial values with keyed fingerprints; secret-looking keys, card
  numbers, SSNs, API keys and amounts are pattern-masked. Screenshots black out the same values,
  plus the product's PII patterns such as member names. The caller receives real outputs on the
  return channel only.
- **Limits:** the model still sees synthetic screen content in discovery, so discovery must run
  against sandbox or synthetic tenants. Pattern-based PII masking is best-effort. Risk
  classification is label-based and can't see what a generic "Submit" commits, which is why
  products declare `irreversible_labels` and reviewers approve artifacts. There is no audit-log
  signing or retention policy yet.

## 7. Cuts

**Deliberately left out:** a real co-browsing console (mocked, with a real lease and API);
desktop and vision surfaces (designed seam only); a broker/queue/multi-process deployment; an
artifact registry with signatures (JSON files in git instead); automatic overlay generation; a
screen recording (masked per-step screenshots instead). `ask`, where a model picks a catalog tool,
is implemented but not in the evidence: the environment that produced the replay evidence can't
reach a model API, so the evidence shows `invoke` by tool name instead.

**Stretch goals taken:** an agent-facing capability catalog (`catalog`/`invoke`/`ask`, with a
real model calling the tool) and cross-tenant reuse with overlays and route canonicalisation. An
approval gate (`draft → approved`) is part of the core.

**Next, in order:**

1. A bounded **assisted fallback**: on `TARGET_NOT_FOUND`, one policy-checked model call proposes a
   replacement target, which is recorded as a draft overlay for review.
2. **Stability scoring:** N-run replays per tenant feeding `review` gating and per-tenant health.
3. **Capability-scoped outcome rules** proposed at record time (e.g. `MEMBER_NOT_FOUND` only
   after the search step).
4. **A UIA desktop surface** against a small WinForms app, to prove the seam.
5. Split the operator console, the lease broker and browser workers into services.
