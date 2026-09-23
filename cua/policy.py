"""Safety guardrails: allowlist + risk classification + irreversible-action gating.

Enforced at three independent layers (defence in depth):
1. network  — WebSurface routes every browser request through ``Policy.url_allowed``;
               anything off-list is aborted before it leaves the browser.
2. action   — every action (agent, replay) is checked for type and risk *at execution
               time*, against the live element, not against what the artifact claims.
3. artifact — replay refuses artifacts that are not approved or whose declared max
               risk exceeds what the invocation authorised.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field

from .schema import ACTION_TYPES, Model, Risk


class PolicyConfig(Model):
    allowed_origins: list[str] = Field(description="scheme://host:port the surface may load.")
    allowed_path_prefixes: list[str] = Field(description="Path prefixes the surface may load (per origin).")
    allowed_actions: list[str] = Field(default_factory=lambda: list(ACTION_TYPES))
    irreversible_name_patterns: list[str] = Field(
        default_factory=lambda: [r"^confirm\b", r"\bpost\b", r"\btransfer\b", r"\bdelete\b", r"\bclose account\b", r"\bsubmit payment\b", r"\bwithdraw", r"\bapprove\b"]
    )
    irreversible_mode: Literal["block", "require_confirmation"] = "require_confirmation"
    discovery_irreversible: Literal["block", "escalate"] = "block"
    require_approved_for_replay: bool = True
    persist_currency_in_evidence: bool = False


class PolicyViolation(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class Decision:
    allowed: bool
    risk: Risk
    reason: str
    needs_confirmation: bool = False


class Policy:
    def __init__(self, cfg: PolicyConfig, extra_irreversible: list[str] | None = None):
        self.cfg = cfg
        self._irr = [re.compile(p, re.I) for p in cfg.irreversible_name_patterns]
        for label in extra_irreversible or []:
            self._irr.append(re.compile(rf"^\s*{re.escape(label)}\s*$", re.I))

    @classmethod
    def load(cls, path: str | Path, extra_irreversible: list[str] | None = None) -> "Policy":
        return cls(PolicyConfig.model_validate(json.loads(Path(path).read_text())), extra_irreversible)

    # ---------------------------------------------------------------- allowlist
    def url_allowed(self, url: str) -> bool:
        if url.startswith(("about:", "data:")):
            return True
        u = urlparse(url)
        origin = f"{u.scheme}://{u.netloc}"
        if origin not in self.cfg.allowed_origins:
            return False
        return any(u.path.startswith(p) for p in self.cfg.allowed_path_prefixes)

    def check_url(self, url: str) -> None:
        if not self.url_allowed(url):
            raise PolicyViolation("POLICY_URL_BLOCKED", f"URL not on allowlist: {urlparse(url).netloc}{urlparse(url).path}")

    def check_action_type(self, action_type: str) -> None:
        if action_type not in self.cfg.allowed_actions:
            raise PolicyViolation("POLICY_ACTION_BLOCKED", f"action type '{action_type}' is not allowed")

    # ---------------------------------------------------------------- risk
    def classify(self, action_type: str, role: str | None = None, name: str | None = None) -> Risk:
        if action_type in ("extract", "wait_for", "navigate"):
            return Risk.read_only
        if action_type == "click" and name and role in ("button", "link") and any(p.search(name) for p in self._irr):
            return Risk.irreversible
        if action_type == "press" and name and any(p.search(name) for p in self._irr):
            return Risk.irreversible
        return Risk.reversible

    def gate(self, risk: Risk, *, mode: Literal["discovery", "replay"], confirmed: bool = False) -> Decision:
        if risk != Risk.irreversible:
            return Decision(True, risk, "safe/reversible")
        if mode == "discovery":
            if self.cfg.discovery_irreversible == "block":
                return Decision(False, risk, "irreversible actions are never taken autonomously during discovery")
            return Decision(False, risk, "irreversible action requires human approval", needs_confirmation=True)
        if self.cfg.irreversible_mode == "block":
            return Decision(False, risk, "irreversible actions are blocked by policy")
        if not confirmed:
            return Decision(False, risk, "irreversible step requires explicit caller confirmation", needs_confirmation=True)
        return Decision(True, risk, "irreversible step confirmed by caller")
