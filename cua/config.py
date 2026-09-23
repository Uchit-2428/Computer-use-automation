"""Loading of on-disk documents (tenants, profiles, capabilities, overlays, policy)."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .policy import Policy
from .schema import AppProfile, Capability, TenantConfig, TenantOverlay

ROOT = Path(os.environ.get("CUA_HOME", Path(__file__).resolve().parent.parent))
CONFIG = ROOT / "config"
CAPABILITIES = ROOT / "capabilities"
EVIDENCE = ROOT / "evidence"


def load_dotenv(path: Path | None = None) -> None:
    p = path or ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_tenant(tenant_id: str) -> TenantConfig:
    return TenantConfig.model_validate_json((CONFIG / "tenants" / f"{tenant_id}.json").read_text())


def load_profile(ref: str) -> AppProfile:
    """ref: 'corelink-teller@1'"""
    pid = ref.split("@", 1)[0]
    prof = AppProfile.model_validate_json((CONFIG / "profiles" / f"{pid}.json").read_text())
    want_major = ref.split("@", 1)[1] if "@" in ref else None
    if want_major and prof.version.split(".")[0] != want_major:
        raise ValueError(f"profile {pid} is v{prof.version}, capability needs major {want_major}")
    return prof


def load_policy(extra_irreversible: list[str] | None = None) -> Policy:
    return Policy.load(CONFIG / "policy.json", extra_irreversible)


def load_capability(path_or_id: str) -> tuple[Capability, Path]:
    p = Path(path_or_id)
    if not p.exists():
        p = CAPABILITIES / f"{path_or_id}.json"
    return Capability.model_validate_json(p.read_text()), p


def list_capabilities() -> list[tuple[Capability, Path]]:
    out = []
    for p in sorted(CAPABILITIES.glob("*.json")):
        try:
            out.append((Capability.model_validate_json(p.read_text()), p))
        except Exception:
            continue
    return out


def load_overlays(tenant: TenantConfig, capability: Capability) -> list[TenantOverlay]:
    out = []
    for rel in tenant.overlays:
        ov = TenantOverlay.model_validate_json((ROOT / rel).read_text())
        if ov.capability != capability.id:
            continue
        major = ov.capability_versions.split(".")[0]
        if capability.version.split(".")[0] != major:
            continue
        out.append(ov)
    return out


def version_satisfies(version: str, spec: str) -> bool:
    """Tiny semver range check: '>=4.2 <5' style, space-separated clauses."""

    def t(v: str) -> tuple[int, ...]:
        return tuple(int(x) for x in re.findall(r"\d+", v)[:3]) + (0,) * (3 - len(re.findall(r"\d+", v)[:3]))

    v = t(version)
    for clause in spec.split():
        m = re.match(r"(>=|<=|>|<|==|=)?(.+)", clause)
        assert m
        op, rhs = m.group(1) or "==", t(m.group(2))
        ok = {">=": v >= rhs, "<=": v <= rhs, ">": v > rhs, "<": v < rhs, "==": v == rhs, "=": v == rhs}[op]
        if not ok:
            return False
    return True
