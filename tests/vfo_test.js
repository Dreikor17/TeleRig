/* The VFO panel (look A: an Icom-style main screen with the meter along its bottom) and typing a frequency,
 * tested on the REAL tr.js + app.js + freqentry.js in a Node vm on tests/fakedom.js: the VFO's markup is built
 * here as index.html has it, /api/radios answers with profiles shaped like backend/profiles.py's, and state
 * and meter frames come in on a fake socket. No browser, no server, no radio; nothing here transmits (a
 * frame saying the radio transmits is only a frame). Run by tests/t_c1.py (unit):
 *
 *     node tests/vfo_test.js            -> exit 0 and "ALL PASS", or exit 1 naming each failure
 *
 *  T1. FreqEntry.parse: MHz with a point or a comma, the VFO's own grouping (7.074.50), a leading point
 *      keeping the MHz, bare numbers as MHz where the radio receives there (1296 on the IC-9700), else kHz
 *      or Hz (7074, 14074000), units (k / kHz / M / MHz), and refusals with the reason (144.2 on an HF
 *      radio is not 144.2 kHz); a profile without rx_ranges uses its bands
 *  T2. the digits: the sub-kHz ones small; the operating band's are a button that says what it does; the
 *      filter's width beside the filter (an Icom's slot; a Yaesu's WIDTH code; none where it isn't known)
 *  T3. beside the digits: RIT with its kHz, split, duplex with its offset, the FM tone, the dial lock; the
 *      lit strip: the radio's own preamp name, AN / DNF, COMP, VOX, MON, NAR; none of it on the other band
 *  T4. typing: a click on the digits (or Enter on them) opens the entry with a hint; Enter tunes (set_freq)
 *      and closes it; Esc cancels; a frequency the radio can't receive is refused, the entry stays; the
 *      other band's row still selects that band; a viewer can't type
 *  T5. the Controls panel's frequency box reads the same way (7074 is 7.074 MHz; 144.2 on HF is refused)
 *  T6. the meter: S on receive; on transmit the picked TX meter with the CI-V reference's values (Po 50%,
 *      SWR 1.5 / >3.0, ALC as a share of its zone, COMP dB, Vd V, Id A), its scale under the bar, the bar's
 *      amber / red from its marks; a Yaesu's raw reading; the peak a mark on the bar that holds, then falls
 *      back, with nothing beside the reading that comes and goes; a radio without meters hides the block
 *  T7. a reload: the page opens its socket before /api/radios answers, so a state frame can come first.
 *      Drawn then, the VFO shows no width (not the filter_bw an Icom's width is read from: a Yaesu showed
 *      2.4 kHz whatever its WIDTH), and once the profiles are in it is drawn again with the radio's own:
 *      a Yaesu's WIDTH and its own function names. Both when the server's hello names the radio and when
 *      the page starts on another profile.
 */
"use strict";
const { makeEnv } = require("./fakedom");

let failures = 0, checks = 0;
function check(cond, msg) { checks++; if (!cond) { failures++; console.log("FAIL " + msg); } }
function eq(a, b, msg) { const ja = JSON.stringify(a), jb = JSON.stringify(b); check(ja === jb, msg + ": got " + ja + ", want " + jb); }

const ICOM_METERS = {
  PO: { unit: "%", pts: [[0, 0], [143, 50], [213, 100]] },
  SWR: { unit: "", pts: [[0, 1.0], [48, 1.5], [80, 2.0], [120, 3.0]], warn: 48, bad: 80 },
  ALC: { unit: "%", pts: [[0, 0], [120, 100]], bad: 120 },
  Vd: { unit: "V", pts: [[0, 0], [13, 10], [241, 16]] },
};
const METERS = ["S", "PO", "SWR", "ALC", "COMP", "Vd", "Id"];
const PROFILES = [
  { id: "ic7300mk2", make: "Icom", name: "IC-7300MK2", protocol: "civ", has_scope: true, default_step: 10,
    bands: [{ name: "40", lo: 7000000, hi: 7300000 }, { name: "20", lo: 14000000, hi: 14350000 }],
    modes: ["LSB", "USB", "CW", "AM", "FM"], steps: [{ v: 10, label: "10 Hz" }], preamp_labels: ["OFF", "P.AMP"],
    rx_ranges: [[30000, 74800000]],
    meter_marks: Object.assign({}, ICOM_METERS, { COMP: { unit: "dB", pts: [[0, 0], [130, 15], [210, 30]] },
                                                  Id: { unit: "A", pts: [[0, 0], [97, 10], [146, 15], [241, 25]] } }),
    capabilities: { meters: METERS, ptt: true, dual_watch: false } },
  { id: "ic9700", make: "Icom", name: "IC-9700", protocol: "civ", has_scope: true, default_step: 10, dual_watch: true,
    bands: [{ name: "144", lo: 144000000, hi: 148000000 }, { name: "430", lo: 430000000, hi: 450000000 },
            { name: "1200", lo: 1240000000, hi: 1300000000 }],
    modes: ["USB", "LSB", "CW", "FM"], steps: [{ v: 10, label: "10 Hz" }], preamp_labels: ["OFF", "P.AMP"],
    rx_ranges: [[144000000, 148000000], [430000000, 450000000], [1240000000, 1300000000]], meter_marks: ICOM_METERS,
    capabilities: { meters: METERS, ptt: true, dual_watch: true } },
  { id: "ft991a", make: "Yaesu", name: "FT-991A", protocol: "yaesu", has_scope: false, default_step: 10,
    bands: [{ name: "20", lo: 14000000, hi: 14350000 }], modes: ["USB", "LSB", "CW-USB", "FM"], steps: [{ v: 10, label: "10 Hz" }],
    preamp_labels: ["IPO", "AMP1", "AMP2"], rx_ranges: [[30000, 56000000], [118000000, 164000000], [420000000, 470000000]],
    meter_marks: {}, capabilities: { meters: METERS, ptt: true, dual_watch: false } },
  { id: "ic728", make: "Icom", name: "IC-728", protocol: "civ", has_scope: false, default_step: 10,
    bands: [{ name: "20", lo: 14000000, hi: 14350000 }], modes: ["USB", "LSB", "CW"], steps: [{ v: 10, label: "10 Hz" }],
    rx_ranges: [[500000, 30000000]], meter_marks: {}, capabilities: { meters: [], ptt: false, dual_watch: false } },
];
const jsonResponse = (status, body) => ({ ok: status >= 200 && status < 300, status, json: async () => body });

function bandRow(e, parent, name, active) {
  const row = e.el("div", { class: "bandrow" + (active ? " active" : ""), "data-act": "select_band", "data-band": name, id: name === "main" ? "rowMain" : "rowSub" }, parent);
  const top = e.el("div", { class: "b-top" }, row);
  if (name === "main") e.el("span", { class: "tx", id: "txTag" }, top);
  e.el("b", { class: "band-mode", id: name + "Mode" }, top);
  e.el("i", { class: "band-fil", id: name + "Fil" }, top);
  e.el("span", { class: "band-bw", id: name + "Bw" }, top);
  e.el("span", { class: "b-vfo", id: name + "Vfo" }, top);
  e.el("i", { id: name + "Band" }, e.el("span", { class: "b-band" }, top));
  e.el("div", { class: "band-freq", id: name + "Freq" }, e.el("div", { class: "b-center" }, row));
  e.el("div", { class: "b-offs", id: name + "Offs" }, row);
  e.el("div", { class: "ind-strip", id: name + "Ind" }, row);
  return row;
}
// the page, booted: tr.js + app.js + freqentry.js run, the socket opens, /api/radios answers
async function page(opts) {
  opts = opts || {};
  const sockets = [];
  function FakeWS() { this.readyState = 1; this.sent = []; this.send = (d) => this.sent.push(JSON.parse(d)); this.close = () => {}; sockets.push(this); }
  // opts.holdRadios: /api/radios answers only at e.releaseRadios() (a reload where a state frame comes first)
  let release = null;
  const radios = () => jsonResponse(200, { radios: PROFILES });
  const fetch = (url) => (String(url).split("?")[0] === "/api/radios"
    ? (opts.holdRadios ? new Promise((res) => { release = () => res(radios()); }) : Promise.resolve(radios()))
    : Promise.resolve(jsonResponse(404, { ok: false, error: "not_found" })));
  const e = makeEnv({ WebSocket: FakeWS, fetch });
  for (const id of ["bandRow", "modeRow", "step", "toasts"]) e.el("div", { id });
  e.el("select", { id: "radioSel" });
  const bands = e.el("div", { class: "bands tft", id: "bands" });
  bandRow(e, bands, "main", true);
  bandRow(e, bands, "sub", false);
  const meter = e.el("div", { class: "meter", id: "meterBlock" });
  e.el("div", { class: "meter-scale" }, meter);
  const track = e.el("div", { class: "meter-track" }, meter);
  e.el("div", { class: "meter-fill", id: "meterFill" }, e.el("div", { class: "meter-bar" }, track));
  e.el("i", { class: "meter-peak", id: "meterPeak" }, track);
  e.el("div", { class: "meter-tx-scale", id: "meterTxScale" }, meter);
  const read = e.el("div", { class: "meter-read" }, meter);
  e.el("b", { class: "meter-val", id: "meterVal" }, read);
  const sel = e.el("div", { class: "meter-sel", id: "meterSel" }, meter);
  for (const k of ["PO", "SWR", "ALC", "COMP", "Vd", "Id"]) e.el("button", { class: "m-btn", "data-act": "meter", "data-meter": k }, sel);
  e.el("input", { id: "freqEntry", class: "freq-entry" });
  e.el("button", { id: "freqSet", class: "key" });
  e.load("tr.js");
  e.load("waterfall.js");                                  // Passband (the page loads it before app.js)
  e.ctx.Scope = function () { return new Proxy({ meta: {} }, { get: (t, k) => (k in t ? t[k] : () => []) }); };   // no canvas here
  e.load("app.js");
  e.load("freqentry.js");
  e.toasts = [];
  e.ctx.TR.toast = (m) => e.toasts.push(m);
  e.ctx.TR.srLive = () => {};
  const ws = sockets[sockets.length - 1];
  e.ws = ws;
  e.frame = (f) => ws.onmessage({ data: JSON.stringify(f) });
  e.frame({ type: "hello", v: 1, conn: 3, user: "ME", role: opts.role || "operator",
            perms: opts.role === "viewer" ? ["view", "listen"] : ["view", "listen", "operate", "tx"],
            status: opts.status || {}, tx: { owner: null }, hb_interval: 2 });
  for (let i = 0; i < 8; i++) await e.flush();
  e.releaseRadios = () => { if (release) release(); };
  e.$ = (id) => e.doc.getElementById(id);
  e.text = (id) => (e.$(id).textContent || "").trim();
  e.inds = (id) => e.$(id).querySelectorAll(".ind").map((x) => x.textContent);
  e.sent = (action) => ws.sent.filter((m) => m.action === action);
  e.click = (el) => el.dispatchEvent(new e.Event("click", { bubbles: true }));
  e.key = (el, key) => el.dispatchEvent(new e.Event("keydown", { key, bubbles: true }));
  e.state = (o) => e.frame(state(o));
  return e;
}
function state(o) {
  o = o || {};
  const radio = o.radio || "ic7300mk2", freq = o.freq || 14230000;
  return Object.assign({ type: "state", connected: true, radio, freq, mode_name: "USB", filter_name: "FIL2", filter_bw: 2400,
    main: { freq, mode_name: o.mode_name || "USB", filter_name: "FIL2" }, sub: { freq: o.subFreq || 0, mode_name: "FM", filter_name: "FIL1" },
    active_band: "main", dual_watch: false, ptt: false, meter: "PO", meter_val: 0, smeter: 0, smeter_s: "S0",
    agc: 2, preamp: 0, att: 0, nb: 0, nr: 0, anotch: 0, mnotch: 0, comp: 0, vox: 0, mon: 0, narrow: 0,
    rit: 0, rit_freq: 0, split: 0, duplex: 0, offset: 0, lock: false, tone_mode: 0 }, o);
}

async function main() {
  // T1: the parser
  {
    const e = await page();
    const F = e.ctx.FreqEntry, hf = [[30000, 74800000]], vu = [[144e6, 148e6], [430e6, 450e6], [1240e6, 1300e6]];
    const cur = 14074000;
    const cases = [["14.074", hf, 14074000], ["14,074", hf, 14074000], ["7.074.50", hf, 7074500], ["14.230.000", hf, 14230000],
      [".230", hf, 14230000], ["7074", hf, 7074000], ["7074.5", hf, 7074500], ["14074000", hf, 14074000], ["7074k", hf, 7074000],
      ["7074 kHz", hf, 7074000], ["14.074 MHz", hf, 14074000], ["0.5", hf, 500000], ["1296", hf, 1296000],
      ["1296", vu, 1296000000], ["144.2", vu, 144200000], ["145500", vu, 145500000], ["7", hf, 7000000]];
    for (const [t, r, want] of cases) eq(F.parse(t, cur, r).hz, want, `T1 "${t}"`);
    for (const [t, r, why] of [["144.2", hf, /outside this radio \(0\.03-74\.8 MHz\)/], ["14.074", vu, /outside/], ["100", hf, /outside/],
      ["abc", hf, /digits/], ["", hf, /Type a frequency/], ["7.07.5", hf, /three digits/], ["0", hf, /above 0/]]) {
      const r2 = F.parse(t, cur, r);
      check(r2.hz === undefined && why.test(r2.error || ""), `T1 "${t}" refused: ${JSON.stringify(r2)}`);
    }
    eq([F.format(7074500), F.format(1296200000)], ["7.074.500", "1296.200.000"], "T1 format: the VFO's grouping");
    eq(F.rangesOf({ bands: [{ lo: 1, hi: 2 }] }), [[1, 2]], "T1 a profile without rx_ranges: its bands");
  }
  // T2 + T3: the digits, the filter width, the offsets, the strip
  {
    const e = await page();
    e.state({ rit: 1, rit_freq: -50, split: 1, lock: true, preamp: 1, anotch: 1, comp: 1, vox: 1, nb: 1 });
    for (let i = 0; i < 4; i++) await e.flush();
    const f = e.$("mainFreq");
    const digits = f.querySelectorAll(".fd").map((d) => [d.textContent, d.classList.contains("fd-lo")]);
    eq(digits.map((d) => d[0]).join(""), "14230000", "T2 the digits");
    eq(digits.filter((d) => d[1]).length, 3, "T2 ... the three under the kHz are small");
    eq(f.querySelectorAll(".fdsep").map((x) => x.classList.contains("lo")), [false, true], "T2 ... and the point before them");
    eq([f.getAttribute("role"), f.getAttribute("aria-label")], ["button", "14.230.000 MHz. Press Enter to type a frequency"],
       "T2 the operating band's digits are a button that says what it does");
    eq([e.text("mainMode"), e.text("mainFil"), e.text("mainBw")], ["USB", "FIL2", "2.4 kHz"], "T2 mode, filter and its width (Icom)");
    eq(e.inds("mainOffs"), ["RIT −0.05", "SPLIT", "LOCK"], "T3 beside the digits: RIT with its kHz, split, the dial lock");
    eq(e.inds("mainInd"), ["AGC-M", "P.AMP", "NB", "AN", "COMP", "VOX"], "T3 the lit strip (Icom names)");
    e.state({ duplex: 2, offset: 600000, mode_name: "FM", tone_mode: 2 });
    for (let i = 0; i < 2; i++) await e.flush();
    eq(e.inds("mainOffs"), ["DUP+ 0.600", "TONE"], "T3 duplex with its offset, the FM tone");
    e.state({ radio: "ft991a", preamp: 1, anotch: 1, nr: 1, narrow: 1 });
    for (let i = 0; i < 4; i++) await e.flush();
    eq(e.inds("mainInd"), ["AGC-M", "AMP1", "DNR", "DNF", "NAR"], "T3 a Yaesu's own names (AMP1, DNR, DNF, NAR)");
    e.state({ radio: "ft991a", narrow: 0, width: 10, filter_bw: 2400 });
    for (let i = 0; i < 2; i++) await e.flush();
    eq(e.text("mainBw"), "1.95 kHz", "T2 a Yaesu's width from its WIDTH code (not the fixed filter_bw)");
    e.state({ radio: "ft991a", mode_name: "FM", width: 10 });
    for (let i = 0; i < 2; i++) await e.flush();
    eq(e.text("mainBw"), "", "T2 ... none in FM, which it doesn't report");
    e.state({ radio: "ic9700", freq: 144200000, dual_watch: true, subFreq: 432100000, active_band: "main", rit: 1, rit_freq: 120 });
    for (let i = 0; i < 4; i++) await e.flush();
    eq([e.inds("mainOffs"), e.inds("subOffs"), e.inds("subInd")], [["RIT +0.12"], [], []], "T3 none of it on the other band");
  }
  // T4: typing a frequency in the VFO
  {
    const e = await page();
    e.state({});
    for (let i = 0; i < 4; i++) await e.flush();
    const f = e.$("mainFreq");
    e.click(f);
    const entry = () => e.$("rowMain").querySelector(".vfo-entry input"), hint = () => e.$("rowMain").querySelector(".ve-hint");
    check(!!entry() && f.hidden === true && e.$("rowMain").classList.contains("typing"), "T4 a click on the digits opens the entry in their place");
    check(/^Type MHz/.test(hint().textContent) && e.doc.activeElement === entry(), "T4 ... with a hint, and the focus: " + hint().textContent);
    eq(e.sent("select_band"), [], "T4 ... and no band-select");
    entry().value = "7074";
    entry().dispatchEvent(new e.Event("input"));
    eq(hint().textContent, "Enter tunes to 7.074.000 MHz, Esc cancels", "T4 the hint reads what was typed");
    e.key(entry(), "Enter");
    eq(e.sent("set_freq"), [{ action: "set_freq", hz: 7074000 }], "T4 Enter tunes");
    check(!e.$("rowMain").querySelector(".vfo-entry") && f.hidden === false && e.doc.activeElement === f, "T4 ... the digits are back, with the focus");
    e.key(f, "Enter");
    check(!!entry(), "T4 Enter on the digits opens it too");
    entry().value = "144.2";
    e.key(entry(), "Enter");
    check(e.sent("set_freq").length === 1 && !!entry() && hint().classList.contains("err") && /outside/.test(hint().textContent),
          "T4 a frequency the radio can't receive: refused with the reason, nothing sent, the entry stays: " + hint().textContent);
    e.key(entry(), "Escape");
    check(!entry() && f.hidden === false && e.sent("set_freq").length === 1, "T4 Esc cancels");
    e.state({ radio: "ic9700", freq: 144200000, dual_watch: true, subFreq: 432100000 });
    for (let i = 0; i < 4; i++) await e.flush();
    e.click(e.$("subFreq"));
    eq([e.sent("select_band").map((m) => m.band), !!e.$("rowSub").querySelector(".vfo-entry")], [["sub"], false],
       "T4 the other band's digits select that band, as before");
    const v = await page({ role: "viewer" });
    v.state({});
    for (let i = 0; i < 4; i++) await v.flush();
    v.click(v.$("mainFreq"));
    check(!v.$("rowMain").querySelector(".vfo-entry"), "T4 a viewer can't type a frequency");
  }
  // T5: the Controls panel's frequency box
  {
    const e = await page();
    e.state({});
    for (let i = 0; i < 4; i++) await e.flush();
    e.$("freqEntry").value = "7074";
    e.$("freqSet").onclick();
    e.$("freqEntry").value = "144.2";
    e.$("freqSet").onclick();
    eq([e.sent("set_freq"), e.toasts.length && /outside/.test(e.toasts[0])], [[{ action: "set_freq", hz: 7074000 }], true],
       "T5 the frequency box: 7074 is 7.074 MHz; 144.2 on an HF radio is refused, with the reason");
  }
  // T6: the meter
  {
    const e = await page();
    e.state({});
    for (let i = 0; i < 4; i++) await e.flush();
    const meter = (o) => e.frame(Object.assign({ type: "meter", meter: "PO", meter_val: 0, smeter: 0, smeter_s: "S0" }, o));
    const blk = e.$("meterBlock"), fill = () => e.$("meterFill").style.width, val = () => e.text("meterVal");
    meter({ smeter: 120, smeter_s: "S9" });
    meter({ smeter: 93, smeter_s: "S7" });
    eq([fill(), val(), blk.classList.contains("tx")], ["38.75%", "S7", false], "T6 receive: the S-meter (0..240), its reading");
    eq([e.$("meterPeak").hidden, e.$("meterPeak").style.left, e.$("meterBlock").querySelectorAll(".meter-read > *").length],
       [false, "50%", 1], "T6 ... the peak a mark on the bar, held; the reading alone in its corner");
    e.clock.advance(900);
    meter({ smeter: 80, smeter_s: "S6" });
    eq(e.$("meterPeak").style.left, "50%", "T6 ... held for a second");
    e.clock.advance(400);
    meter({ smeter: 80, smeter_s: "S6" });
    eq(e.$("meterPeak").style.left, "38%", "T6 ... then falling back (40 % of the bar a second), not jumping");
    e.clock.advance(400);
    meter({ smeter: 80, smeter_s: "S6" });
    eq([e.$("meterPeak").hidden, val()], [true, "S6"], "T6 ... until the reading catches it");
    eq(e.$("meterSel").querySelectorAll(".m-btn.active").map((b) => b.dataset.meter), ["PO"], "T6 the TX meter keys: Po picked");
    const tx = async (m, v) => { e.state({ ptt: true, meter: m, meter_val: v }); for (let i = 0; i < 2; i++) await e.flush(); return val(); };
    eq(await tx("PO", 143), "Po 50%", "T6 transmit: Po from the CI-V reference");
    eq([fill(), blk.classList.contains("tx")], ["56.07843137254902%", true], "T6 ... on the 0..255 bar");
    eq(e.$("meterTxScale").querySelectorAll("span").map((x) => [x.textContent, x.style.left]),
       [["Po", "0%"], ["50", "56.07843137254902%"], ["100%", "83.52941176470588%"]], "T6 ... its scale under the bar");
    eq([await tx("SWR", 48), await tx("SWR", 100), await tx("SWR", 200)], ["SWR 1.5", "SWR 2.5", "SWR >3.0"], "T6 SWR");
    eq([blk.style.getPropertyValue("--m-warn"), blk.style.getPropertyValue("--m-bad")], ["18.823529411764707%", "31.372549019607842%"],
       "T6 ... amber from 1.5, red from 2.0");
    eq(e.$("meterTxScale").querySelectorAll("span").map((x) => x.textContent), ["SWR", "1.5", "2", "3"], "T6 ... its scale");
    eq(await tx("ALC", 60), "ALC 50%", "T6 ALC: a share of its zone");
    const zone = e.$("meterTxScale").querySelector(".zone");
    check(zone && zone.style.width === "47.05882352941176%", "T6 ... the zone marked: " + (zone && zone.style.width));
    eq([await tx("COMP", 130), await tx("Vd", 13), await tx("Id", 146)], ["COMP 15 dB", "Vd 10 V", "Id 15 A"], "T6 COMP, Vd, Id");
    e.state({ ptt: false, meter: "PO" });
    for (let i = 0; i < 2; i++) await e.flush();
    eq([blk.classList.contains("tx"), val()], [false, "S0"], "T6 back on receive: the S-meter");
    e.state({ radio: "ft991a", ptt: true, meter: "PO", meter_val: 143 });
    for (let i = 0; i < 4; i++) await e.flush();
    eq([val(), e.$("meterTxScale").querySelectorAll("span").map((x) => x.textContent)], ["Po 143", ["Po"]],
       "T6 a Yaesu (no calibration in its CAT reference): the raw reading on an unmarked scale");
    e.state({ radio: "ic728", ptt: false, meter: "S" });
    for (let i = 0; i < 4; i++) await e.flush();
    eq(blk.style.display, "none", "T6 a radio with no meters: no meter");
  }
  // T7: a reload, a state frame before the profiles
  for (const [tag, status] of [["the hello names the FT-991A", { connected: true, radio: "ft991a" }],
                               ["the page starts on another profile", {}]]) {
    const e = await page({ holdRadios: true, status });
    e.state({ radio: "ft991a", mode_name: "CW-USB", narrow: 0, width: 10, filter_bw: 2400, preamp: 1, nr: 1 });
    for (let i = 0; i < 4; i++) await e.flush();
    eq(e.text("mainMode"), "CW-USB", "T7 " + tag + ": the frame is drawn before the profiles");
    eq(e.text("mainBw"), "", "T7 " + tag + ": ... with no width yet (not the 2.4 kHz filter_bw an Icom's is read from)");
    e.releaseRadios();
    for (let i = 0; i < 12; i++) await e.flush();
    eq(e.text("mainBw"), "500 Hz", "T7 " + tag + ": once the profiles are in, the FT-991A's own: its WIDTH in CW (500 Hz)");
    eq(e.inds("mainInd"), ["AGC-M", "AMP1", "DNR"], "T7 " + tag + ": ... and its own names (AMP1, DNR)");
  }
  console.log(failures ? `${failures} of ${checks} checks FAILED` : `ALL PASS (${checks} checks)`);
  process.exit(failures ? 1 : 0);
}
main().catch((err) => { console.log("FAIL " + (err && err.stack || err)); process.exit(1); });
