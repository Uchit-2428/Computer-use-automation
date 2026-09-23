"""CoreLink Teller — a deliberately *legacy* mock core-banking back-office app.

This is the proxy target for the take-home. It is intentionally hostile to
automation, to mimic the long tail of bank back-office software:

* server-rendered HTML, framesets (banner / nav / work frames)
* table-based layout, <font> tags, no <label for>, no ids, no test ids
* buttons that are <input type=button onclick=...> and javascript: links
* errors rendered as red text inside the work frame (no status codes)
* the session-expired sign-on page is rendered *inside the work frame*

Two "tenants" run the same vendor product, configured differently:
  /t/acme/     Acme Federal Credit Union    CoreLink Teller 4.2.1
  /t/bayside/  Bayside Community CU         CoreLink Teller 4.3.0 (relabelled fields)

Runtime faults can be injected per tenant through POST /__chaos (a test
harness endpoint that lives *outside* the /t/ prefix, so the automation
allowlist never permits the agent to touch it).

All data is synthetic. Credentials are demo-only.
"""

from __future__ import annotations

import asyncio
import html
import os
import re
import secrets
from dataclasses import dataclass, field

from aiohttp import web

OPERATOR_ID = os.environ.get("MOCK_OPERATOR_ID", "teller01")
OPERATOR_PASSWORD = os.environ.get("MOCK_OPERATOR_PASSWORD", "demo-pass-123")
SUPERVISOR_CODE = os.environ.get("MOCK_SUPERVISOR_CODE", "4321")

# ---------------------------------------------------------------- tenants
TENANTS: dict[str, dict] = {
    "acme": {
        "display": "ACME FEDERAL CREDIT UNION",
        "version": "4.2.1",
        "color": "#003366",
        "labels": {
            "member_no": "Member #",
            "inquire": "Inquire",
            "balance": "Balance",
            "view": "View",
            "member_inquiry": "Member Inquiry",
        },
    },
    "bayside": {
        "display": "BAYSIDE COMMUNITY CU",
        "version": "4.3.0",
        "color": "#5a2d0c",
        "labels": {
            "member_no": "Acct Holder No.",
            "inquire": "Search",
            "balance": "Current Bal",
            "view": "Open",
            "member_inquiry": "Member Inquiry",
        },
    },
}

# ---------------------------------------------------------------- synthetic data
MEMBERS: dict[str, dict] = {
    "100234": {
        "name": "DOE, JANE Q",
        "since": "03/14/2011",
        "branch": "MAIN",
        "status": "ACTIVE",
        "shares": [
            ("00", "PRIMARY SAVINGS", "03/14/2011", "12,480.55", "12,455.55"),
            ("01", "HOLIDAY CLUB", "11/02/2019", "640.00", "640.00"),
            ("10", "SHARE DRAFT CHECKING", "03/14/2011", "2,107.19", "2,107.19"),
        ],
    },
    "100871": {
        "name": "SMITH, ALEX",
        "since": "07/22/2018",
        "branch": "NORTH",
        "status": "ACTIVE",
        "shares": [
            ("00", "PRIMARY SAVINGS", "07/22/2018", "3,215.00", "3,190.00"),
            ("10", "SHARE DRAFT CHECKING", "07/22/2018", "88.42", "88.42"),
        ],
    },
    "77777": {
        "name": "RIVERA, SAM",
        "since": "01/05/2015",
        "branch": "MAIN",
        "status": "FLAGGED",
        "override": True,
        "shares": [
            ("00", "PRIMARY SAVINGS", "01/05/2015", "910.10", "910.10"),
        ],
    },
    "55555": {"restricted": True},
}

SHARE_TYPES = ["REGULAR SAVINGS", "HOLIDAY CLUB", "VACATION CLUB", "CERTIFICATE 12M"]


@dataclass
class Chaos:
    notice: bool = False  # interstitial SYSTEM NOTICE on next GET of a work page
    slow_ms: int = 0  # delay every work response
    expire_after: int | None = None  # session expires after N more authenticated requests
    error_path: str | None = None  # substring of path -> 500 SYSTEM ERROR
    transient_path: str | None = None  # substring of path -> one 503, then OK
    transient_count: int = 0


@dataclass
class Session:
    tenant: str
    user: str
    remaining: int | None = None
    overrides: set[str] = field(default_factory=set)
    pending_share: dict | None = None


SESSIONS: dict[str, Session] = {}
CHAOS: dict[str, Chaos] = {t: Chaos() for t in TENANTS}
OPENED_SHARES: list[dict] = []


# ---------------------------------------------------------------- html helpers
def esc(s: str) -> str:
    return html.escape(str(s))


def page(title: str, body: str, tenant: str, extra_head: str = "") -> web.Response:
    t = TENANTS[tenant]
    return web.Response(
        text=f"""<html><head><title>CoreLink - {esc(title)}</title>{extra_head}</head>
<body bgcolor="#e8e8e0" style="font-family: Verdana, Arial; font-size: 12px; margin:4px">
<table width="100%" border="0" cellpadding="2" cellspacing="0">
<tr><td bgcolor="{t['color']}"><font color="white" size="4"><b>{esc(title)}</b></font></td></tr>
<tr><td>{body}</td></tr>
</table></body></html>""",
        content_type="text/html",
    )


def msg_row(msg: str | None) -> str:
    if not msg:
        return ""
    return f'<tr><td colspan="3"><font color="red"><b>{esc(msg)}</b></font></td></tr>'


def T(request: web.Request) -> str:
    tenant = request.match_info["tenant"]
    if tenant not in TENANTS:
        raise web.HTTPNotFound(text="unknown institution")
    return tenant


def L(tenant: str, key: str) -> str:
    return TENANTS[tenant]["labels"][key]


# ---------------------------------------------------------------- session / chaos middleware
def get_session(request: web.Request, tenant: str) -> Session | None:
    tok = request.cookies.get(f"CLSESSION_{tenant}")
    s = SESSIONS.get(tok or "")
    if s and s.tenant == tenant:
        return s
    return None


def signon_page(tenant: str, message: str | None = None, target: str = "_top") -> web.Response:
    t = TENANTS[tenant]
    body = f"""
<form method="POST" action="/t/{tenant}/signon" target="{target}">
<table border="0" cellpadding="3">
<tr><td colspan="2"><font size="3"><b>{esc(t['display'])}</b></font><br>CoreLink Teller v{t['version']}</td></tr>
{msg_row(message)}
<tr><td align="right">Operator ID</td><td><input type="text" name="OPRID" size="12"></td></tr>
<tr><td align="right">Password</td><td><input type="password" name="PWD" size="12"></td></tr>
<tr><td></td><td><input type="submit" value="Sign On"></td></tr>
</table></form>"""
    return page("Operator Sign On", body, tenant)


async def work_guard(request: web.Request, tenant: str) -> tuple[Session | None, web.Response | None]:
    """Common behaviour for every authenticated page: slowness, expiry, injected errors."""
    chaos = CHAOS[tenant]
    if chaos.slow_ms:
        await asyncio.sleep(chaos.slow_ms / 1000)
    s = get_session(request, tenant)
    if s is None:
        return None, signon_page(tenant, "YOUR SESSION HAS EXPIRED. PLEASE SIGN ON AGAIN.")
    if chaos.expire_after is not None:
        if chaos.expire_after <= 0:
            SESSIONS.pop(request.cookies.get(f"CLSESSION_{tenant}", ""), None)
            chaos.expire_after = None
            return None, signon_page(tenant, "YOUR SESSION HAS EXPIRED. PLEASE SIGN ON AGAIN.")
        chaos.expire_after -= 1
    if chaos.error_path and chaos.error_path in request.path:
        return s, web.Response(
            status=500,
            text=f"""<html><head><title>CoreLink - Error</title></head><body bgcolor="white">
<h2>SYSTEM ERROR SYS-0042</h2><p>An unexpected condition occurred in module CLINQ02.
Contact your system administrator. Reference {secrets.token_hex(4).upper()}</p></body></html>""",
            content_type="text/html",
        )
    if chaos.transient_path and chaos.transient_path in request.path and chaos.transient_count > 0:
        chaos.transient_count -= 1
        return s, web.Response(
            status=503,
            text="""<html><head><title>CoreLink - Busy</title></head><body bgcolor="white">
<h3>HOST BUSY</h3><p>The host is temporarily unavailable. Please retry your request.</p></body></html>""",
            content_type="text/html",
        )
    if chaos.notice and request.method == "GET" and "/work/ack" not in request.path:
        chaos.notice = False
        ret = esc(request.path_qs)
        body = f"""
<table border="1" cellpadding="8" bgcolor="#ffffcc"><tr><td>
<font size="3"><b>SYSTEM NOTICE</b></font><br><br>
End-of-day processing begins at 7:00 PM ET. Transactions posted after that time
will be effective the next business day.<br><br>
<form method="POST" action="/t/{tenant}/work/ack"><input type="hidden" name="ret" value="{ret}">
<input type="submit" value="Acknowledge"></form></td></tr></table>"""
        return s, page("System Notice", body, tenant)
    return s, None


# ---------------------------------------------------------------- routes
routes = web.RouteTableDef()


@routes.get("/")
async def root(request: web.Request) -> web.Response:
    links = "".join(f'<li><a href="/t/{t}/">{esc(v["display"])}</a></li>' for t, v in TENANTS.items())
    return web.Response(text=f"<html><body><h3>CoreLink mock tenants</h3><ul>{links}</ul></body></html>", content_type="text/html")


@routes.get("/t/{tenant}/")
async def tenant_root(request: web.Request) -> web.Response:
    tenant = T(request)
    raise web.HTTPFound(f"/t/{tenant}/signon")


@routes.get("/t/{tenant}/signon")
async def signon_get(request: web.Request) -> web.Response:
    return signon_page(T(request))


@routes.post("/t/{tenant}/signon")
async def signon_post(request: web.Request) -> web.Response:
    tenant = T(request)
    form = await request.post()
    if form.get("OPRID") != OPERATOR_ID or form.get("PWD") != OPERATOR_PASSWORD:
        return signon_page(tenant, "INVALID OPERATOR ID OR PASSWORD")
    tok = secrets.token_urlsafe(16)
    SESSIONS[tok] = Session(tenant=tenant, user=str(form.get("OPRID")))
    resp = web.HTTPFound(f"/t/{tenant}/main")
    resp.set_cookie(f"CLSESSION_{tenant}", tok, path=f"/t/{tenant}", httponly=True)
    raise resp


@routes.get("/t/{tenant}/main")
async def main_frameset(request: web.Request) -> web.Response:
    tenant = T(request)
    if get_session(request, tenant) is None:
        return signon_page(tenant)
    return web.Response(
        text=f"""<html><head><title>CoreLink Teller</title></head>
<frameset rows="56,*" border="1">
  <frame name="banner" src="/t/{tenant}/banner" scrolling="no">
  <frameset cols="190,*">
    <frame name="nav" src="/t/{tenant}/nav">
    <frame name="work" src="/t/{tenant}/work/home">
  </frameset>
</frameset></html>""",
        content_type="text/html",
    )


@routes.get("/t/{tenant}/banner")
async def banner(request: web.Request) -> web.Response:
    tenant = T(request)
    t = TENANTS[tenant]
    s = get_session(request, tenant)
    who = s.user.upper() if s else "-"
    return web.Response(
        text=f"""<html><body bgcolor="{t['color']}" style="margin:4px;font-family:Verdana">
<table width="100%"><tr><td><font color="white" size="4"><b>{esc(t['display'])}</b></font></td>
<td align="right"><font color="#cccccc">CoreLink Teller v{t['version']} &nbsp; OPR: {esc(who)}</font></td></tr></table>
</body></html>""",
        content_type="text/html",
    )


@routes.get("/t/{tenant}/nav")
async def nav(request: web.Request) -> web.Response:
    tenant = T(request)
    return web.Response(
        text=f"""<html><body bgcolor="#d0d0c8" style="font-family:Verdana;font-size:12px">
<table cellpadding="4" width="100%">
<tr><td bgcolor="#999988"><font color="white"><b>FUNCTIONS</b></font></td></tr>
<tr><td><a href="javascript:void(0)" onclick="parent.work.location='/t/{tenant}/work/inquiry'">{esc(L(tenant, 'member_inquiry'))}</a></td></tr>
<tr><td><a href="javascript:void(0)" onclick="parent.work.location='/t/{tenant}/work/newshare'">New Share Account</a></td></tr>
<tr><td><a href="javascript:void(0)" onclick="parent.work.location='/t/{tenant}/work/home'">Teller Home</a></td></tr>
<tr><td><a href="/t/{tenant}/signoff" target="_top">Sign Off</a></td></tr>
</table></body></html>""",
        content_type="text/html",
    )


@routes.get("/t/{tenant}/signoff")
async def signoff(request: web.Request) -> web.Response:
    tenant = T(request)
    SESSIONS.pop(request.cookies.get(f"CLSESSION_{tenant}", ""), None)
    return signon_page(tenant, "YOU HAVE BEEN SIGNED OFF")


@routes.get("/t/{tenant}/work/home")
async def work_home(request: web.Request) -> web.Response:
    tenant = T(request)
    _, early = await work_guard(request, tenant)
    if early:
        return early
    return page("Teller Home", "<p>Select a function from the menu on the left.</p>", tenant)


@routes.post("/t/{tenant}/work/ack")
async def work_ack(request: web.Request) -> web.Response:
    tenant = T(request)
    form = await request.post()
    ret = str(form.get("ret") or f"/t/{tenant}/work/home")
    if not ret.startswith(f"/t/{tenant}/"):
        ret = f"/t/{tenant}/work/home"
    raise web.HTTPFound(ret)


def inquiry_form(tenant: str, message: str | None = None, value: str = "") -> web.Response:
    body = f"""
<form method="POST" action="/t/{tenant}/work/inquiry">
<table border="0" cellpadding="3">
{msg_row(message)}
<tr><td>{esc(L(tenant, 'member_no'))}</td><td><input type="text" name="P_MBR" size="10" value="{esc(value)}"></td>
<td><input type="button" value="{esc(L(tenant, 'inquire'))}" onclick="document.forms[0].submit()"></td></tr>
</table></form>"""
    return page("Member Inquiry", body, tenant)


@routes.get("/t/{tenant}/work/inquiry")
async def inquiry_get(request: web.Request) -> web.Response:
    tenant = T(request)
    _, early = await work_guard(request, tenant)
    if early:
        return early
    return inquiry_form(tenant)


def results_page(tenant: str, mbr: str) -> web.Response:
    m = MEMBERS[mbr]
    body = f"""
<table border="1" cellspacing="0" cellpadding="3" bgcolor="white">
<tr bgcolor="#cccccc"><th>{esc(L(tenant, 'member_no'))}</th><th>Name</th><th>Branch</th><th>Status</th><th>&nbsp;</th></tr>
<tr><td>{esc(mbr)}</td><td>{esc(m['name'])}</td><td>{esc(m['branch'])}</td><td>{esc(m['status'])}</td>
<td><a href="javascript:void(0)" onclick="location='/t/{tenant}/work/member/{esc(mbr)}'">{esc(L(tenant, 'view'))}</a></td></tr>
</table><br>1 RECORD(S) FOUND"""
    return page("Member Search Results", body, tenant)


@routes.post("/t/{tenant}/work/inquiry")
async def inquiry_post(request: web.Request) -> web.Response:
    tenant = T(request)
    s, early = await work_guard(request, tenant)
    if early:
        return early
    form = await request.post()
    mbr = str(form.get("P_MBR", "")).strip()
    if not re.fullmatch(r"\d{1,8}", mbr):
        return inquiry_form(tenant, "INVALID MEMBER NUMBER - ENTER 1 TO 8 DIGITS", mbr)
    m = MEMBERS.get(mbr)
    if m is None:
        return inquiry_form(tenant, f"NO MEMBER FOUND FOR NUMBER {mbr}", mbr)
    if m.get("restricted"):
        return inquiry_form(tenant, "ACCESS DENIED - RESTRICTED ACCOUNT (SEC-11)", mbr)
    if m.get("override") and s is not None and mbr not in s.overrides:
        body = f"""
<form method="POST" action="/t/{tenant}/work/override">
<input type="hidden" name="P_MBR" value="{esc(mbr)}">
<table border="0" cellpadding="3">
<tr><td colspan="2"><font color="#990000"><b>SUPERVISOR OVERRIDE REQUIRED</b></font><br>
This record is flagged. A supervisor must enter their override code to continue.</td></tr>
<tr><td>Supervisor Code</td><td><input type="password" name="SUPCD" size="8"></td></tr>
<tr><td></td><td><input type="submit" value="Approve Override"></td></tr>
</table></form>"""
        return page("Supervisor Override", body, tenant)
    return results_page(tenant, mbr)


@routes.post("/t/{tenant}/work/override")
async def override_post(request: web.Request) -> web.Response:
    tenant = T(request)
    s, early = await work_guard(request, tenant)
    if early:
        return early
    form = await request.post()
    mbr = str(form.get("P_MBR", ""))
    if form.get("SUPCD") != SUPERVISOR_CODE or mbr not in MEMBERS:
        return inquiry_form(tenant, "OVERRIDE REJECTED", mbr)
    assert s is not None
    s.overrides.add(mbr)
    return results_page(tenant, mbr)


@routes.get("/t/{tenant}/work/member/{mbr}")
async def member_detail(request: web.Request) -> web.Response:
    tenant = T(request)
    s, early = await work_guard(request, tenant)
    if early:
        return early
    mbr = request.match_info["mbr"]
    m = MEMBERS.get(mbr)
    if m is None or m.get("restricted") or (m.get("override") and s and mbr not in s.overrides):
        return inquiry_form(tenant, f"NO MEMBER FOUND FOR NUMBER {mbr}", mbr)
    rows = "".join(
        f"<tr><td>{a}</td><td>{esc(b)}</td><td>{c}</td><td align=right>${d}</td><td align=right>${e}</td></tr>"
        for a, b, c, d, e in m["shares"]
    )
    body = f"""
<table border="0" cellpadding="2">
<tr><td><b>{esc(L(tenant, 'member_no'))}</b></td><td>{esc(mbr)}</td><td width="30"></td><td><b>Member Since</b></td><td>{m['since']}</td></tr>
<tr><td><b>Name</b></td><td>{esc(m['name'])}</td><td></td><td><b>Status</b></td><td>{m['status']}</td></tr>
</table><br>
<font size="3"><b>Share Accounts</b></font>
<table border="1" cellspacing="0" cellpadding="3" bgcolor="white">
<tr bgcolor="#cccccc"><th>Share ID</th><th>Description</th><th>Opened</th><th>{esc(L(tenant, 'balance'))}</th><th>Available</th></tr>
{rows}
</table>"""
    return page("Member Detail", body, tenant)


def newshare_form(tenant: str, message: str | None = None, vals: dict | None = None) -> web.Response:
    vals = vals or {}
    opts = "".join(
        f'<option{" selected" if vals.get("SHTYPE") == t else ""}>{t}</option>' for t in [""] + SHARE_TYPES
    )
    body = f"""
<form method="POST" action="/t/{tenant}/work/newshare">
<table border="0" cellpadding="3">
{msg_row(message)}
<tr><td>{esc(L(tenant, 'member_no'))}</td><td><input type="text" name="P_MBR" size="10" value="{esc(vals.get('P_MBR', ''))}"></td></tr>
<tr><td>Share Type</td><td><select name="SHTYPE">{opts}</select></td></tr>
<tr><td>Nickname</td><td><input type="text" name="NICK" size="20" value="{esc(vals.get('NICK', ''))}"></td></tr>
<tr><td>Initial Deposit</td><td><input type="text" name="DEP" size="10" value="{esc(vals.get('DEP', ''))}"></td></tr>
<tr><td></td><td><input type="button" value="Continue" onclick="document.forms[0].submit()"></td></tr>
</table></form>"""
    return page("New Share Account", body, tenant)


@routes.get("/t/{tenant}/work/newshare")
async def newshare_get(request: web.Request) -> web.Response:
    tenant = T(request)
    _, early = await work_guard(request, tenant)
    if early:
        return early
    return newshare_form(tenant)


@routes.post("/t/{tenant}/work/newshare")
async def newshare_post(request: web.Request) -> web.Response:
    tenant = T(request)
    s, early = await work_guard(request, tenant)
    if early:
        return early
    assert s is not None
    form = {k: str(v).strip() for k, v in (await request.post()).items()}
    mbr = form.get("P_MBR", "")
    m = MEMBERS.get(mbr)
    if m is None or m.get("restricted"):
        return newshare_form(tenant, f"NO MEMBER FOUND FOR NUMBER {mbr}", form)
    if form.get("SHTYPE") not in SHARE_TYPES:
        return newshare_form(tenant, "SHARE TYPE IS REQUIRED", form)
    if not re.fullmatch(r"\d{1,7}(\.\d{2})?", form.get("DEP", "")):
        return newshare_form(tenant, "INITIAL DEPOSIT MUST BE A DOLLAR AMOUNT (E.G. 25.00)", form)
    s.pending_share = {"member": mbr, **form}
    body = f"""
<table border="1" cellspacing="0" cellpadding="4" bgcolor="white">
<tr><td><b>Member</b></td><td>{esc(mbr)} {esc(m['name'])}</td></tr>
<tr><td><b>Share Type</b></td><td>{esc(form['SHTYPE'])}</td></tr>
<tr><td><b>Nickname</b></td><td>{esc(form.get('NICK', ''))}</td></tr>
<tr><td><b>Initial Deposit</b></td><td>${esc(form['DEP'])}</td></tr>
</table><br>
Please review. Selecting CONFIRM will open the share account and post the initial deposit.<br><br>
<form method="POST" action="/t/{tenant}/work/newshare/confirm" style="display:inline">
<input type="submit" value="Confirm"></form>
<form method="GET" action="/t/{tenant}/work/newshare" style="display:inline">
<input type="submit" value="Cancel"></form>"""
    return page("Review New Share Account", body, tenant)


@routes.post("/t/{tenant}/work/newshare/confirm")
async def newshare_confirm(request: web.Request) -> web.Response:
    tenant = T(request)
    s, early = await work_guard(request, tenant)
    if early:
        return early
    assert s is not None
    if not s.pending_share:
        return newshare_form(tenant, "NOTHING TO CONFIRM")
    OPENED_SHARES.append(s.pending_share)
    s.pending_share = None
    return page("Share Account Opened", f"<p>SHARE ACCOUNT OPENED. SHARE ID {20 + len(OPENED_SHARES)}</p>", tenant)


# ---------------------------------------------------------------- test harness (outside /t/)
@routes.post("/__chaos/{tenant}")
async def set_chaos(request: web.Request) -> web.Response:
    tenant = request.match_info["tenant"]
    data = await request.json() if request.can_read_body else {}
    c = Chaos()
    for k, v in data.items():
        if hasattr(c, k):
            setattr(c, k, v)
    if c.transient_path and not c.transient_count:
        c.transient_count = 1
    CHAOS[tenant] = c
    return web.json_response({"tenant": tenant, "chaos": c.__dict__})


@routes.get("/__opened")
async def opened(request: web.Request) -> web.Response:
    return web.json_response({"count": len(OPENED_SHARES)})


def make_app() -> web.Application:
    app = web.Application()
    app.add_routes(routes)
    return app


def main() -> None:
    import argparse

    p = argparse.ArgumentParser(description="CoreLink Teller mock (legacy core banking)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8600)
    a = p.parse_args()
    web.run_app(make_app(), host=a.host, port=a.port, print=lambda *_: print(f"CoreLink mock on http://{a.host}:{a.port}/"))


if __name__ == "__main__":
    main()
