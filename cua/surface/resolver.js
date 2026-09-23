// Injected into every frame (context.add_init_script). One implementation of
// role / accessible-name computation is used for BOTH discovery (what the model
// sees, what the recorder derives) and replay (how targets are resolved), so
// record-time and replay-time naming can never disagree.
//
// Deliberately does not rely on ids, classes or test ids: legacy apps have none.
(() => {
  if (window.__cua) return;

  const norm = (s) => (s || "").replace(/[\s ]+/g, " ").trim();
  const key = (s) => norm(s).replace(/[:*]+$/, "").trim().toLowerCase();
  const INTERACTIVE =
    "a[href],a[onclick],button,input:not([type=hidden]),select,textarea,[onclick],[role=button],[role=link]";
  const TEXT_TAGS = new Set(["TD", "TH", "B", "STRONG", "FONT", "SPAN", "P", "DIV", "LI", "H1", "H2", "H3", "H4", "H5", "H6", "LABEL", "CAPTION", "BODY"]);

  function visible(el) {
    if (!el || !el.getBoundingClientRect) return false;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const cs = getComputedStyle(el);
    return cs.visibility !== "hidden" && cs.display !== "none";
  }

  function fontSize(el) {
    const f = el.closest("font[size]");
    return f ? parseInt(f.getAttribute("size"), 10) || 0 : 0;
  }

  function role(el) {
    const explicit = el.getAttribute && el.getAttribute("role");
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === "a") return "link";
    if (tag === "button") return "button";
    if (tag === "input") {
      const t = (el.getAttribute("type") || "text").toLowerCase();
      if (["button", "submit", "reset", "image"].includes(t)) return "button";
      if (t === "checkbox") return "checkbox";
      if (t === "radio") return "radio";
      return "textbox";
    }
    if (tag === "select") return "combobox";
    if (tag === "textarea") return "textbox";
    if (/^h[1-6]$/.test(tag)) return "heading";
    if ((tag === "b" || tag === "strong") && fontSize(el) >= 3) return "heading"; // legacy title idiom
    if (tag === "th") return "columnheader";
    if (tag === "td") return "cell";
    if (el.hasAttribute("onclick")) return "button";
    if (tag === "img") return "img";
    return "text";
  }

  function ownText(el) {
    let t = "";
    for (const n of el.childNodes) if (n.nodeType === 3) t += n.textContent;
    return norm(t);
  }

  function labelFor(el) {
    if (el.id) {
      const l = el.ownerDocument.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) return norm(l.textContent);
    }
    const wl = el.closest("label");
    if (wl) return norm(wl.textContent);
    return null;
  }

  // Legacy idiom: <td>Member #</td><td><input name=P_MBR></td> — the label is the
  // nearest preceding non-empty cell in the same row, or preceding text.
  function proximityLabel(el) {
    const td = el.closest("td,th");
    if (td) {
      let p = td.previousElementSibling;
      while (p) {
        const t = norm(p.innerText);
        if (t) return t.slice(0, 80);
        p = p.previousElementSibling;
      }
    }
    let n = el.previousSibling;
    while (n) {
      const t = norm(n.textContent);
      if (t) return t.slice(-80);
      n = n.previousSibling;
    }
    return null;
  }

  function accName(el) {
    const al = el.getAttribute("aria-label");
    if (al) return [norm(al), "aria-label"];
    const lb = el.getAttribute("aria-labelledby");
    if (lb) {
      const t = lb.split(/\s+/).map((id) => norm((el.ownerDocument.getElementById(id) || {}).textContent)).join(" ");
      if (t.trim()) return [norm(t), "aria-labelledby"];
    }
    const r = role(el);
    const tag = el.tagName.toLowerCase();
    if (["textbox", "combobox", "checkbox", "radio"].includes(r)) {
      const l = labelFor(el);
      if (l) return [l, "label"];
      if (el.title) return [norm(el.title), "title"];
      const p = proximityLabel(el);
      if (p) return [p, "proximity"];
      if (el.placeholder) return [norm(el.placeholder), "placeholder"];
      return ["", "none"];
    }
    if (tag === "input") return [norm(el.value || el.alt || el.title), "value"];
    if (tag === "img") return [norm(el.alt || el.title), "alt"];
    // layout containers (a <td> wrapping a whole nested table) are named by their own text only
    if ((r === "cell" || r === "text") && el.querySelector("table")) return [ownText(el).slice(0, 160), "own-text"];
    const t = norm(el.innerText || el.textContent);
    if (t) return [t.slice(0, 160), "content"];
    const img = el.querySelector && el.querySelector("img[alt]");
    if (img) return [norm(img.alt), "alt"];
    return [norm(el.title), "title"];
  }

  function xpath(el) {
    const parts = [];
    while (el && el.nodeType === 1 && el !== el.ownerDocument.documentElement) {
      let i = 1;
      for (let s = el.previousElementSibling; s; s = s.previousElementSibling) if (s.tagName === el.tagName) i++;
      parts.unshift(`${el.tagName.toLowerCase()}[${i}]`);
      el = el.parentElement;
    }
    return "/html/" + parts.join("/");
  }

  function headerRow(table) {
    const rows = Array.from(table.rows);
    for (const r of rows.slice(0, 3)) if (r.querySelector(":scope > th")) return r;
    return null;
  }

  function tableContext(el) {
    const cell = el.closest("td,th");
    if (!cell) return null;
    const tr = cell.parentElement;
    const table = tr && tr.closest("table");
    if (!table) return null;
    const hr = headerRow(table);
    const ci = cell.cellIndex;
    const column = hr && hr !== tr && hr.cells[ci] ? norm(hr.cells[ci].innerText) : null;
    const rowCells = Array.from(tr.cells).map((c, i) => ({ i, text: norm(c.innerText) }));
    const prev = cell.previousElementSibling;
    const label = prev ? norm(prev.innerText) : null;
    return { column, col_index: ci, row_cells: rowCells, prev_label: label, is_header_row: hr === tr };
  }

  function describe(el) {
    const [name, nameFrom] = accName(el);
    const r = el.getBoundingClientRect();
    const tag = el.tagName.toLowerCase();
    const d = {
      role: role(el),
      name,
      name_from: nameFrom,
      tag,
      text: norm(el.innerText || el.textContent).slice(0, 200),
      attrs: {},
      bbox: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) },
      xpath: xpath(el),
      table: tableContext(el),
    };
    for (const a of ["name", "type", "title", "alt"]) if (el.getAttribute(a)) d.attrs[a] = el.getAttribute(a);
    if (tag === "input" || tag === "textarea" || tag === "select") {
      const isPwd = (el.getAttribute("type") || "").toLowerCase() === "password";
      d.value = isPwd ? (el.value ? "••••" : "") : el.value;
      d.secret_field = isPwd;
      if (tag === "select") d.options = Array.from(el.options).map((o) => norm(o.text));
      d.disabled = !!el.disabled;
    }
    return d;
  }

  // ---------------------------------------------------------------- snapshot
  function collect() {
    const out = [];
    const seen = new Set();
    for (const el of document.querySelectorAll(INTERACTIVE)) {
      if (visible(el)) { out.push(el); seen.add(el); }
    }
    for (const el of document.querySelectorAll("*")) {
      if (seen.has(el) || !TEXT_TAGS.has(el.tagName)) continue;
      if (!ownText(el) || !visible(el)) continue;
      if (el.closest(INTERACTIVE)) continue; // text inside a link/button is already represented
      out.push(el);
    }
    // document order
    out.sort((a, b) => (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1));
    return out;
  }

  function snapshot() {
    const els = collect();
    window.__cua.els = els;
    return {
      title: document.title,
      path: location.pathname + location.search,
      elements: els.map((el, i) => Object.assign({ i }, describe(el))),
    };
  }

  // ---------------------------------------------------------------- resolution
  function textEls(doc) {
    return Array.from(doc.querySelectorAll("*")).filter((e) => TEXT_TAGS.has(e.tagName) || e.matches(INTERACTIVE));
  }

  function innermost(list) {
    return list.filter((e) => !list.some((o) => o !== e && e.contains(o)));
  }

  function resolveRaw(s) {
    const doc = document;
    let found = [];
    if (s.kind === "role_name") {
      const want = key(s.name);
      for (const el of doc.querySelectorAll("*")) {
        if (role(el) !== s.role) continue;
        if (key(accName(el)[0]) === want) found.push(el);
      }
      found = innermost(found);
    } else if (s.kind === "attribute") {
      for (const el of doc.getElementsByTagName(s.tag)) {
        if (Object.entries(s.attrs).every(([k, v]) => el.getAttribute(k) === v)) found.push(el);
      }
    } else if (s.kind === "text") {
      const want = key(s.text);
      found = innermost(textEls(doc).filter((e) => key(e.innerText) === want && (!s.role || role(e) === s.role)));
    } else if (s.kind === "label_value") {
      const want = key(s.label);
      const labels = innermost(textEls(doc).filter((e) => key(e.innerText) === want));
      for (const l of labels) {
        const cell = l.closest("td,th");
        if (!cell) continue;
        let n = cell.nextElementSibling;
        while (n && !norm(n.innerText)) n = n.nextElementSibling;
        if (n) found.push(n);
      }
    } else if (s.kind === "table_cell") {
      const col = key(s.column);
      const rk = key(s.row_key);
      for (const table of doc.querySelectorAll("table")) {
        const hr = headerRow(table);
        if (!hr) continue;
        const ci = Array.from(hr.cells).findIndex((c) => key(c.innerText) === col);
        if (ci < 0) continue;
        for (const tr of table.rows) {
          if (tr === hr) continue;
          const cells = Array.from(tr.cells);
          if (!cells.some((c, i) => i !== ci && key(c.innerText) === rk)) continue;
          let target = cells[ci];
          if (!target) continue;
          if (s.interactive) target = target.querySelector(INTERACTIVE);
          if (target) found.push(target);
        }
      }
    } else if (s.kind === "xpath") {
      const r = doc.evaluate(s.xpath, doc, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
      for (let i = 0; i < r.snapshotLength; i++) found.push(r.snapshotItem(i));
    }
    found = Array.from(new Set(found)).filter(visible);
    if (s.nth !== null && s.nth !== undefined) found = found[s.nth] ? [found[s.nth]] : [];
    return found;
  }

  function resolve(s) {
    const found = resolveRaw(s);
    window.__cua.found = found;
    return found.map(describe);
  }

  function related(a, b) {
    return a === b || a.contains(b) || b.contains(a);
  }

  // Record-time check: does strategy s resolve uniquely to snapshot element i?
  function validate(s, i) {
    const el = window.__cua.els[i];
    const found = resolveRaw(s);
    return { count: found.length, same: found.length === 1 && related(found[0], el) };
  }

  function validateAgainstFound(s) {
    const el = (window.__cua.found || [])[0];
    const f = resolveRaw(s);
    return { count: f.length, same: !!el && f.length === 1 && related(f[0], el) };
  }

  function visibleText() {
    return norm(document.body ? document.body.innerText : "");
  }

  // ---------------------------------------------------------------- redaction of screenshots
  function maskSensitive(values, maskCurrency, patterns) {
    unmask();
    const style = document.createElement("style");
    style.id = "__cua_mask_style";
    style.textContent = ".__cua_mask{background:#222 !important;color:#222 !important;-webkit-text-security:disc}";
    (document.head || document.documentElement).appendChild(style);
    const vals = values.filter((v) => v && v.length >= 3).map((v) => v.toLowerCase());
    const cur = /\$\s?[\d,]+\.\d{2}/;
    const pats = (patterns || []).map((p) => new RegExp(p));
    let n = 0;
    for (const el of document.querySelectorAll("*")) {
      let hit = false;
      if (el.tagName === "INPUT") {
        if ((el.type || "").toLowerCase() === "password") hit = !!el.value;
        else hit = vals.some((v) => (el.value || "").toLowerCase().includes(v));
      } else {
        const t = ownText(el).toLowerCase();
        const raw = ownText(el);
        if (t) hit = vals.some((v) => t.includes(v)) || (maskCurrency && cur.test(t)) || pats.some((p) => p.test(raw));
      }
      if (hit) { el.classList.add("__cua_mask"); n++; }
    }
    return n;
  }

  function unmask() {
    for (const el of document.querySelectorAll(".__cua_mask")) el.classList.remove("__cua_mask");
    const s = document.getElementById("__cua_mask_style");
    if (s) s.remove();
  }

  // ---------------------------------------------------------------- human action capture
  // While a human holds the session lease, every click/change is described in the
  // same vocabulary the recorder uses and reported to the controller.
  function report(type, el, extra) {
    if (!window.__cua.capture || typeof window.__cuaHuman !== "function") return;
    try { window.__cuaHuman(Object.assign({ type, element: describe(el) }, extra || {})); } catch (e) {}
  }
  document.addEventListener("click", (e) => { const t = e.target.closest(INTERACTIVE) || e.target; report("click", t); }, true);
  document.addEventListener("change", (e) => {
    const t = e.target;
    const secret = (t.type || "").toLowerCase() === "password";
    report("change", t, { secret, value: secret ? null : t.value });
  }, true);

  window.__cua = { els: [], found: [], capture: true, snapshot, resolve, validate, validateAgainstFound, describe, visibleText, maskSensitive, unmask };
})();
