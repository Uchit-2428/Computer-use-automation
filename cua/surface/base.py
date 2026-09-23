"""The seam between *how we perceive/act on a surface* and *the recorded flow*.

Everything above this interface (discovery, recorder, replay, handoff) speaks in
surface-neutral terms: frames/windows, elements with a role + accessible name,
``Target`` strategies, ``Condition`` checks. A driver implements this protocol for
one kind of surface:

* ``WebSurface``      (implemented) — Playwright + an injected resolver; works on
                      framesets / table layouts with no ids.
* ``UIASurface``      (designed)    — Windows UI Automation: role_name -> ControlType+Name,
                      FrameScope -> top-level window, table_cell -> Grid/Table patterns.
* ``VisionSurface``   (designed)    — screenshot + OCR/grounding model for surfaces
                      with no accessibility tree at all (Citrix/terminal emulators).

A new surface only has to answer: observe(), resolve(target), act(), read(),
check(condition), screenshot(). Artifacts do not change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ..schema import Condition, Target


@dataclass
class ElementInfo:
    gid: int
    frame: str
    role: str
    name: str
    name_from: str
    tag: str
    text: str
    bbox: dict[str, float]
    raw: dict[str, Any]

    def line(self) -> str:
        """Compact rendering used in the model prompt."""
        s = f'[{self.gid}] {self.role} "{self.name[:80]}"'
        v = self.raw.get("value")
        if self.role in ("textbox", "combobox") and v is not None:
            s += f' value="{v}"'
        if self.raw.get("options"):
            s += " options=" + "|".join(o for o in self.raw["options"] if o)
        t = self.raw.get("table") or {}
        if t.get("column") and self.role in ("cell", "link", "button", "text"):
            s += f' (column "{t["column"]}")'
        return s


@dataclass
class FrameObservation:
    name: str
    path: str
    title: str
    elements: list[ElementInfo]


@dataclass
class Observation:
    frames: list[FrameObservation]
    by_gid: dict[int, ElementInfo] = field(default_factory=dict)

    def render(self, limit: int = 260) -> str:
        out: list[str] = []
        n = 0
        for f in self.frames:
            out.append(f'== frame "{f.name}"  path={f.path}  title="{f.title}"')
            for e in f.elements:
                if n >= limit:
                    out.append("... (truncated)")
                    return "\n".join(out)
                out.append("  " + e.line())
                n += 1
        return "\n".join(out)

    def signature(self) -> str:
        """Cheap state fingerprint used for stuck detection."""
        import hashlib

        blob = "|".join(f.path + f.title + "".join(e.role + e.name + str(e.raw.get("value")) for e in f.elements) for f in self.frames)
        return hashlib.sha1(blob.encode()).hexdigest()[:12]

    def landmarks(self) -> dict[str, set[str]]:
        return {f.name: {e.name for e in f.elements if e.role == "heading" and e.name} | ({f.title} if f.title else set()) for f in self.frames}


class ResolveError(Exception):
    def __init__(self, code: str, message: str, tried: list[dict[str, Any]]):
        super().__init__(message)
        self.code = code  # TARGET_NOT_FOUND | TARGET_AMBIGUOUS
        self.tried = tried


@dataclass
class Resolved:
    frame: str
    strategy: str
    agreeing: int  # how many *other* strategies resolve to the same element (drift signal)
    disagreeing: int
    info: dict[str, Any]
    handle: Any  # surface-specific element handle


class Surface(Protocol):
    async def observe(self) -> Observation: ...
    async def resolve(self, target: Target, render: Any) -> Resolved: ...
    async def click(self, el: Resolved) -> None: ...
    async def fill(self, el: Resolved, value: str) -> None: ...
    async def select(self, el: Resolved, option: str) -> None: ...
    async def press(self, key: str, el: Resolved | None) -> None: ...
    async def read(self, el: Resolved) -> str: ...
    async def check(self, cond: Condition, render: Any) -> bool: ...
    async def visible_text(self) -> dict[str, str]: ...
    async def screenshot(self, path: str, mask_values: list[str], mask_currency: bool) -> None: ...
    async def settle(self, timeout_ms: int = 8000) -> None: ...
