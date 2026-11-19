/* The Spots panel (frontend/spots.js), tested on the REAL tr.js + spots.js in a Node vm on tests/fakedom.js.
 * No browser, no server, no radio: fetch answers /api/spots from the sample below (made-up callsigns),
 * RadioControl / RadioLayout are stubs, and RadioControl.send only records. Run by tests/t_c1.py (unit):
 *
 *     node tests/spots_ui_test.js       -> exit 0 and "ALL PASS", or exit 1 naming each failure
 *
 *  P1. asks /api/spots (the chosen sources) only while the panel is on the grid; not again before 30 s
 *  P2. the list: newest first; age, frequency, mode, call, reference + name, source; text stays text
 *  P3. a click tunes: set_freq, then set_mode in this radio's names (SSB by band, CW, digital, FM)
 *  P4. a spot outside the radio's bands, or for a viewer, is shown but can't be clicked; the panel says why
 *  P5. the filters (sources, mode, this band) work and this browser keeps them; a silent source is named
 */
"use strict";
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
const FT991A = { id: "ft991a", name: "FT-991A", modes: ["LSB", "USB", "AM", "CW-LSB", "CW-USB", "FM", "DATA-LSB", "DATA-USB"],
  bands: [{ name: "80", lo: 3500000, hi: 4000000 }, { name: "40", lo: 7000000, hi: 7300000 }, { name: "20", lo: 14000000, hi: 14350000 },
          { name: "2", lo: 144000000, hi: 148000000 }] };
const IC7300 = { id: "ic7300mk2", name: "IC-7300MK2", modes: ["LSB", "USB", "CW", "CW-R", "AM", "FM"],
  bands: [{ name: "40", lo: 7000000, hi: 7300000 }, { name: "20", lo: 14000000, hi: 14350000 }] };

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
  e.el("div", { id: "spotsList" });
  e.load("tr.js");
  const perms = new Set(opts.viewer ? ["view", "listen"] : ["view", "listen", "operate"]);
  e.ctx.TR.can = (p) => perms.has(p);
  e.vis = opts.visible !== false;
  e.ctx.RadioLayout = { visible: (id) => id === "spots" && e.vis };
  e.sent = [];
  const radio = opts.radio || FT991A;
  e.st = { connected: true, radio: radio.id, freq: 14074000, mode_name: "USB" };
  e.ctx.RadioControl = { send: (o) => e.sent.push(o), state: () => e.st, radios: () => [FT991A, IC7300] };
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
    eq(e.sent, [{ action: "set_freq", hz: 14275000 }], "P3 20 m SSB: the freq; USB already, so no mode change");
    e.sent.length = 0; e.click(rows[3]);
    eq(e.sent, [{ action: "set_freq", hz: 3850000 }, { action: "set_mode", mode: "LSB" }], "P3 80 m SSB -> LSB");
    e.sent.length = 0; e.click(rows[1]);
    eq(e.sent, [{ action: "set_freq", hz: 7032500 }, { action: "set_mode", mode: "CW-USB" }], "P3 CW on a Yaesu -> CW-USB");
    e.sent.length = 0; e.click(rows[2]);
    eq(e.sent, [{ action: "set_freq", hz: 14074000 }, { action: "set_mode", mode: "DATA-USB" }], "P3 digital on a Yaesu -> DATA-USB");
    e.sent.length = 0; e.click(rows[4]);
    eq(e.sent, [{ action: "set_freq", hz: 146520000 }, { action: "set_mode", mode: "FM" }], "P3 2 m FM -> FM");
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
  // P3 on an Icom: CW is CW, digital is USB
  {
    const e = page({ radio: IC7300 });
    await settle(e);
    const rows = e.rows();
    e.click(rows[1]);
    eq(e.sent, [{ action: "set_freq", hz: 7032500 }, { action: "set_mode", mode: "CW" }], "P3 CW on an Icom -> CW");
    e.sent.length = 0; e.click(rows[2]);
    eq(e.sent, [{ action: "set_freq", hz: 14074000 }], "P3 digital on an Icom -> USB, already set: freq only");
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
  console.log(failures ? `${failures} of ${checks} checks FAILED` : `ALL PASS (${checks} checks)`);
  process.exit(failures ? 1 : 0);
})().catch((err) => { console.log("FAIL (crash) " + (err && err.stack || err)); process.exit(1); });
