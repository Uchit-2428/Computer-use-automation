"""Web surface driver: Playwright + the injected resolver (``resolver.js``).

Perception is our own accessibility-style model (role + accessible name + table/label
context) computed in-page for *every frame*, not CSS selectors. That is what lets the
same driver cope with framesets, table layouts and id-less markup.
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from playwright.async_api import Browser, BrowserContext, Frame, Page, Playwright, async_playwright

from ..policy import Policy
from ..schema import Condition, ElementPresent, FrameScope, Target, TextVisible, TitleIs, UrlMatches
from .base import ElementInfo, FrameObservation, Observation, Resolved, ResolveError

RESOLVER_JS = (Path(__file__).parent / "resolver.js").read_text()

if TYPE_CHECKING:
    from ..templating import Renderer as Render


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


class WebSurface:
    def __init__(self, policy: Policy, *, headless: bool = True, viewport: tuple[int, int] = (1280, 800)):
        self.policy = policy
        self.headless = headless
        self.viewport = viewport
        self._pw: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.blocked_requests: list[str] = []
        self.on_human_event: Callable[[dict[str, Any], str], Awaitable[None] | None] | None = None
        self._gid_map: dict[int, tuple[Frame, int]] = {}
        self._inflight: set[Any] = set()

    # ---------------------------------------------------------------- lifecycle
    async def start(self) -> "WebSurface":
        self._pw = await async_playwright().start()
        self.browser = await self._pw.chromium.launch(headless=self.headless)
        self.context = await self.browser.new_context(viewport={"width": self.viewport[0], "height": self.viewport[1]})
        await self.context.add_init_script(RESOLVER_JS)
        await self.context.expose_binding("__cuaHuman", self._human_binding)
        await self.context.route("**/*", self._route)
        self.page = await self.context.new_page()
        self.page.on("request", lambda r: self._inflight.add(r) if r.resource_type == "document" else None)
        self.page.on("requestfinished", lambda r: self._inflight.discard(r))
        self.page.on("requestfailed", lambda r: self._inflight.discard(r))
        return self

    def is_loading(self) -> bool:
        """True while a document navigation is in flight (slow host vs. wrong screen)."""
        return bool(self._inflight)

    async def reload_frame(self, scope: FrameScope | None) -> None:
        for f in self._scoped(scope):
            if f == self.page.main_frame:  # type: ignore[union-attr]
                await self.page.reload()  # type: ignore[union-attr]
            else:
                await f.evaluate("() => location.reload()")
        await self.settle()

    async def close(self) -> None:
        for obj in (self.context, self.browser):
            try:
                if obj:
                    await obj.close()
            except Exception:
                pass
        if self._pw:
            await self._pw.stop()

    async def _route(self, route, request) -> None:  # network-level allowlist enforcement
        if self.policy.url_allowed(request.url):
            await route.continue_()
        else:
            self.blocked_requests.append(request.url)
            await route.abort("blockedbyclient")

    async def _human_binding(self, source: dict[str, Any], payload: dict[str, Any]) -> None:
        if self.on_human_event:
            frame: Frame = source["frame"]
            r = self.on_human_event(payload, self.frame_name(frame))
            if asyncio.iscoroutine(r):
                await r

    # ---------------------------------------------------------------- frames
    def frame_name(self, f: Frame) -> str:
        assert self.page
        return "_top" if f == self.page.main_frame else (f.name or f.url.rsplit("/", 1)[-1])

    def frames(self) -> list[Frame]:
        assert self.page
        return [f for f in self.page.frames if not f.is_detached() and f.url and not f.url.startswith("about:")]

    def _scoped(self, scope: FrameScope | None) -> list[Frame]:
        fs = self.frames()
        if scope is None:
            return fs
        out = []
        for f in fs:
            if scope.name and self.frame_name(f) != scope.name:
                continue
            if scope.url_pattern and not re.search(scope.url_pattern, _path(f.url)):
                continue
            out.append(f)
        return out

    async def goto(self, url: str) -> None:
        self.policy.check_url(url)
        assert self.page
        await self.page.goto(url, wait_until="domcontentloaded")
        await self.settle()

    # ---------------------------------------------------------------- perception
    async def _eval(self, f: Frame, expr: str, arg: Any = None) -> Any:
        try:
            return await f.evaluate(expr, arg)
        except Exception as e:  # frame navigated/detached mid-call
            if "__cua" in str(e) and "undefined" in str(e):
                await f.evaluate(RESOLVER_JS)
                return await f.evaluate(expr, arg)
            raise

    async def observe(self) -> Observation:
        frames: list[FrameObservation] = []
        by_gid: dict[int, ElementInfo] = {}
        self._gid_map = {}
        gid = 1
        for f in self.frames():
            try:
                snap = await self._eval(f, "() => window.__cua.snapshot()")
            except Exception:
                continue
            name = self.frame_name(f)
            els = []
            for e in snap["elements"]:
                info = ElementInfo(gid, name, e["role"], e["name"], e["name_from"], e["tag"], e["text"], e["bbox"], e)
                els.append(info)
                by_gid[gid] = info
                self._gid_map[gid] = (f, e["i"])
                gid += 1
            frames.append(FrameObservation(name, snap["path"], snap["title"], els))
        return Observation(frames, by_gid)

    def element_frame(self, gid: int) -> tuple[Frame, int]:
        return self._gid_map[gid]

    async def handle_for_gid(self, gid: int) -> Any:
        f, i = self._gid_map[gid]
        return await f.evaluate_handle("i => window.__cua.els[i]", i)

    async def validate_strategy(self, gid: int, strategy: dict[str, Any]) -> dict[str, Any]:
        f, i = self._gid_map[gid]
        return await self._eval(f, "([s, i]) => window.__cua.validate(s, i)", [strategy, i])

    async def resolve(self, target: Target, render: Render) -> Resolved:
        """Try strategies in order; first one that is *unique* wins. Ambiguity is never
        silently resolved by picking the first match."""
        tried: list[dict[str, Any]] = []
        frames = self._scoped(target.frame)
        if not frames:
            raise ResolveError("TARGET_NOT_FOUND", f"frame {target.frame} not present", [{"frame": target.frame.model_dump() if target.frame else None, "count": 0}])
        strategies = [_render_strategy(s.model_dump(exclude_none=True), render) for s in target.strategies]
        for s in strategies:
            if s["kind"] == "xpath" and len(strategies) > 1:
                # structural paths are kept for diagnostics / agreement only; acting on
                # them after the semantic strategies failed risks clicking the wrong control.
                continue
            hits: list[tuple[Frame, dict[str, Any]]] = []
            for f in frames:
                try:
                    found = await self._eval(f, "s => window.__cua.resolve(s)", s)
                except Exception:
                    continue
                hits += [(f, d) for d in found]
            tried.append({"strategy": s["kind"], "count": len(hits)})
            if len(hits) == 1:
                f, info = hits[0]
                handle = await f.evaluate_handle("() => window.__cua.found[0]")
                agree = disagree = 0
                for other in strategies:
                    if other is s:
                        continue
                    try:
                        v = await self._eval(f, "s => window.__cua.validateAgainstFound(s)", other)
                    except Exception:
                        continue
                    if v["same"]:
                        agree += 1
                    elif v["count"] >= 1:
                        disagree += 1
                # re-run to restore __cua.found for this strategy (validateAgainstFound doesn't mutate it)
                return Resolved(self.frame_name(f), s["kind"], agree, disagree, info, handle)
        code = "TARGET_AMBIGUOUS" if any(t["count"] > 1 for t in tried) else "TARGET_NOT_FOUND"
        raise ResolveError(code, f"could not uniquely resolve '{target.description}'", tried)

    # ---------------------------------------------------------------- actions
    async def click(self, el: Resolved) -> None:
        await el.handle.click(timeout=5000, no_wait_after=True)

    async def fill(self, el: Resolved, value: str) -> None:
        await el.handle.fill(value, timeout=5000)

    async def select(self, el: Resolved, option: str) -> None:
        await el.handle.select_option(label=option, timeout=5000)

    async def press(self, key: str, el: Resolved | None) -> None:
        if el:
            await el.handle.press(key)
        else:
            assert self.page
            await self.page.keyboard.press(key)

    async def read(self, el: Resolved) -> str:
        v = await el.handle.evaluate("e => (e.tagName==='INPUT'||e.tagName==='SELECT'||e.tagName==='TEXTAREA') ? e.value : e.innerText")
        return re.sub(r"\s+", " ", v or "").strip()

    # raw input for the operator console (human control) -------------------------
    async def mouse_click(self, x: float, y: float) -> None:
        assert self.page
        await self.page.mouse.click(x, y)

    async def type_text(self, text: str) -> None:
        assert self.page
        await self.page.keyboard.type(text, delay=20)

    # ---------------------------------------------------------------- state
    async def visible_text(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for f in self.frames():
            try:
                out[self.frame_name(f)] = await self._eval(f, "() => window.__cua.visibleText()")
            except Exception:
                pass
        return out

    async def frame_paths(self) -> dict[str, str]:
        return {self.frame_name(f): _path(f.url) for f in self.frames()}

    async def check(self, cond: Condition, render: Render) -> bool:
        if isinstance(cond, TextVisible):
            texts = await self.visible_text()
            scoped = {n for n in texts if cond.frame is None or cond.frame.name in (None, n)}
            want = render.regex(cond.text) if cond.regex else render(cond.text)
            for n in scoped:
                t = texts[n]
                if cond.regex and re.search(want, t, re.I):
                    return True
                if not cond.regex and _norm(want) in _norm(t):
                    return True
            return False
        if isinstance(cond, UrlMatches):
            pat = render.regex(cond.pattern)
            for f in self._scoped(cond.frame):
                if re.search(pat, _path(f.url)):
                    return True
            return False
        if isinstance(cond, TitleIs):
            for f in self._scoped(cond.frame):
                try:
                    if _norm(await f.title()) == _norm(render(cond.title)):
                        return True
                except Exception:
                    pass
            return False
        if isinstance(cond, ElementPresent):
            try:
                await self.resolve(cond.target, render)
                return True
            except ResolveError:
                return False
        raise TypeError(cond)

    async def settle(self, timeout_ms: int = 8000) -> None:
        """Wait until every frame has finished loading and the DOM stopped changing."""
        deadline = time.monotonic() + timeout_ms / 1000
        last = None
        stable = 0
        while time.monotonic() < deadline:
            sig = []
            try:
                for f in self.frames():
                    sig.append((f.url, await f.evaluate("() => document.readyState + ':' + (document.body ? document.body.innerHTML.length : 0)")))
            except Exception:
                sig = None  # type: ignore[assignment]
            if sig is not None and sig == last and not self._inflight and all(s[1].startswith("complete") for s in sig):
                stable += 1
                if stable >= 2:
                    return
            else:
                stable = 0
            last = sig
            await asyncio.sleep(0.15)

    # ---------------------------------------------------------------- evidence
    async def screenshot(self, path: str, mask_values: list[str], mask_currency: bool, patterns: list[str] | None = None) -> None:
        assert self.page
        for f in self.frames():
            try:
                await self._eval(f, "([v, c, p]) => window.__cua.maskSensitive(v, c, p)", [mask_values, mask_currency, patterns or []])
            except Exception:
                pass
        try:
            await self.page.screenshot(path=path, full_page=False)
        finally:
            for f in self.frames():
                try:
                    await f.evaluate("() => window.__cua.unmask()")
                except Exception:
                    pass

    async def screenshot_bytes(self, mask_values: list[str], mask_currency: bool, jpeg: bool = True) -> bytes:
        assert self.page
        for f in self.frames():
            try:
                await self._eval(f, "([v, c]) => window.__cua.maskSensitive(v, c)", [mask_values, mask_currency])
            except Exception:
                pass
        try:
            return await self.page.screenshot(type="jpeg" if jpeg else "png", quality=60 if jpeg else None)
        finally:
            for f in self.frames():
                try:
                    await f.evaluate("() => window.__cua.unmask()")
                except Exception:
                    pass

    async def dom_snapshots(self) -> dict[str, str]:
        out = {}
        for f in self.frames():
            try:
                out[self.frame_name(f)] = await f.content()
            except Exception:
                pass
        return out


def _path(url: str) -> str:
    from urllib.parse import urlparse

    u = urlparse(url)
    return u.path + (("?" + u.query) if u.query else "")


def _render_strategy(s: dict[str, Any], render: Render) -> dict[str, Any]:
    out = {}
    for k, v in s.items():
        if isinstance(v, str):
            out[k] = render(v)
        elif isinstance(v, dict):
            out[k] = {kk: render(vv) if isinstance(vv, str) else vv for kk, vv in v.items()}
        else:
            out[k] = v
    return out
