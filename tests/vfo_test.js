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
 *  T3. beside the digits: RIT with its kHz, split, the repeater shift with its offset (600k, 5M, 1.6M; an
 *      Icom's DUP, a Yaesu's shift in FM; no value while the radio hasn't said it), the FM tone with its
 *      CTCSS tone or DCS code (TONE, TSQL, DCS, DCS-ENC), the dial lock; the lit strip: the radio's own
 *      preamp name, AN / DNF, COMP, VOX, MON, NAR; none of it on the other band
 *  T4. typing: a click on the digits (or Enter on them) opens the entry with a hint; Enter tunes (set_freq)
 *      and closes it; Esc cancels; a frequency the radio can't receive is refused, the entry stays; the
 *      other band's row still selects that band; a viewer can't type
 *  T5. (the Controls panel's frequency box, gone 2026-11-22: a frequency is typed on the VFO's digits)
 *  T6. the meter, its bar alone (no number beside it: the step keys are there): S on receive (0..240); on
 *      transmit the picked TX meter on the 0..255 bar, its scale under the bar from the CI-V reference (Po
 *      %, SWR 1.5 / 2 / 3, ALC's zone, COMP dB, Vd V, Id A), the bar's amber / red from its marks; a Yaesu's
 *      unmarked scale; the peak a mark on the bar that holds, then falls back; a radio without meters hides
 *      the meter and keeps the step keys beside it
 *  T7. a reload: the page opens its socket before /api/radios answers, so a state frame can come first.
 *      Drawn then, the VFO shows no width (not the filter_bw an Icom's width is read from: a Yaesu showed
 *      2.4 kHz whatever its WIDTH), and once the profiles are in it is drawn again with the radio's own:
 *      a Yaesu's WIDTH and its own function names. Both when the server's hello names the radio and when
 *      the page starts on another profile.
 *  T8. the line under the operating band's digits: a calling frequency (SpotsPanel.here, its note as the
 *      title), else the band plan's segment (bandplan.js, its description as the title), else nothing (no
 *      memory names); on the right the frequency a repeater shift transmits on (an Icom's DUP -/+, a Yaesu's
 *      shift in FM, with the offset the radio reports), none for split, an unknown offset or another mode;
 *      only the operating band's, nothing while disconnected; text stays text; the same line is not built
 *      again
 *  T9. the wheel over a digit steps it once a notch, whatever the wheel: a plain notch one step, a quick
 *      turn a step a notch; a high-resolution notch (many small events) one step, not one an event; a
 *      touchpad swipe a step at once, then one a notch's travel; Firefox's lines and pixels the same; a
 *      tiny slow notch still steps (a turn's first event); a turn back starts a new turn at once; a
 *      free-spinning wheel at most a step an event, its extra notches not carried; a sideways scroll is
 *      the page's; a viewer's wheel tunes nothing
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
  e.el("div", { class: "b-ctx", id: name + "Ctx" }, row);
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
  const steps = e.el("div", { class: "meter-step", id: "vfoStepCtl", "data-gate": "operate" }, meter);
  e.el("button", { class: "m-btn step-key", id: "stepDn", type: "button" }, steps);
  e.el("select", { class: "step-sel", id: "vfoStep" }, steps);
  e.el("button", { class: "m-btn step-key", id: "stepUp", type: "button" }, steps);
  const sel = e.el("div", { class: "meter-sel", id: "meterSel" }, meter);
  for (const k of ["PO", "SWR", "ALC", "COMP", "Vd", "Id"]) e.el("button", { class: "m-btn", "data-act": "meter", "data-meter": k }, sel);
  e.load("tr.js");
  e.load("waterfall.js");                                  // Passband (the page loads it before app.js)
  e.load("bandplan.js");                                   // the band plan's segments (the line under the digits)
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
    const offs = async (o) => { e.state(o); for (let i = 0; i < 2; i++) await e.flush(); return e.inds("mainOffs"); };
    eq(await offs({ duplex: 2, offset: 600000, mode_name: "FM", tone_mode: 2, tone_freq: 12 }), ["DUP+ 600k", "TONE 100.0"],
       "T3 an Icom's duplex with its offset, the FM tone with its CTCSS tone");
    eq([await offs({ duplex: 1, offset: 5000000, mode_name: "FM", tone_mode: 1, tone_freq: 0 }),
        await offs({ duplex: 1, offset: 1600000, mode_name: "FM" }), await offs({ duplex: 2, offset: 0, mode_name: "FM" })],
       [["DUP− 5M", "TSQL 67.0"], ["DUP− 1.6M"], ["DUP+"]], "T3 ... 5M, 1.6M; TSQL; an offset not known yet: no value");
    eq([await offs({ radio: "ft991a", rpt_shift: 2, offset: 600000, mode_name: "FM", tone_mode: 3, dcs_code: 5 }),
        await offs({ radio: "ft991a", rpt_shift: 1, offset: 600000, mode_name: "FM", tone_mode: 4, dcs_code: 0 }),
        await offs({ radio: "ft991a", rpt_shift: 2, offset: 600000, mode_name: "USB", tone_mode: 3 })],
       [["DUP− 600k", "DCS 036"], ["DUP+ 600k", "DCS-ENC 023"], ["DUP−"]],
       "T3 a Yaesu's shift with its offset in FM (DCS, DCS-ENC with the code); in USB no value and no tone");
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
  // T6: the meter
  {
    const e = await page();
    e.state({});
    for (let i = 0; i < 4; i++) await e.flush();
    const meter = (o) => e.frame(Object.assign({ type: "meter", meter: "PO", meter_val: 0, smeter: 0, smeter_s: "S0" }, o));
    const blk = e.$("meterBlock"), fill = () => e.$("meterFill").style.width;
    const scale = () => e.$("meterTxScale").querySelectorAll("span").map((x) => x.textContent);
    meter({ smeter: 120, smeter_s: "S9" });
    meter({ smeter: 93, smeter_s: "S7" });
    eq([fill(), blk.classList.contains("tx")], ["38.75%", false], "T6 receive: the S-meter (0..240)");
    eq([e.$("meterPeak").hidden, e.$("meterPeak").style.left], [false, "50%"], "T6 ... the peak a mark on the bar, held");
    e.clock.advance(900);
    meter({ smeter: 80, smeter_s: "S6" });
    eq(e.$("meterPeak").style.left, "50%", "T6 ... held for a second");
    e.clock.advance(400);
    meter({ smeter: 80, smeter_s: "S6" });
    eq(e.$("meterPeak").style.left, "38%", "T6 ... then falling back (40 % of the bar a second), not jumping");
    e.clock.advance(400);
    meter({ smeter: 80, smeter_s: "S6" });
    eq(e.$("meterPeak").hidden, true, "T6 ... until the reading catches it");
    eq(e.$("meterSel").querySelectorAll(".m-btn.active").map((b) => b.dataset.meter), ["PO"], "T6 the TX meter keys: Po picked");
    eq(e.$("vfoStepCtl").querySelectorAll(".m-btn.active").length, 0, "T6 ... the step keys beside them are not meter keys");
    const tx = async (m, v) => { e.state({ ptt: true, meter: m, meter_val: v }); for (let i = 0; i < 2; i++) await e.flush(); return fill(); };
    eq([await tx("PO", 143), blk.classList.contains("tx")], ["56.07843137254902%", true], "T6 transmit: Po on the 0..255 bar");
    eq(e.$("meterTxScale").querySelectorAll("span").map((x) => [x.textContent, x.style.left]),
       [["Po", "0%"], ["50", "56.07843137254902%"], ["100%", "83.52941176470588%"]], "T6 ... its scale under the bar, from the CI-V reference");
    eq([await tx("SWR", 48), scale()], ["18.823529411764707%", ["SWR", "1.5", "2", "3"]], "T6 SWR: its scale");
    eq([blk.style.getPropertyValue("--m-warn"), blk.style.getPropertyValue("--m-bad")], ["18.823529411764707%", "31.372549019607842%"],
       "T6 ... amber from 1.5, red from 2.0");
    eq(await tx("ALC", 60), "23.52941176470588%", "T6 ALC");
    const zone = e.$("meterTxScale").querySelector(".zone");
    check(zone && zone.style.width === "47.05882352941176%", "T6 ... its zone marked: " + (zone && zone.style.width));
    await tx("COMP", 130);
    eq(scale(), ["COMP", "15", "30 dB"], "T6 COMP: its scale in dB");
    await tx("Vd", 13);
    eq(scale(), ["Vd", "16 V"], "T6 Vd: its scale in V (10 V too close to the start to label)");
    await tx("Id", 146);
    eq(scale(), ["Id", "10", "15", "25 A"], "T6 Id: its scale in A");
    e.state({ ptt: false, meter: "PO" });
    for (let i = 0; i < 2; i++) await e.flush();
    eq([blk.classList.contains("tx"), fill()], [false, "0%"], "T6 back on receive: the S-meter");
    e.state({ radio: "ft991a", ptt: true, meter: "PO", meter_val: 143 });
    for (let i = 0; i < 4; i++) await e.flush();
    eq([fill(), scale()], ["56.07843137254902%", ["Po"]],
       "T6 a Yaesu (no calibration in its CAT reference): the bar on an unmarked scale");
    check(!blk.classList.contains("no-meter"), "T6 ... a radio with meters shows them");
    e.state({ radio: "ic728", ptt: false, meter: "S" });
    for (let i = 0; i < 4; i++) await e.flush();
    eq([blk.classList.contains("no-meter"), blk.style.display === "none"], [true, false],
       "T6 a radio with no meters: the meter hidden (style.css .meter.no-meter), the block and its step keys still there");
  }
  // T8: the line under the operating band's digits
  {
    const e = await page();
    const ctx = (id) => e.$(id || "mainCtx").children.map((c) => [c.className, c.textContent]);
    const go = async (o) => { e.state(o); for (let i = 0; i < 3; i++) await e.flush(); return ctx(); };
    const RPT = { radio: "ic9700", freq: 146940000, mode_name: "FM" };
    const plan = await go(RPT);
    eq(plan, [["ctx-name plan", "Rptr outputs"]], "T8 no memory, no calling frequency: the band plan's segment, nothing on the right");
    check(/^Band plan: FM repeater outputs/.test(e.$("mainCtx").children[0].title), "T8 ... its description as the title: " + e.$("mainCtx").children[0].title);
    eq([await go(Object.assign({ duplex: 1, offset: 600000 }, RPT)), await go(Object.assign({ duplex: 2, offset: 600000 }, RPT))],
       [[["ctx-name plan", "Rptr outputs"], ["ctx-tx", "TX 146.340.000"]], [["ctx-name plan", "Rptr outputs"], ["ctx-tx", "TX 147.540.000"]]],
       "T8 an Icom's DUP- / DUP+: where it transmits");
    eq([(await go(Object.assign({ duplex: 1, offset: 0 }, RPT))).length, (await go(Object.assign({ split: 1, offset: 600000 }, RPT))).length], [1, 1],
       "T8 ... nothing while the offset isn't known, nothing for split");
    let ask = null;
    e.ctx.SpotsPanel = { here: (hz) => (ask = hz, { kind: "call", name: "2 m <b>FM</b> calling", note: "National simplex" }) };
    eq([await go(Object.assign({ duplex: 1, offset: 600000 }, RPT)), ask, e.$("mainCtx").children[0].title],
       [[["ctx-name call", "2 m <b>FM</b> calling"], ["ctx-tx", "TX 146.340.000"]], 146940000, "2 m <b>FM</b> calling. National simplex"],
       "T8 a calling frequency: its name (text stays text) and its note as the title, asked for the operating frequency");
    check(!e.$("mainCtx").querySelector(".ctx-tag") && !/MEM/.test(e.$("mainCtx").textContent), "T8 ... no MEM tag");
    const kept = e.$("mainCtx").children[0];
    await go(Object.assign({ duplex: 1, offset: 600000, smeter: 50 }, RPT));
    check(e.$("mainCtx").children[0] === kept, "T8 the same line is not built again");
    e.ctx.SpotsPanel = { here: () => null };
    eq([await go({ radio: "ft991a", freq: 146940000, mode_name: "FM", rpt_shift: 1, offset: 600000 }),
        await go({ radio: "ft991a", freq: 146940000, mode_name: "USB", rpt_shift: 1, offset: 600000 }),
        await go({ radio: "ft991a", freq: 442100000, mode_name: "FM", rpt_shift: 2, offset: 5000000 })],
       [[["ctx-name plan", "Rptr outputs"], ["ctx-tx", "TX 147.540.000"]], [["ctx-name plan", "Rptr outputs"]], [["ctx-name plan", "Repeaters"], ["ctx-tx", "TX 437.100.000"]]],
       "T8 a Yaesu: its shift in FM (+600k, -5M), none in USB");
    eq(await go({ radio: "ic7300mk2", freq: 10000000, mode_name: "AM" }), [], "T8 off the band plan (WWV): an empty line");
    eq([await go({ radio: "ic9700", freq: 432100000, dual_watch: true, subFreq: 432100000, active_band: "sub" }), ctx("subCtx")],
       [[], [["ctx-name plan", "SSB/CW call"]]], "T8 only the operating band's line (here SUB)");
    eq(await go({ radio: "ic9700", connected: false, freq: 146940000, mode_name: "FM", duplex: 1, offset: 600000 }), [], "T8 disconnected: nothing");
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
  // T9: the wheel over a digit steps it once a notch, whatever the wheel ("way too sensitive": a smooth or
  // high-resolution wheel, or a touchpad, sends many small events a notch, and each one used to step it)
  {
    const e = await page();
    e.state({ freq: 14230000 });
    for (let i = 0; i < 4; i++) await e.flush();
    const digit = () => e.$("mainFreq").querySelectorAll(".fd").find((d) => d.dataset.place === "1000");   // the 1 kHz digit
    const wheel = (o) => { const ev = new e.Event("wheel", Object.assign({ bubbles: true, cancelable: true, deltaMode: 0 }, o)); digit().dispatchEvent(ev); return ev; };
    const notch = (dir, o) => wheel(Object.assign({ deltaY: 100 * dir, wheelDeltaY: -120 * dir }, o));   // Chromium, a plain wheel
    let base = 14230000;
    const steps = () => { e.clock.advance(400); const s = e.sent("set_freq"); const hz = s.length ? s[s.length - 1].hz : base; return Math.round((hz - base) / 1000); };
    const turn = (fn) => { e.clock.advance(1000); base = (e.sent("set_freq").slice(-1)[0] || { hz: base }).hz; fn(); return steps(); };
    eq(turn(() => notch(1)), -1, "T9 one notch toward you: down one step of that digit (1 kHz)");
    eq(turn(() => notch(-1)), 1, "T9 one notch away: up one step");
    eq(turn(() => { for (let i = 0; i < 5; i++) { notch(1); e.clock.advance(40); } }), -5, "T9 five notches in a quick turn: five steps");
    eq(turn(() => { for (let i = 0; i < 8; i++) { wheel({ deltaY: 12.5, wheelDeltaY: -15 }); e.clock.advance(8); } }), -1,
       "T9 a high-resolution notch (eight events of 15 / 120): one step, not eight");
    eq(turn(() => { for (let n = 0; n < 3; n++) for (let i = 0; i < 8; i++) { wheel({ deltaY: 12.5, wheelDeltaY: -15 }); e.clock.advance(10); } }), -3,
       "T9 three high-resolution notches in a turn: three steps");
    eq(turn(() => { for (let i = 0; i < 40; i++) { wheel({ deltaY: 5, wheelDeltaY: -6 }); e.clock.advance(10); } }), -2,
       "T9 a touchpad swipe of 200 px (40 events): its first step at once, then one a notch's travel (2, not 40)");
    eq(turn(() => { for (let i = 0; i < 3; i++) { wheel({ deltaY: 3, deltaMode: 1 }); e.clock.advance(40); } }), -3,
       "T9 Firefox in lines (3 a notch, no wheelDeltaY): a step a notch");
    eq(turn(() => { for (let i = 0; i < 3; i++) { wheel({ deltaY: 57 }); e.clock.advance(40); } }), -3,
       "T9 Firefox in pixels (57 a notch): a step a notch, never two");
    eq(turn(() => { wheel({ deltaY: 2, wheelDeltaY: -2 }); e.clock.advance(300); wheel({ deltaY: 2, wheelDeltaY: -2 }); }), -2,
       "T9 a slow wheel whose notch is tiny (2 / 120), notch by notch: a step each (a turn's first event steps)");
    eq(turn(() => { notch(1); e.clock.advance(30); notch(-1); e.clock.advance(30); notch(-1); }), 1,
       "T9 a turn the other way starts a new turn: down one, up two");
    eq(turn(() => { for (let i = 0; i < 6; i++) { wheel({ deltaY: 400, wheelDeltaY: -480 }); e.clock.advance(20); } }), -6,
       "T9 a free-spinning wheel (four notches an event): one step an event at most");
    eq(turn(() => { wheel({ deltaY: 12.5, wheelDeltaY: -15 }); e.clock.advance(10); wheel({ deltaY: 400, wheelDeltaY: -480 });
                    for (let i = 0; i < 4; i++) { e.clock.advance(10); wheel({ deltaY: 12.5, wheelDeltaY: -15 }); } }), -3,
       "T9 ... a spin mid-turn: its extra notches don't carry (a step at the turn's start, one for the spin, one more for four small events, not four)");
    eq(turn(() => { for (let i = 0; i < 7; i++) { wheel({ deltaY: 12.5, wheelDeltaY: -15 }); e.clock.advance(8); } wheel({ deltaY: -12.5, wheelDeltaY: 15 }); }), 0,
       "T9 a high-resolution notch turned back at once: down one, and the turn back steps up at once (no sevenths to undo first)");
    eq(turn(() => { for (let i = 0; i < 40; i++) { wheel({ deltaY: 5 }); e.clock.advance(10); } }), -5,
       "T9 a Firefox touchpad swipe (5 px events, no wheelDeltaY): a step at once, then one a notch's travel (48 px), not one an event");
    let side = null;
    const sideSteps = turn(() => { side = wheel({ deltaY: 0, deltaX: 100, wheelDeltaY: 0 }); });
    eq([sideSteps, side.defaultPrevented], [0, false], "T9 a sideways scroll: no step, and left to the page (it stepped the digit down)");
    const v = await page({ role: "viewer" });
    v.state({ freq: 14230000 });
    for (let i = 0; i < 4; i++) await v.flush();
    const vd = v.$("mainFreq").querySelectorAll(".fd").find((d) => d.dataset.place === "1000");
    vd.dispatchEvent(new v.Event("wheel", { bubbles: true, cancelable: true, deltaY: 100, wheelDeltaY: -120, deltaMode: 0 }));
    v.clock.advance(400);
    eq(v.sent("set_freq"), [], "T9 a viewer's wheel tunes nothing");
  }
  console.log(failures ? `${failures} of ${checks} checks FAILED` : `ALL PASS (${checks} checks)`);
  process.exit(failures ? 1 : 0);
}
main().catch((err) => { console.log("FAIL " + (err && err.stack || err)); process.exit(1); });
