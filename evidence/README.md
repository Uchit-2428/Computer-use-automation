# Evidence

Everything here was produced by the code in this repo against the CoreLink mock (synthetic data).
All persisted files are redacted: member numbers show only their last 4 digits, balances are replaced
by keyed fingerprints (`fp:…`), names become `[PII]`, secrets become `[SECRET]`, and screenshots are
masked. The caller gets real values only on the return channel (stdout), which is not stored here.

## 1. Genuine LLM discovery runs

Both runs were made with `bash scripts/discover_all.sh` on 2026-09-24 using free-tier **Gemini Flash**.

| run | goal | result | model calls | saved artifact |
|---|---|---|---|---|
| [`discovery-20260924-160310-233e`](runs/discovery-20260924-160310-233e/) | Look up member 100234 and read the current balance of their primary savings account | succeeded, 5 recorded steps | 6 (≈19k input / 342 output tokens) | [`member.get_savings_balance`](../capabilities/member.get_savings_balance.json) |
| [`discovery-20260924-160907-4596`](runs/discovery-20260924-160907-4596/) | Start opening a HOLIDAY CLUB share for member 100234 with a 25.00 deposit; stop on the review screen without confirming | succeeded, 5 recorded steps | 6 (≈19k / 366) | [`share.open_account_to_review`](../capabilities/share.open_account_to_review.json) |

Each run directory contains:

- `llm_transcript.jsonl`: every model decision (tool, arguments, one-line reasoning), with the model
  that actually answered.
- `events.jsonl`: sign-on via the product profile, policy decisions, each action with the locator
  strategies the recorder validated and the postconditions it derived, and the saved capability.
- `screens/`: masked screenshots after every step.
- `capability.json`: the compiled draft artifact.

Things worth noticing:

- The model typed `{{member_id}}` and never saw the value. Sign-on ran from the product profile
  before the model was involved, so the model never saw credentials either.
- The balance run hit Google 503s ("high demand"). The client failed over between Flash models,
  and the transcript shows `gemini-3.8-flash` answering step 1 and `gemini-3.5-flash` the rest.
  The reviewer corrected the artifact's `provenance.model` to match, and the engine now records
  models actually used.
- In the share run the model stopped on the review screen on its own and never tried **Confirm**.
  The policy block on irreversible clicks during discovery is exercised by
  `tests/test_discovery_pipeline.py::test_discovery_blocks_irreversible_click`. The replay-side
  gate is shown below (`rejected-irreversible-unconfirmed`).
- Determinism check: the balance fingerprint extracted during discovery (`fp:1a4327f904` in
  `events.jsonl`) is the same one the happy-path replay extracts.
- Known cosmetic issue: an early card-number redaction rule over-matched the timestamp inside
  run ids, so `discovery_result.json` shows `"run_id": "discovery-[PAN]…"`. The rule now requires
  a Luhn-valid number (`tests/test_units.py::test_redaction_does_not_mask_ids_that_look_numeric`).
  The evidence is left as produced.
- Three earlier attempts failed before any step and are not included: a retired model id (404),
  then Google overload (503). That led to the failover logic in `cua/llm.py`.

After discovery the artifacts were reviewed and approved. Reviewer edits are listed in each
artifact's `review.notes`: input descriptions, the `share_type` enum, and the provenance correction.

## 2. Deterministic replays (no model)

[`SCENARIOS.md`](SCENARIOS.md) lists 17 replays: happy paths, business outcomes, pre-flight
rejections, recovered runtime conditions, hard failures with evidence bundles, the live-session
handoff, cross-tenant drift and overlay, and the irreversible-action gate. The key ones:

- **Hard failure with debuggable evidence:** `failed-app-error`. Its `result.json` gives code,
  step, expected/observed, and `failure/` holds the redacted DOM of every frame, the observation,
  and a masked screenshot.
- **Human handoff on the live session:** `escalated-supervisor-override`. `events.jsonl` shows
  `control_transfer` automation → awaiting_operator → human:supervisor.kim → automation.
  `result.json → interventions[].human_actions` records what the operator did; the password-field
  value is `[SECRET]`. The run then finishes with the balance.
- **Cross-tenant:** `tenant-bayside-base-artifact` fails `TARGET_NOT_FOUND` at `click_inquire`
  and reports the candidate `button 'Search'`. `tenant-bayside-with-overlay` succeeds with the
  two-entry overlay in [`../config/overlays/`](../config/overlays/).

## 3. Agent-facing interface

See [`agent_invocation.md`](agent_invocation.md) and [`catalog.json`](catalog.json).

## Reproduce

```bash
python -m mockapp.server &          # terminal 1
bash scripts/discover_all.sh        # genuine discovery (needs GEMINI_API_KEY or ANTHROPIC_API_KEY)
python scripts/run_scenarios.py     # the replay suite -> evidence/runs + SCENARIOS.md
```
