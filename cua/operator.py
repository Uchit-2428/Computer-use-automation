"""Minimal operator console (mocked UI, real control transfer).

Runs inside the process that owns the live browser session, so the operator drives
*that* session — same cookies, same frames, same server-side state — not a new one.

    GET  /?token=..                       open interventions (auto-refresh)
    GET  /s/<session>?token=..            console: live screenshot, click-to-click, type, keys
    GET  /api/interventions               JSON: open + recent interventions with context
    GET  /api/s/<session>/screen.jpg      live screenshot (operators are authorised staff: unmasked)
    GET  /api/s/<session>/elements        element list with page coordinates (for highlighting / bots)
    POST /api/s/<session>/claim           {"operator"}             -> lease to human:<operator>
    POST /api/s/<session>/click           {"operator","x","y"}
    POST /api/s/<session>/type            {"operator","text"}
    POST /api/s/<session>/key             {"operator","key"}
    POST /api/s/<session>/release         {"operator","resolution":"resume"|"abort","note"}

A production console would stream the page over CDP screencast/WebRTC and route
requests through a broker with operator identity (SSO) and per-tenant entitlements;
the lease model and API contract stay the same.
"""

from __future__ import annotations

import html
import os
import secrets
from typing import Any

from aiohttp import web

from .control import NOTIFIERS, REGISTRY, ControlError, SessionController

TOKEN = os.environ.get("CUA_OPERATOR_TOKEN") or secrets.token_urlsafe(12)

PAGE = """<!doctype html><html><head><title>CUA operator console</title>
<style>body{font-family:system-ui,sans-serif;margin:16px;background:#f5f5f2;color:#222}
.bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:8px 0}
button{padding:6px 12px}img{border:2px solid #444;cursor:crosshair;max-width:100%}
.ctx{background:#fff;border:1px solid #ccc;padding:8px;white-space:pre-wrap;font-size:12px}
.owner{font-weight:bold}</style></head><body>
<h2>Intervention __IV__ — session __SID__</h2>
<div class="ctx" id="ctx">loading…</div>
<div class="bar">Operator <input id="op" value="operator1" size="10">
<button onclick="post('claim',{})">Take control</button>
<input id="txt" placeholder="text to type" size="24"><button onclick="post('type',{text:txt.value});txt.value=''">Type</button>
<button onclick="post('key',{key:'Enter'})">Enter</button><button onclick="post('key',{key:'Tab'})">Tab</button>
<input id="note" placeholder="note for the log" size="24">
<button onclick="post('release',{resolution:'resume',note:note.value})">Hand back &amp; resume</button>
<button onclick="post('release',{resolution:'abort',note:note.value})">Abort run</button>
<span>controller: <span class="owner" id="owner">?</span></span></div>
<img id="scr" onclick="clk(event)">
<script>
const T=new URLSearchParams(location.search).get('token'), S='__SID__';
async function post(a,b){b.operator=op.value;const r=await fetch(`/api/s/${S}/${a}?token=${T}`,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(b)});if(!r.ok)alert(await r.text());refresh()}
function clk(e){const r=scr.getBoundingClientRect(),sx=scr.naturalWidth/r.width,sy=scr.naturalHeight/r.height;post('click',{x:(e.clientX-r.left)*sx,y:(e.clientY-r.top)*sy})}
async function refresh(){scr.src=`/api/s/${S}/screen.jpg?token=${T}&t=${Date.now()}`;
const d=await (await fetch(`/api/interventions?token=${T}`)).json();const s=d.sessions.find(x=>x.session_id===S);
if(s){owner.textContent=s.owner;ctx.textContent=JSON.stringify(s.current||s.last,null,1)}}
refresh();setInterval(refresh,1500)</script></body></html>"""


def _auth(request: web.Request) -> None:
    tok = request.query.get("token") or request.headers.get("x-operator-token")
    if tok != TOKEN:
        raise web.HTTPUnauthorized(text="bad operator token")


def _session(request: web.Request) -> SessionController:
    _auth(request)
    s = REGISTRY.get(request.match_info["sid"])
    if not s:
        raise web.HTTPNotFound(text="no such live session")
    return s


routes = web.RouteTableDef()


@routes.get("/")
async def index(request: web.Request) -> web.Response:
    _auth(request)
    rows = []
    for sid, s in REGISTRY.items():
        iv = s.current
        if iv:
            rows.append(f'<li><a href="/s/{sid}?token={TOKEN}">{html.escape(iv.id)}</a> [{iv.status}] {html.escape(iv.reason)} '
                        f'— run {html.escape(iv.run_id)} — controller {html.escape(s.owner)}</li>')
    body = "<ul>" + "".join(rows) + "</ul>" if rows else "<p>No open interventions.</p>"
    return web.Response(text=f"<html><head><meta http-equiv=refresh content=2><title>CUA operator</title></head><body><h2>Open interventions</h2>{body}</body></html>",
                        content_type="text/html")


@routes.get("/s/{sid}")
async def console(request: web.Request) -> web.Response:
    s = _session(request)
    iv = s.current.id if s.current else "-"
    return web.Response(text=PAGE.replace("__SID__", s.session_id).replace("__IV__", iv), content_type="text/html")


@routes.get("/api/interventions")
async def interventions(request: web.Request) -> web.Response:
    _auth(request)
    out = []
    for sid, s in REGISTRY.items():
        out.append({"session_id": sid, "owner": s.owner, "current": s.current.public() if s.current else None,
                    "last": s.history[-1].public() if s.history else None})
    return web.json_response({"sessions": out}, dumps=lambda o: __import__("json").dumps(o, default=str))


@routes.get("/api/s/{sid}/screen.jpg")
async def screen(request: web.Request) -> web.Response:
    s = _session(request)
    img = await s.surface.page.screenshot(type="jpeg", quality=70)
    return web.Response(body=img, content_type="image/jpeg")


@routes.get("/api/s/{sid}/elements")
async def elements(request: web.Request) -> web.Response:
    s = _session(request)
    obs = await s.surface.observe()
    out: list[dict[str, Any]] = []
    for e in obs.by_gid.values():
        box = None
        try:
            h = await s.surface.handle_for_gid(e.gid)
            box = await h.bounding_box()
        except Exception:
            pass
        out.append({"gid": e.gid, "frame": e.frame, "role": e.role, "name": e.name, "box": box})
    return web.json_response({"elements": out})


async def _body(request: web.Request) -> dict[str, Any]:
    return await request.json() if request.can_read_body else {}


def _wrap(fn):
    async def h(request: web.Request) -> web.Response:
        s = _session(request)
        b = await _body(request)
        try:
            r = await fn(s, b)
        except ControlError as e:
            raise web.HTTPConflict(text=str(e))
        return web.json_response({"ok": True, "owner": s.owner, **(r or {})})
    return h


async def _claim(s: SessionController, b: dict[str, Any]) -> dict[str, Any]:
    iv = s.claim(b["operator"])
    return {"intervention": iv.id}


async def _click(s: SessionController, b: dict[str, Any]) -> None:
    await s.human_click(b["operator"], float(b["x"]), float(b["y"]))


async def _type(s: SessionController, b: dict[str, Any]) -> None:
    await s.human_type(b["operator"], str(b["text"]))


async def _key(s: SessionController, b: dict[str, Any]) -> None:
    await s.human_key(b["operator"], str(b["key"]))


async def _release(s: SessionController, b: dict[str, Any]) -> dict[str, Any]:
    iv = s.release(b["operator"], b.get("resolution", "resume"), b.get("note"))
    return {"intervention": iv.id, "status": iv.status}


for _name, _fn in {"claim": _claim, "click": _click, "type": _type, "key": _key, "release": _release}.items():
    routes.post(f"/api/s/{{sid}}/{_name}")(_wrap(_fn))


class OperatorConsole:
    def __init__(self, host: str = "127.0.0.1", port: int = 8700):
        self.host, self.port = host, port
        self.runner: web.AppRunner | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/?token={TOKEN}"

    async def start(self) -> "OperatorConsole":
        app = web.Application()
        app.add_routes(routes)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, self.host, self.port).start()
        NOTIFIERS.append(lambda iv: print(
            f"\n>>> INTERVENTION {iv.id} ({iv.kind}): {iv.reason}\n>>> operator console: http://{self.host}:{self.port}/s/{iv.session_id}?token={TOKEN}\n", flush=True))
        return self

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()
