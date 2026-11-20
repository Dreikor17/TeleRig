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
 *     filter_bw; an Icom's filter_bw), and the WIDTH readout and -/+ keys read the same table.
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

// ---- C. app.js hands the scope the width ---------------------------------------------------------------
const PROFILES = [
  { id: "yx", make: "Test", name: "Yaesu X", protocol: "yaesu", has_scope: false, default_step: 100,
    bands: [{ name: "20", lo: 14000000, hi: 14350000 }, { name: "2", lo: 144000000, hi: 148000000 }],
    modes: ["LSB", "USB", "CW-USB", "FM", "DATA-USB"], steps: [{ v: 100, label: "100 Hz" }],
    capabilities: { narrow: true, ext_ops: true } },
  { id: "ix", make: "Test", name: "Icom X", protocol: "civ", has_scope: true, default_step: 100,
    bands: [{ name: "20", lo: 14000000, hi: 14350000 }], modes: ["LSB", "USB", "CW"], steps: [{ v: 100, label: "100 Hz" }],
    capabilities: { data_mode: true } },
];
function jsonResponse(status, body) { return { ok: status >= 200 && status < 300, status, json: async () => body }; }
async function appEnv() {
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
  e.frame({ type: "hello", v: 1, conn: 3, user: "ME", role: "operator", perms: ["view", "listen", "operate"],
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
  console.log(failures ? failures + " of " + checks + " checks FAILED" : "ALL PASS (" + checks + " checks)");
  process.exit(failures ? 1 : 0);
})().catch((err) => { console.log("FAIL (crash) " + (err && err.stack || err)); process.exit(1); });
