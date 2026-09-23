"""End-to-end tests against the live mock app (Playwright, real browser, no model).

Each test replays the fixture capability under a runtime condition and asserts the
result lands in the right bucket of the taxonomy: success / business outcome /
recovered / hard failure / escalation.
"""
import asyncio
import json
from pathlib import Path

import httpx
import pytest

from cua.config import load_policy, load_profile, load_tenant
from cua.operator import TOKEN, OperatorConsole
from cua.operator_bot import run_bot
from cua.replay import ReplayOptions, replay
from cua.schema import Capability, ReviewStatus, RunStatus

FIX = Path(__file__).parent / "fixtures"
pytestmark = pytest.mark.usefixtures("mock_app")


def cap() -> Capability:
    return Capability.model_validate_json((FIX / "member.get_savings_balance.json").read_text())


def run(inputs, chaos=None, tenant="acme", capability=None, **opts):
    tid = tenant if isinstance(tenant, str) else tenant.id
    httpx.post(f"http://127.0.0.1:8600/__chaos/{tid}", json=chaos or {}).raise_for_status()
    c = capability or cap()
    t = tenant if not isinstance(tenant, str) else load_tenant(tenant)
    prof = load_profile(c.app.profile)
    o = ReplayOptions(evidence_root="/tmp/cua-test-evidence", screenshot_every_step=False, **opts)
    return asyncio.run(replay(c, inputs, t, prof, load_policy(prof.irreversible_labels), o))


def test_happy_path_two_members():
    r = run({"member_id": "100234"})
    assert r.status == RunStatus.succeeded and r.outputs == {"savings_balance": "12480.55"}
    r = run({"member_id": "100871"})
    assert r.status == RunStatus.succeeded and r.outputs == {"savings_balance": "3215.00"}
    persisted = json.loads((Path(r.evidence_dir) / "result.json").read_text())
    assert persisted["outputs"]["savings_balance"].startswith("fp:")  # never persisted in clear
    assert "100871" not in (Path(r.evidence_dir) / "events.jsonl").read_text()


def test_business_outcomes_are_not_failures():
    r = run({"member_id": "99999"})
    assert r.status == RunStatus.business_outcome and r.outcome.code == "MEMBER_NOT_FOUND" and r.outcome.declared
    r = run({"member_id": "55555"})
    assert r.status == RunStatus.business_outcome and r.outcome.code == "PERMISSION_DENIED"
    r = run({"member_id": "123456789"})  # passes our schema, rejected by the host
    assert r.status == RunStatus.business_outcome and r.outcome.code == "VALIDATION_ERROR"


def test_bad_input_rejected_before_touching_ui():
    r = run({"member_id": "12ab"})
    assert r.status == RunStatus.rejected and r.error.code == "INVALID_INPUT" and not r.steps


def test_draft_not_replayed_unattended():
    c = cap()
    c.review.status = ReviewStatus.draft
    r = run({"member_id": "100234"}, capability=c)
    assert r.status == RunStatus.rejected and r.error.code == "NOT_APPROVED"


@pytest.mark.parametrize("chaos,code", [({"notice": True}, "SYSTEM_NOTICE"), ({"expire_after": 3}, "SESSION_EXPIRED"),
                                        ({"transient_path": "/work/member/"}, "HOST_BUSY")])
def test_recoverable_conditions(chaos, code):
    r = run({"member_id": "100234"}, chaos)
    assert r.status == RunStatus.succeeded and r.outputs["savings_balance"] == "12480.55"
    assert [x.code for x in r.recoveries] == [code]


def test_app_error_is_hard_failure_with_evidence():
    r = run({"member_id": "100234"}, {"error_path": "/work/member/"})
    assert r.status == RunStatus.failed and r.error.code == "APP_ERROR" and r.error.step_id == "click_view"
    assert "SYS-0042" in r.error.message
    assert any(e.endswith(".png") for e in r.error.evidence) and any("dom-work" in e for e in r.error.evidence)


def test_slow_host_times_out_as_retryable():
    c = cap()
    for s in c.steps:
        s.timeout_ms = 2500
    r = run({"member_id": "100234"}, {"slow_ms": 4000}, capability=c)
    assert r.status == RunStatus.failed and r.error.code == "TIMEOUT" and r.error.retryable


def test_escalation_hands_live_session_to_operator_and_resumes():
    async def go():
        console = await OperatorConsole(port=8711).start()
        bot = asyncio.create_task(run_bot("http://127.0.0.1:8711", TOKEN, max_wait_s=60))
        httpx.post("http://127.0.0.1:8600/__chaos/acme", json={})
        prof = load_profile("corelink-teller@1")
        try:
            return await replay(cap(), {"member_id": "77777"}, load_tenant("acme"), prof, load_policy(prof.irreversible_labels),
                                ReplayOptions(evidence_root="/tmp/cua-test-evidence", escalate=True, escalation_timeout_s=60))
        finally:
            bot.cancel()
            await console.stop()

    r = asyncio.run(go())
    assert r.status == RunStatus.succeeded and r.outputs["savings_balance"] == "910.10"
    iv = r.interventions[0]
    assert iv.operator == "supervisor.kim" and iv.resolution == "resolved" and iv.step_id == "click_inquire"
    dom = [a for a in iv.human_actions if a.get("via") == "dom"]
    assert any(a["name"] == "Approve Override" for a in dom)
    assert all(a.get("value") in (None, "[SECRET]") for a in dom if a.get("secret"))


def test_escalation_disabled_fails_with_human_required():
    r = run({"member_id": "77777"})
    assert r.status == RunStatus.failed and r.error.code == "HUMAN_REQUIRED"


def test_tenant_variant_drift_then_overlay():
    base = load_tenant("bayside").model_copy(update={"overlays": []})
    r = run({"member_id": "100234"}, tenant=base)
    assert r.status == RunStatus.failed and r.error.code == "TARGET_NOT_FOUND" and r.error.step_id == "click_inquire"
    assert "Search" in r.error.observed  # near-miss diagnostics point at the relabelled control
    assert any("fallback 'attribute'" in w for w in r.warnings)  # field survived via its form-field name
    with_overlay = base.model_copy(update={"overlays": ["tests/fixtures/bayside_overlay.json"]})
    r = run({"member_id": "100234"}, tenant=with_overlay)
    assert r.status == RunStatus.succeeded and r.outputs["savings_balance"] == "12480.55"


def test_irreversible_step_requires_confirmation():
    data = json.loads((FIX / "member.get_savings_balance.json").read_text())
    data["id"] = "test.irreversible"
    data["steps"].insert(4, {"id": "confirm_it", "intent": "Commit", "risk": "irreversible", "action": {"type": "click", "target": {
        "description": "Confirm", "strategies": [{"kind": "role_name", "role": "button", "name": "Confirm"}]}}})
    r = run({"member_id": "100234"}, capability=Capability.model_validate(data))
    assert r.status == RunStatus.rejected and r.error.code == "CONFIRMATION_REQUIRED"


def test_navigation_outside_allowlist_blocked():
    data = json.loads((FIX / "member.get_savings_balance.json").read_text())
    data["steps"].insert(0, {"id": "sneak", "intent": "Leave the allowlist", "risk": "read_only",
                             "action": {"type": "navigate", "url": "http://127.0.0.1:8600/__opened"}})
    r = run({"member_id": "100234"}, capability=Capability.model_validate(data))
    assert r.status == RunStatus.failed and r.error.code == "POLICY_URL_BLOCKED"
