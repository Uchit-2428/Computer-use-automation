"""Fast unit tests: schema contract, redaction, policy, templating, input validation."""
import json
from pathlib import Path

import pytest

from cua.policy import Policy, PolicyConfig, PolicyViolation
from cua.redact import Redactor
from cua.replay import apply_overlays, normalise, validate_inputs
from cua.schema import Capability, Risk, Sensitivity, TenantConfig, TenantOverlay, ValueType
from cua.templating import Renderer, SecretError, Templater

FIX = Path(__file__).parent / "fixtures"
CAP = json.loads((FIX / "member.get_savings_balance.json").read_text())
TENANT = TenantConfig(id="acme", display_name="Acme", base_url="http://127.0.0.1:8600/t/acme", product="corelink-teller",
                      app_version="4.2.1", secrets={"pw": "env:CUA_TEST_SECRET"})


# ------------------------------------------------------------------ schema
def test_capability_round_trip_and_max_risk():
    cap = Capability.model_validate(CAP)
    again = Capability.model_validate_json(cap.dump())
    assert again == cap
    assert cap.max_risk == Risk.reversible
    assert cap.ref == "member.get_savings_balance@1.0.0"


def test_undeclared_input_reference_rejected():
    bad = json.loads(json.dumps(CAP))
    bad["steps"][1]["action"]["value"] = "{{inputs.account_no}}"
    with pytest.raises(Exception, match="undeclared input"):
        Capability.model_validate(bad)


def test_declared_output_must_be_extracted():
    bad = json.loads(json.dumps(CAP))
    bad["outputs"].append({"name": "checking_balance", "type": "currency", "description": "x"})
    with pytest.raises(Exception, match="never extracted"):
        Capability.model_validate(bad)


def test_artifact_contains_no_concrete_parameter_values():
    blob = json.dumps(CAP)
    assert "100234" not in blob and "12,480.55" not in blob and "DOE" not in blob


def test_overlay_replaces_only_named_targets():
    cap = Capability.model_validate(CAP)
    ov = TenantOverlay.model_validate_json((FIX / "bayside_overlay.json").read_text())
    new, applied = apply_overlays(cap, [ov])
    assert applied == ["test-bayside-balance"]
    by = {s.id: s for s in new.steps}
    assert by["click_inquire"].action.target.strategies[0].name == "Search"
    assert by["enter_member"] == {s.id: s for s in cap.steps}["enter_member"]


# ------------------------------------------------------------------ redaction
def test_redaction_masks_by_sensitivity():
    r = Redactor(key=b"k")
    r.register("100234", Sensitivity.pii)
    r.register("12480.55", Sensitivity.financial)
    out = r.text("member 100234 has $12,480.55 (12480.55) card 4111 1111 1111 1111 ssn 123-45-6789")
    assert "100234" not in out and "0234" in out
    assert "12480.55" not in out and "12,480.55" not in out and "fp:" in out
    assert "4111" not in out and "123-45-6789" not in out


def test_redaction_does_not_mask_ids_that_look_numeric():
    r = Redactor(key=b"k")
    s = "run discovery-20260924-160310-233e ref 20260924160310"
    assert r.text(s) == s  # not Luhn-valid card numbers


def test_redaction_scrubs_secret_keys_and_is_deterministic():
    r = Redactor(key=b"k")
    assert r.scrub({"password": "hunter2", "nested": {"api_key": "x"}}) == {"password": "[SECRET]", "nested": {"api_key": "[SECRET]"}}
    assert r.fingerprint("1.00") == Redactor(key=b"k").fingerprint("1.00") != Redactor(key=b"other").fingerprint("1.00")


# ------------------------------------------------------------------ policy
POL = Policy(PolicyConfig(allowed_origins=["http://127.0.0.1:8600"], allowed_path_prefixes=["/t/acme/"]), ["Confirm"])


def test_allowlist():
    assert POL.url_allowed("http://127.0.0.1:8600/t/acme/main")
    assert not POL.url_allowed("http://127.0.0.1:8600/__chaos/acme")
    assert not POL.url_allowed("http://evil.example/t/acme/")
    with pytest.raises(PolicyViolation):
        POL.check_url("http://127.0.0.1:8600/t/bayside/main")


def test_risk_classification_and_gating():
    assert POL.classify("click", "button", "Confirm") == Risk.irreversible
    assert POL.classify("click", "button", "Continue") == Risk.reversible
    assert POL.classify("extract", "cell", "x") == Risk.read_only
    assert not POL.gate(Risk.irreversible, mode="discovery").allowed
    d = POL.gate(Risk.irreversible, mode="replay")
    assert not d.allowed and d.needs_confirmation
    assert POL.gate(Risk.irreversible, mode="replay", confirmed=True).allowed
    assert POL.gate(Risk.reversible, mode="discovery").allowed


# ------------------------------------------------------------------ templating & inputs
def test_render_and_regex_escape(monkeypatch):
    r = Renderer({"q": "a.b"}, TENANT)
    assert r("{{tenant.base_url}}/x/{{inputs.q}}") == "http://127.0.0.1:8600/t/acme/x/a.b"
    assert r.regex("^{{inputs.q}}$") == r"^a\.b$"
    with pytest.raises(SecretError):
        r("{{secrets.pw}}")
    monkeypatch.setenv("CUA_TEST_SECRET", "s3cr3t")
    assert r("{{secrets.pw}}") == "s3cr3t"


def test_route_canonicalisation():
    t = Templater({"member_id": "100234"}, TENANT)
    assert t.path_regex("/t/acme/work/member/100234") == r"^{{tenant.base_path}}/work/member/{{inputs.member_id}}(\?.*)?$"
    assert t.path_regex("/t/acme/work/item/55") == r"^{{tenant.base_path}}/work/item/\d+(\?.*)?$"


def test_input_validation():
    specs = Capability.model_validate(CAP).inputs
    assert validate_inputs(specs, {"member_id": "100234"}) == []
    assert validate_inputs(specs, {"member_id": "12ab"})
    assert validate_inputs(specs, {})
    assert validate_inputs(specs, {"member_id": "1", "extra": "x"})


def test_normalise_currency():
    assert normalise("$12,480.55", ValueType.currency) == "12480.55"
    assert normalise("($5.00)", ValueType.currency) == "-5.00"
    with pytest.raises(ValueError):
        normalise("N/A", ValueType.currency)
