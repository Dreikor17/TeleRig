/* The Spots panel (frontend/spots.js), tested on the REAL tr.js + waterfall.js + freqentry.js + callfreqs.js
 * + spots.js in a Node vm on tests/fakedom.js. No browser, no server, no radio: fetch answers /api/spots
 * from the sample below (made-up callsigns) and /api/me/memories from a small stand-in for the server's
 * store, RadioControl / RadioLayout are stubs, and RadioControl.send only records. Run by
 * tests/t_c1.py (unit), once as below and once on the real profiles (backend/profiles.py to_json()):
 *
 *     node tests/spots_ui_test.js [--profiles FILE]   -> exit 0 and "ALL PASS", or exit 1 naming each failure
 *
 *  P1. asks /api/spots (the chosen sources) only while the panel is on the grid; not again before 30 s
 *  P2. the list: newest first; age, frequency, mode, call, reference + name, source; text stays text
 *  P3. a click tunes: set_freq, then the mode in this radio's names (SSB by band, CW, digital, FM) and
 *      the filter for it
 *  P4. a spot outside the radio's bands, or for a viewer, is shown but can't be clicked; the panel says why
 *  P5. the filters (sources, mode, this band) work and this browser keeps them; a silent source is named
 *  P6. the spot you are on stays at the top
 *  P7. every radio, every spot mode: the mode, the Icom DATA switch and the filter (an Icom FIL slot, a
 *      Yaesu NAR off + WIDTH, nothing on the IC-728), each only when the radio isn't there already; a
 *      spot whose mode is not known tunes the frequency and changes nothing else
 *  The Calling view (callfreqs.js):
 *  P8.  the view keys; the list: this radio's bands, lowest first; the server is asked nothing
 *  P9.  a click tunes as a spot's does; nothing is pinned or moved
 *  P10. the row the radio is on is marked, and the mark follows the radio in place (within 100 Hz)
 *  P11. the two filters work there too; the view is kept with them
 *  P12. each radio lists its own bands; a switched radio's list follows
 *  P13. a viewer sees the list and can't click it
 *  P14. the keys are a tablist (the arrow keys)      P15. each view keeps its place in the panel
 *  P16. an answer that arrives after the view changed
 *  P17. the list itself: well-formed, inside the US bands, in order, short; every radio tunes every row
 *  The Memory view (/api/me/memories):
 *  P18. the list: this radio's memories, read once, lowest first, one line each with its delete key
 *  P19. a click brings the radio back: the mode by its name, a Yaesu's NAR + WIDTH code, an Icom's FIL
 *       slot + DATA, each only when the radio isn't there already; anywhere the radio receives
 *  P20. SAVE: the radio as it is, then a name (the frequency, selected); SAVE again or Enter keeps it,
 *       Esc or the X drops it; what is kept per radio; no radio, an unknown mode, a full list, a refusal
 *  P21. delete: the X arms at the first tap and deletes at the second
 *  P22. the two filters      P23. operators only      P24. when the memories are read; a failed read
 *  P25. the mark follows the radio in place; a row drawn for another radio tunes nothing
 */
"use strict";
const fs = require("fs");
const { makeEnv } = require("./fakedom");

let failures = 0, checks = 0;
function check(cond, msg) { checks++; if (!cond) { failures++; console.log("FAIL " + msg); } }
function eq(a, b, msg) { const ja = JSON.stringify(a), jb = JSON.stringify(b); check(ja === jb, msg + ": got " + ja + ", want " + jb); }

const NOW = Date.UTC(2026, 10, 19, 14, 0, 0);
const at = (min) => new Date(NOW - min * 60000).toISOString().replace(/\.\d+Z$/, "Z");
const SPOTS = [
  { src: "pota", call: "N0CALL", hz: 14275000, mode: "SSB", ref: "US-0001", name: "Sample State Park (US-WI)", spotter: "N1CALL", time: at(2), note: "" },
  { src: "wwff", call: "F0CALL/P", hz: 7032500, mode: "CW", ref: "FFF-0001", name: "SAMPLE FOREST", spotter: "OK0CALL", time: at(3), note: "" },
  { src: "dx", call: "JA0CALL", hz: 14074000, mode: "DIGI", ref: "", name: "Japan", spotter: "K0CALL", time: at(6), note: "" },
  { src: "pota", call: "N2CALL", hz: 3850000, mode: "SSB", ref: "US-0002", name: "<b>Tag</b> Park", spotter: "N3CALL", time: at(9), note: "<img src=x>" },
  { src: "dx", call: "W0CALL", hz: 146520000, mode: "FM", ref: "", name: "United States", spotter: "K1CALL", time: at(12), note: "" },
  { src: "dx", call: "VK0CALL", hz: 10368100000, mode: "CW", ref: "", name: "Australia", spotter: "K2CALL", time: at(15), note: "" },
];
// The five radios as /api/radios gives them (backend/profiles.py to_json()), only the fields spots.js
// reads; t_c1.py runs this file again with --profiles, the real to_json() of every profile.
const LO = [["160", 1.8, 2.0], ["80", 3.5, 4.0]], YAESU60 = [["60", 5.3305, 5.4035]];
const HI = [["40", 7.0, 7.3], ["30", 10.1, 10.15], ["20", 14.0, 14.35], ["17", 18.068, 18.168], ["15", 21.0, 21.45],
            ["12", 24.89, 24.99], ["10", 28.0, 29.7]];
const bands = (...lists) => [].concat(...lists).map(([name, lo, hi]) => ({ name, lo: Math.round(lo * 1e6), hi: Math.round(hi * 1e6) }));
const caps = (o) => Object.assign({ narrow: false, ext_ops: false, data_mode: false, tab_rx: true }, o);
// rx_ranges: where the radio receives (a memory may be anywhere in it); the IC-9700's are its bands
const STUBS = [
  { id: "ic9700", name: "IC-9700", protocol: "civ", modes: ["LSB", "USB", "CW", "CW-R", "AM", "FM", "RTTY", "DV"],
    bands: bands([["144", 144, 148], ["430", 430, 450], ["1200", 1240, 1300]]), capabilities: caps({ data_mode: true }),
    rx_ranges: [[144e6, 148e6], [430e6, 450e6], [1240e6, 1300e6]] },
  { id: "ic7300mk2", name: "IC-7300MK2", protocol: "civ", modes: ["LSB", "USB", "CW", "CW-R", "RTTY", "RTTY-R", "AM", "FM"],
    bands: bands(LO, [["60", 5.3, 5.41]], HI, [["6", 50, 54]]), capabilities: caps({ data_mode: true }), rx_ranges: [[30e3, 74.8e6]] },
  { id: "ft991a", name: "FT-991A", protocol: "yaesu", modes: ["LSB", "USB", "AM", "CW-LSB", "CW-USB", "FM", "RTTY-LSB", "RTTY-USB",
      "C4FM", "DATA-LSB", "DATA-USB", "DATA-FM"],
    bands: bands(LO, YAESU60, HI, [["6", 50, 54], ["2", 144, 148], ["70", 430, 450]]), capabilities: caps({ narrow: true, ext_ops: true }),
    rx_ranges: [[30e3, 56e6], [118e6, 164e6], [420e6, 470e6]] },
  { id: "ft891", name: "FT-891", protocol: "yaesu", modes: ["LSB", "USB", "AM", "CW-LSB", "CW-USB", "FM", "RTTY-LSB", "RTTY-USB",
      "DATA-LSB", "DATA-USB"],
    bands: bands(LO, YAESU60, HI, [["6", 50, 54]]), capabilities: caps({ narrow: true, ext_ops: true }), rx_ranges: [[30e3, 56e6]] },
  { id: "ic728", name: "IC-728", protocol: "civ", modes: ["LSB", "USB", "CW", "CW-NAR", "AM", "FM"],
    bands: bands(LO, HI), capabilities: caps({ tab_rx: false }), rx_ranges: [[500e3, 30e6]] },
];
const PROFILES_ARG = process.argv.indexOf("--profiles");
const REAL = PROFILES_ARG > 0 ? JSON.parse(fs.readFileSync(process.argv[PROFILES_ARG + 1], "utf8")) : null;
const RADIOS = REAL ? STUBS.map((s) => REAL[s.id]) : STUBS;
const R = (id) => RADIOS.find((p) => p.id === id);
const FT991A = R("ft991a"), IC7300 = R("ic7300mk2");

// The account's memories as GET /api/me/memories gives them, per radio (made-up names): a Yaesu's keep
// NAR and the WIDTH code, an Icom's the FIL slot and the DATA switch.
const MEMS = () => ({
  ft991a: [
    { id: 1, hz: 146520000, mode: "FM", name: "Simplex", narrow: false },
    { id: 2, hz: 14074000, mode: "DATA-USB", name: "FT8 20 m", narrow: false, width: 17 },
    { id: 3, hz: 7200000, mode: "LSB", name: "40 m net", narrow: false, width: 0 },
    { id: 4, hz: 10000000, mode: "AM", name: "WWV", narrow: false },
  ],
  ic9700: [
    { id: 5, hz: 145230000, mode: "FM", name: "Repeater", filter: 1, data: false },
    { id: 6, hz: 144174000, mode: "USB", name: "FT8 2 m", filter: 1, data: true },
  ],
});
const copy = (x) => JSON.parse(JSON.stringify(x));

function page(opts) {
  opts = opts || {};
  const asked = [];
  const answer = opts.answer || { spots: SPOTS, sources: { pota: { ok: true }, wwff: { ok: true }, dx: { ok: true } }, now: NOW / 1000 };
  // /api/me/memories as backend/account_api.py serves it: GET (every radio's list), POST {radio, item}
  // and DELETE ?radio=&id= (that radio's list back). mem.calls records each request; mem.fail = {status,
  // body} answers the next one that way; mem.hold = [] keeps answers back until each held function is called.
  const mem = { calls: [], fail: opts.memFail || null, hold: null, db: { rev: 0, next: 100, radios: copy(opts.mem || {}) } };
  function memApi(url, init) {
    const method = (init && init.method) || "GET", call = { method, url: String(url) };
    if (method === "POST") call.body = JSON.parse(init.body);
    mem.calls.push(call);
    const reply = (status, body) => ({ ok: status >= 200 && status < 300, status, json: async () => body });
    if (mem.fail) { const f = mem.fail; mem.fail = null; return reply(f.status, f.body); }
    const db = mem.db;
    if (method === "GET") return reply(200, { rev: db.rev, radios: copy(db.radios), max: opts.memMax || 100, memories_error: false });
    if (method === "POST") {
      const l = (db.radios[call.body.radio] = db.radios[call.body.radio] || []), id = db.next++;
      l.push(Object.assign({ id }, call.body.item));
      return reply(200, { ok: true, rev: ++db.rev, id, items: copy(l) });
    }
    const q = new URLSearchParams(String(url).split("?")[1] || ""), l = db.radios[q.get("radio")] || [];
    const i = l.findIndex((m) => m.id === +q.get("id"));
    if (i >= 0) { l.splice(i, 1); db.rev++; }
    return reply(200, { ok: true, rev: db.rev, items: copy(l) });
  }
  const fetch = (url, init) => {
    if (String(url).startsWith("/api/me/memories")) {
      const r = memApi(url, init);
      return mem.hold ? new Promise((res) => mem.hold.push(() => res(r))) : Promise.resolve(r);
    }
    asked.push(String(url));
    return Promise.resolve({ ok: true, status: 200, json: async () => answer });
  };
  const e = makeEnv({ fetch, localStorage: opts.localStorage || {} });
  e.mem = mem;
  e.clock.now = NOW + 1000;
  const keys = e.el("div", { class: "spots-tabs", role: "tablist" });
  e.el("button", { type: "button", class: "spots-tab on", id: "spotsTabLive", role: "tab", "aria-selected": "true" }, keys).textContent = "LIVE";
  e.el("button", { type: "button", class: "spots-tab", id: "spotsTabCall", role: "tab", "aria-selected": "false", tabindex: "-1" }, keys).textContent = "CALLING";
  e.el("button", { type: "button", class: "spots-tab", id: "spotsTabMem", role: "tab", "aria-selected": "false", tabindex: "-1" }, keys).textContent = "MEMORY";
  e.el("p", { id: "spotsCredit" });
  e.el("p", { id: "callCredit", hidden: true });
  const bar = e.el("div", { class: "spots-bar" });
  for (const s of ["pota", "wwff", "dx"]) e.el("button", { type: "button", class: "key tog spots-src on", "data-src": s, "aria-pressed": "true" }, bar).textContent = s;
  // the Memory view's SAVE, its name field and the X (index.html: #memForm)
  e.form = e.el("form", { id: "memForm", class: "mem-form", hidden: true }, bar);
  e.saveKey = e.el("button", { type: "submit", class: "key sm", id: "memSave" }, e.form);
  e.nameEl = e.el("input", { type: "text", id: "memName", hidden: true }, e.form);
  e.cancelKey = e.el("button", { type: "button", class: "key sm", id: "memCancel", hidden: true }, e.form);
  e.selects = 0; e.selRange = null;                         // the name field's text selected: how often, and from where to where
  e.nameEl.select = () => { e.selects++; };
  e.nameEl.setSelectionRange = (a, b) => { e.selRange = [a, b]; };
  const sel = e.el("select", { id: "spotsMode" });
  for (const v of ["", "CW", "SSB", "DIGI", "FMAM"]) e.el("option", { value: v }, sel);
  sel.value = "";
  e.el("input", { id: "spotsBand", type: "checkbox" });
  e.el("p", { id: "spotsStatus" });
  e.body = e.el("div", { class: "tile-body tile-spots" });  // the panel's scrolling body
  e.el("div", { id: "spotsList" }, e.body);
  e.load("tr.js");
  e.load("waterfall.js");                                   // Passband (the Yaesu WIDTH table), as on the page
  const perms = e.perms = new Set(opts.viewer ? ["view", "listen"] : ["view", "listen", "operate"]);
  e.ctx.TR.can = (p) => perms.has(p);
  e.toasts = [];
  e.ctx.TR.toast = (msg, kind) => { e.toasts.push([msg, kind]); };
  e.vis = opts.visible !== false;
  e.ctx.RadioLayout = { visible: (id) => id === "spots" && e.vis };
  e.sent = [];
  const radio = opts.radio || FT991A;
  e.el("select", { id: "radioSel" }).value = radio.id;      // the radio picker: the profile while no radio is connected
  e.st = { connected: true, radio: radio.id, freq: 14074000, mode_name: "USB", filter: 1, data: 0, narrow: 0, width: 0 };
  e.ctx.RadioControl = { send: (o) => e.sent.push(o), state: () => e.st, radios: () => RADIOS.slice() };
  e.load("freqentry.js");                                   // FreqEntry.rangesOf: where a radio receives (a memory may be anywhere there)
  e.load("callfreqs.js");                                   // the Calling view's list, as on the page
  e.load("spots.js");
  e.asked = asked;
  e.list = e.doc.getElementById("spotsList");
  e.rows = () => e.list.querySelectorAll(".spot");
  e.text = (row) => row.querySelectorAll("span").map((x) => x.textContent);
  e.status = () => e.doc.getElementById("spotsStatus").textContent;
  e.src = (k) => e.doc.querySelector('.spots-src[data-src="' + k + '"]');
  e.click = (el) => el.dispatchEvent(new e.Event("click", { bubbles: true }));
  e.change = (el) => el.dispatchEvent(new e.Event("change", { bubbles: true }));
  e.tab = (v) => e.doc.getElementById({ live: "spotsTabLive", call: "spotsTabCall", mem: "spotsTabMem" }[v]);
  e.key = (el, key) => { const ev = new e.Event("keydown", { bubbles: true, key }); el.dispatchEvent(ev); return ev; };
  e.heads = () => e.list.querySelectorAll(".spots-bandh").map((h) => h.textContent);   // the Calling view's band names
  e.row = (freq) => e.rows().find((r) => e.text(r)[0] === freq);                       // ... its row at "14.074.0"
  e.marked = () => e.rows().filter((r) => r.classList.contains("picked")).map((r) => e.text(r)[0]);
  // the Memory view: SAVE (or Enter in the name) submits the form; a memory's row by its name; its delete key
  e.submit = () => { const ev = new e.Event("submit", { bubbles: true }); e.form.dispatchEvent(ev); return ev; };
  e.names = () => e.rows().map((r) => e.text(r)[2]);
  e.memRow = (name) => e.rows().find((r) => e.text(r)[2] === name);
  e.del = (name) => e.memRow(name).parentNode.querySelector(".mem-del");
  e.posts = () => e.mem.calls.filter((c) => c.method === "POST").map((c) => c.body);
  e.gets = () => e.mem.calls.filter((c) => c.method === "GET").length;
  return e;
}
// The Calling view's list as callfreqs.js has it: [{band, rows: [[kHz, mode, name, note]]}], read once
// from the real file in a vm (the tests below compare the page against it and against fixed landmarks).
const CALLS = (() => { const e = makeEnv({}); e.load("callfreqs.js"); return JSON.parse(JSON.stringify(e.ctx.CALL_FREQS)); })();
const inBands = (p, hz) => p.bands.some((b) => hz >= b.lo && hz <= b.hi);
const callsOf = (p) => CALLS.map((g) => ({ band: g.band, rows: g.rows.filter((r) => inBands(p, Math.round(r[0] * 1000))) })).filter((g) => g.rows.length);
const calling = (opts) => page(Object.assign({ localStorage: { "telerig.spots": JSON.stringify({ view: "call" }) } }, opts || {}));
// a page that opens on the Memory view, the account holding MEMS
const memory = (opts) => page(Object.assign({ localStorage: { "telerig.spots": JSON.stringify({ view: "mem" }) }, mem: MEMS() }, opts || {}));
const settle = async (e) => { for (let i = 0; i < 6; i++) await e.flush(); };

(async function main() {
  // P1: only while on the grid
  {
    const e = page({ visible: false });
    await settle(e);
    eq(e.asked, [], "P1 not on the grid: nothing asked");
    e.vis = true;
    e.ctx.document.dispatchEvent(new e.ctx.CustomEvent("tr:tile", { detail: { id: "spots", visible: true } }));
    await settle(e);
    eq(e.asked, ["/api/spots?src=pota%2Cwwff%2Cdx"], "P1 shown: asks once, for the chosen sources");
    e.clock.advance(20000); await settle(e);
    eq(e.asked.length, 1, "P1 not again before 30 s");
    e.clock.advance(11000); await settle(e);
    eq(e.asked.length, 2, "P1 again after 30 s");
    e.vis = false;                                          // removed from the grid (or collapsed)
    e.ctx.document.dispatchEvent(new e.ctx.CustomEvent("tr:tile", { detail: { id: "spots", visible: false } }));
    e.clock.advance(65000); await settle(e);
    eq(e.asked.length, 2, "P1 hidden again: no more asking");
  }
  // P2-P3: the list and tuning (FT-991A, operator)
  {
    const e = page();
    await settle(e);
    const rows = e.rows();
    eq(rows.length, SPOTS.length, "P2 every spot is listed");
    eq(e.text(rows[0]), ["2m", "14.275.0", "SSB", "N0CALL", "US-0001 Sample State Park (US-WI)", "POTA"], "P2 a row: age, freq, mode, call, ref + name, source");
    eq(rows.map((r) => e.text(r)[3]), SPOTS.map((s) => s.call), "P2 newest first");
    const tag = rows[3];
    eq(e.text(tag)[4], "US-0002 <b>Tag</b> Park", "P2 text from a source stays text");
    check(!tag.querySelector("b") && !tag.querySelector("img"), "P2 ... no element is made from it");
    check(/6 spots/.test(e.status()), "P2 the status counts: " + e.status());
    e.click(rows[0]);
    eq(e.sent, [{ action: "set_freq", hz: 14275000 }], "P3 20 m SSB: the freq; USB at WIDTH 00 (2.4 kHz) already, so nothing else");
    e.sent.length = 0; e.click(rows[3]);
    eq(e.sent, [{ action: "set_freq", hz: 3850000 }, { action: "set_mode", mode: "LSB" }, { action: "narrow", on: false },
                { action: "width", code: 14 }], "P3 80 m SSB -> LSB, NAR off, WIDTH 2.4 kHz");
    e.sent.length = 0; e.click(rows[1]);
    eq(e.sent, [{ action: "set_freq", hz: 7032500 }, { action: "set_mode", mode: "CW-USB" }, { action: "narrow", on: false },
                { action: "width", code: 10 }], "P3 CW on a Yaesu -> CW-USB, WIDTH 500 Hz");
    e.sent.length = 0; e.click(rows[2]);
    eq(e.sent, [{ action: "set_freq", hz: 14074000 }, { action: "set_mode", mode: "DATA-USB" }, { action: "narrow", on: false },
                { action: "width", code: 17 }], "P3 digital on a Yaesu -> DATA-USB, WIDTH 3 kHz");
    e.sent.length = 0; e.click(rows[4]);
    eq(e.sent, [{ action: "set_freq", hz: 146520000 }, { action: "set_mode", mode: "FM" }, { action: "narrow", on: false }],
       "P3 2 m FM -> FM, NAR off (its fixed filter)");
    // P4: 10 GHz is not on an FT-991A
    check(rows[5].tagName === "DIV" && rows[5].classList.contains("off"), "P4 a spot off the radio's bands is not a button");
    check(/^Not on this radio/.test(rows[5].title), "P4 ... and says why: " + rows[5].title);
    e.sent.length = 0; e.click(rows[5]);
    eq(e.sent, [], "P4 ... a click does nothing");
    eq(e.ctx.SpotsPanel.summary(), "6 spots", "the collapsed panel's summary");
    // the radio is switched before the list is drawn again: a row drawn for the FT-991A (2 m) is not
    // tuned on an HF-only IC-7300MK2
    e.st = { connected: true, radio: "ic7300mk2", freq: 14074000, mode_name: "USB" };
    e.sent.length = 0; e.click(rows[4]);
    eq(e.sent, [], "P4 a row drawn for the previous radio does not tune off the new radio's bands");
  }
  // P3 on an Icom: CW is CW on FIL2, digital is USB with the DATA switch on
  {
    const e = page({ radio: IC7300 });
    await settle(e);
    const rows = e.rows();
    e.click(rows[1]);
    eq(e.sent, [{ action: "set_freq", hz: 7032500 }, { action: "set_mode", mode: "CW" }, { action: "set_filter", filter: 2 }],
       "P3 CW on an Icom -> CW, FIL2 (500 Hz)");
    e.sent.length = 0; e.click(rows[2]);
    eq(e.sent, [{ action: "set_freq", hz: 14074000 }, { action: "data_mode", on: true }],
       "P3 digital on an Icom -> USB with DATA on (USB FIL1 already: USB-D FIL1, 3 kHz)");
    check(rows[4].tagName === "DIV", "P4 2 m is not on an IC-7300MK2");
  }
  // P4: a viewer
  {
    const e = page({ viewer: true });
    await settle(e);
    check(e.rows().every((r) => r.tagName === "DIV"), "P4 a viewer sees the spots, no buttons");
    e.click(e.rows()[0]);
    eq(e.sent, [], "P4 ... and a click sends nothing");
    check(/Operators can click a spot to tune/.test(e.status()), "P4 the panel says who can tune: " + e.status());
  }
  // P6: the spot you are on stays at the top
  {
    const ans = { spots: SPOTS.slice(), sources: { pota: { ok: true }, wwff: { ok: true }, dx: { ok: true } }, now: NOW / 1000 };
    const e = page({ answer: ans });
    await settle(e);
    e.body.scrollTop = 140;                                 // scrolled down the list to it
    e.click(e.rows()[3]);                                   // N2CALL, 3.850 SSB, the 4th newest
    let rows = e.rows();
    eq(e.text(rows[0])[3], "N2CALL", "P6 the spot clicked goes to the top");
    eq(e.body.scrollTop, 0, "P6 ... and the panel scrolls back up to show it");
    check(rows[0].classList.contains("picked") && rows[0].getAttribute("aria-current") === "true", "P6 ... marked as the one tuned to");
    eq(e.text(rows[0])[5], "Tuned", "P6 ... and says so");
    eq(rows.slice(1).map((r) => e.text(r)[3]), ["N0CALL", "F0CALL/P", "JA0CALL", "W0CALL", "VK0CALL"], "P6 the rest stay newest first");
    e.st = Object.assign({}, e.st, { freq: 3850000, mode_name: "LSB" });     // the radio got there
    e.clock.advance(31000); await settle(e);
    eq(e.text(e.rows()[0])[3], "N2CALL", "P6 still on top after a refresh");
    e.click(e.src("pota"));
    eq(e.text(e.rows()[0])[3], "N2CALL", "P6 shown even when its source is filtered out");
    e.click(e.src("pota"));
    ans.spots = SPOTS.filter((x) => x.call !== "N2CALL");   // the sources stop listing it
    e.clock.advance(31000); await settle(e);
    eq(e.text(e.rows()[0])[3], "N2CALL", "P6 still there when the sources drop it (the radio is still on it)");
    e.st = Object.assign({}, e.st, { freq: 3860000 });       // tuned away, 10 kHz
    e.clock.advance(2100); await settle(e);
    eq(e.rows().map((r) => e.text(r)[3]), ["N0CALL", "F0CALL/P", "JA0CALL", "W0CALL", "VK0CALL"], "P6 tuned away: unpinned, the list as it is");
    check(!e.rows().some((r) => r.classList.contains("picked")), "P6 ... no row marked");
  }
  {
    const e = page();
    await settle(e);
    e.click(e.rows()[3]);                                   // the radio has not reported 3.850 yet
    e.clock.advance(2100); await settle(e);
    eq(e.text(e.rows()[0])[3], "N2CALL", "P6 right after a click, before the radio reports the new frequency: still pinned");
  }
  // P5: the filters
  {
    const e = page();
    await settle(e);
    e.click(e.src("pota"));
    check(e.rows().every((r) => e.text(r)[5] !== "POTA"), "P5 POTA off: no POTA spots");
    eq(e.src("pota").getAttribute("aria-pressed"), "false", "P5 ... its key says so");
    const sel = e.doc.getElementById("spotsMode");
    sel.value = "CW"; e.change(sel);
    eq(e.rows().map((r) => e.text(r)[3]), ["F0CALL/P", "VK0CALL"], "P5 mode CW");
    sel.value = ""; e.change(sel);
    const chk = e.doc.getElementById("spotsBand");
    chk.checked = true; e.change(chk);
    eq(e.rows().map((r) => e.text(r)[3]), ["JA0CALL"], "P5 this band (20 m, POTA off)");
    const kept = JSON.parse(e.ctx.localStorage.getItem("telerig.spots"));
    eq(kept, { src: ["wwff", "dx"], mode: "", band: true, view: "live" }, "P5 the filters are kept by this browser");
    const e2 = page({ localStorage: { "telerig.spots": JSON.stringify(kept) } });
    await settle(e2);
    eq([e2.src("pota").getAttribute("aria-pressed"), e2.doc.getElementById("spotsBand").checked, e2.rows().length],
       ["false", true, 1], "P5 ... and come back on the next visit");
    for (const k of ["wwff", "dx"]) e.click(e.src(k));
    eq(JSON.parse(e.ctx.localStorage.getItem("telerig.spots")).src, ["dx"], "P5 the last source can't be turned off");
  }
  {
    const e = page({ answer: { spots: SPOTS.slice(0, 1), sources: { pota: { ok: true }, wwff: { ok: false }, dx: { ok: true } }, now: NOW / 1000 } });
    await settle(e);
    check(/WWFF is not answering/.test(e.status()), "P5 a silent source is named: " + e.status());
  }
  // P7: every radio, every spot mode. What one click sends, from the radio state given (BASE: USB on
  // FIL1, DATA off, NAR off, WIDTH 00).
  {
    const F = (hz) => ({ action: "set_freq", hz }), M = (mode) => ({ action: "set_mode", mode });
    const FIL = (filter) => ({ action: "set_filter", filter }), DATA = (on) => ({ action: "data_mode", on });
    const NAR = { action: "narrow", on: false }, W = (code) => ({ action: "width", code });
    const BASE = { mode_name: "USB", filter: 1, data: 0, narrow: 0, width: 0 };
    async function sends(id, hz, mode, st) {
      const spot = { src: "dx", call: "K0TEST", hz, mode, ref: "", name: "", spotter: "K1CALL", time: at(1), note: "" };
      const e = page({ radio: R(id), answer: { spots: [spot], sources: { pota: { ok: true }, wwff: { ok: true }, dx: { ok: true } }, now: NOW / 1000 } });
      await settle(e);
      e.st = Object.assign({ connected: true, radio: id, freq: 14074000 }, BASE, st || {});
      const row = e.rows()[0];
      check(row && row.tagName === "BUTTON", "P7 " + id + " " + (hz / 1e6) + " MHz is on the radio's bands");
      e.click(row);
      return e.sent;
    }
    const TABLE = {
      ic7300mk2: [[14285000, "SSB", [FIL(2)]], [7200000, "SSB", [M("LSB"), FIL(2)]], [14030000, "CW", [M("CW"), FIL(2)]],
                  [14074000, "DIGI", [DATA(true)]], [3885000, "AM", [M("AM")]], [29600000, "FM", [M("FM")]]],
      ic9700:    [[144300000, "SSB", [FIL(2)]], [144050000, "CW", [M("CW"), FIL(2)]], [144174000, "DIGI", [DATA(true)]],
                  [144400000, "AM", [M("AM")]], [146520000, "FM", [M("FM")]]],
      ft991a:    [[14285000, "SSB", []], [7200000, "SSB", [M("LSB"), NAR, W(14)]], [14030000, "CW", [M("CW-USB"), NAR, W(10)]],
                  [14074000, "DIGI", [M("DATA-USB"), NAR, W(17)]], [7290000, "AM", [M("AM"), NAR]], [146520000, "FM", [M("FM"), NAR]]],
      ft891:     [[14285000, "SSB", []], [7200000, "SSB", [M("LSB"), NAR, W(14)]], [14030000, "CW", [M("CW-USB"), NAR, W(10)]],
                  [14074000, "DIGI", [M("DATA-USB"), NAR, W(17)]], [7290000, "AM", [M("AM"), NAR]], [29600000, "FM", [M("FM"), NAR]]],
      ic728:     [[14285000, "SSB", []], [7200000, "SSB", [M("LSB")]], [14030000, "CW", [M("CW")]], [14074000, "DIGI", []],
                  [3885000, "AM", [M("AM")]], [29600000, "FM", [M("FM")]]],
    };
    for (const [id, rows] of Object.entries(TABLE)) {
      for (const [hz, mode, want] of rows) {
        eq(await sends(id, hz, mode), [F(hz)].concat(want), "P7 " + id + " " + mode + " " + (hz / 1e6) + " MHz from USB FIL1");
      }
      // a spot whose mode is not known: the frequency, nothing else, even with DATA / NAR / a WIDTH set
      const hz = id === "ic9700" ? 145100000 : 14100000;
      eq(await sends(id, hz, "", { data: 1, narrow: 1, width: 11, filter: 3 }), [F(hz)], "P7 " + id + " unknown mode: the frequency only");
    }
    // from other states: only what differs is sent
    const MOVES = [
      ["ic7300mk2", 14285000, "SSB", { data: 1 }, [DATA(false), FIL(2)], "USB-D -> SSB: DATA off, then FIL2"],
      ["ic7300mk2", 14074000, "DIGI", { data: 1 }, [], "USB-D FIL1 -> digital: there already"],
      ["ic7300mk2", 7074000, "DIGI", { mode_name: "LSB", filter: 2, data: 1 }, [M("USB"), FIL(1), DATA(true)],
       "LSB-D FIL2 -> digital: USB (DATA goes off with the mode), FIL1, DATA on"],
      ["ic7300mk2", 7030000, "CW", { mode_name: "CW", filter: 2 }, [], "CW FIL2 -> CW: there already"],
      ["ic7300mk2", 29600000, "FM", { mode_name: "FM", filter: 2 }, [FIL(1)], "FM on FIL2 (narrow) -> FM: FIL1"],
      ["ic7300mk2", 14285000, "SSB", { filter: 2 }, [], "USB FIL2 -> SSB: there already"],
      ["ic9700", 144300000, "SSB", { data: 1, filter: 3 }, [DATA(false), FIL(2)], "IC-9700 USB-D FIL3 -> SSB"],
      ["ic728", 14030000, "CW", { mode_name: "CW-NAR" }, [], "IC-728 in CW-NAR -> CW: stays narrow (its optional filter, never chosen for a spot)"],
      ["ic728", 14285000, "SSB", { mode_name: "CW-NAR" }, [M("USB")], "IC-728 CW-NAR -> SSB: USB (no filter command; the mode only)"],
      ["ft991a", 14285000, "SSB", { narrow: 1 }, [NAR, W(14)], "NAR on -> SSB: NAR off, WIDTH 2.4 kHz"],
      ["ft991a", 14285000, "SSB", { width: 11 }, [W(14)], "WIDTH 2.1 kHz -> SSB: WIDTH 2.4 kHz"],
      ["ft991a", 14285000, "SSB", { width: 14 }, [], "WIDTH 2.4 kHz -> SSB: there already"],
      ["ft991a", 14074000, "DIGI", { mode_name: "DATA-USB" }, [W(17)], "DATA-USB at its 500 Hz default -> digital: WIDTH 3 kHz"],
      ["ft991a", 14074000, "DIGI", { mode_name: "DATA-USB", width: 17 }, [], "DATA-USB at 3 kHz -> digital: there already"],
      ["ft991a", 14030000, "CW", { mode_name: "CW-USB", narrow: 1 }, [NAR, W(10)], "CW NAR on -> CW: NAR off, WIDTH 500 Hz"],
      ["ft991a", 146520000, "FM", { mode_name: "FM-N" }, [M("FM"), NAR], "FM-N -> FM: FM, NAR off"],
      ["ft891", 14074000, "DIGI", { mode_name: "DATA-USB", narrow: 1, width: 5 }, [NAR, W(17)], "FT-891 DATA-USB NAR -> digital"],
    ];
    for (const [id, hz, mode, st, want, what] of MOVES) eq(await sends(id, hz, mode, st), [F(hz)].concat(want), "P7 " + id + " " + what);
  }

  // ---- the Calling view (callfreqs.js) ----
  const F = (hz) => ({ action: "set_freq", hz }), M = (mode) => ({ action: "set_mode", mode });
  const FIL = (filter) => ({ action: "set_filter", filter }), DATA = (on) => ({ action: "data_mode", on });
  const NAR = { action: "narrow", on: false }, W = (code) => ({ action: "width", code });
  const HF = ["160 m", "80 m", "40 m", "30 m", "20 m", "17 m", "15 m", "12 m", "10 m"];
  const HF60 = ["160 m", "80 m", "60 m", "40 m", "30 m", "20 m", "17 m", "15 m", "12 m", "10 m"];
  const view = (e) => e.ctx.SpotsPanel.view();
  // P8: the view keys; the list: this radio's bands, lowest first; nothing asked of the server
  {
    const e = page();
    await settle(e);
    eq([view(e), e.asked.length], ["live", 1], "P8 the page opens on the live list and asks for it");
    e.click(e.tab("call"));
    eq(view(e), "call", "P8 the CALLING key shows the calling view");
    eq([e.tab("live"), e.tab("call")].map((k) => [k.classList.contains("on"), k.getAttribute("aria-selected"), k.tabIndex]),
       [[false, "false", -1], [true, "true", 0]], "P8 ... the keys say which (on, aria-selected, the roving tabindex)");
    eq(e.list.getAttribute("aria-labelledby"), "spotsTabCall", "P8 ... the list is that key's panel");
    check(["pota", "wwff", "dx"].every((k) => e.src(k).hidden), "P8 the source keys are the live list's: hidden");
    check(e.doc.getElementById("spotsCredit").hidden && !e.doc.getElementById("callCredit").hidden, "P8 the calling list's own credit shows, the spots' is hidden");
    const want = callsOf(FT991A);
    eq(e.heads(), want.map((g) => g.band), "P8 one heading per band the radio has");
    eq(e.heads(), HF60.concat(["6 m", "2 m", "70 cm"]), "P8 ... an FT-991A: 160 m to 70 cm, lowest first, no 23 cm");
    const rows = e.rows();
    eq(rows.length, want.reduce((n, g) => n + g.rows.length, 0), "P8 every frequency on those bands is listed");
    check(rows.length > 30 && rows.every((r) => r.tagName === "BUTTON" && r.classList.contains("spot-call")), "P8 each is a button");
    eq(e.text(e.row("146.520.0")), ["146.520.0", "FM", "FM calling", "National FM simplex", ""], "P8 a row: frequency, mode, name, note");
    eq(e.text(e.row("14.074.0")), ["14.074.0", "DATA", "FT8", "Dial frequency, USB", "Tuned"], "P8 the row the radio is on says Tuned");
    eq(e.row("14.074.0").title, "Tuned to FT8 14.074.0 DATA · Dial frequency, USB", "P8 ... in its title too");
    eq(e.row("146.520.0").title, "Tune to FM calling 146.520.0 FM · National FM simplex", "P8 another row's title");
    eq(e.status(), rows.length + " frequencies", "P8 the status counts them");
    eq(e.ctx.SpotsPanel.summary(), "", "P8 the folded panel shows no spot count for this view");
    const firsts = e.list.children.map((c) => (c.classList.contains("spots-bandh") ? c.textContent : e.text(c)[0]));
    eq(firsts.slice(0, 2), ["160 m", e.text(rows[0])[0]], "P8 a band's name, then its rows");
    const i20 = firsts.indexOf("20 m");
    eq(firsts.slice(i20, i20 + 8), ["20 m", "14.060.0", "14.074.0", "14.100.0", "14.230.0", "14.285.0", "14.300.0", "17 m"], "P8 ... lowest first, then the next band");
    e.clock.advance(65000); await settle(e);
    eq(e.asked.length, 1, "P8 nothing is asked of the server while the calling view shows");
    e.click(e.tab("live")); await settle(e);
    eq([e.asked.length, e.rows().length, e.heads().length], [2, SPOTS.length, 0], "P8 back on LIVE: asks again (its last answer is over 30 s old), the spots are back");
    check(["pota", "wwff", "dx"].every((k) => !e.src(k).hidden) && e.doc.getElementById("callCredit").hidden && !e.doc.getElementById("spotsCredit").hidden,
          "P8 ... with the source keys and the spots' credit");
    eq(e.list.getAttribute("aria-labelledby"), "spotsTabLive", "P8 ... the list is the LIVE key's panel again");
    eq(e.ctx.SpotsPanel.summary(), "6 spots", "P8 ... and the folded panel counts spots again");
  }
  // P9: a click tunes as a spot's does; nothing is pinned or moved
  {
    const e = calling();
    await settle(e);
    eq([view(e), e.asked], ["call", []], "P9 a page that opens on the calling view (kept by the browser) asks the server nothing");
    const order = () => e.rows().map((r) => e.text(r)[0]);
    const before = order();
    const sends = (freq) => { e.sent.length = 0; e.click(e.row(freq)); return e.sent.slice(); };
    eq(sends("146.520.0"), [F(146520000), M("FM"), NAR], "P9 FM simplex on a Yaesu: the freq, FM, NAR off");
    eq(sends("14.060.0"), [F(14060000), M("CW-USB"), NAR, W(10)], "P9 QRP CW: CW-USB, WIDTH 500 Hz");
    eq(sends("14.074.0"), [F(14074000), M("DATA-USB"), NAR, W(17)], "P9 FT8: DATA-USB, WIDTH 3 kHz");
    eq(sends("14.230.0"), [F(14230000)], "P9 20 m SSTV: USB at 2.4 kHz already, the freq only");
    eq(sends("7.171.0"), [F(7171000), M("LSB"), NAR, W(14)], "P9 40 m SSTV: LSB below 10 MHz");
    eq(sends("14.100.0"), [F(14100000), M("CW-USB"), NAR, W(10)], "P9 the beacons: CW");
    eq(order(), before, "P9 the rows stay where they are");
    eq(e.ctx.sessionStorage.getItem("telerig.spots.picked"), null, "P9 ... and nothing is pinned for the live list");
    e.clock.advance(20);
    eq(e.doc.getElementById("srLive").textContent, "Tuned to NCDXF beacons, 14.100.0", "P9 a screen reader hears where it went");
  }
  // P10: the mark follows the radio, in place
  {
    const e = calling();
    await settle(e);
    eq(e.marked(), ["14.074.0"], "P10 on 14.074: that row is marked");
    eq(e.row("14.074.0").getAttribute("aria-current"), "true", "P10 ... aria-current");
    const els = e.rows(), focus = e.row("146.520.0");
    focus.focus();
    e.st = Object.assign({}, e.st, { freq: 146520000, mode_name: "FM" });
    e.clock.advance(500);
    eq(e.marked(), ["146.520.0"], "P10 the radio moved to 146.520: the mark moved within half a second");
    eq([e.text(e.row("146.520.0"))[4], e.text(e.row("14.074.0"))[4]], ["Tuned", ""], "P10 ... and the word with it");
    eq(e.row("14.074.0").getAttribute("aria-current"), null, "P10 ... the row it left is not current");
    eq([e.row("146.520.0").title, e.row("14.074.0").title],
       ["Tuned to FM calling 146.520.0 FM · National FM simplex", "Tune to FT8 14.074.0 DATA · Dial frequency, USB"], "P10 ... the titles too");
    check(e.rows().length === els.length && e.rows().every((r, i) => r === els[i]), "P10 the rows are the same elements (the list is not rebuilt)");
    check(e.doc.activeElement === focus && focus.isConnected, "P10 ... so the keyboard focus stays on its row");
    for (const [hz, on] of [[146520100, true], [146519900, true], [146520101, false], [146519899, false]]) {
      e.st = Object.assign({}, e.st, { freq: hz }); e.clock.advance(500);
      eq(e.marked(), on ? ["146.520.0"] : [], "P10 " + hz + " Hz is " + (on ? "within" : "over") + " 100 Hz of 146.520");
    }
    e.st = Object.assign({}, e.st, { freq: 146520000 }); e.clock.advance(500);
    e.vis = false;                                          // folded, or off the grid: nothing to follow
    e.st = Object.assign({}, e.st, { freq: 14074000 }); e.clock.advance(2000);
    eq(e.marked(), ["146.520.0"], "P10 a panel that is not shown is not kept up to date");
    e.vis = true; e.clock.advance(500);
    eq(e.marked(), ["14.074.0"], "P10 ... shown again: it catches up");
    e.st = Object.assign({}, e.st, { connected: false }); e.clock.advance(500);
    eq([e.rows().length > 30, e.marked()], [true, []], "P10 no radio connected: the picked radio's list, no row marked");
    e.clock.advance(65000); await settle(e);
    check(e.rows().every((r, i) => r === els[i]), "P10 the list is not rebuilt as time passes either");
  }
  // P11: the two filters work here too; the view is kept with them
  {
    const e = calling();
    await settle(e);
    const sel = e.doc.getElementById("spotsMode"), chk = e.doc.getElementById("spotsBand");
    const modes = () => [...new Set(e.rows().map((r) => e.text(r)[1]))].sort();
    sel.value = "CW"; e.change(sel);
    check(e.rows().length > 5, "P11 mode CW: there are CW rows");
    eq(modes(), ["CW"], "P11 ... and only CW rows");
    sel.value = "FMAM"; e.change(sel);
    eq([modes(), e.heads()], [["FM"], ["10 m", "6 m", "2 m", "70 cm"]], "P11 FM / AM: the FM rows; a band with nothing left loses its heading");
    sel.value = "DIGI"; e.change(sel);
    eq(modes(), ["DATA"], "P11 data");
    sel.value = "SSB"; e.change(sel);
    eq(modes(), ["SSB"], "P11 SSB");
    sel.value = ""; e.change(sel);
    chk.checked = true; e.change(chk);
    eq([e.heads(), e.rows().map((r) => e.text(r)[0]), e.status()],
       [["20 m"], ["14.060.0", "14.074.0", "14.100.0", "14.230.0", "14.285.0", "14.300.0"], "6 frequencies"], "P11 this band: the band the radio is on");
    e.st = Object.assign({}, e.st, { freq: 146520000 }); e.clock.advance(500);
    eq(e.heads(), ["2 m"], "P11 the radio changes band: the list follows within half a second");
    sel.value = "DIGI"; e.change(sel);
    eq([e.rows().map((r) => e.text(r)[0]), e.status()], [["144.174.0"], "1 frequency"], "P11 both filters: one row, counted in the singular");
    sel.value = ""; e.change(sel);
    e.st = Object.assign({}, e.st, { freq: 10000000 }); e.clock.advance(500);
    eq([e.rows().length, e.heads(), e.status()], [0, [], "No calling frequencies match the filters."], "P11 the radio is off its bands: nothing matches, and the panel says so");
    eq(JSON.parse(e.ctx.localStorage.getItem("telerig.spots")), { src: ["pota", "wwff", "dx"], mode: "", band: true, view: "call" },
       "P11 the view is kept with the filters");
    e.click(e.tab("live")); await settle(e);
    eq(e.doc.getElementById("spotsBand").checked, true, "P11 the filters are the same ones on LIVE");
    eq(JSON.parse(e.ctx.localStorage.getItem("telerig.spots")).view, "live", "P11 ... and LIVE is kept in its turn");
  }
  // P12: each radio lists its own bands; a switched radio's list follows
  {
    const BANDS = { ic9700: ["2 m", "70 cm", "23 cm"], ic7300mk2: HF60.concat(["6 m"]), ft891: HF60.concat(["6 m"]), ic728: HF };
    for (const [id, want] of Object.entries(BANDS)) {
      const e = calling({ radio: R(id) });
      await settle(e);
      eq(e.heads(), want, "P12 " + id + ": its bands");
      eq(e.rows().length, callsOf(R(id)).reduce((n, g) => n + g.rows.length, 0), "P12 " + id + ": every frequency on them");
    }
    const e = calling();
    await settle(e);
    e.st = { connected: true, radio: "ic9700", freq: 144200000, mode_name: "USB", filter: 1, data: 0 };
    e.clock.advance(500);
    eq([e.heads(), e.marked()], [["2 m", "70 cm", "23 cm"], ["144.200.0"]], "P12 another radio is connected: its list within half a second, its row marked");
    const sends = (freq) => { e.sent.length = 0; e.click(e.row(freq)); return e.sent.slice(); };
    eq(sends("144.174.0"), [F(144174000), DATA(true)], "P12 FT8 on an Icom: USB FIL1 with DATA on");
    eq(sends("146.520.0"), [F(146520000), M("FM")], "P12 FM simplex on an Icom: FM (FIL1 already)");
    eq(sends("432.100.0"), [F(432100000), FIL(2)], "P12 70 cm SSB calling: FIL2");
    eq(sends("1296.100.0"), [F(1296100000), FIL(2)], "P12 23 cm SSB calling");
    eq(sends("1294.500.0"), [F(1294500000), M("FM")], "P12 23 cm FM simplex");
  }
  // P13: a viewer
  {
    const e = calling({ viewer: true });
    await settle(e);
    check(e.rows().length > 30 && e.rows().every((r) => r.tagName === "DIV"), "P13 a viewer sees the list, no buttons");
    e.click(e.row("14.060.0"));
    eq(e.sent, [], "P13 ... and a click sends nothing");
    check(/^\d+ frequencies Operators can click one to tune\.$/.test(e.status()), "P13 the panel says who can tune: " + e.status());
    eq([e.marked(), e.row("14.074.0").title, e.row("14.060.0").title],
       [["14.074.0"], "Tuned to FT8 14.074.0 DATA · Dial frequency, USB", "QRP calling 14.060.0 CW · Low-power CW"], "P13 the row the radio is on is marked for a viewer too");
    e.perms.add("operate"); e.clock.advance(500);
    check(e.rows().every((r) => r.tagName === "BUTTON"), "P13 made an operator: the rows are buttons within half a second");
    e.click(e.row("14.060.0"));
    eq(e.sent[0], F(14060000), "P13 ... and tune");
  }
  // P14: the keys are a tablist: the arrow keys move between the views
  {
    const e = page();
    await settle(e);
    let ev = e.key(e.tab("live"), "ArrowRight");
    check(ev.defaultPrevented && view(e) === "call" && e.doc.activeElement === e.tab("call"), "P14 ArrowRight on LIVE: the calling view, its key focused");
    ev = e.key(e.tab("call"), "ArrowRight");
    check(view(e) === "mem" && e.doc.activeElement === e.tab("mem"), "P14 ArrowRight again: the memory view");
    ev = e.key(e.tab("mem"), "ArrowRight");
    check(view(e) === "live" && e.doc.activeElement === e.tab("live"), "P14 ArrowRight on the last key wraps to the first");
    ev = e.key(e.tab("live"), "ArrowLeft");
    check(ev.defaultPrevented && view(e) === "mem" && e.doc.activeElement === e.tab("mem"), "P14 ArrowLeft on the first key wraps to the last");
    ev = e.key(e.tab("mem"), "ArrowLeft");
    check(view(e) === "call" && e.doc.activeElement === e.tab("call"), "P14 ArrowLeft: back one key");
    eq(["live", "call", "mem"].map((v) => [e.tab(v).getAttribute("aria-selected"), e.tab(v).tabIndex]), [["false", -1], ["true", 0], ["false", -1]],
       "P14 one key is selected and in the tab order");
    e.click(e.tab("live"));
    ev = e.key(e.tab("live"), "Enter");
    check(!ev.defaultPrevented && view(e) === "live", "P14 any other key is left alone");
    e.click(e.tab("live"));
    eq(view(e), "live", "P14 a click on the key already chosen changes nothing");
  }
  // P15: each view keeps its place in the panel
  {
    const e = page();
    await settle(e);
    e.body.scrollTop = 40;
    e.click(e.tab("call"));
    eq(e.body.scrollTop, 0, "P15 the calling view starts at its top");
    e.body.scrollTop = 120;
    e.click(e.tab("live"));
    eq(e.body.scrollTop, 40, "P15 LIVE is where it was left");
    e.click(e.tab("call"));
    eq(e.body.scrollTop, 120, "P15 ... and so is CALLING");
  }
  // P16: an answer that arrives after the view changed
  {
    const e = page();                                       // the first request is on its way
    e.click(e.tab("call"));
    const drawn = e.rows();
    await settle(e);
    check(e.rows().length > 30 && e.rows().every((r) => r.classList.contains("spot-call")), "P16 a late answer doesn't draw the spots over the calling list");
    check(e.rows().every((r, i) => r === drawn[i]), "P16 ... and doesn't draw the calling list again either (a focused row keeps the focus)");
    e.click(e.tab("live")); await settle(e);
    eq([e.rows().length, e.asked.length], [SPOTS.length, 1], "P16 ... the spots are there when LIVE is shown again, with no second request");
  }
  // P17: the list itself (callfreqs.js): well-formed, in the US bands, in order, reachable; every radio
  // tunes every row it lists
  {
    const US = [[1800, 2000], [3500, 4000], [5330.5, 5406.4], [7000, 7300], [10100, 10150], [14000, 14350], [18068, 18168], [21000, 21450],
                [24890, 24990], [28000, 29700], [50000, 54000], [144000, 148000], [420000, 450000], [1240000, 1300000]];
    const usBand = (khz) => US.findIndex(([lo, hi]) => khz >= lo && khz <= hi);
    check(Array.isArray(CALLS) && CALLS.length >= 10, "P17 callfreqs.js: a list of bands");
    const seen = new Set();
    let last = 0;
    for (const g of CALLS) {
      check(typeof g.band === "string" && /^\d+ (m|cm)$/.test(g.band), "P17 a band's name: " + JSON.stringify(g.band));
      check(Array.isArray(g.rows) && g.rows.length > 0, "P17 " + g.band + " has rows");
      const bands = new Set();
      for (const r of g.rows || []) {
        const [khz, mode, name, note] = r, what = "P17 " + g.band + " " + JSON.stringify(r) + ": ";
        check(r.length === 4 && typeof khz === "number" && Number.isInteger(khz * 1000) && khz > 0, what + "kHz, mode, name, note");
        check(["CW", "SSB", "DIGI", "FM"].includes(mode), what + "a spot's mode");
        // short enough to read whole in the narrowest panel (3 of 12 columns, about 250 px)
        check(typeof name === "string" && name.trim() === name && name.length >= 2 && name.length <= 14, what + "a name of at most 14 characters");
        check(typeof note === "string" && note.trim() === note && note.length >= 4 && note.length <= 34, what + "a note of at most 34 characters");
        check(usBand(khz) >= 0, what + "inside a US amateur band");
        check(khz > last, what + "above the row before it (bands and rows lowest first, no frequency twice)");
        check(!seen.has(khz), what + "listed once");
        check(RADIOS.some((p) => inBands(p, Math.round(khz * 1000))), what + "on at least one radio");
        seen.add(khz); last = khz; bands.add(usBand(khz));
      }
      eq(bands.size, 1, "P17 " + g.band + ": its rows are all in one band");
    }
    const OK = ["set_mode", "narrow", "width", "set_filter", "data_mode"];
    for (const p of RADIOS) {
      const e = calling({ radio: p });
      await settle(e);
      for (const row of e.rows()) {
        const freq = e.text(row)[0], mode = e.text(row)[1];
        e.sent.length = 0; e.click(row);
        const what = "P17 " + p.id + " " + freq + " " + mode + ": ";
        check(e.sent.length >= 1 && e.sent[0].action === "set_freq" && inBands(p, e.sent[0].hz), what + "the frequency first, on the radio's bands");
        check(e.sent.slice(1).every((m) => OK.includes(m.action)), what + "then only the mode and the filter: " + JSON.stringify(e.sent));
        const sm = e.sent.find((m) => m.action === "set_mode");
        check(!sm || p.modes.includes(sm.mode), what + "a mode the radio has");
        if (mode === "CW" || mode === "FM") check(!!sm && sm.mode.startsWith(mode), what + "from USB: the mode is set");
      }
    }
  }

  // ---- the Memory view (/api/me/memories) ----
  const NARon = { action: "narrow", on: true }, X = "×";
  const BASE = { connected: true, freq: 14074000, mode_name: "USB", filter: 1, data: 0, narrow: 0, width: 0 };
  // what a click on the memory `name` sends, from the page's state or from BASE + st
  const recalls = (e) => (name, st) => {
    if (st) e.st = Object.assign({}, BASE, { radio: e.st.radio }, st);
    e.sent.length = 0;
    e.click(e.memRow(name));
    return e.sent.slice();
  };
  // P18: the list: this radio's memories, read once, lowest first, one line each
  {
    const e = memory();
    eq([view(e), e.status(), e.form.hidden], ["mem", "Loading memories…", false], "P18 a page that opens on MEMORY: SAVE is there, the list is on its way");
    await settle(e);
    eq([e.gets(), e.mem.calls[0].url, e.asked], [1, "/api/me/memories", []], "P18 the memories are read once; the spots are not asked for");
    eq(e.names(), ["40 m net", "WWV", "FT8 20 m", "Simplex"], "P18 this radio's memories, lowest frequency first");
    eq(e.text(e.memRow("Simplex")), ["146.520.0", "FM", "Simplex", ""], "P18 a row: frequency, mode, name");
    eq(e.text(e.memRow("FT8 20 m")), ["14.074.0", "DATA-USB", "FT8 20 m", "Tuned"], "P18 the one the radio is on says Tuned");
    eq([e.memRow("FT8 20 m").title, e.memRow("40 m net").title, e.memRow("Simplex").title],
       ["Tuned to FT8 20 m 14.074.0 DATA-USB · 3 kHz", "Tune to 40 m net 7.200.0 LSB · 2.4 kHz", "Tune to Simplex 146.520.0 FM"], "P18 the titles say what a click brings back");
    check(e.rows().every((r) => r.tagName === "BUTTON" && r.parentNode.classList.contains("spot-mem")), "P18 each is a button, in a row with its delete key");
    eq(e.list.querySelectorAll(".mem-del").map((b) => [b.tagName, b.type, b.textContent, b.getAttribute("aria-label")]),
       ["40 m net", "WWV", "FT8 20 m", "Simplex"].map((n) => ["BUTTON", "button", X, "Delete " + n]), "P18 the delete keys are named");
    eq([e.status(), e.heads()], ["4 memories", []], "P18 the status counts them; no band headings here");
    check(["pota", "wwff", "dx"].every((k) => e.src(k).hidden) && e.doc.getElementById("spotsCredit").hidden && e.doc.getElementById("callCredit").hidden,
          "P18 the source keys and both credits belong to the other views");
    eq([e.nameEl.hidden, e.cancelKey.hidden, e.form.classList.contains("naming")], [true, true, false], "P18 no name is asked for until SAVE");
    eq(e.list.getAttribute("aria-labelledby"), "spotsTabMem", "P18 the list is the MEMORY key's panel");
    eq(e.memRow("WWV").tagName, "BUTTON", "P18 a memory outside the amateur bands, where the radio receives, can be clicked");
    eq(e.ctx.SpotsPanel.summary(), "", "P18 the folded panel shows no spot count for this view");
    e.clock.advance(65000); await settle(e);
    eq([e.gets(), e.asked], [1, []], "P18 nothing more is asked while the view just stays open");
  }
  // P19: a click brings the radio back: the frequency, the mode by its name, the filter as it was kept
  {
    const e = memory();
    await settle(e);
    const sends = recalls(e), order = e.names();
    eq(sends("Simplex"), [F(146520000), M("FM"), NAR], "P19 FM: the freq, FM, NAR as kept (off)");
    eq(sends("FT8 20 m"), [F(14074000), M("DATA-USB"), NAR, W(17)], "P19 DATA-USB at 3 kHz: the mode, NAR, the WIDTH code kept");
    eq(sends("40 m net"), [F(7200000), M("LSB"), NAR, W(0)], "P19 LSB at its default width: WIDTH 00 with the mode");
    eq(sends("WWV"), [F(10000000), M("AM"), NAR], "P19 AM off the ham bands: tuned (AM has no WIDTH)");
    eq(sends("FT8 20 m", { mode_name: "DATA-USB", width: 17 }), [F(14074000)], "P19 the radio is there already: the frequency only");
    eq(sends("FT8 20 m", { mode_name: "DATA-USB", width: 10 }), [F(14074000), W(17)], "P19 only the width differs: the width");
    eq(sends("FT8 20 m", { mode_name: "DATA-USB", width: 17, narrow: 1 }), [F(14074000), NAR, W(17)], "P19 NAR is on: off, and the width again (a code is another width under NAR)");
    eq(sends("Simplex", { mode_name: "FM" }), [F(146520000)], "P19 FM to an FM memory: the frequency only");
    eq(sends("Simplex", { mode_name: "FM-N" }), [F(146520000), NAR], "P19 from narrow FM (a Yaesu reports FM-N): NAR off");
    e.clock.advance(20);
    eq(e.doc.getElementById("srLive").textContent, "Tuned to Simplex, 146.520.0", "P19 a screen reader hears where it went");
    eq(e.names(), order, "P19 the rows stay where they are");
    eq(e.mem.calls.length, 1, "P19 a click asks the server nothing");
  }
  {
    const e = memory({ mem: { ft991a: [
      { id: 1, hz: 7030000, mode: "CW-USB", name: "Narrow CW", narrow: true, width: 5 },
      { id: 2, hz: 145500000, mode: "FM", name: "Narrow FM", narrow: true },
      { id: 3, hz: 3700000, mode: "XYZ", name: "Old mode" },
      { id: 4, hz: 600000000, mode: "FM", name: "Too high", narrow: false },
      { id: 5, hz: 7150000, mode: "LSB", name: "Width only", width: 14 },
    ] } });
    await settle(e);
    const sends = recalls(e);
    eq(sends("Width only", { width: 14 }), [F(7150000), M("LSB"), W(14)], "P19 a memory with a width and no NAR: the width goes with the new mode (each mode has its own)");
    eq(sends("Width only", { mode_name: "LSB", width: 14 }), [F(7150000)], "P19 ... and not when the radio is there");
    eq(sends("Narrow CW"), [F(7030000), M("CW-USB"), NARon, W(5)], "P19 a narrow CW memory: the mode, NAR on, its WIDTH code");
    eq(sends("Narrow CW", { mode_name: "CW-USB", narrow: 1, width: 5 }), [F(7030000)], "P19 ... there already");
    eq(sends("Narrow CW", { mode_name: "CW-USB", narrow: 0, width: 5 }), [F(7030000), NARon, W(5)], "P19 ... NAR off on the radio: on, and the code again");
    eq(sends("Narrow FM"), [F(145500000), M("FM"), NARon], "P19 narrow FM: FM, then NAR on");
    eq(sends("Narrow FM", { mode_name: "FM-N" }), [F(145500000)], "P19 ... the radio is in narrow FM already");
    eq(sends("Old mode"), [F(3700000)], "P19 a mode the radio's list no longer has: the frequency only");
    const far = e.memRow("Too high");
    eq([far.tagName, far.classList.contains("off"), far.title], ["DIV", true, "Not on this radio: Too high 600.000.0 FM"], "P19 a memory outside the radio's receiver is shown, not a button");
    e.sent.length = 0; e.click(far);
    eq(e.sent, [], "P19 ... and a click sends nothing");
    check(!!e.del("Too high"), "P19 ... it can still be deleted");
  }
  {
    const e = memory({ radio: R("ic9700") });                // an Icom: the FIL slot and the DATA switch
    await settle(e);
    const sends = recalls(e);
    eq(e.names(), ["FT8 2 m", "Repeater"], "P19 an IC-9700 shows its own memories");
    eq([e.text(e.memRow("FT8 2 m")), e.memRow("FT8 2 m").title, e.memRow("Repeater").title],
       [["144.174.0", "USB-D", "FT8 2 m", ""], "Tune to FT8 2 m 144.174.0 USB-D · FIL1", "Tune to Repeater 145.230.0 FM · FIL1"], "P19 DATA shows as USB-D; the title names the FIL slot");
    eq(sends("Repeater"), [F(145230000), M("FM"), FIL(1)], "P19 Icom FM: the mode, and its FIL slot again (each mode has its own)");
    eq(sends("FT8 2 m"), [F(144174000), DATA(true)], "P19 Icom USB-D from USB FIL1: DATA on");
    eq(sends("Repeater", { data: 1 }), [F(145230000), M("FM"), FIL(1)], "P19 from USB-D to FM: the mode change turns DATA off itself");
    eq(sends("FT8 2 m", { data: 1, filter: 2 }), [F(144174000), FIL(1)], "P19 USB-D on FIL2 to USB-D FIL1: the slot only");
    eq(sends("FT8 2 m", { mode_name: "LSB", filter: 2, data: 1 }), [F(144174000), M("USB"), FIL(1), DATA(true)], "P19 LSB-D to USB-D: the mode, the slot, DATA on");
    eq(sends("FT8 2 m", { data: 1 }), [F(144174000)], "P19 there already: the frequency only");
  }
  {
    const e = memory({ radio: R("ic9700"), mem: { ic9700: [{ id: 1, hz: 144300000, mode: "USB", name: "SSB", filter: 2, data: false }] } });
    await settle(e);
    eq(recalls(e)("SSB", { data: 1 }), [F(144300000), DATA(false), FIL(2)], "P19 USB-D to a plain USB memory: DATA off, then its slot");
    const e2 = memory({ radio: R("ic728"), mem: { ic728: [{ id: 1, hz: 7030000, mode: "CW-NAR", name: "CW" }] } });
    await settle(e2);
    eq([recalls(e2)("CW"), e2.memRow("CW").title], [[F(7030000), M("CW-NAR")], "Tune to CW 7.030.0 CW-NAR"], "P19 an IC-728 (no filter command): the frequency and the mode by its name");
  }
  // P20: SAVE: the radio as it is, then a name (the frequency, selected); SAVE again or Enter keeps it
  {
    const e = memory();
    await settle(e);
    e.st = Object.assign({}, BASE, { radio: "ft991a", freq: 146520000, mode_name: "FM" });
    let ev = e.submit();
    check(ev.defaultPrevented, "P20 SAVE never submits the form to the server");
    eq([e.form.classList.contains("naming"), e.nameEl.hidden, e.cancelKey.hidden, e.nameEl.value], [true, false, false, "146.520.0"],
       "P20 SAVE asks for a name: the field shows, filled with the frequency");
    eq([e.doc.activeElement === e.nameEl, e.selects, e.selRange], [true, 1, [0, 9]], "P20 ... focused, its text selected, so typing replaces it");
    eq(e.posts(), [], "P20 ... nothing is saved yet");
    e.st = Object.assign({}, e.st, { freq: 14074000, mode_name: "USB" });   // the radio moves on while the name is typed
    e.nameEl.value = "Club simplex";
    ev = e.submit();                                         // SAVE again, or Enter in the field
    check(ev.defaultPrevented, "P20 the second SAVE is handled here too");
    eq(e.posts(), [{ radio: "ft991a", item: { hz: 146520000, mode: "FM", narrow: false, name: "Club simplex" } }],
       "P20 the second SAVE keeps the radio as it was at the first, under the name typed");
    eq(e.saveKey.disabled, true, "P20 SAVE waits for the server's answer");
    await settle(e);
    eq(e.names(), ["40 m net", "WWV", "FT8 20 m", "Simplex", "Club simplex"], "P20 the new memory is in the list");
    eq([e.form.classList.contains("naming"), e.nameEl.hidden, e.cancelKey.hidden, e.nameEl.value, e.saveKey.disabled, e.doc.activeElement === e.saveKey],
       [false, true, true, "", false, true], "P20 the field is gone again and the focus is back on SAVE");
    eq(e.status(), "5 memories", "P20 the status counts it");
    e.clock.advance(20);
    eq(e.doc.getElementById("srLive").textContent, "Saved Club simplex, 146.520.0", "P20 a screen reader hears it");
    eq(e.gets(), 1, "P20 the list comes with the answer: it is not read again");
    eq(e.toasts, [], "P20 no message is needed");
  }
  {
    const e = memory();
    await settle(e);
    e.submit(); e.submit(); await settle(e);                 // SAVE, SAVE: the name as offered
    eq(e.posts()[0].item.name, "14.074.0", "P20 the frequency is the name when nothing is typed");
    const named = async (typed) => { e.submit(); e.nameEl.value = typed; e.submit(); await settle(e); return e.posts()[e.posts().length - 1].item.name; };
    eq(await named("  Net\ton\n20  "), "Net on 20", "P20 the name is trimmed; a tab or a line break becomes a space");
    eq(await named("   "), "14.074.0", "P20 a name of spaces only: the frequency");
    eq(await named("x".repeat(40)), "x".repeat(32), "P20 at most 32 characters");
    eq(await named("     " + "x".repeat(32)), "x".repeat(32), "P20 spaces before the name don't count toward the 32");
    eq(await named("w".repeat(31) + " and more"), "w".repeat(31), "P20 a cut that ends on a space leaves no space at the end");
    const face = String.fromCodePoint(0x1F600);              // one character, two UTF-16 units
    eq(await named("y".repeat(31) + face), "y".repeat(31) + face, "P20 the 32nd character may be an emoji: it is kept whole");
    eq(await named("z".repeat(32) + face), "z".repeat(32), "P20 ... and a 33rd is dropped whole, never cut in half");
    eq(await named("<b>Net</b>"), "<b>Net</b>", "P20 markup in a name is kept as typed");
    const row = e.memRow("<b>Net</b>");
    check(!!row && !row.querySelector("b") && !e.del("<b>Net</b>").querySelector("b"), "P20 ... and shown as text: no element is made from it");
  }
  {
    const e = memory();
    await settle(e);
    e.submit();
    let ev = e.key(e.nameEl, "Escape");
    eq([ev.defaultPrevented, ev._stop, e.form.classList.contains("naming"), e.nameEl.hidden, e.doc.activeElement === e.saveKey, e.posts()],
       [true, true, false, true, true, []], "P20 Esc in the name drops it: nothing saved, the focus back on SAVE, the key kept from the page");
    e.submit();
    ev = e.key(e.nameEl, "a");
    eq([ev.defaultPrevented, e.form.classList.contains("naming")], [false, true], "P20 any other key is typing");
    e.click(e.cancelKey);
    eq([e.form.classList.contains("naming"), e.posts(), e.doc.activeElement === e.saveKey], [false, [], true], "P20 the X drops it too");
    e.submit();
    e.click(e.tab("call"));
    eq([e.form.hidden, e.form.classList.contains("naming")], [true, false], "P20 another view: SAVE and the name go with the Memory view");
    e.click(e.tab("mem"));
    eq([e.form.hidden, e.form.classList.contains("naming"), e.nameEl.hidden], [false, false, true], "P20 ... and back: SAVE, no name half typed");
    e.submit(); await settle(e);
    eq(e.posts(), [], "P20 ... so one SAVE asks for the name again, it saves nothing");
  }
  // what SAVE keeps, per radio and state
  {
    const KEEPS = [
      ["ft991a", { freq: 14285000, mode_name: "USB", width: 14 }, { hz: 14285000, mode: "USB", narrow: false, width: 14 }, "USB at WIDTH 14"],
      ["ft991a", { freq: 7030000, mode_name: "CW-USB", narrow: 1, width: 5 }, { hz: 7030000, mode: "CW-USB", narrow: true, width: 5 }, "narrow CW"],
      ["ft991a", { freq: 146520000, mode_name: "FM" }, { hz: 146520000, mode: "FM", narrow: false }, "FM: no WIDTH (the mode has none)"],
      ["ft991a", { freq: 146520000, mode_name: "FM-N" }, { hz: 146520000, mode: "FM", narrow: true }, "a Yaesu's FM-N: FM, narrow"],
      ["ft991a", { freq: 14285000, mode_name: "USB", width: 99 }, { hz: 14285000, mode: "USB", narrow: false }, "a WIDTH code the mode doesn't have is not kept"],
      ["ft991a", { freq: 10000000, mode_name: "AM" }, { hz: 10000000, mode: "AM", narrow: false }, "off the ham bands"],
      ["ft891", { freq: 7074000, mode_name: "DATA-USB", width: 17 }, { hz: 7074000, mode: "DATA-USB", narrow: false, width: 17 }, "FT-891 DATA-USB"],
      ["ic9700", { freq: 144174000, mode_name: "USB", filter: 2, data: 1 }, { hz: 144174000, mode: "USB", filter: 2, data: true }, "Icom USB-D on FIL2"],
      ["ic9700", { freq: 145230000, mode_name: "FM", filter: 1 }, { hz: 145230000, mode: "FM", filter: 1, data: false }, "Icom FM"],
      ["ic9700", { freq: 145230000, mode_name: "FM", filter: 0 }, { hz: 145230000, mode: "FM", data: false }, "an Icom that reports no FIL slot: none kept"],
      ["ic7300mk2", { freq: 7030000, mode_name: "CW", filter: 3 }, { hz: 7030000, mode: "CW", filter: 3, data: false }, "IC-7300MK2 CW FIL3"],
      ["ic728", { freq: 7030000, mode_name: "CW-NAR" }, { hz: 7030000, mode: "CW-NAR" }, "IC-728: the frequency and the mode"],
    ];
    for (const [id, st, want, what] of KEEPS) {
      const e = memory({ radio: R(id) });
      await settle(e);
      e.st = Object.assign({}, BASE, { radio: id }, st);
      e.submit(); e.submit(); await settle(e);
      const name = e.nameEl.value;
      eq([e.posts(), name], [[{ radio: id, item: Object.assign({}, want, { name: e.posts()[0] ? e.posts()[0].item.name : "?" }) }], ""], "P20 " + id + " keeps " + what);
      check(/^\d+\.\d{3}\.\d$/.test(e.posts()[0] ? e.posts()[0].item.name : ""), "P20 " + id + " " + what + ": named by its frequency");
    }
  }
  // SAVE that can't: no radio, a mode that is not in the radio's list, the list full, a refusal
  {
    const e = memory();
    await settle(e);
    e.st = Object.assign({}, e.st, { connected: false });
    e.submit();
    eq([e.form.classList.contains("naming"), e.toasts], [false, [["Connect a radio first: a memory keeps the frequency it is on.", "warn"]]], "P20 no radio connected: SAVE says so and asks for no name");
    e.toasts.length = 0;
    e.st = Object.assign({}, BASE, { radio: "ft991a", mode_name: "WFM" });
    e.submit();
    eq([e.form.classList.contains("naming"), e.toasts], [false, [["This mode can't be kept as a memory.", "warn"]]], "P20 a mode the radio's list doesn't have: not kept");
    eq(e.posts(), [], "P20 ... nothing is sent either way");
    const full = memory({ memMax: 4 });
    await settle(full);
    full.submit();
    eq([full.form.classList.contains("naming"), full.toasts], [false, [["This radio's memories are full (4). Delete one first.", "warn"]]], "P20 the radio's list is full: SAVE says so");
  }
  {
    const e = memory();
    await settle(e);
    e.submit(); e.nameEl.value = "Keep me";
    e.mem.fail = { status: 409, body: { ok: false, error: "full", message: "This radio already has 100 memories." } };
    e.submit(); await settle(e);
    eq([e.toasts, e.form.classList.contains("naming"), e.nameEl.value, e.saveKey.disabled, e.names().length],
       [[["This radio already has 100 memories.", "error"]], true, "Keep me", false, 4], "P20 the server refuses: its message shows, the name stays to try again");
    e.submit(); await settle(e);
    eq([e.posts().length, e.names().includes("Keep me"), e.form.classList.contains("naming")], [2, true, false], "P20 ... and the next SAVE keeps it");
    // the answer is slow: a second press does not save twice
    e.mem.hold = [];
    e.submit(); e.nameEl.value = "Once";
    e.submit(); e.submit(); e.submit();
    await settle(e);
    eq([e.posts().length, e.saveKey.disabled], [3, true], "P20 while the answer is on its way, SAVE is off and nothing is sent twice");
    e.mem.hold.shift()(); e.mem.hold = null;
    await settle(e);
    eq([e.names().filter((n) => n === "Once").length, e.saveKey.disabled], [1, false], "P20 ... the answer comes: kept once");
  }
  // P21: delete: the X arms at the first tap, deletes at the second
  {
    const e = memory();
    await settle(e);
    const del = () => e.del("WWV"), dels = () => e.mem.calls.filter((c) => c.method === "DELETE").map((c) => c.url);
    e.click(del());
    eq([del().textContent, del().classList.contains("arm"), del().getAttribute("aria-label"), dels()], ["Delete?", true, "Delete WWV? Tap again", []],
       "P21 the first tap arms the key and deletes nothing");
    e.clock.advance(4000);
    eq([del().textContent, del().classList.contains("arm"), del().getAttribute("aria-label")], [X, false, "Delete WWV"], "P21 it lets go after 4 s");
    e.click(del());
    e.click(e.del("Simplex"));
    eq([del().classList.contains("arm"), e.del("Simplex").classList.contains("arm")], [false, true], "P21 arming another memory's key lets the first go");
    e.del("Simplex").dispatchEvent(new e.Event("blur", { bubbles: false }));
    eq(e.del("Simplex").classList.contains("arm"), false, "P21 it lets go when the focus leaves it");
    eq(dels(), [], "P21 ... and nothing was deleted by any of that");
    e.click(del()); e.click(del());
    eq(dels(), ["/api/me/memories?radio=ft991a&id=4"], "P21 the second tap deletes: this radio, this memory");
    await settle(e);
    eq([e.names(), e.status()], [["40 m net", "FT8 20 m", "Simplex"], "3 memories"], "P21 it is gone from the list");
    check(e.doc.activeElement === e.del("FT8 20 m"), "P21 the focus goes to the delete key of the memory that took its place");
    e.clock.advance(20);
    eq(e.doc.getElementById("srLive").textContent, "Deleted WWV", "P21 a screen reader hears it");
    e.mem.fail = { status: 500, body: { ok: false, error: "store_error", message: "The memories could not be saved." } };
    e.click(e.del("Simplex")); e.click(e.del("Simplex")); await settle(e);
    eq([e.names(), e.toasts], [["40 m net", "FT8 20 m", "Simplex"], [["The memories could not be saved.", "error"]]], "P21 the server fails: its message shows and the memory stays");
    for (const n of ["Simplex", "FT8 20 m", "40 m net"]) { e.click(e.del(n)); e.click(e.del(n)); await settle(e); }
    eq([e.names(), e.status(), e.doc.activeElement === e.saveKey], [[], "No memories for this radio yet. SAVE keeps the frequency it is on.", true],
       "P21 the last one deleted: the panel says how to make one, and the focus goes to SAVE");
  }
  // P22: the two filters work here too
  {
    const e = memory();
    await settle(e);
    const sel = e.doc.getElementById("spotsMode"), chk = e.doc.getElementById("spotsBand");
    const pick = (v) => { sel.value = v; e.change(sel); return e.names(); };
    eq([pick("FMAM"), pick("DIGI"), pick("SSB")], [["WWV", "Simplex"], ["FT8 20 m"], ["40 m net"]], "P22 FM / AM, data, SSB");
    eq([pick("CW"), e.status()], [[], "No memories match the filters."], "P22 no CW memory: the panel says so");
    pick("");
    chk.checked = true; e.change(chk);
    eq([e.names(), e.status()], [["FT8 20 m"], "1 memory"], "P22 this band (20 m): one, counted in the singular");
    e.st = Object.assign({}, e.st, { freq: 7100000 }); e.clock.advance(500);
    eq(e.names(), ["40 m net"], "P22 the radio changes band: the list follows within half a second");
    const i = memory({ radio: R("ic9700") });
    await settle(i);
    const isel = i.doc.getElementById("spotsMode");
    isel.value = "DIGI"; i.change(isel);
    eq(i.names(), ["FT8 2 m"], "P22 an Icom's DATA memory is data");
  }
  // P23: operators only
  {
    const e = memory({ viewer: true });
    await settle(e);
    eq([e.gets(), e.rows().length, e.status(), e.form.hidden], [0, 0, "Operators can save and recall memories.", true],
       "P23 a viewer: no SAVE, no list, nothing asked of the server, and the panel says why");
    e.submit(); await settle(e);
    eq([e.posts(), e.form.classList.contains("naming")], [[], false], "P23 ... SAVE does nothing for a viewer");
    e.perms.add("operate"); e.clock.advance(500); await settle(e);
    eq([e.gets(), e.names().length, e.form.hidden, e.status()], [1, 4, false, "4 memories"], "P23 made an operator: SAVE and the memories within half a second");
    e.perms.delete("operate"); e.clock.advance(500);
    eq([e.rows().length, e.form.hidden, e.status()], [0, true, "Operators can save and recall memories."], "P23 made a viewer again: SAVE and the list are gone within half a second");
    e.perms.add("operate"); e.clock.advance(500);
    eq([e.names().length, e.form.hidden, e.gets()], [4, false, 1], "P23 ... and back, with nothing read again");
  }
  // P24: when the memories are read; another radio; a read that fails
  {
    const e = page({ mem: MEMS() });
    await settle(e);
    eq(e.gets(), 0, "P24 on LIVE the memories are not read");
    e.click(e.tab("mem")); await settle(e);
    eq([e.gets(), e.names().length], [1, 4], "P24 the MEMORY key reads them");
    e.click(e.tab("live")); e.click(e.tab("mem")); await settle(e);
    eq(e.gets(), 1, "P24 shown again within 30 s: not read again");
    e.st = Object.assign({}, BASE, { radio: "ic9700", freq: 145230000, mode_name: "FM" }); e.clock.advance(500);
    eq([e.names(), e.marked(), e.gets()], [["FT8 2 m", "Repeater"], ["145.230.0"], 1], "P24 another radio is connected: its memories, its row marked, nothing read again");
    e.click(e.tab("live")); e.clock.advance(31000); await settle(e);
    e.click(e.tab("mem")); await settle(e);
    eq(e.gets(), 2, "P24 shown again after 30 s: read again (another device may have saved one)");
    const off = memory({ visible: false });
    await settle(off);
    eq(off.gets(), 0, "P24 a panel that is not on the grid reads nothing");
    off.vis = true;
    off.ctx.document.dispatchEvent(new off.ctx.CustomEvent("tr:tile", { detail: { id: "spots", visible: true } }));
    await settle(off);
    eq([off.gets(), off.names().length], [1, 4], "P24 ... until it is shown");
  }
  {
    const e = page({ mem: MEMS() });
    await settle(e);
    e.mem.fail = { status: 500, body: { ok: false, error: "store_error" } };
    e.click(e.tab("mem")); await settle(e);
    eq([e.rows().length, e.status(), e.form.hidden], [0, "The memories can't be read right now.", false], "P24 the read fails: the panel says so");
    e.clock.advance(5000); await settle(e);
    eq(e.gets(), 1, "P24 ... and does not ask over and over");
    e.click(e.tab("live")); e.click(e.tab("mem")); await settle(e);
    eq([e.gets(), e.names().length, e.status()], [2, 4, "4 memories"], "P24 ... it asks again the next time the view is shown");
    const bad = memory({ mem: {}, memFail: { status: 200, body: { rev: 0, radios: {}, max: 100, memories_error: true } } });
    await settle(bad);
    eq([bad.rows().length, bad.status()], [0, "The memories can't be read right now."], "P24 the server can't read the account's file: the same message");
    bad.submit(); bad.submit(); await settle(bad);
    eq([bad.names(), bad.status()], [["14.074.0"], "1 memory"], "P24 ... saving still works (the server starts the file again)");
  }
  // P25: the mark follows the radio in place; a row drawn for another radio tunes nothing
  {
    const e = memory();
    await settle(e);
    eq(e.marked(), ["14.074.0"], "P25 on 14.074: that memory is marked");
    const els = e.rows(), keys = e.list.querySelectorAll(".mem-del");
    e.del("WWV").focus();
    e.st = Object.assign({}, e.st, { freq: 146520050, mode_name: "FM" }); e.clock.advance(500);
    eq([e.marked(), e.text(e.memRow("Simplex"))[3], e.memRow("Simplex").getAttribute("aria-current"), e.memRow("FT8 20 m").getAttribute("aria-current")],
       [["146.520.0"], "Tuned", "true", null], "P25 the radio moved (within 100 Hz of 146.520): the mark moved");
    check(e.rows().every((r, i) => r === els[i]) && e.list.querySelectorAll(".mem-del").every((b, i) => b === keys[i]), "P25 the rows and keys are the same elements");
    check(e.doc.activeElement === e.del("WWV"), "P25 ... so the keyboard focus stays where it was");
    const row = e.memRow("Simplex");
    e.st = Object.assign({}, BASE, { radio: "ic9700", freq: 145230000, mode_name: "FM" });   // the radio is switched; the list is not drawn again yet
    e.sent.length = 0; e.click(row);
    eq(e.sent, [], "P25 a row drawn for the radio before tunes nothing on the new one");
    const v = memory();
    await settle(v);
    const mine = v.memRow("Simplex");
    v.perms.delete("operate");                              // made a viewer; the list is not drawn again yet
    v.click(mine);
    eq(v.sent, [], "P25 a row drawn for an operator tunes nothing once they are a viewer");
  }
  console.log(failures ? `${failures} of ${checks} checks FAILED` : `ALL PASS (${checks} checks)`);
  process.exit(failures ? 1 : 0);
})().catch((err) => { console.log("FAIL (crash) " + (err && err.stack || err)); process.exit(1); });
