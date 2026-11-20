/* The Spots panel (frontend/spots.js), tested on the REAL tr.js + waterfall.js + spots.js in a Node vm on
 * tests/fakedom.js. No browser, no server, no radio: fetch answers /api/spots from the sample below
 * (made-up callsigns), RadioControl / RadioLayout are stubs, and RadioControl.send only records. Run by
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
const STUBS = [
  { id: "ic9700", name: "IC-9700", protocol: "civ", modes: ["LSB", "USB", "CW", "CW-R", "AM", "FM", "RTTY", "DV"],
    bands: bands([["144", 144, 148], ["430", 430, 450], ["1200", 1240, 1300]]), capabilities: caps({ data_mode: true }) },
  { id: "ic7300mk2", name: "IC-7300MK2", protocol: "civ", modes: ["LSB", "USB", "CW", "CW-R", "RTTY", "RTTY-R", "AM", "FM"],
    bands: bands(LO, [["60", 5.3, 5.41]], HI, [["6", 50, 54]]), capabilities: caps({ data_mode: true }) },
  { id: "ft991a", name: "FT-991A", protocol: "yaesu", modes: ["LSB", "USB", "AM", "CW-LSB", "CW-USB", "FM", "RTTY-LSB", "RTTY-USB",
      "C4FM", "DATA-LSB", "DATA-USB", "DATA-FM"],
    bands: bands(LO, YAESU60, HI, [["6", 50, 54], ["2", 144, 148], ["70", 430, 450]]), capabilities: caps({ narrow: true, ext_ops: true }) },
  { id: "ft891", name: "FT-891", protocol: "yaesu", modes: ["LSB", "USB", "AM", "CW-LSB", "CW-USB", "FM", "RTTY-LSB", "RTTY-USB",
      "DATA-LSB", "DATA-USB"],
    bands: bands(LO, YAESU60, HI, [["6", 50, 54]]), capabilities: caps({ narrow: true, ext_ops: true }) },
  { id: "ic728", name: "IC-728", protocol: "civ", modes: ["LSB", "USB", "CW", "CW-NAR", "AM", "FM"],
    bands: bands(LO, HI), capabilities: caps({ tab_rx: false }) },
];
const PROFILES_ARG = process.argv.indexOf("--profiles");
const REAL = PROFILES_ARG > 0 ? JSON.parse(fs.readFileSync(process.argv[PROFILES_ARG + 1], "utf8")) : null;
const RADIOS = REAL ? STUBS.map((s) => REAL[s.id]) : STUBS;
const R = (id) => RADIOS.find((p) => p.id === id);
const FT991A = R("ft991a"), IC7300 = R("ic7300mk2");

function page(opts) {
  opts = opts || {};
  const asked = [];
  const answer = opts.answer || { spots: SPOTS, sources: { pota: { ok: true }, wwff: { ok: true }, dx: { ok: true } }, now: NOW / 1000 };
  const fetch = (url) => { asked.push(String(url)); return Promise.resolve({ ok: true, status: 200, json: async () => answer }); };
  const e = makeEnv({ fetch, localStorage: opts.localStorage || {} });
  e.clock.now = NOW + 1000;
  const bar = e.el("div", { class: "spots-bar" });
  for (const s of ["pota", "wwff", "dx"]) e.el("button", { type: "button", class: "key tog spots-src on", "data-src": s, "aria-pressed": "true" }, bar).textContent = s;
  const sel = e.el("select", { id: "spotsMode" });
  for (const v of ["", "CW", "SSB", "DIGI", "FMAM"]) e.el("option", { value: v }, sel);
  sel.value = "";
  e.el("input", { id: "spotsBand", type: "checkbox" });
  e.el("p", { id: "spotsStatus" });
  e.body = e.el("div", { class: "tile-body tile-spots" });  // the panel's scrolling body
  e.el("div", { id: "spotsList" }, e.body);
  e.load("tr.js");
  e.load("waterfall.js");                                   // Passband (the Yaesu WIDTH table), as on the page
  const perms = new Set(opts.viewer ? ["view", "listen"] : ["view", "listen", "operate"]);
  e.ctx.TR.can = (p) => perms.has(p);
  e.vis = opts.visible !== false;
  e.ctx.RadioLayout = { visible: (id) => id === "spots" && e.vis };
  e.sent = [];
  const radio = opts.radio || FT991A;
  e.st = { connected: true, radio: radio.id, freq: 14074000, mode_name: "USB", filter: 1, data: 0, narrow: 0, width: 0 };
  e.ctx.RadioControl = { send: (o) => e.sent.push(o), state: () => e.st, radios: () => RADIOS.slice() };
  e.load("spots.js");
  e.asked = asked;
  e.rows = () => e.doc.getElementById("spotsList").querySelectorAll(".spot");
  e.text = (row) => row.querySelectorAll("span").map((x) => x.textContent);
  e.status = () => e.doc.getElementById("spotsStatus").textContent;
  e.src = (k) => e.doc.querySelector('.spots-src[data-src="' + k + '"]');
  e.click = (el) => el.dispatchEvent(new e.Event("click", { bubbles: true }));
  e.change = (el) => el.dispatchEvent(new e.Event("change", { bubbles: true }));
  return e;
}
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
    eq(kept, { src: ["wwff", "dx"], mode: "", band: true }, "P5 the filters are kept by this browser");
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
  console.log(failures ? `${failures} of ${checks} checks FAILED` : `ALL PASS (${checks} checks)`);
  process.exit(failures ? 1 : 0);
})().catch((err) => { console.log("FAIL (crash) " + (err && err.stack || err)); process.exit(1); });
