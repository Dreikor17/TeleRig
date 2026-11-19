/* Where the Recorder panel goes, tested on the REAL tr.js + layout.js in a Node vm on tests/fakedom.js
 * with a stand-in GridStack that records every placement (no browser, no server, no radio).
 * Run by tests/t_layout_ui.py (unit):
 *
 *     node tests/layout_recorder_test.js   -> exit 0 and "ALL PASS", or exit 1 naming each failure
 *
 *  L1. no saved layout: the default has the Recorder under Controls (desktop 0/18 3x6, Controls 3x18;
 *      phone right after Controls).
 *  L2. a desktop layout saved before the Recorder existed: it arrives under Controls, shown, and a
 *      full-height Controls gives up the rows it takes; every other tile stays where it was.
 *  L3. a short Controls keeps its height: the Recorder goes in the rows under it.
 *  L4. Controls hidden: the Recorder starts hidden too (like any tile new to a layout).
 *  L5. a phone layout saved before the Recorder: it comes right after Controls, the tiles after it
 *      move down one place.
 *  L6. a layout that has the Recorder (here: removed by the user) keeps it as it is.
 */
"use strict";
const { makeEnv } = require("./fakedom");

let failures = 0, checks = 0;
function check(cond, msg) { checks++; if (!cond) { failures++; console.log("FAIL " + msg); } }
function eq(a, b, msg) { const ja = JSON.stringify(a), jb = JSON.stringify(b); check(ja === jb, msg + ": got " + ja + ", want " + jb); }

const IDS = ["vfo", "ptt", "audio", "meter", "scope", "controls", "cw", "recorder", "spots", "sstv"];
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
  e.ctx.fetch = (url, init) => { put.push([String(url), init && init.method]); return new Promise(() => {}); };
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
  eq(d.at("cw"), null, "L1 desktop default: CW still starts hidden");
  const p = boot(null, false);
  check(p.at("recorder") !== null && p.at("recorder") > p.at("controls") && (p.at("cw") === null),
        "L1 phone default: the Recorder right after Controls: " + JSON.stringify([p.at("controls"), p.at("recorder")]));
  const order = IDS.filter((id) => p.at(id) !== null).sort((a, b) => p.at(a) - p.at(b));
  eq(order, ["vfo", "ptt", "scope", "meter", "audio", "controls", "recorder"], "L1 phone default order");
}
// L2: a desktop layout saved before the Recorder
{
  const d = boot({ rev: 4, desktop: { column: 12, items: OLD_DESKTOP } }, true);
  eq([d.at("controls"), d.at("recorder")], [[0, 0, 3, 18], [0, 18, 3, 6]], "L2 the Recorder arrives under a full-height Controls");
  for (const it of OLD_DESKTOP.filter((i) => i.id !== "controls"))
    eq(d.at(it.id), [it.x, it.y, it.w, it.h], "L2 " + it.id + " stays where it was");
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
  eq(order, ["vfo", "ptt", "scope", "meter", "audio", "controls", "recorder", "cw"], "L5 phone: right after Controls, CW moves down one");
}
// L6: the user removed the Recorder: it stays removed
{
  const items = OLD_DESKTOP.concat([{ id: "recorder", x: 0, y: 18, w: 3, h: 6, hidden: true }]);
  const d = boot({ rev: 5, desktop: { column: 12, items } }, true);
  eq([d.at("controls"), d.at("recorder")], [[0, 0, 3, 24], null], "L6 a removed Recorder stays removed, Controls keeps its rows");
}

console.log(failures ? `${failures} of ${checks} checks FAILED` : `ALL PASS (${checks} checks)`);
process.exit(failures ? 1 : 0);
