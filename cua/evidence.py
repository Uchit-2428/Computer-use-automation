"""Run evidence: a structured, redacted event log plus richer signals on failure.

Layout of one run directory::

    <root>/<run_id>/
        events.jsonl          every decision/action/check, redacted, ordered (seq)
        result.json           final result (redacted)
        screens/NN-<label>.png  masked screenshots (sensitive values and amounts blacked out)
        failure/              DOM snapshot per frame (redacted) + observation text, on failure
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .redact import Redactor
from .schema import utcnow


class Evidence:
    def __init__(self, root: str | Path, run_id: str, redactor: Redactor):
        self.dir = Path(root) / run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "screens").mkdir(exist_ok=True)
        self.redactor = redactor
        self.run_id = run_id
        self._seq = 0
        self._shot = 0
        self._t0 = time.monotonic()
        self._fh = (self.dir / "events.jsonl").open("a", encoding="utf-8")

    def event(self, type_: str, **data: Any) -> None:
        self._seq += 1
        rec = {"seq": self._seq, "ts": utcnow(), "t_ms": int((time.monotonic() - self._t0) * 1000), "type": type_, **data}
        self._fh.write(json.dumps(self.redactor.scrub(rec), default=str) + "\n")
        self._fh.flush()

    def rel(self, p: Path) -> str:
        return str(p.relative_to(self.dir))

    async def screenshot(self, surface: Any, label: str) -> str | None:
        self._shot += 1
        p = self.dir / "screens" / f"{self._shot:02d}-{label}.png"
        try:
            await surface.screenshot(str(p), self.redactor.sensitive_values(), self.redactor.mask_currency, self.redactor.pii_patterns)
        except Exception as e:  # evidence must never crash the run
            self.event("evidence_error", what="screenshot", error=str(e))
            return None
        return self.rel(p)

    async def failure_bundle(self, surface: Any, label: str, observation_text: str | None = None) -> list[str]:
        out: list[str] = []
        d = self.dir / "failure"
        d.mkdir(exist_ok=True)
        shot = await self.screenshot(surface, f"FAIL-{label}")
        if shot:
            out.append(shot)
        try:
            for frame, html in (await surface.dom_snapshots()).items():
                p = d / f"{label}-dom-{frame}.html"
                p.write_text(self.redactor.text(html), encoding="utf-8")
                out.append(self.rel(p))
        except Exception as e:
            self.event("evidence_error", what="dom", error=str(e))
        if observation_text:
            p = d / f"{label}-observation.txt"
            p.write_text(self.redactor.text(observation_text), encoding="utf-8")
            out.append(self.rel(p))
        return out

    def write_json(self, name: str, obj: Any) -> str:
        p = self.dir / name
        p.write_text(json.dumps(self.redactor.scrub(obj), indent=2, default=str), encoding="utf-8")
        return self.rel(p)

    def close(self) -> None:
        self._fh.close()
