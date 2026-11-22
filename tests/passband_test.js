/* The RX passband on the scope: waterfall.js Passband (how wide the radio listens) and the static band-plan
 * view of a radio with no scope over CAT (FT-991A, FT-891, IC-728), tested on the REAL waterfall.js, and
 * app.js handing the scope that width, on the REAL tr.js + app.js, in a Node vm on tests/fakedom.js. No
 * browser, no server, no socket, no radio. Run by tests/t_af_scope.py (unit group):
 *
 *     node tests/passband_test.js       -> exit 0 and "ALL PASS", or exit 1 naming each failure
 *
 *  A. Passband: the Yaesu WIDTH table (FT-991A / FT-891 CAT manuals), the code for a width, the width of
 *     a state (an Icom's filter_bw; a Yaesu's WIDTH code by mode and NAR; AM / FM unknown), the label.
 *  B. The static view draws the passband on its sideband and labels its width beside it, on the band's
 *     own side of the marker; nothing is labelled on a sweep or on the audio scope; no band, no label
 *     when the width is not known.
 *  C. app.js gives the scope that width on every state frame (a Yaesu's WIDTH, not the never-updated
 *     filter_bw; an Icom's filter_bw), and the WIDTH readout and -/+ keys read the same table; the Modem
 *     window's data-filter key (tr:datafix) is mended by app.js from its own state (Passband.dataFix):
 *     an operator's with the menu permission, never on air, never a need that is no [lo, hi].
 *  D. a Yaesu's data filter (Passband.data / dataFix): DATA MODE PSK centers it on the PSK TONE, OTHERS
 *     on OTHER SHIFT (0 Hz, the carrier: 3 kHz passes only 0-1.5 kHz), its WIDTH either side; what it
 *     cuts of SSTV, FT8 and RTTY, and the least that mends it (the WIDTH alone where that does it, else
 *     the center to 1500 Hz as well; OTHER DISP to 0), by the radio's own menu numbers.
 */
"use strict";
const { makeEnv } = require("./fakedom");

let failures = 0, checks = 0;
function check(cond, msg) { checks++; if (!cond) { failures++; console.log("FAIL " + msg); } }
function eq(a, b, msg) { const ja = JSON.stringify(a), jb = JSON.stringify(b); check(ja === jb, msg + ": got " + ja + ", want " + jb); }
function near(a, b, tol, msg) { check(Math.abs(a - b) <= tol, msg + ": got " + a + ", want " + b + " (+/- " + tol + ")"); }

const w = makeEnv();
w.load("waterfall.js");
const { Passband, Scope, AfMap } = w.ctx;
const YAESU = { protocol: "yaesu" }, ICOM = { protocol: "civ" };

// ---- A. Passband ------------------------------------------------------------------------------------
{
  const sh = (cls, nar, code) => (Passband.SH[cls][nar ? 1 : 0].find((e) => e[0] === code) || [])[1];
  // the CAT manuals' WIDTH table (FT-991A p. 16, FT-891 p. 16): default (00) and edge codes per column
  eq([sh("ssb", 0, 0), sh("ssb", 0, 9), sh("ssb", 0, 14), sh("ssb", 0, 21), sh("ssb", 1, 0), sh("ssb", 1, 1)],
     [2400, 1800, 2400, 3200, 1500, 200], "A SSB wide 00 / 09 / 14 / 21, narrow 00 / 01");
  eq([sh("cw", 0, 0), sh("cw", 0, 10), sh("cw", 0, 17), sh("cw", 1, 0), sh("cw", 1, 1)],
     [2400, 500, 3000, 500, 50], "A CW wide 00 / 10 / 17, narrow 00 / 01");
  eq([sh("data", 0, 0), sh("data", 0, 17), sh("data", 1, 0), sh("data", 1, 10)],
     [500, 3000, 300, 500], "A RTTY / DATA wide 00 (500 Hz: too narrow for FT8) / 17, narrow 00 / 10");
  for (const [cls, nar] of [["ssb", 0], ["ssb", 1], ["cw", 0], ["cw", 1], ["data", 0], ["data", 1]]) {
    const hz = Passband.SH[cls][nar].map((e) => e[1]);
    check(hz.every((v, i) => !i || v >= hz[i - 1]), "A " + cls + (nar ? " narrow" : " wide") + " ascends (the -/+ keys step in order)");
  }
  // the code for a width: a numbered code before the 00 default; -1 where the mode has no WIDTH
  eq([Passband.shCode("USB", false, 2400), Passband.shCode("LSB", false, 2400), Passband.shCode("CW-USB", false, 500),
      Passband.shCode("CW-LSB", false, 500), Passband.shCode("DATA-USB", false, 3000), Passband.shCode("RTTY-USB", false, 500),
      Passband.shCode("USB", true, 1500)], [14, 14, 10, 10, 17, 10, 7], "A shCode: the numbered code for a width");
  eq([Passband.shCode("USB", false, 3300), Passband.shCode("AM", false, 9000), Passband.shCode("FM", false, 16000),
      Passband.shCode("CW", false, 500)], [-1, -1, -1, -1], "A shCode: -1 for a width or a mode the table lacks");
  // the width of a state
  eq([Passband.hz(ICOM, { mode_name: "USB", filter_bw: 2400 }), Passband.hz(ICOM, { mode_name: "CW-NAR", filter_bw: 500 }),
      Passband.hz(ICOM, { mode_name: "USB" }), Passband.hz(null, { filter_bw: 3000 })],
     [2400, 500, 0, 3000], "A an Icom (and the IC-728): filter_bw, from the server");
  const y = (mode_name, narrow, width) => Passband.hz(YAESU, { mode_name, narrow, width, filter_bw: 2400 });
  eq([y("USB", 0, 0), y("USB", 0, 14), y("LSB", 0, 21), y("USB", 1, 0), y("CW-USB", 0, 0), y("CW-LSB", 1, 0),
      y("DATA-USB", 0, 0), y("DATA-USB", 0, 17), y("RTTY-LSB", 1, 0)],
     [2400, 2400, 3200, 1500, 2400, 500, 500, 3000, 300], "A a Yaesu: its WIDTH code by mode and NAR (never filter_bw)");
  eq([y("AM", 0, 0), y("FM", 0, 0), y("FM-N", 1, 0), y("C4FM", 0, 0), y("DATA-FM", 0, 0), y("USB", 0, 3), y("CW-USB", 1, 17)],
     [0, 0, 0, 0, 0, 0, 0], "A a Yaesu in AM / FM, or a code its column lacks: 0, not known");
  eq([Passband.label(2400), Passband.label(3000), Passband.label(500), Passband.label(1950), Passband.label(15000),
      Passband.label(6250), Passband.label(50)], ["2.4 kHz", "3 kHz", "500 Hz", "1.95 kHz", "15 kHz", "6.25 kHz", "50 Hz"],
     "A the label");
}

// ---- B. the static view ------------------------------------------------------------------------------
function rec() {
  const calls = [];
  const t = { fillStyle: null, strokeStyle: null, textAlign: "start",
    fillRect: (...a) => calls.push(["fillRect", t.fillStyle, ...a]),
    fillText: (s, x, y) => calls.push(["fillText", t.fillStyle, String(s), x, y, t.textAlign]),
    moveTo: (x) => calls.push(["moveTo", t.strokeStyle, x]),
    measureText: (s) => ({ width: String(s).length * 6 }) };
  return { ctx: new Proxy(t, { get: (o, k) => (k in o ? o[k] : () => {}), set: (o, k, v) => { o[k] = v; return true; } }), calls };
}
const W = 800;
function scope(theme) {
  const ov = rec(), sp = rec(), wf = rec();
  const sc = Object.create(Scope.prototype);
  Object.assign(sc, { W, specH: 150, wfH: 150, scopeH: 300, octx: ov.ctx, sctx: sp.ctx, wctx: wf.ctx, stctx: null, strip: null,
                      bandplan: [], showBandplan: false, opMode: "USB", meta: {} });
  if (theme) sc.setTheme(theme);
  return { sc, ov };
}
// app.js updateState on a radio with no scope: showStatic, then the mode, the dial and the width
function staticView(radio, st, theme) {
  const x = scope(theme);
  x.sc.showStatic(st.freq, 200000);
  x.sc.setOpMode(st.mode_name);
  x.sc.meta.tuned = st.freq;
  x.sc.meta.filterBw = Passband.hz(radio, st);
  x.ov.calls.length = 0;
  x.sc.drawOverlay();
  const pal = x.sc.pal;
  return { sc: x.sc, pal,
           bands: x.ov.calls.filter((c) => c[0] === "fillRect" && c[1] === pal.pass),
           labels: x.ov.calls.filter((c) => c[0] === "fillText" && c[1] === pal.passText),
           texts: x.ov.calls.filter((c) => c[0] === "fillText").map((c) => c[2]) };
}
{
  const v = staticView(YAESU, { freq: 14285000, mode_name: "USB", narrow: 0, width: 14 });
  eq(v.sc.meta.still, true, "B showStatic marks the band-plan view");
  eq(v.bands.length, 1, "B USB 2.4 kHz: the passband is drawn");
  const [, , bx, , bw] = v.bands[0] || [];
  near(bx, v.sc.freqToX(14285000), 1e-6, "B ... from the dial");
  near(bx + bw, v.sc.freqToX(14287400), 1e-6, "B ... up 2.4 kHz (USB)");
  eq(v.labels.map((c) => c[2]), ["2.4 kHz"], "B ... labelled with its width");
  const [, , , lx, ly, la] = v.labels[0] || [];
  near(lx, bx + bw + 4, 1e-6, "B ... beside the band, right of it (its side of the marker)");
  eq([ly, la], [22, "left"], "B ... under the frequency label (y 9) and clear of it");
  check(v.texts.includes("14.2850 MHz"), "B the frequency label stays: " + JSON.stringify(v.texts));
}
{
  const v = staticView(YAESU, { freq: 7200000, mode_name: "LSB", narrow: 0, width: 14 });
  const [, , bx, , bw] = v.bands[0] || [];
  near(bx, v.sc.freqToX(7197600), 1e-6, "B LSB: the band is below the dial");
  near(bx + bw, v.sc.freqToX(7200000), 1e-6, "B ... up to it");
  const [, , text, lx] = v.labels[0] || [];
  eq(text, "2.4 kHz", "B LSB: labelled");
  near(lx + 6 * text.length, bx - 4, 1e-6, "B ... left of the band (its side of the marker)");
}
{
  const v = staticView(YAESU, { freq: 14030000, mode_name: "CW-USB", narrow: 0, width: 10 });
  const [, , bx, , bw] = v.bands[0] || [];
  near(bx, v.sc.freqToX(14029750), 1e-6, "B CW 500 Hz: centred on the dial");
  near(bx + bw, v.sc.freqToX(14030250), 1e-6, "B ...");
  eq(v.labels.map((c) => [c[2], c[3] > bx + bw]), [["500 Hz", true]], "B ... labelled 500 Hz, right of it (a sliver at 200 kHz)");
}
{
  const v = staticView(YAESU, { freq: 14074000, mode_name: "DATA-USB", narrow: 0, width: 17 });
  eq(v.labels.map((c) => c[2]), ["3 kHz"], "B DATA-USB at WIDTH 17: 3 kHz");
  const s = staticView(YAESU, { freq: 14074000, mode_name: "DATA-USB", narrow: 0, width: 17 }, "screen");
  eq([s.labels.length, s.pal.passText], [1, Scope.PALETTES.screen.passText], "B the radio screen theme labels it in its own colour");
  const i = staticView(ICOM, { freq: 29600000, mode_name: "FM", filter_bw: 12000 });
  eq([i.bands.length, i.labels.map((c) => c[2])], [1, ["12 kHz"]], "B the IC-728 in FM: its fixed 12 kHz filter (filter_bw)");
}
{
  const v = staticView(YAESU, { freq: 146520000, mode_name: "FM", narrow: 0, width: 0, filter_bw: 2400 });
  eq([v.bands.length, v.labels.length], [0, 0], "B a Yaesu in FM: no band, no label (not known over CAT; filter_bw is not it)");
  check(v.texts.includes("146.5200 MHz"), "B ... the dial is still labelled");
}
{
  // a sweep (the native Icom scope) or the audio scope: never labelled
  const x = scope();
  x.sc.meta = { mode: 0, center: 14074000, span: 50000, tuned: 14074000, filterBw: 2400 };
  x.sc.drawOverlay();
  const pal = x.sc.pal;
  eq([x.ov.calls.some((c) => c[0] === "fillRect" && c[1] === pal.pass), x.ov.calls.filter((c) => c[1] === pal.passText).length],
     [true, 0], "B a sweep: the passband as before, no label");
  const a = scope();
  a.sc.meta = AfMap.view(14074000, 3600, "USB", null);
  a.sc.drawOverlay();
  eq(a.ov.calls.filter((c) => c[0] === "fillText" && c[1] === a.sc.pal.passText).length, 0, "B the audio scope: no label (it IS the passband)");
  // a sweep replaces the static view's meta
  const s = scope();
  s.sc.showStatic(14074000, 200000);
  s.sc.rowBuf = null;                                     // parked: pushSweep keeps the meta and draws nothing
  s.sc.pushSweep({ mode: 0, center: 14074000, span: 50000, tuned: 14074000, filterBw: 2400 }, new Uint8Array(4));
  eq(s.sc.meta.still, undefined, "B a sweep ends the band-plan view");
}

// ---- D. a Yaesu's data filter (FT-991A 062-065, FT-891 08-01..08-04): where it sits, what it cuts, the fix ----
{
  const R = { protocol: "yaesu", data_menus: { mode: 62, tone: 63, disp: 64, shift: 65 } };
  const st = (o) => Object.assign({ mode_name: "DATA-USB", narrow: 0, width: 17, data_mode: 0, psk_tone: 1000, data_disp: 0, data_shift: 0 }, o);
  const band = (o) => { const d = Passband.data(R, st(o)); return d && [d.lo, d.hi, d.center, d.width, d.others, d.disp]; };
  eq(band({}), [0, 2500, 1000, 3000, false, 0], "D factory PSK: centered on the PSK TONE (1000 Hz), 3 kHz wide: 0-2.5 kHz");
  eq(band({ data_mode: 1 }), [0, 1500, 0, 3000, true, 0],
     "D OTHERS at the factory OTHER SHIFT (0 Hz, the carrier): 0-1.5 kHz (the 14.230 MHz recording's filter)");
  eq(band({ data_mode: 1, data_shift: 1500 }), [0, 3000, 1500, 3000, true, 0], "D OTHERS, OTHER SHIFT +1500: 0-3 kHz");
  eq(band({ psk_tone: 2000, width: 12 }), [1400, 2600, 2000, 1200, false, 0], "D PSK 2000 Hz at 1.2 kHz: 1.4-2.6 kHz");
  eq(band({ mode_name: "DATA-LSB", data_mode: 1, data_shift: 1500, data_disp: 1500 }), [0, 3000, 1500, 3000, true, 1500],
     "D DATA-LSB the same; OTHERS carries OTHER DISP");
  eq(band({ psk_tone: 1500, data_disp: 1500 }), [0, 3000, 1500, 3000, false, 0], "D PSK: OTHER DISP not counted (it is the OTHERS dial)");
  eq(band({ width: 0 }), [750, 1250, 1000, 500, false, 0], "D the DATA WIDTH default (00 = 500 Hz): 0.75-1.25 kHz");
  eq([Passband.data(R, st({ mode_name: "USB" })), Passband.data(R, st({ mode_name: "RTTY-USB" })), Passband.data(R, st({ mode_name: "DATA-FM" })),
      Passband.data({ protocol: "civ" }, st({})), Passband.data(R, st({ data_mode: undefined })), Passband.data(R, st({ psk_tone: undefined })),
      Passband.data(R, st({ data_mode: 1, data_shift: undefined })), Passband.data(R, st({ width: 3 })), Passband.data(null, st({})),
      Passband.data(R, null)],
     [null, null, null, null, null, null, null, null, null, null],
     "D null: voice, RTTY, DATA-FM, an Icom, a menu not answered yet, a WIDTH code not known, no profile, no state");
  // the fix, against what each Modem mode needs (digital.js NEED)
  const SSTV = [1050, 2350], FT8 = [300, 2500], RTTY = [2050, 2370];
  const fix = (o, need, r) => Passband.dataFix(r || R, st(o), need);
  eq([fix({}, SSTV), fix({}, FT8), fix({}, RTTY)], [null, null, null], "D the factory filter (PSK 1000 Hz, 3 kHz) passes SSTV, FT8 and RTTY");
  const f = fix({ data_mode: 1 }, SSTV);
  eq([f.cut, f.off, f.cmds, f.band.hi], [true, false, [{ action: "menu_write", num: 65, value: 1500 }], 1500],
     "D OTHERS at 0 Hz cuts SSTV: OTHER SHIFT to +1500 (the band it found with it)");
  eq([fix({ data_mode: 1 }, RTTY).cmds, fix({ data_mode: 1 }, FT8).cmds], [[{ action: "menu_write", num: 65, value: 1500 }],
     [{ action: "menu_write", num: 65, value: 1500 }]], "D ... RTTY and FT8 too");
  eq([fix({ data_mode: 1, data_shift: 1500 }, SSTV), fix({ data_mode: 1, data_shift: 1500 }, FT8), fix({ data_mode: 1, data_shift: 900 }, SSTV)],
     [null, null, null], "D OTHERS at +1500 passes; at +900 (0-2.4 kHz) SSTV still passes");
  eq(fix({ width: 0 }, SSTV).cmds, [{ action: "width", code: 17 }], "D PSK 1000 Hz at 500 Hz: the WIDTH to 3 kHz (code 17)");
  eq(fix({ psk_tone: 2000 }, FT8).cmds, [{ action: "menu_write", num: 63, value: "1500 Hz" }],
     "D PSK 2000 Hz cuts FT8 below 0.5 kHz: the PSK TONE to 1500 Hz (its option's name, as the Setup tab writes it)");
  const d = fix({ data_mode: 1, data_shift: 1500, data_disp: 1500 }, SSTV);
  eq([d.cut, d.off, d.cmds], [false, true, [{ action: "menu_write", num: 64, value: 0 }]],
     "D the filter right but the dial 1.5 kHz off the carrier (OTHER DISP): OTHER DISP to 0");
  eq(fix({ data_mode: 1, data_disp: -300 }, SSTV).cmds, [{ action: "menu_write", num: 65, value: 1500 }, { action: "menu_write", num: 64, value: 0 }],
     "D both: OTHER SHIFT, then OTHER DISP");
  eq(fix({ narrow: 1, width: 0 }, SSTV).cmds, [{ action: "narrow", on: false }, { action: "width", code: 17 }],
     "D NAR on (300 Hz) at PSK 1000 Hz: NAR off, then the WIDTH (a wide code needs NAR off); the tone stays");
  eq(fix({ data_mode: 1, narrow: 1, width: 0 }, SSTV).cmds,
     [{ action: "menu_write", num: 65, value: 1500 }, { action: "narrow", on: false }, { action: "width", code: 17 }],
     "D NAR on at OTHERS 0 Hz: 3 kHz alone passes 0-1.5 kHz, so the shift as well");
  eq(fix({ psk_tone: 1500, width: 12 }, SSTV).cmds, [{ action: "width", code: 17 }], "D centered at 1500 Hz but 1.2 kHz wide: the WIDTH only");
  eq(fix({ mode_name: "DATA-LSB", data_mode: 1, width: 16 }, SSTV).cmds,
     [{ action: "menu_write", num: 65, value: 1500 }, { action: "width", code: 17 }], "D DATA-LSB at 2.4 kHz: the shift and the WIDTH");
  eq(fix({ data_mode: 1 }, SSTV, { protocol: "yaesu", data_menus: { mode: 801, tone: 802, disp: 803, shift: 804 } }).cmds,
     [{ action: "menu_write", num: 804, value: 1500 }], "D the FT-891's own numbers (08-04 OTHER SHIFT)");
  const bare = fix({ data_mode: 1 }, SSTV, { protocol: "yaesu" });
  eq([bare.cut, bare.cmds], [true, []], "D a profile with no menu numbers: the cut said, nothing to send");
  eq([fix({ mode_name: "USB", data_mode: 1 }, SSTV), fix({ data_mode: 1 }, null), fix({ data_mode: 1 }, undefined)], [null, null, null],
     "D null: not a data mode, no need given");
}

// ---- C. app.js hands the scope the width ---------------------------------------------------------------
const PROFILES = [
  { id: "yx", make: "Test", name: "Yaesu X", protocol: "yaesu", has_scope: false, default_step: 100,
    bands: [{ name: "20", lo: 14000000, hi: 14350000 }, { name: "2", lo: 144000000, hi: 148000000 }],
    modes: ["LSB", "USB", "CW-USB", "FM", "DATA-USB"], steps: [{ v: 100, label: "100 Hz" }],
    capabilities: { narrow: true, ext_ops: true }, data_menus: { mode: 62, tone: 63, disp: 64, shift: 65 } },
  { id: "ix", make: "Test", name: "Icom X", protocol: "civ", has_scope: true, default_step: 100,
    bands: [{ name: "20", lo: 14000000, hi: 14350000 }], modes: ["LSB", "USB", "CW"], steps: [{ v: 100, label: "100 Hz" }],
    capabilities: { data_mode: true } },
];
function jsonResponse(status, body) { return { ok: status >= 200 && status < 300, status, json: async () => body }; }
async function appEnv(perms) {
  const sockets = [];
  function FakeWS() { this.readyState = 1; this.sent = []; this.send = (d) => this.sent.push(JSON.parse(d)); this.close = () => {}; sockets.push(this); }
  const fetch = (url) => Promise.resolve(String(url).split("?")[0] === "/api/radios" ? jsonResponse(200, { radios: PROFILES })
    : jsonResponse(404, { ok: false, error: "not_found" }));
  const e = makeEnv({ WebSocket: FakeWS, fetch });
  e.load("waterfall.js");                                // Passband, as the page loads it before app.js
  e.ctx.Scope = function () { return new Proxy({ meta: {} }, { get: (t, k) => (k in t ? t[k] : () => []) }); };
  for (const id of ["bandRow", "modeRow", "radioSel", "step", "toasts", "widthVal"]) e.el("div", { id });
  e.wUp = e.el("button", { "data-act": "width_d", "data-d": "1" });
  e.load("tr.js");
  e.load("app.js");
  const ws = sockets[sockets.length - 1];
  e.frame = (f) => ws.onmessage({ data: JSON.stringify(f) });
  e.frame({ type: "hello", v: 1, conn: 3, user: "ME", role: "operator", perms: perms || ["view", "listen", "operate"],
            status: { connected: true, radio: "yx" }, tx: { owner: null }, hb_interval: 2 });
  for (let i = 0; i < 6; i++) await e.flush();
  e.sent = ws.sent;
  e.state = (o) => e.frame(Object.assign({ type: "state", connected: true, radio: "yx", freq: 14074000, mode_name: "USB",
    filter: 1, filter_bw: 2400, narrow: 0, width: 0, main: { freq: (o && o.freq) || 14074000 }, sub: { freq: 0 }, active_band: "main" }, o || {}));
  e.bw = () => e.ctx.RadioScope.meta.filterBw;
  return e;
}
(async function main() {
  {
    const e = await appEnv();
    e.state({ mode_name: "DATA-USB", width: 17 });
    eq(e.bw(), 3000, "C Yaesu static view: DATA-USB at WIDTH 17 -> 3 kHz");
    eq(e.doc.getElementById("widthVal").textContent, "3000 Hz", "C ... the WIDTH readout reads the same table");
    e.state({ mode_name: "DATA-USB", width: 16 });
    e.sent.length = 0;
    e.wUp.click();
    eq(e.sent.filter((f) => f.action === "width"), [{ action: "width", code: 17 }], "C ... and the WIDTH + key steps it (2.4 -> 3 kHz)");
    e.state({ mode_name: "CW-USB", narrow: 1, width: 0 });
    eq(e.bw(), 500, "C CW-USB with NAR at 00 -> 500 Hz");
    e.state({ mode_name: "USB", width: 0 });
    eq(e.bw(), 2400, "C USB at 00 -> 2.4 kHz");
    e.state({ mode_name: "FM", freq: 146520000, width: 0, filter_bw: 2400 });
    eq(e.bw(), 0, "C FM -> 0: no band (it used to draw the never-updated 2.4 kHz filter_bw)");
  }
  {
    const e = await appEnv();
    e.frame({ type: "state", connected: true, radio: "ix", freq: 14030000, mode_name: "CW", filter: 2, filter_bw: 500, data: 0,
              width: 0, main: { freq: 14030000 }, sub: { freq: 0 }, active_band: "main" });
    for (let i = 0; i < 4; i++) await e.flush();
    eq(e.bw(), 500, "C an Icom (the server switched to it): filter_bw, as before (CW FIL2 500 Hz)");
  }
  {
    // the Modem window's data-filter key (digital.js: tr:datafix {need, label}): app.js mends it from its own state
    const e = await appEnv(["view", "listen", "operate", "menu"]);
    const ask = (detail) => e.doc.dispatchEvent(new e.ctx.CustomEvent("tr:datafix", { detail }));
    const SSTV = [1050, 2350], acts = () => e.sent.filter((f) => ["menu_write", "width", "narrow"].includes(f.action));
    const toasts = () => e.doc.getElementById("toasts").textContent;
    e.state({ mode_name: "DATA-USB", width: 17, data_mode: 1, psk_tone: 1000, data_disp: 0, data_shift: 0 });
    e.sent.length = 0;
    ask({ need: SSTV, label: "Center it at 1.5 kHz" });
    eq(acts(), [{ action: "menu_write", num: 65, value: 1500 }], "C tr:datafix: app.js sends what Passband.dataFix finds (OTHER SHIFT +1500)");
    check(toasts().includes("Center it at 1.5 kHz: sent to the radio."), "C ... said in the key's words: " + toasts());
    e.state({ mode_name: "DATA-USB", width: 0, data_mode: 1, psk_tone: 1000, data_disp: 0, data_shift: 0 });
    e.sent.length = 0;
    ask({ need: SSTV, label: 7 });
    eq(acts(), [{ action: "menu_write", num: 65, value: 1500 }, { action: "width", code: 17 }], "C ... 500 Hz wide too: the shift, then the WIDTH");
    check(toasts().includes("The data filter: sent to the radio."), "C ... a label that is no text: plain words");
    // nothing sent: a need that is no [lo, hi] (the filter cut), the filter right, the radio on air
    e.state({ mode_name: "DATA-USB", width: 17, data_mode: 1, psk_tone: 1000, data_disp: 0, data_shift: 0 });
    e.sent.length = 0;
    for (const need of [undefined, "1050-2350", [1050], [1050, 2350, 0], [1050, NaN], ["1050", "2350"], [1050, Infinity]]) ask({ need });
    ask(null);
    eq(acts(), [], "C a need that is no [lo, hi] of numbers: nothing sent, though the filter cuts SSTV");
    e.state({ mode_name: "DATA-USB", width: 17, data_mode: 1, psk_tone: 1000, data_disp: 0, data_shift: 1500 });
    ask({ need: SSTV });
    eq(acts(), [], "C nothing to mend (OTHER SHIFT +1500): nothing sent");
    e.state({ mode_name: "DATA-USB", width: 17, data_mode: 1, psk_tone: 1000, data_disp: 0, data_shift: 0, ptt: true });
    ask({ need: SSTV });
    eq(acts(), [], "C the radio on air: nothing sent");
    e.state({ mode_name: "USB", width: 17, data_mode: 1, psk_tone: 1000, data_disp: 0, data_shift: 0 });
    ask({ need: SSTV });
    eq(acts(), [], "C voice USB: nothing sent (its own filter)");
  }
  {
    const e = await appEnv();                               // operate, no menu permission
    e.state({ mode_name: "DATA-USB", width: 17, data_mode: 1, psk_tone: 1000, data_disp: 0, data_shift: 0 });
    e.sent.length = 0;
    e.doc.dispatchEvent(new e.ctx.CustomEvent("tr:datafix", { detail: { need: [1050, 2350] } }));
    eq(e.sent.filter((f) => f.action === "menu_write" || f.action === "width"), [], "C no menu permission: nothing sent");
  }
  console.log(failures ? failures + " of " + checks + " checks FAILED" : "ALL PASS (" + checks + " checks)");
  process.exit(failures ? 1 : 0);
})().catch((err) => { console.log("FAIL (crash) " + (err && err.stack || err)); process.exit(1); });
