/* TeleRig — the panel grid (spec §3): GridStack 14.0.0 tiles, three-layer visibility, arrange mode,
 * collapse, add / remove / make-room, reset + undo, per-user persistence, device switching and the
 * failure fallback. Loads after tr.js and before app.js, so the grid is built before app.js measures.
 *
 *   intent[dev]   the user's saved layout for this device (boot.layout, else DEFAULT)
 *   gate(id)      role + radio capability (ptt: TR.can("tx") && caps.ptt)
 *   effective     intent items that are not hidden and pass the gate; only these are in the grid
 *
 * Saving happens on GESTURES only (drag/resize stop in arrange mode, keyboard moves, ▲▼, add, remove,
 * reset/undo, collapse, the scope's CW button, the scope split) and reads the GridStack engine, never save()
 * and never the `change` event (§3.9). Gating, automatic placements and content-fit heights are
 * never saved (E3). Every GridStack call used here was verified in the spike / S0 (§3.4).
 *
 * window.RadioLayout (replaces the tr.js stub):
 *   arranging() setRadio(p) applyGates() showTile(id) hideTile(id) visible(id) setScopeSplit(f)
 *   scopeSplit(dev) summaries() enterArrange() exitArrange() device() isFallback() grid()
 */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const TR = window.TR || {};
  const boot = TR.boot || {};

  // ---- Controls tab switcher (moved here from index.html, §3.3 step 7; tabs keep their markup) ----
  (function tabs() {
    const list = $("ctlTabs");
    if (!list || list.dataset.bound) return;
    list.dataset.bound = "1";
    list.setAttribute("role", "tablist");
    list.setAttribute("aria-label", "Control panels");
    const all = [...list.querySelectorAll(".tab")];
    function select(t) {
      for (const x of all) {
        const on = x === t;
        x.classList.toggle("on", on);
        x.setAttribute("aria-selected", on ? "true" : "false");
      }
      document.querySelectorAll(".controls .panel").forEach((p) =>
        p.classList.toggle("hide", p.getAttribute("data-p") !== t.getAttribute("data-t")));
      document.dispatchEvent(new CustomEvent("tr:tab", { detail: { tab: t.getAttribute("data-t") } }));
    }
    for (const t of all) {
      t.setAttribute("role", "tab");
      t.tabIndex = 0;
      t.setAttribute("aria-selected", t.classList.contains("on") ? "true" : "false");
      t.addEventListener("click", () => select(t));
      t.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); select(t); }
      });
    }
    document.querySelectorAll(".controls .panel").forEach((p) => p.setAttribute("role", "tabpanel"));
  })();

  const gridEl = $("grid"), wrapEl = $("gridWrap"), parking = $("tileParking");
  if (!gridEl || !parking) return;                     // a page without the tiled layout

  let failed = false;
  function fallback(why, err) {
    failed = true;
    try { console.error("TeleRig layout: " + why, err || ""); } catch (_) {}
    try { if (typeof TR.fallbackLayout === "function") TR.fallbackLayout(); } catch (_) {}
  }
  if (!window.GridStack) { fallback("GridStack did not load"); return; }

  // ---- the tile registry (§3.1), mirrored by layout_store.PANEL_IDS (the server also accepts the retired
  // "cw": CW had a panel of its own before it became a mode of the Modem window; foldCw) ----
  const TILE_IDS = ["vfo", "ptt", "audio", "scope", "controls", "recorder", "spots", "sstv"];
  const TILES = {
    // vfo bodyPx: one band's lines + the meter along the bottom with 16 px digits (style.css: 112 px + 16);
    // bodyPxAlt: two bands, each on one line (the short-body layout) + the meter
    vfo:      { title: "VFO",      icon: "radio",              minW: 3, bodyPx: 130, bodyPxAlt: 108 },
    // ptt bodyPxAlt = the body with the ATAS row (style.css .tile-ptt): desktop PTT 44 + gap 6 + row 31 (+3);
    // phone 44 + 6 + a 52 px row (its keys keep the 44 px touch height) + 2 -> 6 phone rows (§3.6)
    ptt:      { title: "Transmit", icon: "mic",                minW: 2, bodyPx: 52,  bodyPxAlt: 84, bodyPxAltPhone: 104 },
    audio:    { title: "Audio",    icon: "volume-2",           minW: 2, bodyPx: 72 },
    scope:    { title: "Scope",    icon: "audio-waveform",     minW: 4, bodyPx: 200 },
    controls: { title: "Controls", icon: "sliders-horizontal", minW: 3, bodyPx: 240 },
    // recorder bodyPx: the Record row (31) and the hint line (21), with room to start the list
    recorder: { title: "Recorder", icon: "tr-record",          minW: 2, bodyPx: 80 },
    // spots bodyPx: the filter row (31), the status line (21) and a few two-line spots
    spots:    { title: "Spots",    icon: "globe",              minW: 3, bodyPx: 180 },
    // sstv = the Modem window (CW, RTTY, FT8, SSTV; digital.js), its id kept so saved layouts keep it.
    // bodyPx: the mode keys (28), SSTV's tabs and mode row (31 + 31), the status line (21) and a picture
    sstv:     { title: "Modem",    icon: "tr-sstv",            minW: 3, bodyPx: 250 },
  };
  const GATEABLE = new Set(["ptt"]);                       // the only tile a gate can park
  const PHONE_RESIZABLE = new Set(["scope", "spots", "sstv"]); // phone: user-sized (stored h, `s` handle)
  const PHONE_FIT = new Set(["audio", "controls", "recorder"]); // phone: height follows the content
  const HEAD_ROWS = 2;                                     // collapsed slot, fixed (AR-58)
  const B = (boot.layout_bounds && boot.layout_bounds.desktop && boot.layout_bounds.phone) ? boot.layout_bounds
    : { desktop: { cols: 12, y_max: 199, h_max: 96, bottom_max: 200, dw_max: 11 },
        phone: { rank_max: 15, h_max: 60 }, split: [0.15, 0.85] };
  const SPLIT = Array.isArray(B.split) ? B.split : [0.15, 0.85];
  const SPLIT_DEFAULT = 0.38;
  const DEFAULT = {                                        // §3.6
    desktop: { column: 12, items: [
      { id: "controls", x: 0, y: 0, w: 3, h: 18 }, { id: "vfo", x: 3, y: 0, w: 4, h: 7 },
      { id: "ptt", x: 7, y: 0, w: 2, h: 7 }, { id: "audio", x: 9, y: 0, w: 3, h: 7 },
      { id: "scope", x: 3, y: 7, w: 9, h: 17 },
      { id: "recorder", x: 0, y: 18, w: 3, h: 6 },
      { id: "spots", x: 9, y: 7, w: 3, h: 17, hidden: true }, { id: "sstv", x: 9, y: 7, w: 3, h: 17, hidden: true }] },
    phone: { column: 1, items: [
      { id: "vfo", y: 0 }, { id: "ptt", y: 1 }, { id: "scope", y: 2, h: 11 },
      { id: "audio", y: 3 }, { id: "controls", y: 4 }, { id: "recorder", y: 5 },
      { id: "spots", y: 6, h: 10, hidden: true }, { id: "sstv", y: 7, h: 12, hidden: true }] },
  };
  // the Recorder came after layouts were first saved: a saved layout without it gets it right under
  // Controls (placeRecorder), the place the default gives it
  const REC_ROWS = 6, CONTROLS_KEEP = 12;                  // desktop rows: the Recorder's; the least Controls keeps
  const CANCEL = "input,textarea,select,option,button,canvas,.scope,.scope-split,.tool-panel,.tile-body,[data-act]";
  const REDUCED = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const MODES = new Set(["float", "list"]);               // GridStack accepts any string silently (S10)

  const tileEl = {};
  for (const id of TILE_IDS) {
    const el = parking.querySelector('.grid-stack-item[data-tile="' + id + '"]') ||
               document.querySelector('.grid-stack-item[data-tile="' + id + '"]');
    if (!el || !el.querySelector(":scope > .tile > .tile-head")) { fallback("tile " + id + " has no header (S3)"); return; }
    tileEl[id] = el;
    el._src = "intent";
    if (PHONE_FIT.has(id)) el.querySelector(":scope > .tile").classList.add("fit");
  }

  // ---- small helpers ----
  const clamp = (v, lo, hi) => (v < lo ? lo : v > hi ? hi : v);
  const isInt = (v) => typeof v === "number" && Number.isInteger(v);
  const deepCopy = (o) => (o == null ? o : JSON.parse(JSON.stringify(o)));
  const title = (id) => TILES[id].title;
  function clampSplit(f) {
    f = +f;
    if (!isFinite(f) || f <= 0) f = SPLIT_DEFAULT;
    return Math.min(SPLIT[1], Math.max(SPLIT[0], f));
  }
  function overlap(a, b) { return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h; }
  function overlapArea(a, b) {
    const w = Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x), h = Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y);
    return w > 0 && h > 0 ? w * h : 0;
  }
  const say = (m) => { try { TR.srLive(m); } catch (_) {} };
  const toast = (m, k, o) => { try { return TR.toast(m, k, o); } catch (_) { return null; } };

  // ---- layouts: canonical items, normalisation, clamping (I22: the bounds come from the bootstrap) ----
  function canonItem(d, src) {
    const id = src.id, o = { id };
    if (d === "desktop") {
      const w = clamp(isInt(src.w) ? src.w : 1, 1, B.desktop.cols);
      const h = clamp(isInt(src.h) ? src.h : 1, 1, B.desktop.h_max);
      o.x = clamp(isInt(src.x) ? src.x : 0, 0, B.desktop.cols - w);
      o.y = clamp(isInt(src.y) ? src.y : 0, 0, B.desktop.bottom_max - h);
      o.w = w; o.h = h;
    } else {
      o.x = 0; o.y = clamp(isInt(src.y) ? src.y : 0, 0, B.phone.rank_max); o.w = 1;
      if (isInt(src.h)) o.h = clamp(src.h, 1, B.phone.h_max);
    }
    o.hidden = src.hidden === true;
    o.collapsed = src.collapsed === true;
    if (d === "desktop" && src.room && typeof src.room === "object" && TILE_IDS.includes(src.room.from) && isInt(src.room.dw))
      o.room = { from: src.room.from, dw: clamp(src.room.dw, 1, B.desktop.dw_max) };
    return o;
  }
  // A layout saved before the Recorder existed: the Recorder goes right under Controls and is shown. A
  // full-height Controls gives up the Recorder's rows; a shorter (or collapsed) one keeps its height
  // and the Recorder takes the rows under it (applyFull finds it a free spot if those are taken). On
  // the phone it comes right after Controls. With Controls hidden it starts hidden, like any new tile.
  function placeRecorder(d, seen) {
    const c = seen.get("controls");
    if (!c || c.hidden) return;
    if (d === "desktop") {
      if (!c.collapsed && c.h - REC_ROWS >= CONTROLS_KEEP) c.h -= REC_ROWS;
      seen.set("recorder", canonItem(d, { id: "recorder", x: c.x, y: c.y + c.h, w: c.w, h: REC_ROWS }));
    } else {
      for (const it of seen.values()) if (it.y > c.y) it.y = Math.min(B.phone.rank_max, it.y + 1);
      seen.set("recorder", canonItem(d, { id: "recorder", y: c.y + 1 }));
    }
  }
  // A layout saved while CW had a panel of its own ("cw"): CW is a mode of the Modem window now. A CW panel
  // that was showing hands the window its place, its size and the columns it borrowed (room), unless the
  // window was showing too: then the CW panel just goes, and on the desktop the columns it borrowed go back
  // to the panel that lent them, as closing it would have done (removeTile).
  function foldCw(d, items, seen) {
    const raw = items.find((it) => it && typeof it === "object" && it.id === "cw");
    if (!raw || raw.hidden === true) return;
    const cw = canonItem(d, raw), win = seen.get("sstv");
    if (!win || win.hidden) { cw.id = "sstv"; seen.set("sstv", cw); return; }
    const lender = d === "desktop" && cw.room ? seen.get(cw.room.from) : null;
    if (!lender || lender.hidden) return;
    const l = rectOf(lender), slot = { x: l.x + l.w, y: l.y, w: cw.room.dw, h: l.h };
    if (slot.x !== cw.x || slot.x + slot.w > B.desktop.cols) return;
    for (const o of seen.values()) if (o !== lender && !o.hidden && overlap(slot, rectOf(o))) return;
    lender.w += cw.room.dw;
  }
  // A layout saved while the meter had a panel of its own ("meter"): the meter is in the VFO panel now. On the
  // desktop the rows a showing Meter panel took go to the panels right above it (inside its columns), so the
  // VFO above it gets the room its meter needs; with none above, the panels right below it grow up into them.
  // On the phone the panels simply close up (list order).
  function foldMeter(d, items, seen) {
    if (d !== "desktop") return;
    const raw = items.find((it) => it && typeof it === "object" && it.id === "meter");
    if (!raw || raw.hidden === true) return;
    const m = rectOf(canonItem(d, raw));
    const within = (r) => r.x >= m.x && r.x + r.w <= m.x + m.w;
    const shown = [...seen.values()].filter((it) => !it.hidden && !it.collapsed);
    const above = shown.filter((it) => { const r = rectOf(it); return within(r) && r.y + r.h === m.y; });
    if (above.length) { for (const it of above) it.h = Math.min(B.desktop.h_max, it.h + m.h); return; }
    for (const it of shown) {
      const r = rectOf(it);
      if (within(r) && r.y === m.y + m.h) { it.y -= m.h; it.h = Math.min(B.desktop.h_max, it.h + m.h); }
    }
  }
  // drop unknown / duplicate ids; a known id missing from a saved layout gets its DEFAULT item, hidden
  // (the Recorder in a layout saved before it existed: placeRecorder; a CW panel: foldCw, and a Meter panel:
  // foldMeter, first, so the Recorder's place counts the Modem window where the CW panel was)
  function normalize(d, data) {
    if (!data || typeof data !== "object" || !Array.isArray(data.items)) return null;
    const seen = new Map();
    for (const it of data.items) {
      if (it && typeof it === "object" && TILE_IDS.includes(it.id) && !seen.has(it.id)) seen.set(it.id, canonItem(d, it));
    }
    foldCw(d, data.items, seen);
    foldMeter(d, data.items, seen);
    if (!seen.has("recorder")) placeRecorder(d, seen);
    const items = TILE_IDS.map((id) => seen.get(id) ||
      Object.assign(canonItem(d, DEFAULT[d].items.find((i) => i.id === id)), { hidden: true }));
    return { column: d === "desktop" ? 12 : 1, items };
  }
  function defaults(d) { return normalize(d, DEFAULT[d]); }
  function clampLayout(d, s) { return normalize(d, s); }
  function sameLayout(a, b) {
    if (!a || !b) return false;
    const key = (l) => JSON.stringify(l.items.slice().sort((p, q) => (p.id < q.id ? -1 : 1)));
    return a.column === b.column && key(a) === key(b);
  }
  function item(d, id) { return intent[d].items.find((i) => i.id === id); }

  // ---- state ----
  const mq = matchMedia("(min-width: 900px)");
  let dev = mq.matches ? "desktop" : "phone";
  document.body.dataset.device = dev;
  const intent = { desktop: null, phone: null };
  const lastSaved = { desktop: null, phone: null };        // {data, split}: the last SUCCESSFUL PUT (AR-41)
  const scopeSplit = { desktop: SPLIT_DEFAULT, phone: SPLIT_DEFAULT };
  const pending = {};                                      // device -> {data, scope_split, base_rev} not yet stored
  let rev = 0;
  let grid = null, applying = false, arranging = false, gestureEl = null;
  const fitH = {};                                         // phone content-fit heights (never saved)

  // the bootstrap layout, the paint cache (§3.14) and a pending save from the last visit
  const CACHE_KEY = "telerig.layout." + (boot.port != null ? boot.port : location.port) + "." + (boot.user || TR.user || "");
  function cacheRead() {
    try { const v = JSON.parse(localStorage.getItem(CACHE_KEY) || "null"); return v && typeof v === "object" ? v : null; }
    catch (_) { return null; }
  }
  function cacheWrite() {
    try {
      const v = { rev, layout: { desktop: intent.desktop, phone: intent.phone, scope_split: Object.assign({}, scopeSplit) } };
      if (Object.keys(pending).length) v.pending = pending;
      localStorage.setItem(CACHE_KEY, JSON.stringify(v));
    } catch (_) {}
  }
  let rePut = null;
  (function initIntent() {
    let src = boot.layout && typeof boot.layout === "object" ? boot.layout : null;
    rev = src && isInt(src.rev) ? src.rev : 0;
    const cache = cacheRead();
    if (boot.layout_error && cache && cache.layout) src = cache.layout;      // the file is unreadable: paint from cache
    for (const d of ["desktop", "phone"]) {
      intent[d] = normalize(d, src && src[d]) || defaults(d);
      const ss = src && src.scope_split && typeof src.scope_split === "object" ? src.scope_split[d] : undefined;
      scopeSplit[d] = clampSplit(ss);
      lastSaved[d] = { data: deepCopy(intent[d]), split: scopeSplit[d] };
    }
    // a save that never reached the server, made on top of the layout the server still has: re-PUT once
    const pd = cache && cache.pending && typeof cache.pending === "object" ? cache.pending : null;
    if (pd && !boot.layout_error) {
      for (const d of ["desktop", "phone"]) {
        const p = pd[d];
        if (!p || p.base_rev !== rev) continue;
        const data = normalize(d, p.data);
        if (!data) continue;
        intent[d] = data;
        scopeSplit[d] = clampSplit(p.scope_split);
        rePut = rePut || [];
        rePut.push(d);
      }
    }
  })();

  // ---- gates (E3 layer 2): role + the current radio's capabilities ----
  function capsFromBoot(id) {
    const r = (Array.isArray(boot.radios) ? boot.radios : []).find((x) => x && x.id === id);
    const tc = (r && r.tile_caps) || {};
    return { ptt: tc.ptt !== false, dual: tc.dual_watch === true, name: r ? r.name : "" };
  }
  function capsFromProfile(p) {
    const c = (p && p.capabilities) || {};
    return { ptt: c.ptt !== false,
             dual: ("dual_watch" in c ? !!c.dual_watch : !!(p && p.dual_watch)), name: (p && p.name) || "" };
  }
  let caps = capsFromBoot(TR.initialRadio);
  const can = (perm) => { try { return !!TR.can(perm); } catch (_) { return false; } };
  function gate(id) {
    if (id === "ptt") return can("tx") && caps.ptt;
    return true;
  }
  function gateReason(id) {
    if (id === "ptt" && !can("tx")) return "Operator role required";
    if (!gate(id)) return "Not available on " + (caps.name || "this radio");
    return "";
  }
  function effective(d) {
    d = d || dev;
    const eff = intent[d].items.filter((i) => !i.hidden && gate(i.id)).map((i) => Object.assign({}, i));
    const ord = (id) => TILE_IDS.indexOf(id);
    if (d === "desktop") eff.sort((a, b) => a.y - b.y || a.x - b.x || ord(a.id) - ord(b.id));
    else eff.sort((a, b) => a.y - b.y || ord(a.id) - ord(b.id));
    return eff;
  }

  // ---- sizes: desktop cell, pixel-derived minimums (AR-38), phone heights (AR-37) ----
  function desktopCell() {
    const h = wrapEl ? wrapEl.clientHeight : 0;
    return clamp(Math.floor((h || 720) / 24), 22, 64);
  }
  function dualWatch() {
    const b = $("bands");
    return b ? b.classList.contains("dual") : caps.dual;
  }
  function atasShown() { const a = $("atasTune"); return !!a && !a.hidden; }
  function bodyPx(id, d) {
    const t = TILES[id];
    if (id === "vfo" && dualWatch()) return t.bodyPxAlt;
    if (id === "ptt" && atasShown()) return (d === "phone" && t.bodyPxAltPhone) || t.bodyPxAlt;
    return t.bodyPx;
  }
  function minFor(d, id) {
    const head = d === "phone" ? 38 : 30;
    const c = d === "phone" ? 32 : (grid && dev === "desktop" ? grid.getCellHeight() : desktopCell());
    return { w: Math.min(TILES[id].minW, d === "phone" ? 1 : B.desktop.cols),
             h: Math.max(HEAD_ROWS, Math.ceil((head + bodyPx(id, d) + 16 + 2 * 4 + 2) / c)) };
  }
  function phoneH(it) {
    const m = minFor("phone", it.id).h;
    if (it.id === "vfo" || it.id === "ptt") return m;     // container-query sized: exactly the minimum
    if (PHONE_RESIZABLE.has(it.id)) {
      const def = DEFAULT.phone.items.find((i) => i.id === it.id).h;
      return clamp(isInt(it.h) ? it.h : def, m, B.phone.h_max);
    }
    return clamp(fitH[it.id] || m, m, B.phone.h_max);    // content-fit (fitPhone measures it)
  }

  // ---- GridStack options (§3.4); alwaysShowResizeHandle:true instead of the dead resizable.autoHide (S0 note 1) ----
  function OPTS(d) {
    const common = { handle: ".tile-head", draggable: { handle: ".tile-head", scroll: true, cancel: CANCEL },
                     alwaysShowResizeHandle: true, acceptWidgets: false, removable: false, staticGrid: true, margin: 4 };
    return d === "desktop"
      ? Object.assign({ column: 12, minRow: 24, cellHeight: desktopCell(), mode: "float", animate: !REDUCED,
                        resizable: { handles: "e,se,s,sw,w" } }, common)
      : Object.assign({ column: 1, minRow: 1, cellHeight: 32, mode: "list", animate: false,
                        resizable: { handles: "s" } }, common);
  }
  const anim = () => OPTS(dev).animate;
  // update() then restore animation on the next frame, so the change itself never animates (S0 note 2)
  function gsUpdate(el, o) {
    grid.setAnimation(false);
    grid.update(el, o);
    grid.setAnimation(anim(), true);
  }

  // ---- placement (§3.8) ----
  function griddedNodes(exceptId) {
    const out = [];
    for (const id of TILE_IDS) { const n = tileEl[id].gridstackNode; if (n && id !== exceptId) out.push(n); }
    return out;
  }
  function slotFreeArith(x, y, w, h, exceptId) {
    const r = { x, y, w, h };
    return griddedNodes(exceptId).every((n) => !overlap(r, n));
  }
  function clampToDevice(it, col) {
    const w = clamp(it.w, 1, col), h = clamp(it.h, 1, B.desktop.h_max);
    return { x: clamp(it.x, 0, col - w), y: clamp(it.y, 0, B.desktop.bottom_max - h), w, h };
  }
  function rectOf(it) { const p = clampToDevice(it, 12); return { x: p.x, y: p.y, w: p.w, h: it.collapsed ? HEAD_ROWS : p.h }; }
  function firstFreeArea(w, h, maxRows) {
    for (let y = 0; y + h <= maxRows; y++)
      for (let x = 0; x + w <= B.desktop.cols; x++)
        if (slotFreeArith(x, y, w, h)) return { x, y };
    return null;
  }
  // make room (D-7): narrow the largest-overlapping gridded tile to the LEFT of the slot, if it can
  // still keep its minimum width; revert it when the slot is still not free
  function makeRoom(it, h) {
    const pos = clampToDevice(it, 12), slot = { x: pos.x, y: pos.y, w: pos.w, h };
    const cands = griddedNodes(it.id).filter((n) => overlap(slot, n) && n.x < slot.x)
      .map((n) => ({ n, ov: overlapArea(slot, n), newW: slot.x - n.x }))
      .filter((c) => c.newW >= Math.min(minFor("desktop", c.n.id).w, c.n.w))
      .sort((a, b) => b.ov - a.ov);
    for (const c of cands) {
      const el = tileEl[c.n.id], oldW = c.n.w;
      gsUpdate(el, { w: c.newW });
      if (slotFreeArith(slot.x, slot.y, slot.w, slot.h, it.id)) return { from: c.n.id, dw: oldW - c.newW, el };
      gsUpdate(el, { w: oldW });
    }
    return null;
  }
  function noticePlaced(el, id, spot) {
    toast(title(id) + " panel placed at column " + (spot.x + 1) + ", row " + (spot.y + 1) + " — use Arrange to move it", "info", { ms: 6000 });
    try { el.scrollIntoView({ block: "nearest" }); } catch (_) {}
  }
  // how: "intent" (boot / rebuild) | "ungate" (a gate opened) | "add" (a user gesture)
  // returns {how:"slot"|"room"|"spot"|"auto", room?, spot?}
  function placeTile(el, it, how) {
    gridEl.appendChild(el);                                   // MUST precede makeWidget (S2)
    if (el.parentElement !== gridEl) throw new Error("tile not in grid");
    const col = grid.getColumn(), m = minFor(dev, it.id), coll = !!it.collapsed;
    if (dev === "phone") {                                    // rank layout: explicit row, never autoPosition (AR-29)
      const h = coll ? HEAD_ROWS : phoneH(it);
      grid.makeWidget(el, { id: it.id, x: 0, y: it._row, w: 1, h, minH: coll ? HEAD_ROWS : Math.min(m.h, h),
                            maxH: B.phone.h_max, noResize: coll || !PHONE_RESIZABLE.has(it.id) });
      el._src = "intent";
      return { how: "slot" };
    }
    const pos = clampToDevice(it, col), h = coll ? HEAD_ROWS : pos.h;
    // the pixel minimum constrains resize GESTURES; it never rewrites an existing intent
    const opts = { id: it.id, w: pos.w, h, minW: Math.min(m.w, col, pos.w), minH: coll ? HEAD_ROWS : Math.min(m.h, pos.h),
                   maxH: B.desktop.h_max, noResize: coll };
    if (slotFreeArith(pos.x, pos.y, pos.w, h, it.id)) {
      grid.makeWidget(el, Object.assign({}, opts, { x: pos.x, y: pos.y }));
      el._src = "intent";
      return { how: "slot" };
    }
    if (how !== "intent") {                                   // un-gate / add (AR-43): make room, then a VISIBLE free area
      const room = makeRoom(it, h);
      if (room) {
        grid.makeWidget(el, Object.assign({}, opts, { x: pos.x, y: pos.y }));
        el._src = "intent";
        // a gate opening is not a gesture: the narrowed donor's own intent stays as it was
        room.el._src = how === "add" ? "intent" : "auto";
        return { how: "room", room };
      }
      const spot = firstFreeArea(pos.w, h, 24) || firstFreeArea(pos.w, h, B.desktop.bottom_max);
      if (spot) {
        grid.makeWidget(el, Object.assign({}, opts, spot));
        el._src = "auto";
        noticePlaced(el, it.id, spot);
        return { how: "spot", spot };
      }
    }
    grid.makeWidget(el, Object.assign({}, opts, { autoPosition: true }));
    el._src = "auto";
    return { how: "auto" };
  }
  function tileEvent(id, detail) {
    document.dispatchEvent(new CustomEvent("tr:tile", { detail: Object.assign({ id }, detail) }));
  }
  function park(id) {
    const el = tileEl[id];
    if (el.gridstackNode) grid.removeWidget(el, false, false);
    parking.appendChild(el);
  }
  function applyFull() {                                       // boot, device switch, reset, phone changes, rebuild
    if (!grid || failed) return;
    const before = new Set(TILE_IDS.filter((id) => tileEl[id].gridstackNode));
    applying = true;
    try {
      grid.setAnimation(false);                                // final sizes synchronously (S7)
      for (const el of grid.getGridItems()) if (el.gridstackNode) grid.removeWidget(el, false, false);
      for (const id of TILE_IDS) parking.appendChild(tileEl[id]);
      const eff = effective();
      if (dev === "phone") {
        let row = 0;
        for (const it of eff) { it._row = row; row += it.collapsed ? HEAD_ROWS : phoneH(it); placeTile(tileEl[it.id], it, "intent"); }
      } else {
        // pass 1: tiles whose intent slot is free among the accepted fits; pass 2: the rest. A tile a gate
        // could have parked (ptt) comes last, so a panel the user moved into its hole while it was
        // gated keeps that spot, and the returning tile goes to a visible free area with a notice (AR-43).
        const order = eff.slice().sort((a, b) => (GATEABLE.has(a.id) ? 1 : 0) - (GATEABLE.has(b.id) ? 1 : 0));
        const fits = [], rest = [];
        for (const it of order) {
          const r = rectOf(it);
          (fits.every((f) => !overlap(r, rectOf(f))) ? fits : rest).push(it);
        }
        for (const it of fits) placeTile(tileEl[it.id], it, "intent");
        for (const it of rest) placeTile(tileEl[it.id], it, GATEABLE.has(it.id) ? "ungate" : "intent");
      }
      grid.setAnimation(anim(), true);
      syncChrome();
    } finally {
      applying = false;
    }
    for (const id of before) if (!tileEl[id].gridstackNode) tileEvent(id, { visible: false });
    gridEl.classList.add("tr-ready");
    document.dispatchEvent(new CustomEvent("tr:layout", { detail: { device: dev } }));
    if (dev === "phone") scheduleFit();
  }

  // ---- gates at runtime (§3.8): radio switch, role from hello ----
  function applyGates() {
    if (!grid || failed) return;
    try {
      const want = new Set(effective().map((i) => i.id));
      const have = new Set(TILE_IDS.filter((id) => tileEl[id].gridstackNode));
      if (want.size === have.size && [...want].every((id) => have.has(id))) { refreshMins(); return; }
      if (dev === "phone") { applyFull(); return; }           // ranks are never compacted: a gated tile returns to its rank
      applying = true;
      try {
        grid.setAnimation(false);
        for (const id of have) if (!want.has(id)) { park(id); tileEvent(id, { visible: false }); }
        for (const it of effective()) if (!have.has(it.id)) placeTile(tileEl[it.id], it, "ungate");
        grid.setAnimation(anim(), true);
        syncChrome();
      } finally { applying = false; }
      refreshMins();
    } catch (e) { applying = false; fallback("applyGates", e); }
  }
  function setRadio(p) {
    if (!p) return;
    caps = capsFromProfile(p);
    applyGates();
  }

  // ---- sizes at runtime: desktop re-fit (AR-42), pixel minimums, phone content-fit ----
  function refreshMins() {
    if (!grid || failed || applying) return;
    for (const id of TILE_IDS) {
      const el = tileEl[id], n = el.gridstackNode;
      if (!n) continue;
      const it = item(dev, id);
      if (it.collapsed) continue;
      const m = minFor(dev, id);
      if (dev === "desktop") {
        const mh = Math.min(m.h, n.h), mw = Math.min(m.w, n.w);
        if (n.minH !== mh || n.minW !== mw) grid.update(el, { minH: mh, minW: mw });
      } else if (id === "vfo" || id === "ptt") {
        if (n.h !== m.h || n.minH !== m.h) gsUpdate(el, { h: m.h, minH: m.h });
      } else if (PHONE_RESIZABLE.has(id) && n.minH !== Math.min(m.h, n.h)) {
        grid.update(el, { minH: Math.min(m.h, n.h) });
      }
    }
  }
  let refitT = 0;
  function refit() {
    if (!grid || failed || dev !== "desktop") return;
    const c = desktopCell();
    if (c === grid.getCellHeight()) return;
    grid.setAnimation(false);
    grid.cellHeight(c);                                         // S0 h: a live pitch change relayouts everything
    grid.setAnimation(anim(), true);
    refreshMins();
  }
  if ("ResizeObserver" in window && wrapEl) {
    new ResizeObserver(() => { clearTimeout(refitT); refitT = setTimeout(refit, 150); }).observe(wrapEl);
  }
  let fitT = 0;
  function scheduleFit() { clearTimeout(fitT); fitT = setTimeout(fitPhone, 150); }
  function fitPhone() {
    if (dev !== "phone" || !grid || failed || applying) return;
    for (const id of PHONE_FIT) {
      const el = tileEl[id], n = el.gridstackNode;
      if (!n || item("phone", id).collapsed) continue;
      const fit = el.querySelector(".tile-fit");
      if (!fit || !fit.getClientRects().length) continue;
      const m = minFor("phone", id).h;
      const want = Math.ceil((38 + fit.scrollHeight + 16 + 2 * 4 + 2) / 32);
      const h = clamp(want, m, B.phone.h_max);
      // content taller than h_max (Controls with every SET group open): the body scrolls instead of clipping
      el.querySelector(":scope > .tile").classList.toggle("fit-capped", want > B.phone.h_max);
      fitH[id] = h;
      if (h !== n.h) gsUpdate(el, { h, minH: Math.min(m, h) });   // list mode reflows the tiles below (S0 v)
    }
    refreshMins();
  }
  if ("ResizeObserver" in window) {
    const ro = new ResizeObserver(() => { if (dev === "phone") scheduleFit(); });
    for (const id of PHONE_FIT) ro.observe(tileEl[id].querySelector(".tile-fit"));
  }
  // the VFO's dual-watch row and the PTT tile's ATAS row change their pixel minimums
  if ("MutationObserver" in window) {
    const mo = new MutationObserver(() => { if (dev === "phone") scheduleFit(); else refreshMins(); });
    if ($("bands")) mo.observe($("bands"), { attributes: true, attributeFilter: ["class"] });
    if ($("atasTune")) mo.observe($("atasTune"), { attributes: true, attributeFilter: ["hidden"] });
  }

  // ---- saving: gestures only (§3.9) ----
  function snapshot(d, gEl) {
    const prev = intent[d];
    if (d === "phone") return phoneSnapshot(prev, gEl);
    const items = TILE_IDS.map((id) => {
      const p = prev.items.find((i) => i.id === id), el = tileEl[id], n = el.gridstackNode;
      if (!n) return Object.assign({}, p);                      // parked (hidden or gated): keep intent
      if (el._src === "auto" && el !== gEl) return Object.assign({}, p);   // an automatic placement is not intent (AR-43)
      if (el === gEl) el._src = "intent";
      const o = { id, x: n.x, y: n.y, w: n.w, h: p.collapsed ? p.h : n.h,   // the ENGINE, never save() (S1)
                  hidden: false, collapsed: !!p.collapsed };
      if (p.room) o.room = p.room;
      return o;
    });
    return { column: 12, items };
  }
  // phone ranks (AR-29): the gridded tiles in visual order refill the rank slots they held; parked
  // tiles keep theirs. A resized scope / Spots / Modem keeps its new height (the only phone heights stored).
  function phoneSnapshot(prev, gEl) {
    const order = prev.items.slice().sort((a, b) => a.y - b.y || TILE_IDS.indexOf(a.id) - TILE_IDS.indexOf(b.id));
    const vis = TILE_IDS.filter((id) => tileEl[id].gridstackNode)
      .sort((a, b) => tileEl[a].gridstackNode.y - tileEl[b].gridstackNode.y);
    let k = 0;
    const ids = order.map((it) => (tileEl[it.id].gridstackNode ? vis[k++] : it.id));
    return { column: 1, items: ids.map((id, r) => {
      const o = Object.assign({}, prev.items.find((i) => i.id === id), { y: r });
      const n = tileEl[id].gridstackNode;
      if (n && PHONE_RESIZABLE.has(id) && !o.collapsed && tileEl[id] === gEl) o.h = n.h;
      return o;
    }) };
  }
  // One PUT in flight at a time (so the server can never apply them out of order); the newest
  // snapshot waits in `queued` (300 ms debounce) and goes out when the previous PUT has finished.
  let putT = 0, queued = null, inflight = null, saveSeq = 0;
  function saveSoon(gEl, d) {
    d = d || dev;
    const s = clampLayout(d, snapshot(d, gEl)), split = clampSplit(scopeSplit[d]);
    intent[d] = s;
    const ls = lastSaved[d];
    // compared with the last SUCCESSFUL PUT (AR-41); while a PUT of this device is still in flight the
    // comparison is not final yet, so the snapshot is queued anyway (a move and its undo both arrive)
    if (ls && sameLayout(s, ls.data) && split === ls.split && !(inflight && inflight.d === d)) {
      if (queued && queued.d === d) { clearTimeout(putT); putT = 0; queued = null; saveSeq++; }
      delete pending[d];
      cacheWrite();
      return false;
    }
    queue(d, s, split, 300);
    return true;
  }
  function queue(d, s, split, delay) {
    pending[d] = { data: s, scope_split: split, base_rev: rev };
    cacheWrite();
    if (queued && queued.d !== d) flushPut();                     // the other device's save goes first
    clearTimeout(putT);
    queued = { d, s, split, seq: ++saveSeq };
    if (delay) putT = setTimeout(flushPut, delay); else flushPut();
  }
  function flushPut() {
    clearTimeout(putT); putT = 0;
    if (!queued || inflight) return;                              // an in-flight PUT sends it when done
    const j = queued; queued = null; inflight = j;
    doPut(j, 0);
  }
  function putDone() { inflight = null; if (queued) flushPut(); }
  const RETRY = [1000, 3000, 9000];
  function doPut(j, attempt) {
    TR.api("/api/me/layout", { method: "PUT", json: { device: j.d, data: j.s, scope_split: j.split, base_rev: rev } })
      .then((r) => {
        if (isInt(r.rev)) rev = r.rev;
        const stored = normalize(j.d, r.layout) || j.s, split = clampSplit(r.scope_split != null ? r.scope_split : j.split);
        lastSaved[j.d] = { data: deepCopy(stored), split };
        if (j.seq === saveSeq) {                                  // nothing newer meanwhile: adopt the echo (I22)
          intent[j.d] = stored;
          if (split !== scopeSplit[j.d]) { scopeSplit[j.d] = split; emitSplit(j.d); }
          delete pending[j.d];
        }
        cacheWrite();
        putDone();
      })
      .catch((e) => {
        if (e && e.status === 400) {                              // never retry a 400 (I22)
          if (j.seq === saveSeq) delete pending[j.d];
          cacheWrite();
          toast("Layout not saved — invalid " + (e.field || "layout"), "error");
          putDone();
          return;
        }
        if (e && (e.status === 401 || e.status === 403)) { putDone(); return; }   // tr.js signs in again
        if (queued || j.seq !== saveSeq) { putDone(); return; }   // a newer snapshot supersedes this one
        if (attempt < RETRY.length) { setTimeout(() => doPut(j, attempt + 1), RETRY[attempt]); return; }
        toast("Layout not saved — will retry on next change", "warn");   // the pending copy stays in the cache
        putDone();
      });
  }
  function putNow(d) { queue(d, intent[d], clampSplit(scopeSplit[d]), 0); }   // reset Undo / a re-PUT at boot
  function emitSplit(d) {
    document.dispatchEvent(new CustomEvent("tr:split", { detail: { device: d, fraction: scopeSplit[d] } }));
  }
  function setScopeSplit(f) {                                   // app.js, at the end of a split drag (any mode, D-10)
    if (failed) return;
    scopeSplit[dev] = clampSplit(f);
    saveSoon(null);
  }

  // ---- chrome: collapse state, arrange attributes, ▲▼ ends, summaries ----
  function setIcon(btn, name) {
    const u = btn && btn.querySelector("use");
    if (u && u.getAttribute("href") !== "#i-" + name) u.setAttribute("href", "#i-" + name);
  }
  function syncChrome() {
    const eff = dev === "phone" ? effective().map((i) => i.id) : [];
    for (const id of TILE_IDS) {
      const el = tileEl[id], it = item(dev, id), coll = !!it.collapsed && !!el.gridstackNode;
      el.classList.toggle("is-collapsed", coll);
      const cb = el.querySelector(".tile-collapse");
      if (cb) {
        cb.setAttribute("aria-expanded", coll ? "false" : "true");
        cb.setAttribute("aria-label", (coll ? "Expand " : "Collapse ") + title(id));
        cb.title = coll ? "Expand" : "Collapse";
        setIcon(cb, coll ? "chevron-down" : "chevron-up");
      }
      const up = el.querySelector(".tile-up"), dn = el.querySelector(".tile-down"), i = eff.indexOf(id);
      if (up) up.disabled = dev !== "phone" || i <= 0;
      if (dn) dn.disabled = dev !== "phone" || i < 0 || i >= eff.length - 1;
      headAttrs(el);
    }
    refreshSummaries(true);
  }
  function headAttrs(el) {
    const head = el.querySelector(":scope > .tile > .tile-head"), body = el.querySelector(":scope > .tile > .tile-body");
    if (arranging) {
      head.tabIndex = 0;
      head.setAttribute("role", "button");
      head.setAttribute("aria-roledescription", "movable panel");
      head.setAttribute("aria-describedby", "arrangeHelp");
      head.setAttribute("aria-label", title(el.dataset.tile) + " panel");
      if (body) body.inert = true;
    } else {
      head.removeAttribute("tabindex");
      for (const a of ["role", "aria-roledescription", "aria-describedby", "aria-label"]) head.removeAttribute(a);
      if (body) body.inert = false;
    }
  }
  function fmtFreq(hz) {
    hz = Math.max(0, Math.round(+hz || 0));
    const mhz = Math.floor(hz / 1e6), rem = hz % 1e6;
    return mhz + "." + String(Math.floor(rem / 1000)).padStart(3, "0") + "." + String(rem % 1000).padStart(3, "0");
  }
  function summaryText(id) {
    const st = (window.RadioControl && typeof RadioControl.state === "function" && RadioControl.state()) || {};
    const txt = (eid) => { const e = $(eid); return e ? (e.textContent || "").trim() : ""; };
    switch (id) {
      case "vfo": return st.freq ? fmtFreq(st.freq) + (st.mode_name ? " " + st.mode_name : "") : "—";
      case "scope": return txt("spanVal");
      case "ptt": {
        const o = TR.tx && TR.tx.owner;
        return !o ? "RX" : (TR.isTxOwner && TR.isTxOwner() ? "ON AIR" : "BUSY");
      }
      case "audio": {
        const on = !!$("audioBtn") && $("audioBtn").classList.contains("active");
        return on ? "RX on · VOL " + (txt("volVal") || ($("vol") || {}).value || "") : "RX off";
      }
      case "controls": { const t = document.querySelector(".controls .tab.on"); return t ? t.textContent.trim() : ""; }
      case "recorder": return (window.Recorder && typeof window.Recorder.summary === "function" && window.Recorder.summary()) || "";
      case "spots": return (window.SpotsPanel && typeof window.SpotsPanel.summary === "function" && window.SpotsPanel.summary()) || "";
      case "sstv": return (window.DigitalPanel && typeof window.DigitalPanel.summary === "function" && window.DigitalPanel.summary()) ||
        (window.SstvTx && typeof window.SstvTx.summary === "function" && window.SstvTx.summary()) ||
        (window.SstvPanel && typeof window.SstvPanel.summary === "function" && window.SstvPanel.summary()) || "";
      default: return "";
    }
  }
  function refreshSummaries(all) {
    if (failed || !intent[dev]) return;
    for (const id of TILE_IDS) {
      const it = item(dev, id);
      if (!it.collapsed && !all) continue;
      const s = tileEl[id].querySelector(".tile-summary");
      if (!s) continue;
      const t = it.collapsed ? summaryText(id) : "";
      if (s.textContent !== t) s.textContent = t;
    }
  }
  setInterval(() => { if (intent[dev] && intent[dev].items.some((i) => i.collapsed)) refreshSummaries(); }, 1000);
  document.addEventListener("tr:tab", () => refreshSummaries());
  document.addEventListener("click", (e) => { if (e.target.closest("#audioBtn,#cwDecodeBtn,#cwToolBtn")) setTimeout(refreshSummaries, 0); });

  // ---- collapse (§3.11; any mode, persisted — F5) ----
  function ownsTx() {
    const tx = window.RadioTx;
    return !!(tx && typeof tx.ownedOrPending === "function" && tx.ownedOrPending()) || !!(TR.isTxOwner && TR.isTxOwner());
  }
  function setCollapsed(id, want) {
    if (failed || !grid) return;
    const it = item(dev, id), el = tileEl[id], n = el.gridstackNode;
    if (!n || !!it.collapsed === want) return;
    if (want && id === "ptt" && ownsTx()) {
      toast("Stop transmitting before collapsing the Transmit panel", "warn");
      say("Stop transmitting before collapsing the Transmit panel");
      return;
    }
    try {
      if (dev === "phone") {
        it.collapsed = want;
        applyFull();
      } else if (want) {
        if (el._src !== "auto") it.h = n.h;                     // the expanded height to come back to
        it.collapsed = true;
        gsUpdate(el, { h: HEAD_ROWS, minH: HEAD_ROWS, noResize: true });   // minH lowered in the SAME call
      } else {
        it.collapsed = false;
        const h = clamp(it.h, 1, B.desktop.h_max);
        gsUpdate(el, { h, minH: Math.min(minFor(dev, id).h, h), noResize: false });
      }
    } catch (e) { fallback("collapse", e); return; }
    syncChrome();
    saveSoon(el);
    tileEvent(id, { visible: true, collapsed: want });
    say(title(id) + " panel " + (want ? "collapsed" : "expanded"));
  }

  // ---- add / remove / make-room (§3.12) ----
  function removeTile(id) {
    if (failed || !grid) return false;
    const it = item(dev, id), el = tileEl[id], n = el.gridstackNode;
    if (!n || it.hidden) return false;
    const was = { x: n.x, y: n.y, w: n.w, h: n.h }, room = it.room;
    it.hidden = true;
    delete it.room;
    let donorEl = null;
    try {
      park(id);
      if (dev === "phone") applyFull();
      else if (room) {                                           // give the borrowed columns back
        const dEl = tileEl[room.from], d = dEl && dEl.gridstackNode;
        if (d && d.x + d.w === was.x && d.x + d.w + room.dw <= B.desktop.cols &&
            slotFreeArith(d.x + d.w, d.y, room.dw, d.h, room.from)) {
          gsUpdate(dEl, { w: d.w + room.dw });
          donorEl = dEl;
        }
      }
    } catch (e) { fallback("remove", e); return false; }
    syncChrome();
    saveSoon(donorEl);
    tileEvent(id, { visible: false });
    say(title(id) + " panel removed");
    return true;
  }
  function addTile(id) {
    if (failed || !grid) return false;
    const it = item(dev, id), el = tileEl[id];
    if (el.gridstackNode) { if (it.collapsed) setCollapsed(id, false); return true; }
    if (!gate(id)) { toast(title(id) + ": " + gateReason(id), "warn"); return false; }
    it.hidden = false;
    it.collapsed = false;                                       // an added panel always comes back open
    delete it.room;
    try {
      if (dev === "phone") applyFull();
      else {
        grid.setAnimation(false);
        const res = placeTile(el, it, "add");
        grid.setAnimation(anim(), true);
        if (res.how === "room") it.room = { from: res.room.from, dw: res.room.dw };
      }
    } catch (e) { fallback("add", e); return false; }
    syncChrome();
    saveSoon(el);
    tileEvent(id, { visible: true });
    try { el.scrollIntoView({ block: "nearest" }); } catch (_) {}
    say(title(id) + " panel added");
    return true;
  }
  function visible(id) { return !failed && !!tileEl[id] && !!tileEl[id].gridstackNode && !item(dev, id).collapsed; }
  function showTile(id) {                                      // the scope's CW button (the Modem window): add, or expand (persisted)
    if (failed || !grid || !TILE_IDS.includes(id)) return false;
    if (tileEl[id].gridstackNode) { if (item(dev, id).collapsed) setCollapsed(id, false); return true; }
    return addTile(id);
  }
  function hideTile(id) { return TILE_IDS.includes(id) ? removeTile(id) : false; }

  // ---- arrange mode (§3.10, E4) ----
  const bar = $("arrangeBar"), arrangeBtn = $("arrangeBtn"), umArrange = $("umArrange");
  function txBlocksArrange() {
    const rc = window.RadioControl, st = rc && typeof rc.state === "function" ? (rc.state() || {}) : {};
    return ownsTx() || !!st.cw_tx;
  }
  function enterArrange() {
    if (arranging || failed || !grid) return false;
    if (txBlocksArrange()) {
      toast("Stop transmitting before arranging panels", "warn");
      say("Stop transmitting before arranging panels");
      return false;
    }
    arranging = true;
    document.body.classList.add("arranging");
    try { grid.setStatic(false); } catch (e) { fallback("arrange", e); return false; }
    for (const id of TILE_IDS) headAttrs(tileEl[id]);
    if (bar) {
      $("arrangeDev").textContent = dev === "phone" ? "PHONE" : "DESKTOP";
      $("arrangeHelp").textContent = dev === "phone" ? "Hold a header to drag, or use ▲ ▼"
        : "Drag headers to move · drag edges to resize · arrow keys move · Shift+arrows resize · Esc when done";
      cancelReset();
      bar.hidden = false;
    }
    if (arrangeBtn) arrangeBtn.setAttribute("aria-pressed", "true");
    hideTips();
    syncChrome();
    say("Arranging panels. " + (dev === "phone" ? "Use the up and down buttons to reorder." : "Focus a panel header and use the arrow keys to move it."));
    return true;
  }
  function exitArrange() {
    if (!arranging) return;
    arranging = false;
    document.body.classList.remove("arranging");
    try { if (grid) grid.setStatic(true); } catch (_) {}
    closeAdd();
    cancelReset();
    if (bar) bar.hidden = true;
    for (const id of TILE_IDS) headAttrs(tileEl[id]);
    if (arrangeBtn) arrangeBtn.setAttribute("aria-pressed", "false");
    flushPut();
    say("Done arranging");
  }
  if (arrangeBtn) { arrangeBtn.hidden = false; arrangeBtn.addEventListener("click", () => (arranging ? exitArrange() : enterArrange())); }
  if (umArrange) { umArrange.hidden = false; umArrange.addEventListener("click", () => (arranging ? exitArrange() : enterArrange())); }
  if ($("arrangeDone")) $("arrangeDone").addEventListener("click", () => exitArrange());
  // safety net: this connection became the TX owner while arranging -> leave at once
  document.addEventListener("tr:tx", () => {
    if (arranging && ownsTx()) { exitArrange(); toast("Arrange mode ended — you are transmitting", "warn"); }
    refreshSummaries();
  });
  function hideTips() { for (const id of ["hoverFreq", "bandTip"]) { const e = $(id); if (e) e.hidden = true; } }

  // Escape (S6) and rotate (S4) guards; capture phase on window runs before GridStack's own handler
  window.addEventListener("keydown", (e) => {
    if ((e.key === "r" || e.key === "R") && document.querySelector(".ui-draggable-dragging")) {
      e.stopImmediatePropagation(); e.preventDefault(); return;
    }
    if (e.key !== "Escape" || !arranging) return;
    if (document.querySelector(".ui-draggable-dragging, .ui-resizable-resizing")) return;   // GridStack cancels the gesture
    if (addPanel && !addPanel.hidden) { e.preventDefault(); closeAdd(true); return; }
    if (resetOpen) { e.preventDefault(); cancelReset(true); return; }
    if ((TR.menu && TR.menu.isOpen && TR.menu.isOpen()) || document.querySelector(".tr-confirm")) return;
    e.preventDefault();
    exitArrange();
  }, true);

  // keyboard arrange on a focused header (§3.10)
  gridEl.addEventListener("keydown", (e) => {
    if (!arranging || !grid) return;
    const head = e.target.closest(".tile-head");
    if (!head || e.target !== head) return;
    const el = head.closest(".grid-stack-item"), id = el && el.dataset.tile;
    if (!id || !el.gridstackNode) return;
    if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); keyRemove(id); return; }
    if (dev === "phone") {
      const to = { ArrowUp: -1, ArrowDown: 1, Home: -Infinity, End: Infinity }[e.key];
      if (to === undefined) return;
      e.preventDefault();
      movePhone(id, to);
      return;
    }
    const dx = { ArrowLeft: -1, ArrowRight: 1 }[e.key] || 0, dy = { ArrowUp: -1, ArrowDown: 1 }[e.key] || 0;
    if (!dx && !dy) return;
    e.preventDefault();
    keyMoveDesktop(id, dx, dy, e.shiftKey);
  });
  function keyRemove(id) {
    const order = TILE_IDS.filter((t) => tileEl[t].gridstackNode && t !== id);
    if (removeTile(id) && order.length) focusHead(order[0]);
  }
  function focusHead(id) {
    const h = tileEl[id] && tileEl[id].querySelector(":scope > .tile > .tile-head");
    if (h && arranging) { try { h.focus({ preventScroll: false }); } catch (_) {} }
  }
  function keyMoveDesktop(id, dx, dy, resize) {
    const el = tileEl[id], n = el.gridstackNode, it = item(dev, id);
    let { x, y, w, h } = n;
    if (resize) {
      if (it.collapsed) { say("Expand the " + title(id) + " panel before resizing it"); return; }   // update() ignores noResize (S0 note 3)
      const m = minFor("desktop", id);
      w += dx; h += dy;
      if (w < Math.min(m.w, n.w) || h < Math.min(m.h, n.h) || w < 1 || h < 1) { say("Can't make it smaller"); return; }
      if (x + w > B.desktop.cols || h > B.desktop.h_max) { say("Can't make it bigger"); return; }
    } else { x += dx; y += dy; }
    if (x < 0 || y < 0 || x + w > B.desktop.cols || y + h > B.desktop.bottom_max || !slotFreeArith(x, y, w, h, id)) {
      say("Can't move there"); return;
    }
    try { gsUpdate(el, { x, y, w, h }); } catch (e) { fallback("keyboard arrange", e); return; }   // S0 r
    if (!resize && it.collapsed) { /* the collapsed slot moves; the expanded height stays in intent */ }
    saveSoon(el);
    focusHead(id);
    say(resize ? title(id) + " resized to " + w + " by " + h
               : title(id) + " moved to column " + (x + 1) + ", row " + (y + 1));
  }
  // phone ▲▼ / Home / End: reorder ranks among the visible tiles (AR-29), rebuild, save, refocus
  function movePhone(id, to) {
    const eff = effective(), i = eff.findIndex((t) => t.id === id);
    if (i < 0) return;
    let j = to === -Infinity ? 0 : to === Infinity ? eff.length - 1 : i + to;
    j = clamp(j, 0, eff.length - 1);
    if (j === i) { say(title(id) + " is already " + (i === 0 ? "first" : "last")); return; }
    const slots = eff.map((t) => item("phone", t.id).y).sort((a, b) => a - b);
    const ids = eff.map((t) => t.id);
    ids.splice(i, 1);
    ids.splice(j, 0, id);
    ids.forEach((t, k) => { item("phone", t).y = slots[k]; });
    try { applyFull(); } catch (e) { fallback("reorder", e); return; }
    saveSoon(tileEl[id]);
    const btn = tileEl[id].querySelector(to < 0 ? ".tile-up" : ".tile-down");
    if (btn && !btn.disabled && document.activeElement && document.activeElement.closest && document.activeElement.closest(".tile-up,.tile-down")) btn.focus();
    else focusHead(id);
    say(title(id) + " moved to position " + (j + 1) + " of " + eff.length);
  }

  // header buttons: collapse (any mode), remove (arrange), ▲▼ (phone arrange)
  function onHeadButton(e) {
    const b = e.target.closest(".tile-collapse, .tile-remove, .tile-up, .tile-down");
    if (!b) return;
    const el = b.closest(".grid-stack-item"), id = el && el.dataset.tile;
    if (!id || !el.gridstackNode) return;
    e.preventDefault();
    if (b.classList.contains("tile-collapse")) setCollapsed(id, !item(dev, id).collapsed);
    else if (b.classList.contains("tile-remove")) { if (arranging) removeTile(id); }
    else if (arranging && dev === "phone") movePhone(id, b.classList.contains("tile-up") ? -1 : 1);
  }
  gridEl.addEventListener("click", onHeadButton);

  // ---- add-panel popover (§3.12) ----
  const addPanel = $("addPanel"), addGrid = $("addPanelGrid"), addBtn = $("arrangeAdd");
  function openAdd() {
    if (!addPanel || !addGrid || !arranging) return;
    addGrid.textContent = "";
    for (const id of TILE_IDS) {
      const b = document.createElement("button");
      b.type = "button"; b.className = "ap-item"; b.dataset.tile = id;
      if (TR.icon) b.appendChild(TR.icon(TILES[id].icon));
      const l = document.createElement("span"); l.textContent = title(id); b.appendChild(l);
      let why = "";
      if (tileEl[id].gridstackNode) why = "on grid";
      else if (!gate(id)) why = gateReason(id);
      if (why) {
        b.disabled = true;
        const w = document.createElement("span"); w.className = "ap-why"; w.textContent = why; b.appendChild(w);
      }
      b.addEventListener("click", () => {
        if (b.disabled) return;
        closeAdd();
        if (addTile(id)) focusHead(id);
      });
      addGrid.appendChild(b);
    }
    addPanel.hidden = false;
    const r = addBtn ? addBtn.getBoundingClientRect() : { left: 8, top: innerHeight - 60 };
    addPanel.style.left = clamp(Math.round(r.left), 8, Math.max(8, innerWidth - addPanel.offsetWidth - 8)) + "px";
    addPanel.style.bottom = Math.round(innerHeight - r.top + 8) + "px";
    if (addBtn) addBtn.setAttribute("aria-expanded", "true");
    const first = addGrid.querySelector(".ap-item:not(:disabled)") || $("addPanelClose");
    if (first) first.focus();
  }
  function closeAdd(refocus) {
    if (!addPanel || addPanel.hidden) return;
    addPanel.hidden = true;
    if (addBtn) { addBtn.setAttribute("aria-expanded", "false"); if (refocus) addBtn.focus(); }
  }
  if (addBtn) addBtn.addEventListener("click", () => (addPanel && !addPanel.hidden ? closeAdd() : openAdd()));
  if ($("addPanelClose")) $("addPanelClose").addEventListener("click", () => closeAdd(true));
  document.addEventListener("pointerdown", (e) => {
    if (addPanel && !addPanel.hidden && !addPanel.contains(e.target) && !(addBtn && addBtn.contains(e.target))) closeAdd();
  }, true);

  // ---- reset + undo (§3.13) ----
  let resetOpen = false;
  function askReset() {
    if (!arranging) return;
    closeAdd();
    resetOpen = true;
    $("arrangeConfirmMsg").textContent = "Reset the " + dev + " layout to default?";
    $("arrangeActions").hidden = true;
    $("arrangeConfirm").hidden = false;
    $("arrangeResetNo").focus();
  }
  function cancelReset(refocus) {
    if (!$("arrangeConfirm")) return;
    const was = resetOpen;
    resetOpen = false;
    $("arrangeConfirm").hidden = true;
    $("arrangeActions").hidden = false;
    if (refocus && was && $("arrangeReset")) $("arrangeReset").focus();
  }
  async function doReset() {
    const d = dev, prev = deepCopy(intent[d]), prevSplit = scopeSplit[d];
    cancelReset();
    if (queued && queued.d === d) { clearTimeout(putT); putT = 0; queued = null; }   // the reset supersedes it
    saveSeq++;
    let r;
    try { r = await TR.api("/api/me/layout?device=" + d, { method: "DELETE" }); }
    catch (e) { toast("Reset failed: " + (e && e.message ? e.message : "error"), "error"); return; }
    if (r && isInt(r.rev)) rev = r.rev;
    saveSeq++;
    intent[d] = defaults(d);
    scopeSplit[d] = SPLIT_DEFAULT;
    lastSaved[d] = { data: deepCopy(intent[d]), split: SPLIT_DEFAULT };
    delete pending[d];
    cacheWrite();
    if (d === dev) { try { applyFull(); } catch (e) { fallback("reset", e); return; } }
    emitSplit(d);
    say("The " + d + " layout was reset to default");
    toast("Layout reset to default", "ok", { ms: 8000, force: true, action: { label: "Undo", fn: () => {
      intent[d] = prev;
      scopeSplit[d] = prevSplit;
      if (d === dev) { try { applyFull(); } catch (e) { fallback("undo", e); return; } }
      emitSplit(d);
      putNow(d);
      say("Layout restored");
    } } });
  }
  if ($("arrangeReset")) $("arrangeReset").addEventListener("click", askReset);
  if ($("arrangeResetNo")) $("arrangeResetNo").addEventListener("click", () => cancelReset(true));
  if ($("arrangeResetYes")) $("arrangeResetYes").addEventListener("click", doReset);

  // ---- GridStack events: gestures only (§3.9) ----
  function wireGrid() {
    grid.on("dragstart", (e, el) => { gestureEl = el; hideTips(); });
    grid.on("resizestart", (e, el) => { gestureEl = el; hideTips(); document.dispatchEvent(new CustomEvent("tr:resizestart")); });
    grid.on("dragstop", (e, el) => endGesture(el));
    grid.on("resizestop", (e, el) => { endGesture(el); document.dispatchEvent(new CustomEvent("tr:resizestop")); });
  }
  function endGesture(el) {
    const g = gestureEl;
    gestureEl = null;
    if (!arranging || applying || !g || failed) return;
    const n = el && el.gridstackNode;
    if (dev === "desktop" && n && n.y + n.h > B.desktop.bottom_max) {   // unbounded auto-scroll (S0 u): revert
      try { applyFull(); } catch (e) { fallback("revert", e); return; }
      toast("Panels can't go that far down", "warn");
      return;
    }
    saveSoon(el);
  }

  // ---- device switching (§3.19, E2) ----
  function switchDevice(nd) {
    if (nd === dev || failed) return;
    try {
      exitArrange();                                              // flushes the pending save
      flushPut();
      if (grid) {
        for (const el of grid.getGridItems()) if (el.gridstackNode) grid.removeWidget(el, false, false);
        for (const id of TILE_IDS) parking.appendChild(tileEl[id]);   // park FIRST: init would adopt stale gs-*
        grid.destroy(false);                                      // re-init on a live grid ignores options
      }
      dev = nd;
      document.body.dataset.device = dev;
      grid = GridStack.init(OPTS(dev), gridEl);
      wireGrid();
      applyFull();
      emitSplit(dev);
    } catch (e) { fallback("device switch", e); }
  }
  mq.addEventListener("change", () => switchDevice(mq.matches ? "desktop" : "phone"));

  // ---- public API (replaces the tr.js stub) ----
  window.RadioLayout = {
    arranging: () => arranging,
    setRadio: (p) => { try { setRadio(p); } catch (e) { fallback("setRadio", e); } },
    applyGates: () => applyGates(),
    showTile, hideTile, visible,
    setScopeSplit,
    scopeSplit: (d) => scopeSplit[d || dev],
    summaries: () => refreshSummaries(),
    enterArrange, exitArrange,
    device: () => dev,
    isFallback: () => failed,
    grid: () => grid,
  };

  // ---- boot ----
  try {
    // the first paint already knows the initial radio's receivers (boot.radios tile_caps, AR-44), so the
    // VFO tile's pixel minimum (and its phone height) is final before app.js renders the profile
    if ($("bands")) $("bands").classList.toggle("dual", caps.dual);
    if ($("rowSub")) $("rowSub").style.display = caps.dual ? "" : "none";
    const o = OPTS(dev);
    console.assert(MODES.has(o.mode), "layout mode");
    grid = GridStack.init(o, gridEl);                             // #grid is EMPTY here
    wireGrid();
    applyFull();
    emitSplit(dev);
    if (rePut) for (const d of rePut) putNow(d);
  } catch (e) {
    fallback("init", e);
  }
})();
