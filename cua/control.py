"""Control-transfer model for human-in-the-loop escalation.

One live session has exactly one *controller* at a time:

    automation ──escalate()──▶ awaiting_operator ──claim(op)──▶ human:<op>
        ▲                                                         │
        └──────────────── release(op, "resume") ◀─────────────────┘
                          release(op, "abort")  → run fails ESCALATION_ABORTED

* The automation calls ``assert_automation()`` before every action: if it does not
  hold the lease it cannot act (no "both drive at once").
* Only the operator who claimed the session may send input or release it.
* While a human holds the lease, every click/change in *any* frame is captured by the
  in-page listener (same element-description vocabulary as the recorder) and stored,
  redacted, on the intervention — so evidence spans the handoff and the human's steps
  are reviewable (and can be promoted into the artifact).
* Everything happens on the *same* browser context: cookies, frames, server session
  and history survive the handoff.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from .evidence import Evidence
from .redact import Redactor
from .schema import utcnow

Owner = str  # "automation" | "awaiting_operator" | "human:<operator>"


class ControlError(Exception):
    pass


@dataclass
class Intervention:
    id: str
    session_id: str
    run_id: str
    kind: Literal["stuck", "approval", "unrecoverable", "escalation_rule"]
    reason: str
    context: dict[str, Any]
    screenshot: str | None
    created_at: str = field(default_factory=utcnow)
    status: Literal["open", "claimed", "resolved", "aborted", "timed_out"] = "open"
    operator: str | None = None
    resolution_note: str | None = None
    human_actions: list[dict[str, Any]] = field(default_factory=list)
    _done: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _t0: float = field(default_factory=time.monotonic, repr=False)

    def public(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}


# process-local registry the operator console reads (a real deployment: a broker service)
REGISTRY: dict[str, "SessionController"] = {}
NOTIFIERS: list[Callable[[Intervention], None]] = []


class SessionController:
    def __init__(self, surface: Any, evidence: Evidence, redactor: Redactor, run_id: str):
        self.session_id = "sess-" + secrets.token_hex(4)
        self.surface = surface
        self.evidence = evidence
        self.redactor = redactor
        self.run_id = run_id
        self.owner: Owner = "automation"
        self.current: Intervention | None = None
        self.history: list[Intervention] = []
        surface.on_human_event = self._on_dom_event
        REGISTRY[self.session_id] = self

    def close(self) -> None:
        REGISTRY.pop(self.session_id, None)

    # ---------------------------------------------------------------- automation side
    def assert_automation(self) -> None:
        if self.owner != "automation":
            raise ControlError(f"automation does not hold the session (owner={self.owner})")

    async def escalate(self, kind: Any, reason: str, context: dict[str, Any], timeout_s: float) -> Intervention:
        self.assert_automation()
        shot = await self.evidence.screenshot(self.surface, "escalation")
        iv = Intervention(
            id="iv-" + secrets.token_hex(3), session_id=self.session_id, run_id=self.run_id, kind=kind,
            reason=reason, context=self.redactor.scrub(context), screenshot=shot,
        )
        self.current = iv
        self.history.append(iv)
        self._set_owner("awaiting_operator", f"intervention {iv.id} opened")
        self.evidence.event("intervention_requested", intervention=iv.public())
        for n in NOTIFIERS:
            try:
                n(iv)
            except Exception:
                pass
        try:
            await asyncio.wait_for(iv._done.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            iv.status = "timed_out"
            self._set_owner("automation", "operator did not respond; automation takes the session back")
        self.current = None
        self.evidence.event("intervention_closed", intervention=iv.public(), waited_ms=int((time.monotonic() - iv._t0) * 1000))
        return iv

    # ---------------------------------------------------------------- operator side
    def claim(self, operator: str) -> Intervention:
        iv = self.current
        if iv is None or iv.status != "open":
            raise ControlError("no open intervention on this session")
        iv.status, iv.operator = "claimed", operator
        self._set_owner(f"human:{operator}", f"{operator} took control")
        return iv

    def _require_human(self, operator: str) -> Intervention:
        if self.owner != f"human:{operator}" or self.current is None:
            raise ControlError(f"{operator} does not hold the session (owner={self.owner})")
        return self.current

    async def human_click(self, operator: str, x: float, y: float) -> None:
        iv = self._require_human(operator)
        iv.human_actions.append({"ts": utcnow(), "via": "console", "type": "pointer_click", "x": x, "y": y})
        await self.surface.mouse_click(x, y)
        await self.surface.settle(3000)

    async def human_type(self, operator: str, text: str) -> None:
        iv = self._require_human(operator)
        # typed text is not stored; the DOM 'change' capture records the field (and masks secrets)
        iv.human_actions.append({"ts": utcnow(), "via": "console", "type": "keyboard_type", "chars": len(text)})
        await self.surface.type_text(text)

    async def human_key(self, operator: str, key: str) -> None:
        iv = self._require_human(operator)
        iv.human_actions.append({"ts": utcnow(), "via": "console", "type": "key", "key": key})
        await self.surface.page.keyboard.press(key)
        await self.surface.settle(3000)

    def release(self, operator: str, resolution: Literal["resume", "abort"], note: str | None = None) -> Intervention:
        iv = self._require_human(operator)
        iv.status = "resolved" if resolution == "resume" else "aborted"
        iv.resolution_note = note
        self._set_owner("automation", f"{operator} handed control back ({resolution})")
        iv._done.set()
        return iv

    # ---------------------------------------------------------------- capture
    def _on_dom_event(self, payload: dict[str, Any], frame: str) -> None:
        if not self.owner.startswith("human:") or self.current is None:
            return  # automation's own actions are logged by the engine itself
        el = payload.get("element") or {}
        rec = {
            "ts": utcnow(),
            "via": "dom",
            "type": payload.get("type"),
            "frame": frame,
            "role": el.get("role"),
            "name": el.get("name"),
            "field": (el.get("attrs") or {}).get("name"),
            "secret": bool(payload.get("secret")),
        }
        if payload.get("type") == "change":
            rec["value"] = "[SECRET]" if payload.get("secret") else self.redactor.text(str(payload.get("value") or ""))
        rec["element"] = {k: el.get(k) for k in ("role", "name", "tag", "attrs", "table", "xpath")}
        self.current.human_actions.append(self.redactor.scrub(rec))
        self.evidence.event("human_action", intervention=self.current.id, action=rec)

    def _set_owner(self, owner: Owner, why: str) -> None:
        prev, self.owner = self.owner, owner
        self.evidence.event("control_transfer", session=self.session_id, from_=prev, to=owner, reason=why)
