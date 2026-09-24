"""Redaction for everything that is *persisted* (logs, evidence, artifacts) or sent to a model.

Values are classified by the capability contract (``Sensitivity``) and registered
per run; anything registered is replaced wherever it appears. Pattern rules catch
common regulated data that was never declared (card numbers, SSNs, amounts).

* pii        -> "••••0234" (last 4 kept so operators can correlate)
* financial  -> "fp:3f9a1c0b2d" (keyed HMAC fingerprint: proves two runs saw the same
                value — useful for determinism checks — without persisting it)
* secret     -> "[SECRET]" (and secrets are never registered with their value in any
                model-visible string in the first place)
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from typing import Any

from .schema import Sensitivity

_SECRET_KEYS = re.compile(r"(pass(word)?|pwd|secret|token|cookie|authorization|api[_-]?key|supcd)", re.I)
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[SSN]"),
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]+"), "[API_KEY]"),
    (re.compile(r"AIza[0-9A-Za-z_\-]{30,}"), "[API_KEY]"),
]
_CURRENCY = re.compile(r"\$\s?[\d,]+\.\d{2}")
# card numbers: 13-19 digits, contiguous or in 4-digit groups, and Luhn-valid (so run ids,
# timestamps and reference numbers are not over-masked)
_PAN = re.compile(r"\b(?:\d{4}[ -]){2,3}\d{1,7}\b|\b\d{13,19}\b")


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _mask_pan(m: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", m.group(0))
    return "[PAN]" if 13 <= len(digits) <= 19 and _luhn(digits) else m.group(0)


class Redactor:
    def __init__(self, key: bytes | None = None, mask_currency: bool = True, pii_patterns: list[str] | None = None):
        self.key = key or os.environ.get("CUA_EVIDENCE_KEY", "dev-only-evidence-key").encode()
        self.mask_currency = mask_currency
        self.pii_patterns = list(pii_patterns or [])
        self._pii = [re.compile(p) for p in self.pii_patterns]
        self._values: dict[str, str] = {}

    def fingerprint(self, value: str) -> str:
        return "fp:" + hmac.new(self.key, value.encode(), hashlib.sha256).hexdigest()[:10]

    def mask(self, value: str, sensitivity: Sensitivity) -> str:
        if sensitivity == Sensitivity.secret:
            return "[SECRET]"
        if sensitivity == Sensitivity.pii:
            return "••••" + value[-4:] if len(value) > 4 else "••••"
        if sensitivity == Sensitivity.financial:
            return self.fingerprint(value)
        return value

    def register(self, value: Any, sensitivity: Sensitivity) -> None:
        if value is None:
            return
        v = str(value)
        if len(v) < 3 or sensitivity in (Sensitivity.public, Sensitivity.internal):
            return
        self._values[v] = self.mask(v, sensitivity)
        # also catch the display form of financial values ("12480.55" is shown as "12,480.55")
        if sensitivity == Sensitivity.financial and re.fullmatch(r"-?\d+(\.\d+)?", v):
            whole, _, frac = v.partition(".")
            disp = f"{int(whole):,}" + (f".{frac}" if frac else "")
            self._values[disp] = self._values[v]

    def sensitive_values(self) -> list[str]:
        return list(self._values)

    def text(self, s: str) -> str:
        for v in sorted(self._values, key=len, reverse=True):
            s = s.replace(v, self._values[v])
        for pat, rep in _PATTERNS:
            s = pat.sub(rep, s)
        s = _PAN.sub(_mask_pan, s)
        for pat in self._pii:
            s = pat.sub("[PII]", s)
        if self.mask_currency:
            s = _CURRENCY.sub("$[AMT]", s)
        return s

    def scrub(self, obj: Any, _key: str = "") -> Any:
        if isinstance(obj, dict):
            return {k: ("[SECRET]" if _SECRET_KEYS.search(str(k)) and isinstance(v, str) and v else self.scrub(v, str(k))) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.scrub(v) for v in obj]
        if isinstance(obj, str):
            return self.text(obj)
        return obj
