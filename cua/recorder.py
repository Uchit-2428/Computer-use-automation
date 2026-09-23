"""Turns what happened in a discovery run into a reviewable ``Capability``.

The model chose *elements*; the recorder decides *how to find them again*:

* derive several independent locator strategies from the element's context,
* **validate** every strategy against the live page at record time (must resolve to
  exactly that element) and drop the rest,
* replace concrete parameter values with templates (by construction the model typed
  ``{{member_id}}``; row keys / link names / URLs that contain the value get templated too),
* derive a postcondition per step from what changed on screen,
* canonicalise URLs into parameterised route patterns.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .schema import (
    Attribute, Condition, FrameScope, LabelValue, RoleName, Target, TableCell, TextMatch, TextVisible, TitleIs,
    UrlMatches, XPath,
)
from .surface.base import ElementInfo, Observation
from .surface.web import WebSurface
from .templating import Templater

_VOLATILE = re.compile(r"\$|\d{3,}|\d+/\d+/\d+")  # amounts, long numbers, dates: never use as locator text


def _looks_volatile(s: str) -> bool:
    return bool(_VOLATILE.search(s))


def candidate_strategies(d: dict[str, Any], purpose: str, tmpl: Templater) -> list[dict[str, Any]]:
    """Ordered candidates (concrete values). Templating happens after validation."""
    role, name, tag = d["role"], d.get("name") or "", d["tag"]
    table = d.get("table") or {}
    out: list[dict[str, Any]] = []

    table_cands: list[dict[str, Any]] = []
    if table.get("column") is not None and not table.get("is_header_row"):
        keys = [c["text"] for c in table.get("row_cells", []) if c["i"] != table.get("col_index") and c["text"]]
        # prefer a key that is an input parameter, then stable (non-volatile) text
        keys.sort(key=lambda k: (not tmpl.is_exact_param(k), _looks_volatile(k) and not tmpl.is_exact_param(k), not re.search("[A-Za-z]{3}", k)))
        for k in keys[:2]:
            if _looks_volatile(k) and not tmpl.is_exact_param(k):
                continue
            table_cands.append({"kind": "table_cell", "column": table["column"], "row_key": k,
                                "interactive": purpose == "act" and tag not in ("td", "th")})

    role_cand = []
    if name and role not in ("cell", "text") and not (purpose == "extract"):
        if not _looks_volatile(name) or tmpl.is_exact_param(name):
            role_cand.append({"kind": "role_name", "role": role, "name": name})
    attr_cand = []
    field_name = (d.get("attrs") or {}).get("name")
    if field_name and tag in ("input", "select", "textarea"):
        attr_cand.append({"kind": "attribute", "tag": tag, "attrs": {"name": field_name}})
    label_cand = []
    if purpose == "extract" and table.get("prev_label") and not _looks_volatile(table["prev_label"]):
        label_cand.append({"kind": "label_value", "label": table["prev_label"]})
    text_cand = []
    if purpose == "act" and role in ("cell", "text", "heading") and name and not _looks_volatile(name):
        text_cand.append({"kind": "text", "text": name})

    param_row = [c for c in table_cands if tmpl.is_exact_param(c["row_key"])]
    if purpose == "extract":
        out = table_cands + label_cand
    elif param_row:
        # other cells of a data row are data (names, balances), not stable keys: drop them
        out = param_row + role_cand + attr_cand + text_cand
    else:
        out = role_cand + attr_cand + table_cands + text_cand
    out.append({"kind": "xpath", "xpath": d["xpath"]})
    return out


def _templated(s: dict[str, Any], tmpl: Templater) -> dict[str, Any]:
    out = dict(s)
    for k in ("name", "row_key", "text", "label"):
        if k in out and isinstance(out[k], str):
            out[k] = tmpl.text(out[k])
    return out


async def derive_target(surface: WebSurface, el: ElementInfo, purpose: str, tmpl: Templater, description: str) -> tuple[Target, list[str]]:
    notes: list[str] = []
    validated: list[dict[str, Any]] = []
    for cand in candidate_strategies(el.raw, purpose, tmpl):
        v = await surface.validate_strategy(el.gid, cand)
        if v["same"]:
            validated.append(cand)
        else:
            notes.append(f"dropped {cand['kind']} (matches={v['count']}, same={v['same']})")
    if not any(s["kind"] != "xpath" for s in validated):
        notes.append("WARNING: only a structural xpath identifies this element; needs reviewer attention")
    if not validated:
        raise RuntimeError(f"no strategy uniquely identifies element [{el.gid}] {el.role} '{el.name}'")
    strategies = [_templated(s, tmpl) for s in validated]
    model_strats: list[Any] = []
    for s in strategies:
        k = s["kind"]
        cls = {"role_name": RoleName, "attribute": Attribute, "table_cell": TableCell, "label_value": LabelValue,
               "text": TextMatch, "xpath": XPath}[k]
        if k == "xpath":
            s = {**s, "note": "diagnostic/last resort; replay skips it while semantic strategies exist"}
        model_strats.append(cls.model_validate(s))
    return Target(
        description=description,
        frame=FrameScope(name=el.frame),
        strategies=model_strats,
        validated=[s["kind"] for s in validated],
        visual_hint={k: float(v) for k, v in el.bbox.items()},
    ), notes


def derive_expect(before: Observation, after: Observation, tmpl: Templater) -> list[Condition]:
    """Postconditions from what changed: navigated frames (route pattern), new titles, new headings."""
    conds: list[Condition] = []
    bmap = {f.name: f for f in before.frames}
    for f in after.frames:
        b = bmap.get(f.name)
        if f.name == "_top" and b is not None and b.path == f.path:
            continue
        path_changed = b is None or b.path.split("?")[0] != f.path.split("?")[0]
        title_changed = b is None or b.title != f.title
        if path_changed:
            conds.append(UrlMatches(pattern=tmpl.path_regex(f.path), frame=FrameScope(name=f.name)))
        if title_changed and f.title:
            conds.append(TitleIs(title=tmpl.text(f.title), frame=FrameScope(name=f.name)))
        if path_changed or title_changed:
            before_heads = {e.name for e in b.elements if e.role == "heading"} if b else set()
            for h in [e.name for e in f.elements if e.role == "heading" and e.name not in before_heads][:2]:
                if not _looks_volatile(h):
                    conds.append(TextVisible(text=tmpl.text(h), frame=FrameScope(name=f.name)))
    return conds


@dataclass
class RecordedStep:
    id: str
    intent: str
    action: dict[str, Any]
    risk: str
    expect: list[Condition]
    source: str = "agent"
    notes: list[str] = field(default_factory=list)


def step_id(verb: str, text: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", f"{verb}_{text}".lower()).strip("_")[:40] or verb
    sid, n = base, 2
    while sid in taken:
        sid, n = f"{base}_{n}", n + 1
    taken.add(sid)
    return sid
