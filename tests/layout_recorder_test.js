/* Layouts saved by an older TeleRig, tested on the REAL tr.js + layout.js in a Node vm on tests/fakedom.js
 * with a stand-in GridStack that records every placement (no browser, no server, no radio): where the
 * Recorder panel goes, where a CW panel goes now that CW is a mode of the Modem window, and where a Meter
 * panel's rows go now that the meter is in the VFO panel.
 * Run by tests/t_layout_ui.py (unit):
 *
 *     node tests/layout_recorder_test.js   -> exit 0 and "ALL PASS", or exit 1 naming each failure
 *
 *  L1. no saved layout: the default has the Recorder under Controls (desktop 0/18 3x6, Controls 3x18;
 *      phone right after Controls).
 *  L2. a desktop layout saved before the Recorder existed: it arrives under Controls, shown, and a
 *      full-height Controls gives up the rows it takes; every other tile stays where it was (that
 *      layout's CW panel: the Modem window in its place, F1; its Meter panel's rows: the VFO, Transmit
 *      and Audio panels above it, M1).
 *  L3. a short Controls keeps its height: the Recorder goes in the rows under it.
 *  L4. Controls hidden: the Recorder starts hidden too (like any tile new to a layout).
 *  L5. a phone layout saved before the Recorder: it comes right after Controls, the tiles after it
 *      move down one place.
 *  L6. a layout that has the Recorder (here: removed by the user) keeps it as it is.
 *  F1. a desktop layout with the CW panel showing and the Modem window not: the window takes the CW
 *      panel's place and size and the columns it borrowed (closing the window gives them back to the
 *      Scope); nothing else moves; nothing is saved until the user changes the layout, and then no CW
 *      panel is in it.
 *  F2. the Modem window showing too: the CW panel goes, its borrowed columns back to the Scope, as
 *      closing it did; the window stays where it is.
 *  F3. ... the Scope's side not free for them: the CW panel's place is left empty, nothing moves.
 *  F4. a CW panel the user had closed: nothing changes, the window stays closed.
 *  M1. a desktop layout with the Meter panel showing: its rows go to the panels right above it (inside its
 *      columns: the VFO gets the room its meter needs now); nothing else moves; the next save has no Meter
 *      panel in it.
 *  M2. nothing right above it: the panels right below it grow up into its rows.
 *  M3. a Meter panel the user had closed: nothing changes.
 *  M4. a panel above it that reaches past its columns, or a collapsed one, keeps its size.
 *  M5. phone: the panels close up, in their order (L5).
 */
"use strict";
const { makeEnv } = require("./fakedom");

let failures = 0, checks = 0;
function check(cond, msg) { checks++; if (!cond) { failures++; console.log("FAIL " + msg); } }
function eq(a, b, msg) { const ja = JSON.stringify(a), jb = JSON.stringify(b); check(ja === jb, msg + ": got " + ja + ", want " + jb); }

const IDS = ["vfo", "ptt", "audio", "scope", "controls", "recorder", "spots", "sstv"];   // the page's panels
const OLD_DESKTOP = [
  { id: "controls", x: 0, y: 0, w: 3, h: 24 }, { id: "vfo", x: 3, y: 0, w: 4, h: 5 }, { id: "ptt", x: 7, y: 0, w: 2, h: 5 },
  { id: "audio", x: 9, y: 0, w: 3, h: 5 }, { id: "meter", x: 3, y: 5, w: 9, h: 4 }, { id: "scope", x: 3, y: 9, w: 6, h: 15 },
  { id: "cw", x: 9, y: 9, w: 3, h: 15 }];
const OLD_PHONE = [{ id: "vfo", y: 0 }, { id: "ptt", y: 1 }, { id: "scope", y: 2, h: 11 }, { id: "meter", y: 3 },
  { id: "audio", y: 4 }, { id: "controls", y: 5 }, { id: "cw", y: 6, h: 10 }];

// the page as layout.js sees it: the tiles parked, a grid, the bootstrap with this user's saved layout
function boot(layout, desktop) {
  const e = makeEnv();
  e.ctx.matchMedia = (q) => ({ matches: desktop && /min-width: 900px/.test(q), addEventListener() {}, removeEventListener() {}, addListener() {} });
  e.ctx.ResizeObserver = function () { this.observe = () => {}; this.unobserve = () => {}; this.disconnect = () => {}; };
  const put = [];
  function Grid(el, opts) { this.el = el; this.opts = opts; this.items = []; }
  Grid.prototype = {
    makeWidget(el, o) { el.gridstackNode = Object.assign({ el, x: 0, y: 0 }, o); if (o.autoPosition) el.gridstackNode.auto = true; this.items.push(el); return el; },
    removeWidget(el) { delete el.gridstackNode; this.items = this.items.filter((x) => x !== el); },
    update(el, o) { Object.assign(el.gridstackNode, o); },
    getColumn() { return this.opts.column; }, getCellHeight() { return this.opts.cellHeight; }, cellHeight(h) { this.opts.cellHeight = h; },
    setAnimation() {}, getGridItems() { return this.items.slice(); }, on() {}, setStatic() {}, destroy() {},
  };
  e.ctx.GridStack = { init: (opts, el) => new Grid(el, opts) };
  e.ctx.fetch = (url, init) => { put.push([String(url), init && init.method, init && init.body]); return new Promise(() => {}); };
  const wrap = e.el("div", { id: "gridWrap" });
  e.el("div", { id: "grid", class: "grid-stack tr-grid" }, wrap);
  const park = e.el("div", { id: "tileParking", hidden: true });
  for (const id of IDS) {
    const it = e.el("div", { class: "grid-stack-item", "gs-id": id, "data-tile": id }, park);
    const sec = e.el("section", { class: "grid-stack-item-content tile" }, it);
    const head = e.el("header", { class: "tile-head" }, sec);
    e.el("h2", { class: "tile-title", id: "th-" + id }, head).textContent = id.toUpperCase();
    e.el("span", { class: "tile-summary" }, head);
    const acts = e.el("div", { class: "tile-actions" }, head);
    for (const c of ["tile-up", "tile-down", "tile-collapse", "tile-remove"]) e.el("button", { type: "button", class: "tile-btn " + c }, acts);
    e.el("div", { class: "tile-fit" }, e.el("div", { class: "tile-body" }, sec));
  }
  e.el("script", { id: "tr-boot", type: "application/json" }).textContent = JSON.stringify({
    v: 1, user: "OP1", role: "operator", perms: ["view", "listen", "operate", "tx"], port: 8796, radios: [], layout });
  e.load("tr.js");
  e.load("layout.js");
  e.domReady();
  e.clock.advance(100);
  e.at = (id) => { const n = e.doc.querySelector('.grid-stack-item[data-tile="' + id + '"]').gridstackNode;
                   return n ? (desktop ? [n.x, n.y, n.w, n.h] : n.y) : null; };
  e.put = put;
  return e;
}

// L1: the defaults
{
  const d = boot(null, true);
  eq([d.at("controls"), d.at("recorder")], [[0, 0, 3, 18], [0, 18, 3, 6]], "L1 desktop default: the Recorder under Controls");
  eq(d.at("sstv"), null, "L1 desktop default: the Modem window still starts hidden");
  const p = boot(null, false);
  check(p.at("recorder") !== null && p.at("recorder") > p.at("controls") && (p.at("sstv") === null),
        "L1 phone default: the Recorder right after Controls: " + JSON.stringify([p.at("controls"), p.at("recorder")]));
  const order = IDS.filter((id) => p.at(id) !== null).sort((a, b) => p.at(a) - p.at(b));
  eq(order, ["vfo", "ptt", "scope", "audio", "controls", "recorder"], "L1 phone default order");
  eq([d.at("vfo"), d.at("ptt"), d.at("audio"), d.at("scope")], [[3, 0, 4, 7], [7, 0, 2, 7], [9, 0, 3, 7], [3, 7, 9, 17]],
     "L1 desktop default: the VFO (with its meter), Transmit and Audio 7 rows, the Scope under them");
}
// L2: a desktop layout saved before the Recorder
{
  const d = boot({ rev: 4, desktop: { column: 12, items: OLD_DESKTOP } }, true);
  eq([d.at("controls"), d.at("recorder")], [[0, 0, 3, 18], [0, 18, 3, 6]], "L2 the Recorder arrives under a full-height Controls");
  for (const it of OLD_DESKTOP.filter((i) => i.id !== "controls" && i.id !== "meter")) {
    const id = it.id === "cw" ? "sstv" : it.id;                // the CW panel's place: the Modem window's (F1)
    const h = ["vfo", "ptt", "audio"].includes(id) ? it.h + 4 : it.h;   // the Meter panel's 4 rows (M1)
    eq(d.at(id), [it.x, it.y, it.w, h], "L2 " + it.id + (h !== it.h ? " takes the Meter panel's rows" : " stays where it was") +
       (id !== it.id ? " (as the Modem window)" : ""));
  }
  eq(d.put.filter((p) => /layout/.test(p[0])), [], "L2 nothing is saved until the user changes the layout");
}
// L3: a short Controls keeps its height
{
  const items = OLD_DESKTOP.map((i) => (i.id === "controls" ? Object.assign({}, i, { h: 14 }) : i));
  const d = boot({ rev: 4, desktop: { column: 12, items } }, true);
  eq([d.at("controls"), d.at("recorder")], [[0, 0, 3, 14], [0, 14, 3, 6]], "L3 a short Controls keeps its height; the Recorder goes under it");
}
// L4: Controls hidden -> the Recorder starts hidden
{
  const items = OLD_DESKTOP.map((i) => (i.id === "controls" ? Object.assign({}, i, { hidden: true }) : i));
  const d = boot({ rev: 4, desktop: { column: 12, items } }, true);
  eq([d.at("controls"), d.at("recorder")], [null, null], "L4 with Controls hidden the Recorder starts hidden too");
}
// L5: a phone layout saved before the Recorder
{
  const p = boot({ rev: 4, phone: { column: 1, items: OLD_PHONE } }, false);
  const order = IDS.filter((id) => p.at(id) !== null).sort((a, b) => p.at(a) - p.at(b));
  eq(order, ["vfo", "ptt", "scope", "audio", "controls", "recorder", "sstv"],
     "L5 phone: right after Controls; the CW panel's place, the Modem window's now, moves down one; the Meter panel's closes up (M5)");
}
// L6: the user removed the Recorder: it stays removed
{
  const items = OLD_DESKTOP.concat([{ id: "recorder", x: 0, y: 18, w: 3, h: 6, hidden: true }]);
  const d = boot({ rev: 5, desktop: { column: 12, items } }, true);
  eq([d.at("controls"), d.at("recorder")], [[0, 0, 3, 24], null], "L6 a removed Recorder stays removed, Controls keeps its rows");
}

// F1-F4: a layout saved while CW had a panel of its own (now a mode of the Modem window, "sstv")
const TOP = [{ id: "vfo", x: 3, y: 0, w: 4, h: 5 }, { id: "ptt", x: 7, y: 0, w: 2, h: 5 }, { id: "audio", x: 9, y: 0, w: 3, h: 5 },
  { id: "meter", x: 3, y: 5, w: 9, h: 4 }];
const LEFT = [{ id: "controls", x: 0, y: 0, w: 3, h: 18 }, { id: "recorder", x: 0, y: 18, w: 3, h: 6 }];
const CW = { id: "cw", x: 9, y: 9, w: 3, h: 15, room: { from: "scope", dw: 3 } };   // shown: 3 columns from the Scope
const stays = (d, items, tag) => { for (const it of items) eq(d.at(it.id), [it.x, it.y, it.w, it.h], tag + " " + it.id + " stays where it was"); };
const TOP_NOW = TOP.filter((i) => i.id !== "meter").map((i) => Object.assign({}, i, { h: i.h + 4 }));   // with the Meter panel's rows (M1)
{
  const items = LEFT.concat(TOP, [{ id: "scope", x: 3, y: 9, w: 6, h: 15 }, CW,
    { id: "spots", x: 9, y: 9, w: 3, h: 15, hidden: true }, { id: "sstv", x: 9, y: 9, w: 3, h: 15, hidden: true }]);
  const d = boot({ rev: 9, desktop: { column: 12, items } }, true);
  eq(d.at("sstv"), [9, 9, 3, 15], "F1 the Modem window takes the CW panel's place and size");
  stays(d, LEFT.concat(TOP_NOW, [{ id: "scope", x: 3, y: 9, w: 6, h: 15 }]), "F1");
  eq(d.at("spots"), null, "F1 Spots stays closed");
  eq(d.put.filter((p) => /layout/.test(p[0])), [], "F1 nothing is saved until the user changes the layout");
  check(d.ctx.RadioLayout.hideTile("sstv") === true, "F1 the window closes");
  eq(d.at("scope"), [3, 9, 9, 15], "F1 ... and gives the columns the CW panel borrowed back to the Scope");
  d.clock.advance(400);
  const saved = d.put.filter((p) => /layout/.test(p[0]) && p[1] === "PUT").map((p) => JSON.parse(p[2]));
  eq(saved.map((s) => [s.device, s.data.items.some((i) => i.id === "cw"), (s.data.items.find((i) => i.id === "sstv") || {}).hidden]),
     [["desktop", false, true]], "F1 the next save: no CW panel in it, the window closed");
}
{
  const win = { id: "sstv", x: 0, y: 0, w: 3, h: 24 };
  const items = [win, { id: "controls", x: 0, y: 0, w: 3, h: 18, hidden: true }, { id: "recorder", x: 0, y: 18, w: 3, h: 6, hidden: true }]
    .concat(TOP, [{ id: "scope", x: 3, y: 9, w: 6, h: 15 }, CW]);
  const d = boot({ rev: 9, desktop: { column: 12, items } }, true);
  eq(d.at("scope"), [3, 9, 9, 15], "F2 the Modem window showing too: the CW panel goes, its columns back to the Scope");
  stays(d, [win].concat(TOP_NOW), "F2");
}
{
  const win = { id: "sstv", x: 0, y: 0, w: 3, h: 24 }, spots = { id: "spots", x: 9, y: 18, w: 3, h: 6 };
  const items = [win, { id: "controls", x: 0, y: 0, w: 3, h: 18, hidden: true }, { id: "recorder", x: 0, y: 18, w: 3, h: 6, hidden: true }]
    .concat(TOP, [{ id: "scope", x: 3, y: 9, w: 6, h: 15 }, Object.assign({}, CW, { h: 9 }), spots]);
  const d = boot({ rev: 9, desktop: { column: 12, items } }, true);
  stays(d, [win, spots, { id: "scope", x: 3, y: 9, w: 6, h: 15 }].concat(TOP_NOW), "F3 Spots beside the Scope: the CW panel's place stays empty;");
}
{
  const items = LEFT.concat(TOP, [{ id: "scope", x: 3, y: 9, w: 9, h: 15 }, { id: "cw", x: 9, y: 9, w: 3, h: 15, hidden: true }]);
  const d = boot({ rev: 9, desktop: { column: 12, items } }, true);
  eq(d.at("sstv"), null, "F4 a CW panel the user had closed: the Modem window stays closed");
  stays(d, LEFT.concat(TOP_NOW, [{ id: "scope", x: 3, y: 9, w: 9, h: 15 }]), "F4");
}

// M1-M5: a layout saved while the meter had a panel of its own (now along the bottom of the VFO panel)
{
  const items = LEFT.concat(TOP, [{ id: "scope", x: 3, y: 9, w: 9, h: 15 }]);
  const d = boot({ rev: 9, desktop: { column: 12, items } }, true);
  stays(d, TOP_NOW, "M1 the Meter panel's rows go to the panel right above it:");
  stays(d, LEFT.concat([{ id: "scope", x: 3, y: 9, w: 9, h: 15 }]), "M1");
  eq(d.put.filter((p) => /layout/.test(p[0])), [], "M1 nothing is saved until the user changes the layout");
  check(d.ctx.RadioLayout.hideTile("audio") === true, "M1 a change (Audio closed)");
  d.clock.advance(400);
  const saved = d.put.filter((p) => /layout/.test(p[0]) && p[1] === "PUT").map((p) => JSON.parse(p[2]));
  eq(saved.map((sv) => [sv.device, sv.data.items.some((i) => i.id === "meter"), (sv.data.items.find((i) => i.id === "vfo") || {}).h]),
     [["desktop", false, 9]], "M1 the next save: no Meter panel in it, the VFO with its rows");
}
{
  const items = LEFT.concat([{ id: "meter", x: 3, y: 0, w: 9, h: 4 }, { id: "scope", x: 3, y: 4, w: 9, h: 20 },
    { id: "vfo", x: 0, y: 0, w: 3, h: 6, hidden: true }, { id: "ptt", x: 0, y: 0, w: 2, h: 5, hidden: true },
    { id: "audio", x: 0, y: 0, w: 3, h: 5, hidden: true }]);
  const d = boot({ rev: 9, desktop: { column: 12, items } }, true);
  eq(d.at("scope"), [3, 0, 9, 24], "M2 nothing above it: the Scope below grows up into the Meter panel's rows");
}
{
  const items = LEFT.concat(TOP.map((i) => (i.id === "meter" ? Object.assign({}, i, { hidden: true }) : i)),
    [{ id: "scope", x: 3, y: 9, w: 9, h: 15 }]);
  const d = boot({ rev: 9, desktop: { column: 12, items } }, true);
  stays(d, TOP.filter((i) => i.id !== "meter").concat([{ id: "scope", x: 3, y: 9, w: 9, h: 15 }]), "M3 a closed Meter panel: nothing changes;");
}
{
  const items = [{ id: "controls", x: 0, y: 0, w: 4, h: 5 }, { id: "recorder", x: 0, y: 18, w: 3, h: 6 },
    { id: "vfo", x: 4, y: 0, w: 4, h: 5, collapsed: true }, { id: "ptt", x: 8, y: 0, w: 4, h: 5 },
    { id: "meter", x: 3, y: 5, w: 9, h: 4 }, { id: "scope", x: 3, y: 9, w: 9, h: 15 }, { id: "audio", x: 0, y: 5, w: 3, h: 5 }];
  const d = boot({ rev: 9, desktop: { column: 12, items } }, true);
  eq(d.at("controls"), [0, 0, 4, 5], "M4 a panel above that reaches past the Meter panel's columns keeps its size");
  eq(d.at("vfo"), [4, 0, 4, 2], "M4 ... and a collapsed one too (its header slot)");
  eq(d.at("ptt"), [8, 0, 4, 9], "M4 ... while the one inside its columns takes its rows");
}

console.log(failures ? `${failures} of ${checks} checks FAILED` : `ALL PASS (${checks} checks)`);
process.exit(failures ? 1 : 0);
