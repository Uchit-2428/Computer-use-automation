"""Scripted stand-in for a human operator, used to produce reproducible evidence.

It uses exactly the same HTTP API as the console UI (claim -> look at the screen ->
click/type -> release), so everything it does goes through the real control-transfer
path and is captured like a person's actions would be. For a manual demo, open the
console URL printed by the run instead of starting the bot.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx


async def run_bot(console: str, token: str, operator: str = "supervisor.kim", code: str | None = None,
                  poll_s: float = 0.5, max_wait_s: float = 120, once: bool = True) -> list[dict[str, Any]]:
    code = code or os.environ.get("MOCK_SUPERVISOR_CODE", "4321")
    handled: list[dict[str, Any]] = []
    h = {"x-operator-token": token}
    async with httpx.AsyncClient(base_url=console, headers=h, timeout=30) as c:
        waited = 0.0
        while waited < max_wait_s:
            data = (await c.get("/api/interventions")).json()
            open_ = [s for s in data["sessions"] if s["current"] and s["current"]["status"] == "open"]
            if not open_:
                await asyncio.sleep(poll_s)
                waited += poll_s
                continue
            s = open_[0]
            sid, iv = s["session_id"], s["current"]
            print(f"[operator-bot] intervention {iv['id']}: {iv['reason']}")
            await asyncio.sleep(1.0)  # a human reads the context first
            (await c.post(f"/api/s/{sid}/claim", json={"operator": operator})).raise_for_status()
            els = (await c.get(f"/api/s/{sid}/elements")).json()["elements"]

            def center(role: str, name: str) -> tuple[float, float] | None:
                for e in els:
                    if e["role"] == role and e["name"].lower() == name.lower() and e["box"]:
                        b = e["box"]
                        return b["x"] + b["width"] / 2, b["y"] + b["height"] / 2
                return None

            field, btn = center("textbox", "Supervisor Code"), center("button", "Approve Override")
            if field and btn:
                await c.post(f"/api/s/{sid}/click", json={"operator": operator, "x": field[0], "y": field[1]})
                await c.post(f"/api/s/{sid}/type", json={"operator": operator, "text": code})
                await c.post(f"/api/s/{sid}/click", json={"operator": operator, "x": btn[0], "y": btn[1]})
                await asyncio.sleep(0.8)
                r = await c.post(f"/api/s/{sid}/release", json={"operator": operator, "resolution": "resume",
                                                               "note": "Verified member identity; supervisor override entered."})
            else:
                r = await c.post(f"/api/s/{sid}/release", json={"operator": operator, "resolution": "abort",
                                                               "note": "Not a situation this bot handles."})
            print(f"[operator-bot] released: {r.json()}")
            handled.append(r.json())
            if once:
                break
    return handled
