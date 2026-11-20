/* A small fake DOM + clock for running the REAL frontend scripts in a Node vm (no browser, no server,
 * no socket). Enough of the DOM for tr.js, account.js, admin.js, app.js and waterfall.js: a node tree
 * with text nodes, attributes (id / class / data-* / hidden / disabled), classList, dataset, events with
 * bubbling, focus, and querySelector(All) for simple selectors (tag, #id, .class, [attr], [attr="v"],
 * :not(...), :scope, descendant and child combinators, comma lists). Used by tests/c1_ui_test.js.
 *
 * makeEnv() -> { ctx, doc, clock, load(file), flush(), body } — ctx is the vm global (ctx.window === ctx).
 */
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const FRONT = path.join(__dirname, "..", "frontend");

// ---- fake clock -----------------------------------------------------------------------------------
function makeClock() {
  const c = { now: 1_000_000, seq: 0, tasks: [] };
  c.setTimeout = (fn, ms) => { const id = ++c.seq; c.tasks.push({ id, at: c.now + (+ms || 0), fn, every: 0 }); return id; };
  c.setInterval = (fn, ms) => { const id = ++c.seq; c.tasks.push({ id, at: c.now + (+ms || 0), fn, every: +ms || 1 }); return id; };
  c.clear = (id) => { c.tasks = c.tasks.filter((t) => t.id !== id); };
  c.advance = (ms) => {
    const end = c.now + ms;
    for (;;) {
      const due = c.tasks.filter((t) => t.at <= end).sort((a, b) => a.at - b.at || a.id - b.id)[0];
      if (!due) break;
      c.now = due.at;
      if (due.every) due.at += due.every; else c.clear(due.id);
      due.fn();
    }
    c.now = end;
  };
  return c;
}

// ---- selectors ------------------------------------------------------------------------------------
function splitTop(s, sep) {                 // split on `sep` outside (...) and [...]
  const out = []; let depth = 0, cur = "", q = null;
  for (const ch of s) {
    if (q) { cur += ch; if (ch === q) q = null; continue; }
    if (ch === '"' || ch === "'") { q = ch; cur += ch; continue; }
    if (ch === "(" || ch === "[") depth++;
    if (ch === ")" || ch === "]") depth--;
    if (ch === sep && depth === 0) { out.push(cur); cur = ""; continue; }
    cur += ch;
  }
  out.push(cur);
  return out;
}
function parseCompound(s) {
  const c = { tag: null, id: null, cls: [], attrs: [], nots: [] };
  let i = 0;
  const ident = () => { const m = /^[A-Za-z0-9_-]+/.exec(s.slice(i)); i += m ? m[0].length : 0; return m ? m[0] : ""; };
  if (/[A-Za-z*]/.test(s[0] || "")) { if (s[0] === "*") i = 1; else c.tag = ident().toUpperCase(); }
  while (i < s.length) {
    const ch = s[i];
    if (ch === "#") { i++; c.id = ident(); }
    else if (ch === ".") { i++; c.cls.push(ident()); }
    else if (ch === "[") {
      const j = s.indexOf("]", i);
      const body = s.slice(i + 1, j); i = j + 1;
      const m = /^([A-Za-z0-9_:-]+)(?:=(?:"([^"]*)"|'([^']*)'|([^\]]*)))?$/.exec(body.trim());
      c.attrs.push({ name: m[1], value: m[2] !== undefined ? m[2] : m[3] !== undefined ? m[3] : m[4] });
    } else if (s.startsWith(":not(", i)) {
      let depth = 0, j = i + 4;
      for (; j < s.length; j++) { if (s[j] === "(") depth++; else if (s[j] === ")" && --depth === 0) break; }
      c.nots.push(parseCompound(s.slice(i + 5, j)));
      i = j + 1;
    } else throw new Error("fakedom: unsupported selector " + JSON.stringify(s));
  }
  return c;
}
function parseSelector(sel) {                // -> list of chains [{c, comb}] right-to-left
  return splitTop(sel, ",").map((part) => {
    const toks = splitTop(part.trim().replace(/\s*>\s*/g, " > ").replace(/\s+/g, " "), " ").filter(Boolean);
    const chain = []; let comb = null;
    for (const t of toks) { if (t === ">") { comb = ">"; continue; } chain.push({ c: parseCompound(t), comb }); comb = " "; }
    if (chain.length) chain[0].comb = null;
    return chain;
  });
}
function matchCompound(el, c) {
  if (!(el instanceof Element)) return false;
  if (c.tag && el.tagName !== c.tag) return false;
  if (c.id && el.getAttribute("id") !== c.id) return false;
  for (const k of c.cls) if (!el.classList.contains(k)) return false;
  for (const a of c.attrs) {
    if (!el.hasAttribute(a.name)) return false;
    if (a.value !== undefined && el.getAttribute(a.name) !== a.value) return false;
  }
  for (const n of c.nots) if (matchCompound(el, n)) return false;
  return true;
}
function matchChain(el, chain) {
  let i = chain.length - 1;
  if (!matchCompound(el, chain[i].c)) return false;
  let cur = el;
  while (i > 0) {
    const comb = chain[i].comb;
    i--;
    if (comb === ">") { cur = cur.parentElement; if (!cur || !matchCompound(cur, chain[i].c)) return false; }
    else {
      let p = cur.parentElement;
      while (p && !matchCompound(p, chain[i].c)) p = p.parentElement;
      if (!p) return false;
      cur = p;
    }
  }
  return true;
}

// ---- nodes ----------------------------------------------------------------------------------------
class Node {
  constructor(doc) { this.ownerDocument = doc; this.parentNode = null; this.childNodes = []; }
  get parentElement() { return this.parentNode instanceof Element ? this.parentNode : null; }
  get firstChild() { return this.childNodes[0] || null; }
  get lastChild() { return this.childNodes[this.childNodes.length - 1] || null; }
  get isConnected() { let n = this; while (n.parentNode) n = n.parentNode; return n === this.ownerDocument; }
  get textContent() { return this.childNodes.map((n) => n.textContent).join(""); }
  set textContent(v) { for (const n of this.childNodes) n.parentNode = null; this.childNodes = []; v = String(v ?? ""); if (v) this.appendChild(new Text(this.ownerDocument, v)); }
  appendChild(n) { return this.insertBefore(n, null); }
  insertBefore(n, ref) {
    if (n.parentNode) n.parentNode.removeChild(n);
    const i = ref ? this.childNodes.indexOf(ref) : -1;
    if (i < 0) this.childNodes.push(n); else this.childNodes.splice(i, 0, n);
    n.parentNode = this;
    return n;
  }
  removeChild(n) { const i = this.childNodes.indexOf(n); if (i >= 0) this.childNodes.splice(i, 1); n.parentNode = null; return n; }
  contains(n) { for (let x = n; x; x = x.parentNode) if (x === this) return true; return false; }
}
class Text extends Node {
  constructor(doc, data) { super(doc); this.data = String(data); this.nodeType = 3; }
  get textContent() { return this.data; }
  set textContent(v) { this.data = String(v); }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
}
class Element extends Node {
  constructor(doc, tag) {
    super(doc);
    this.tagName = String(tag).toUpperCase(); this.nodeType = 1;
    this._attrs = new Map(); this._listeners = {};
    const props = new Map();                 // style.setProperty is kept (a slider's --p fill), so tests can read it
    this.style = {
      setProperty: (k, v) => { props.set(String(k), String(v)); },
      getPropertyValue: (k) => (props.has(String(k)) ? props.get(String(k)) : ""),
      removeProperty: (k) => { const v = props.has(String(k)) ? props.get(String(k)) : ""; props.delete(String(k)); return v; },
    };
    this.value = ""; this.checked = false; this.inert = false; this.scrollTop = 0; this.scrollHeight = 0; this.tabIndex = 0;
    this._html = "";
    const self = this;
    this.classList = {
      _get: () => (self.getAttribute("class") || "").split(/\s+/).filter(Boolean),
      contains: (c) => self.classList._get().includes(c),
      add: (...cs) => { const l = self.classList._get(); for (const c of cs) if (!l.includes(c)) l.push(c); self.setAttribute("class", l.join(" ")); },
      remove: (...cs) => { self.setAttribute("class", self.classList._get().filter((c) => !cs.includes(c)).join(" ")); },
      toggle: (c, on) => { const v = on === undefined ? !self.classList.contains(c) : !!on; if (v) self.classList.add(c); else self.classList.remove(c); return v; },
    };
    this.dataset = new Proxy({}, {
      get: (_, k) => (typeof k === "string" ? self.getAttribute("data-" + k.replace(/[A-Z]/g, (m) => "-" + m.toLowerCase())) ?? undefined : undefined),
      set: (_, k, v) => { self.setAttribute("data-" + k.replace(/[A-Z]/g, (m) => "-" + m.toLowerCase()), v); return true; },
      deleteProperty: (_, k) => { self.removeAttribute("data-" + k); return true; },
      has: (_, k) => self.hasAttribute("data-" + k),
      ownKeys: () => [...self._attrs.keys()].filter((a) => a.startsWith("data-")).map((a) => a.slice(5)),
      getOwnPropertyDescriptor: () => ({ enumerable: true, configurable: true }),
    });
  }
  // attributes
  setAttribute(k, v) { this._attrs.set(String(k).toLowerCase(), String(v)); }
  getAttribute(k) { const v = this._attrs.get(String(k).toLowerCase()); return v === undefined ? null : v; }
  hasAttribute(k) { return this._attrs.has(String(k).toLowerCase()); }
  removeAttribute(k) { this._attrs.delete(String(k).toLowerCase()); }
  get id() { return this.getAttribute("id") || ""; } set id(v) { this.setAttribute("id", v); }
  get className() { return this.getAttribute("class") || ""; } set className(v) { this.setAttribute("class", v); }
  get hidden() { return this.hasAttribute("hidden"); } set hidden(v) { if (v) this.setAttribute("hidden", ""); else this.removeAttribute("hidden"); }
  get disabled() { return this.hasAttribute("disabled"); } set disabled(v) { if (v) this.setAttribute("disabled", ""); else this.removeAttribute("disabled"); }
  get title() { return this.getAttribute("title") || ""; } set title(v) { this.setAttribute("title", v); }
  get type() { return this.getAttribute("type") || ""; } set type(v) { this.setAttribute("type", v); }
  get innerHTML() { return this._html; } set innerHTML(v) { this.textContent = ""; this._html = String(v); }
  get options() { return this.querySelectorAll("option"); }
  get selectedIndex() { const o = this.options; if (!o.length) return -1; return Math.max(0, o.findIndex((x) => x.value === this.value)); }
  set selectedIndex(i) { const o = this.options[i]; this.value = o ? o.value : ""; }
  // tree
  get children() { return this.childNodes.filter((n) => n instanceof Element); }
  get nextElementSibling() { const p = this.parentNode; if (!p) return null; const l = p.childNodes; for (let i = l.indexOf(this) + 1; i < l.length; i++) if (l[i] instanceof Element) return l[i]; return null; }
  get previousElementSibling() { const p = this.parentNode; if (!p) return null; const l = p.childNodes; for (let i = l.indexOf(this) - 1; i >= 0; i--) if (l[i] instanceof Element) return l[i]; return null; }
  _wrap(x) { return x instanceof Node ? x : new Text(this.ownerDocument, String(x)); }
  append(...xs) { for (const x of xs) this.appendChild(this._wrap(x)); }
  prepend(...xs) { const f = this.firstChild; for (const x of xs) this.insertBefore(this._wrap(x), f); }
  replaceChildren(...xs) { this.textContent = ""; this.append(...xs); }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  after(...xs) { const p = this.parentNode; if (!p) return; const next = p.childNodes[p.childNodes.indexOf(this) + 1] || null; for (const x of xs) p.insertBefore(this._wrap(x), next); }
  before(...xs) { const p = this.parentNode; if (!p) return; for (const x of xs) p.insertBefore(this._wrap(x), this); }
  _all() { const out = []; const walk = (n) => { for (const c of n.childNodes) if (c instanceof Element) { out.push(c); walk(c); } }; walk(this); return out; }
  querySelectorAll(sel) {
    // :scope = this element (layout.js: el.querySelector(":scope > .tile > .tile-head")): mark it for the query
    if (String(sel).indexOf(":scope") >= 0 && this instanceof Element) {
      this.setAttribute("data-fakedom-scope", "");
      try { return this.querySelectorAll(String(sel).replace(/:scope/g, "[data-fakedom-scope]")); }
      finally { this.removeAttribute("data-fakedom-scope"); }
    }
    const chains = parseSelector(sel); return this._all().filter((el) => chains.some((ch) => matchChain(el, ch)));
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  matches(sel) { return parseSelector(sel).some((ch) => matchChain(this, ch)); }
  closest(sel) { for (let e = this; e instanceof Element; e = e.parentNode) if (e.matches(sel)) return e; return null; }
  // layout: an element is "rendered" when it is in the document and no ancestor is hidden
  getClientRects() { if (!this.isConnected) return []; for (let e = this; e instanceof Element; e = e.parentNode) if (e.hidden) return []; return [{ width: 10, height: 10 }]; }
  getBoundingClientRect() { return { left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 }; }
  focus() { this.ownerDocument.activeElement = this; }
  blur() { if (this.ownerDocument.activeElement === this) this.ownerDocument.activeElement = this.ownerDocument.body; }
  select() {}
  getContext() { return null; }
  // events
  addEventListener(t, fn) { (this._listeners[t] = this._listeners[t] || []).push(fn); }
  removeEventListener(t, fn) { const l = this._listeners[t]; if (l) { const i = l.indexOf(fn); if (i >= 0) l.splice(i, 1); } }
  dispatchEvent(ev) {
    ev.target = ev.target || this;
    for (let n = this; n instanceof Element && !ev._stop; n = n.parentNode) {   // the document: below
      ev.currentTarget = n;
      for (const fn of (n._listeners && n._listeners[ev.type]) || []) fn.call(n, ev);
      if (ev.bubbles === false) break;
    }
    return !ev.defaultPrevented;
  }
  click() { if (!this.disabled) this.dispatchEvent(new FakeEvent("click")); }
}
class FakeEvent {
  constructor(type, init) { this.type = type; Object.assign(this, init || {}); if (this.bubbles === undefined) this.bubbles = true; this.defaultPrevented = false; this._stop = false; }
  preventDefault() { this.defaultPrevented = true; }
  stopPropagation() { this._stop = true; }
  stopImmediatePropagation() { this._stop = true; }
}
class Document extends Node {
  constructor() {
    super(null); this.ownerDocument = this; this._listeners = {};
    this.documentElement = new Element(this, "html"); this.appendChild(this.documentElement);
    this.body = new Element(this, "body"); this.documentElement.appendChild(this.body);
    this.activeElement = this.body; this.hidden = false; this._detached = {};
  }
  createElement(t) { return new Element(this, t); }
  createElementNS(_, t) { return new Element(this, t); }
  createTextNode(t) { return new Text(this, t); }
  getElementById(id) {
    const hit = this.documentElement.querySelectorAll("#" + id)[0];
    if (hit) return hit;
    if (id === "tr-boot") return null;
    return (this._detached[id] = this._detached[id] || Object.assign(new Element(this, "div"), { id }));   // tolerate stray ids
  }
  querySelectorAll(sel) { return this.documentElement.querySelectorAll(sel); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  addEventListener(t, fn) { (this._listeners[t] = this._listeners[t] || []).push(fn); }
  removeEventListener(t, fn) { const l = this._listeners[t]; if (l) { const i = l.indexOf(fn); if (i >= 0) l.splice(i, 1); } }
  dispatchEvent(ev) { ev.target = ev.target || this; for (const fn of this._listeners[ev.type] || []) fn.call(this, ev); return !ev.defaultPrevented; }
}
// element events bubble up to the document too
const _dispatch = Element.prototype.dispatchEvent;
Element.prototype.dispatchEvent = function (ev) {
  const r = _dispatch.call(this, ev);
  if (!ev._stop && ev.bubbles !== false && this.isConnected) this.ownerDocument.dispatchEvent(ev);
  return r && !ev.defaultPrevented;
};

function makeEnv(opts) {
  opts = opts || {};
  const clock = makeClock();
  const doc = new Document();
  const store = Object.assign({}, opts.localStorage || {});
  const RealDate = Date;
  function FakeDate(...a) { return a.length ? new RealDate(...a) : new RealDate(clock.now); }
  FakeDate.now = () => clock.now;
  FakeDate.prototype = RealDate.prototype;
  FakeDate.parse = RealDate.parse; FakeDate.UTC = RealDate.UTC;
  const ctx = {
    console, JSON, Math, Set, Map, WeakMap, Array, Object, String, Number, Boolean, Promise, Error, RegExp, Symbol, Proxy, Reflect,
    Float32Array, Float64Array, Uint8Array, Int16Array, DataView, ArrayBuffer, URLSearchParams, URL, TextEncoder, TextDecoder, isNaN, parseInt, parseFloat,
    encodeURIComponent, decodeURIComponent, unescape, escape,
    Date: FakeDate,
    setTimeout: clock.setTimeout, clearTimeout: clock.clear, setInterval: clock.setInterval, clearInterval: clock.clear,
    requestAnimationFrame: (fn) => clock.setTimeout(fn, 16), cancelAnimationFrame: clock.clear,
    performance: { now: () => clock.now },
    localStorage: { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: (k) => { delete store[k]; } },
    sessionStorage: (() => { const s = {}; return { getItem: (k) => (k in s ? s[k] : null), setItem: (k, v) => { s[k] = String(v); }, removeItem: (k) => { delete s[k]; } }; })(),
    location: { pathname: "/", search: "", hash: "", protocol: "http:", host: "localhost:8790", hostname: "localhost", replaced: [],
                replace(u) { this.replaced.push(String(u)); }, reload() { this.reloaded = (this.reloaded || 0) + 1; } },
    history: { replaceState() {} },
    navigator: { clipboard: null, userAgent: "node" },
    CustomEvent: function (t, o) { return new FakeEvent(t, { detail: o && o.detail, bubbles: !!(o && o.bubbles) }); },
    Event: function (t, o) { return new FakeEvent(t, o); },
    Node, Element, HTMLElement: Element,
    document: doc,
    getComputedStyle: () => ({ overflowY: "visible", getPropertyValue: () => "" }),
    matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {} }),
    isSecureContext: false,
    fetch: opts.fetch || (() => new Promise(() => {})),
    WebSocket: opts.WebSocket || function () { this.readyState = 0; this.send = () => {}; this.close = () => {}; },
  };
  ctx.window = ctx; ctx.self = ctx; ctx.globalThis = ctx;
  ctx.addEventListener = () => {}; ctx.removeEventListener = () => {};
  vm.createContext(ctx);
  return {
    ctx, doc, clock, body: doc.body,
    el(tag, attrs, parent) {                 // build a page element: el("button", {id: "x"}, body)
      const e = doc.createElement(tag);
      for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v === true ? "" : v);
      (parent || doc.body).appendChild(e);
      return e;
    },
    load(f) { vm.runInContext(fs.readFileSync(path.join(FRONT, f), "utf8"), ctx, { filename: f }); },
    flush: () => new Promise((r) => setImmediate(r)),
    domReady() { doc.dispatchEvent(new FakeEvent("DOMContentLoaded")); },
    Event: FakeEvent,
  };
}

module.exports = { makeEnv, makeClock, FakeEvent };
