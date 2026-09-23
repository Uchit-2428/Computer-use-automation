"""Test doubles. `RuleDecider` stands in for the model in offline tests of the discovery
pipeline (it reads the element list and follows a fixed plan). It is NOT used for the
evidence discovery run, which is a genuine model run (see evidence/README.md)."""
from __future__ import annotations

import re
from typing import Any

from cua.llm import ToolCall


def find(text: str, role: str, name: str) -> int | None:
    for line in text.splitlines():
        m = re.match(r'\s*\[(\d+)\] (\S+) "([^"]*)"', line)
        if m and m.group(2) == role and m.group(3).strip().lower() == name.lower():
            return int(m.group(1))
    return None


def find_cell_in_row(text: str, row_key: str, column: str) -> int | None:
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if f'"{row_key}"' in line:
            for nxt in lines[i:i + 8]:
                m = re.match(r'\s*\[(\d+)\] cell "([^"]*)" \(column "%s"\)' % re.escape(column), nxt)
                if m:
                    return int(m.group(1))
    return None


class RuleDecider:
    model = "rule-based-test-double"

    def __init__(self, plan: list[tuple[str, dict[str, Any]]]):
        self.plan = plan
        self.i = 0

    async def decide(self, system: str, text: str, image: bytes | None, tools: list[dict[str, Any]]) -> ToolCall:
        screen = text.split("CURRENT SCREEN:", 1)[1]
        tool, spec = self.plan[self.i]
        self.i += 1
        args = dict(spec)
        if "find" in args:
            role, name = args.pop("find")
            args["element"] = find(screen, role, name)
        if "cell" in args:
            rk, col = args.pop("cell")
            args["element"] = find_cell_in_row(screen, rk, col)
        args.setdefault("reasoning", "test plan")
        args.setdefault("intent", tool)
        return ToolCall(tool, args, "", {}, self.model)


BALANCE_PLAN = [
    ("click", {"find": ("link", "Member Inquiry"), "intent": "Open Member Inquiry"}),
    ("type_text", {"find": ("textbox", "Member #"), "text": "{{member_id}}", "intent": "Enter the member number"}),
    ("click", {"find": ("button", "Inquire"), "intent": "Search for the member"}),
    ("click", {"find": ("link", "View"), "intent": "Open the member detail"}),
    ("extract", {"cell": ("PRIMARY SAVINGS", "Balance"), "output_name": "savings_balance", "value_type": "currency",
                 "description": "Current balance of the primary savings share", "sensitivity": "financial"}),
    ("finish", {"success_evidence": "Member Detail", "capability_id": "member.get_savings_balance",
                "title": "Get member savings balance", "description": "Looks up a member and returns the primary savings balance."}),
]
