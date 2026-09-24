# Computer-use automation: discover once, replay deterministically

An LLM figures out a task on a legacy back-office app once. The system turns that run into a
typed, versioned, reviewable **capability artifact**, and AI agents then invoke it by name. The
artifact is replayed **deterministically without the model**, with an explicit error taxonomy,
policy guardrails, redacted evidence, and a **live-session handoff to a human operator** when the
automation can't safely continue.

```
goal ──▶ discover (LLM observe→decide→act) ──▶ capability artifact (draft) ──▶ review/approve
                                                                                  │
agent ──▶ catalog tool call(member_id=…) ──▶ replay (no LLM) ──▶ succeeded | business_outcome | failed
                                                   │                    ▲
                                                   └─ escalate ─▶ operator takes the SAME live session ─┘
```

The target is **CoreLink Teller**, a mock *legacy* core-banking app included in this repo
(`mockapp/`). It is deliberately hostile to automation: framesets, table layouts, `<font>` tags,
no ids or labels, `javascript:` links, and errors rendered as red text. It runs two tenants of the
same vendor product (`acme` 4.2.1, `bayside` 4.3.0, which relabels fields) and supports runtime
fault injection. All data is synthetic.

- **Design write-up:** [`REPORT.md`](REPORT.md)
- **Evidence** (the real LLM discovery runs, replays, errors, handoff): [`evidence/README.md`](evidence/README.md)
- **Artifacts:** [`capabilities/`](capabilities/) · schemas: [`schemas/`](schemas/) · product profile: [`config/profiles/corelink-teller.json`](config/profiles/corelink-teller.json)

## Setup

Requires Python 3.10+.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env            # add ANTHROPIC_API_KEY or GEMINI_API_KEY (only needed for `discover` and `ask`)
```

`.env` keys:

| key | needed for | notes |
|---|---|---|
| `ANTHROPIC_API_KEY` **or** `GEMINI_API_KEY` | `discover`, `ask` | Replay never calls a model. With both set, `CUA_PROVIDER=anthropic\|gemini` picks one |
| `CUA_MODEL` / `CUA_GEMINI_MODEL` | optional | Model ids (defaults `claude-sonnet-4-5` / `gemini-3.6-flash`; a retired or overloaded Gemini model fails over to another Flash model automatically) |
| `CORELINK_OPERATOR_ID` / `CORELINK_OPERATOR_PASSWORD` | everything | Demo creds for the mock app (`teller01` / `demo-pass-123`). Resolved at action time via `env:` secret refs, never written anywhere |
| `CUA_EVIDENCE_KEY` | optional | HMAC key for fingerprints of financial values in evidence |

Start the mock app in its own terminal (keep it running):

```bash
python -m mockapp.server        # http://127.0.0.1:8600/
```

## Demo path

**1. Discovery: a real LLM drives the live app and records a capability** (6 model calls in the evidence run; free-tier Gemini is enough). `bash scripts/discover_all.sh` does setup + both evidence discovery runs in one go.

```bash
python -m cua discover --tenant acme \
  --capability-id member.get_savings_balance \
  --goal "Look up member 100234 and read the current balance of their primary savings account" \
  --param member_id=100234:integer:pii
```

This writes `capabilities/member.get_savings_balance.json` (status `draft`) plus
`evidence/runs/discovery-…/` (event log, model transcript, masked screenshots). If the file
already exists, discovery bumps the minor version and archives the previous one in
`capabilities/history/`, so a reviewed artifact is never silently replaced.

**2. Review and approve** (unattended replay refuses drafts):

```bash
python -m cua approve member.get_savings_balance --by "your-name" --notes "reviewed targets + checkpoint"
```

**3. Replay deterministically with new inputs (no model):**

```bash
python -m cua replay member.get_savings_balance --input member_id=100871
# -> {"status": "succeeded", "outputs": {"primary_savings_balance": "3215.00"}, ...}
```

**4. Runtime conditions** (`--chaos` is a test-harness hook on the mock app, not part of the engine):

```bash
python -m cua replay member.get_savings_balance --input member_id=99999                          # business_outcome MEMBER_NOT_FOUND
python -m cua replay member.get_savings_balance --input member_id=55555                          # business_outcome PERMISSION_DENIED
python -m cua replay member.get_savings_balance --input member_id=12ab                           # rejected INVALID_INPUT (UI never touched)
python -m cua replay member.get_savings_balance --input member_id=100234 --chaos '{"notice": true}'        # recovered: SYSTEM_NOTICE dismissed
python -m cua replay member.get_savings_balance --input member_id=100234 --chaos '{"expire_after": 3}'     # recovered: re-auth + restart
python -m cua replay member.get_savings_balance --input member_id=100234 --chaos '{"error_path": "/work/member/"}'  # failed APP_ERROR + evidence bundle
```

**5. Human-in-the-loop handoff on the live session.** Member 77777 is flagged and needs a
supervisor override, which automation is never allowed to enter:

```bash
python -m cua replay member.get_savings_balance --input member_id=77777 --escalate
# prints:  >>> INTERVENTION iv-…  operator console: http://127.0.0.1:8700/s/sess-…?token=…
```

Open the console URL and click **Take control**. Click the Supervisor Code field, type `4321`,
press **Type**, click **Approve Override** on the screenshot, then **Hand back & resume**. The run
continues on the same browser session and returns the balance. What you did is recorded in
`interventions[].human_actions`, with the code masked. Add `--operator-bot` to have a scripted
operator do the same thing through the same API.

**6. Tenant variant (same vendor product, different configuration):**

```bash
python -m cua replay member.get_savings_balance --tenant bayside --input member_id=100234 --no-overlays  # drift pinpointed
python -m cua replay member.get_savings_balance --tenant bayside --input member_id=100234                # base artifact + overlay
```

**7. Agent-facing interface (stretch goal):**

```bash
python -m cua catalog                                             # approved capabilities as typed tools
python -m cua invoke member__get_savings_balance --args '{"tenant":"acme","member_id":"100234"}'
python -m cua ask "What is the primary savings balance for Acme member 100871?"   # a model picks and calls the tool
```

A second capability (`share.open_account_to_review`) fills a multi-field form and stops on the
review screen. It never clicks **Confirm**, which is classified irreversible:

```bash
python -m cua replay share.open_account_to_review --input member_id=100871 \
  --input "share_type=VACATION CLUB" --input initial_deposit=50.00
```

Regenerate the whole replay evidence suite with `python scripts/run_scenarios.py`.

## Running without live services

- **No API key:** everything except `discover` and `ask` works. The committed, approved
  artifacts in `capabilities/` replay against the local mock app. `discover --scripted
  evidence/runs/<discovery-run>/llm_transcript.jsonl` re-drives the recorded decisions of the real
  model run through the full pipeline (clearly labelled `scripted:` in its evidence; it is not a
  discovery run).
- **No network at all:** the mock app, browser and engine are local. `pytest` runs the unit and
  end-to-end suite (about 2.5 min) and starts the mock app itself if it isn't running.

```bash
pytest -q
```

## Repository layout

```
cua/
  schema.py        typed documents: Capability, AppProfile, TenantOverlay, ReplayResult (pydantic → JSON Schema)
  surface/         the perception/action seam: base.py (protocol), web.py (Playwright), resolver.js (in-page a11y model)
  discovery.py     LLM observe→decide→act loop + compile to a Capability
  recorder.py      locator-strategy derivation + record-time validation, postconditions, route canonicalisation
  replay.py        deterministic executor, screen classifier, recovery, escalation, result contract
  policy.py        allowlist, risk classification, irreversible gating
  redact.py        sensitivity-driven redaction / fingerprints
  control.py       session control lease + interventions (human handoff)
  operator.py      operator console (mock UI, real control transfer) · operator_bot.py scripted operator
  catalog.py       capabilities as agent tools · llm.py model access · evidence.py run evidence · cli.py
config/            policy.json · profiles/ (per vendor product) · tenants/ · overlays/ (per tenant)
capabilities/      recorded artifacts (versioned, reviewed)
mockapp/           CoreLink Teller legacy mock (2 tenants, fault injection)
evidence/          discovery + replay runs, scenario index
tests/             unit + end-to-end tests
```
