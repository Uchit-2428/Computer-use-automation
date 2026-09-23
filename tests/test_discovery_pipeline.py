"""Discovery pipeline with a rule-based stand-in for the model (offline).

Proves the recorder turns an observe->decide->act run into a valid, parameterised,
replayable artifact, and that the policy gate stops irreversible actions during
discovery. The *genuine* model run lives in evidence/.
"""
import asyncio
from pathlib import Path

import pytest

from cua.config import load_policy, load_profile, load_tenant
from cua.discovery import DeclaredParam, discover
from cua.replay import ReplayOptions, replay
from cua.schema import RunStatus
from fakes import BALANCE_PLAN, RuleDecider

pytestmark = pytest.mark.usefixtures("mock_app")


def test_discover_then_replay_with_other_input(tmp_path: Path):
    import httpx
    httpx.post("http://127.0.0.1:8600/__chaos/acme", json={})
    t, prof = load_tenant("acme"), load_profile("corelink-teller@1")
    pol = load_policy(prof.irreversible_labels)
    res = asyncio.run(discover("Look up member 100234 and read their primary savings balance", t, prof, pol, RuleDecider(BALANCE_PLAN),
                               [DeclaredParam.parse("member_id=100234:integer:pii")], evidence_root=str(tmp_path / "ev"), save_dir=tmp_path))
    assert res.status == "succeeded", res.reason
    cap = res.capability
    assert [i.name for i in cap.inputs] == ["member_id"] and cap.outputs[0].name == "savings_balance"
    assert "100234" not in cap.dump()
    fill = next(s for s in cap.steps if s.action.type == "fill")
    assert fill.action.value == "{{inputs.member_id}}"
    view = next(s for s in cap.steps if s.id.startswith("click_view"))
    assert view.action.target.strategies[0].kind == "table_cell" and view.action.target.strategies[0].row_key == "{{inputs.member_id}}"
    r = asyncio.run(replay(cap, {"member_id": "100871"}, t, prof, pol, ReplayOptions(allow_draft=True, evidence_root=str(tmp_path / "ev"))))
    assert r.status == RunStatus.succeeded and r.outputs == {"savings_balance": "3215.00"}


def test_discovery_blocks_irreversible_click(tmp_path: Path):
    plan = [
        ("click", {"find": ("link", "New Share Account")}),
        ("type_text", {"find": ("textbox", "Member #"), "text": "{{member_id}}"}),
        ("select_option", {"find": ("combobox", "Share Type"), "option": "HOLIDAY CLUB"}),
        ("type_text", {"find": ("textbox", "Initial Deposit"), "text": "25.00"}),
        ("click", {"find": ("button", "Continue")}),
        ("click", {"find": ("button", "Confirm")}),  # must be blocked
        ("finish", {"success_evidence": "Review New Share Account", "capability_id": "share.open_review", "title": "t", "description": "d"}),
    ]
    import httpx
    before = httpx.get("http://127.0.0.1:8600/__opened").json()["count"]
    t, prof = load_tenant("acme"), load_profile("corelink-teller@1")
    res = asyncio.run(discover("Open a holiday club share for member 100234 and reach the review screen", t, prof,
                               load_policy(prof.irreversible_labels), RuleDecider(plan), [DeclaredParam.parse("member_id=100234:integer:pii")],
                               evidence_root=str(tmp_path / "ev"), save_dir=tmp_path))
    assert res.status == "succeeded", res.reason
    assert httpx.get("http://127.0.0.1:8600/__opened").json()["count"] == before  # nothing was committed
    assert all(s.risk.value != "irreversible" for s in res.capability.steps)
    events = (Path(res.evidence_dir) / "events.jsonl").read_text()
    assert '"allowed": false' in events and "irreversible" in events
