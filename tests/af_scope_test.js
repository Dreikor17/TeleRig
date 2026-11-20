/* The audio (AF) scope's frequency map (waterfall.js AfMap), tested on the REAL waterfall.js in a Node vm
 * (tests/fakedom.js). No browser, no server, no socket, no radio. Run by tests/t_af_scope.py (unit group):
 *
 *     node tests/af_scope_test.js     -> exit 0 and "ALL PASS", or exit 1 naming each failure
 *
 * A model radio below turns an RF signal into the tone it really plays: its BFO side and, in CW, its pitch
 * offset (the FT-891 / FT-991A manuals: in CW the dial shows the zero beat = the frequency heard AT the
 * pitch, unless CW FREQ DISPLAY is set to FREQ / DIRECT FREQ; the IC-728 BFO table: an LSB-side 800 Hz).
 * For every case the scope must
 *   1. draw each tone where its RF reads back (freqAtX at the tone's pixel = the signal's RF), so the
 *      edge labels, the cursor readout and the band plan read true;
 *   2. in CW, draw the tuned marker at the pixel of the PITCH, carrying the frequency heard there;
 *   3. on a tap on a signal, tune so the radio plays it AT THE PITCH, i.e. on the marker. The reported
 *      bug: a tap on a CW-LSB signal tuned it to a 0 Hz tone, the deaf right-hand edge.
 *
 *  A. AfMap.cw: side and pitch (profile sides, the FT-891 CW BFO menu incl. AUTO, KP, the fixed IC-728
 *     pitch) and the CW FREQ DISPLAY setting.
 *  B. CW: both sides x both display settings x five pitches x four dials, with taps across the passband;
 *     plus the reported case.
 *  C. SSB / data: unchanged (the dial is an edge, no marker, a tap makes the tapped point the carrier).
 *  D. drawOverlay draws the CW marker line at the pitch and labels it with the dial to 10 Hz.
 *  E. The CW zoom (AfMap.frame): with a CW filter (up to 1.2 kHz, centred on the pitch), 2.5 widths centred
 *     on the pitch (400 Hz at the least, inside 0..3600 Hz); the other modes, CW with a wider filter (it may
 *     be the radio's SSB filter, placed by its BFO: the IC-728's CW) or the width not known, 0..3600 Hz.
 *  F. The zoomed view reads true like B (every tone its own RF, the marker at the pitch, a tap lands the
 *     signal on it) on both sides, both display settings, five pitches and five widths, and shades the
 *     passband: the width, centred on the marker.
 *  G. The FFT for a frame (AfMap.plan: ~1 bin a pixel, 4096..16384; as before for 0..3600 Hz) and the
 *     passband's bins (AfMap.pass; none for a wider filter, which keeps the mean floor).
 *  H. The noise floor (AfMap.floor) on synthetic frames through a CW filter: the old mean lights the
 *     passband's rim up in two humps (the reported picture; a 250 Hz filter all over); measured inside the
 *     passband the rim looks like the middle, the noise like a flat band's on the old path, the outside
 *     dark; a filter wider or narrower than its nominal width, or off the pitch, adds no hump; signals
 *     don't lift the floor.
 */
"use strict";
const { makeEnv } = require("./fakedom");

let failures = 0, checks = 0;
const pending = [];
function check(cond, msg) { checks++; if (!cond) { failures++; console.log("FAIL " + msg); } }
function eq(a, b, msg) { const ja = JSON.stringify(a), jb = JSON.stringify(b); check(ja === jb, msg + ": got " + ja + ", want " + jb); }
function near(a, b, tol, msg) { check(Math.abs(a - b) <= tol, msg + ": got " + a + ", want " + b + " (+/- " + tol + ")"); }
function test(name, fn) { pending.push([name, fn]); }

const env = makeEnv();
env.load("waterfall.js");
const { AfMap, Scope } = env.ctx;
const SPAN = 3600, W = 900;                      // app.js AF_MAX; any scope width

// The radio: the tone (Hz) it plays for a signal at RF f. side +1 = carrier on the USB side, -1 = LSB.
// With the pitch shown (factory) the dial is the zero beat, so the carrier sits `pitch` Hz away.
function tone(f, dial, side, pitch, shown) {
  const carrier = shown ? dial - side * pitch : dial;
  return side * (f - carrier);
}
function scopeFor(meta) {                        // only what freqToX / freqAtX / visibleRange read
  const sc = Object.create(Scope.prototype);
  Object.assign(sc, { meta, W });
  return sc;
}
// where the AF scope draws a tone: the FFT bins left to right, mirrored on the LSB side (app.js)
function xOfTone(a, view) { return (view.lsb ? 1 - a / SPAN : a / SPAN) * W; }
// what a tap does (app.js scEnd): freqAtX + retune, snapped to 10 Hz on this narrow view
function tap(sc, x) { return Math.round((sc.freqAtX(x) + (sc.meta.retune || 0)) / 10) * 10; }

const FT891 = { cw_sides: { "CW-USB": 1, "CW-LSB": -1 }, cw_pitch_hz: 0 };
const FT991A = { cw_sides: { "CW-USB": 1, "CW-LSB": -1 }, cw_pitch_hz: 0 };
const IC728 = { cw_sides: { "CW": -1, "CW-NAR": -1 }, cw_pitch_hz: 800 };
const IC9700 = { cw_sides: {}, cw_pitch_hz: 0 };

test("A AfMap.cw: side, pitch and display setting", () => {
  eq(AfMap.cw(FT991A, { key_pitch: 40 }, "CW-USB"), { side: 1, pitch: 700, shown: true }, "A FT-991A CW-U, KP40");
  eq(AfMap.cw(FT991A, { key_pitch: 30 }, "CW-LSB"), { side: -1, pitch: 600, shown: true }, "A FT-991A CW-L, KP30");
  eq(AfMap.cw(FT991A, {}, "CW-USB"), { side: 1, pitch: 700, shown: true }, "A nothing read yet: 700 Hz, PITCH display");
  eq(AfMap.cw(FT991A, { key_pitch: 0, cw_disp: 0 }, "CW-USB"), { side: 1, pitch: 300, shown: false }, "A DIRECT FREQ, KP00 = 300 Hz");
  eq(AfMap.cw(FT991A, { key_pitch: 75, cw_disp: 1 }, "CW-USB").pitch, 1050, "A KP75 = 1050 Hz");
  for (const m of ["USB", "LSB", "RTTY-LSB", "DATA-USB", "AM", "FM"]) eq(AfMap.cw(FT991A, {}, m), null, "A " + m + " is not CW");
  // FT-891: MD3 and MD7 are both "CW (CW BFO)": menu 07-07 decides the side
  eq(AfMap.cw(FT891, { cw_bfo: 0, freq: 7020000 }, "CW-LSB").side, 1, "A FT-891 CW BFO USB wins over the mode name");
  eq(AfMap.cw(FT891, { cw_bfo: 1, freq: 14020000 }, "CW-USB").side, -1, "A FT-891 CW BFO LSB wins over the mode name");
  eq(AfMap.cw(FT891, { cw_bfo: 2, freq: 7020000 }, "CW-USB").side, -1, "A FT-891 AUTO on 40 m = LSB");
  eq(AfMap.cw(FT891, { cw_bfo: 2, freq: 3550000 }, "CW-USB").side, -1, "A FT-891 AUTO on 80 m = LSB");
  eq(AfMap.cw(FT891, { cw_bfo: 2, freq: 10110000 }, "CW-LSB").side, 1, "A FT-891 AUTO on 30 m = USB");
  eq(AfMap.cw(FT891, { cw_bfo: 2, freq: 14020000 }, "CW-LSB").side, 1, "A FT-891 AUTO on 20 m = USB");
  eq(AfMap.cw(FT891, { freq: 7020000 }, "CW-LSB").side, -1, "A FT-891 menu not read yet: the mode name");
  // IC-728: a fixed 800 Hz on the LSB side, the display is the TX frequency; KP never applies
  eq(AfMap.cw(IC728, { key_pitch: 10 }, "CW"), { side: -1, pitch: 800, shown: true }, "A IC-728 CW");
  eq(AfMap.cw(IC728, {}, "CW-NAR"), { side: -1, pitch: 800, shown: true }, "A IC-728 CW-NAR");
  eq(AfMap.cw(IC728, {}, "USB"), null, "A IC-728 USB is not CW");
  eq(AfMap.cw(IC9700, {}, "CW"), null, "A a radio without cw_sides gets no CW map");
  eq(AfMap.cw(null, {}, "CW-USB"), null, "A no radio yet");
});

test("B CW: each signal reads its own RF, the marker is the pitch, a tap puts the signal on the marker", () => {
  let taps = 0;
  for (const side of [1, -1]) for (const shown of [true, false]) for (const pitch of [300, 600, 700, 800, 1050])
    for (const dial of [7020900, 7030000, 14025000, 3555500]) {
      const cw = { side, pitch, shown };
      const view = AfMap.view(dial, SPAN, "CW-ANY", cw), sc = scopeFor(view);
      const tag = "B side " + side + (shown ? " PITCH" : " DIRECT") + " pitch " + pitch + " dial " + dial;
      eq(view.lsb, side < 0, tag + ": mirrored exactly on the LSB side");
      eq(view.upper - view.lower, SPAN, tag + ": span");
      near(sc.freqToX(view.tuned), xOfTone(pitch, view), 1e-6, tag + ": the marker is drawn at the pitch");
      eq(tone(view.tuned, dial, side, pitch, shown), pitch, tag + ": the marker's frequency sounds at the pitch");
      if (shown) eq([view.tuned, view.retune], [dial, 0], tag + ": with PITCH display the marker IS the dial");
      for (let a = 150; a <= 3450; a += 330) {                // signals across the passband
        const f = (shown ? dial - side * pitch : dial) + side * a;     // the RF that plays tone a
        eq(tone(f, dial, side, pitch, shown), a, tag + ": model check");
        const x = xOfTone(a, view);
        near(sc.freqAtX(x), f, 1e-6, tag + ": tone " + a + " Hz reads back its RF");
        const dial2 = tap(sc, x), a2 = tone(f, dial2, side, pitch, shown);
        near(a2, pitch, 5, tag + ": a tap on tone " + a + " Hz -> heard at the pitch");
        const view2 = AfMap.view(dial2, SPAN, "CW-ANY", cw);
        near(scopeFor(view2).freqToX(view2.tuned), xOfTone(a2, view2), 2, tag + ": after the tap the signal sits on the marker");
        taps++;
      }
    }
  check(taps === 2 * 2 * 5 * 4 * 11, "B covered " + taps + " taps");
});

test("B the reported case: a tapped CW-LSB signal is heard at the pitch, not moved to 0 Hz (the deaf edge)", () => {
  // 40 m, an FT-891 in CW on the LSB side (07-07 AUTO), pitch 700 Hz, a signal playing at 590 Hz
  const st = { freq: 7020900, key_pitch: 40, cw_bfo: 2, cw_disp: 1 };
  const cw = AfMap.cw(FT891, st, "CW-USB");
  const view = AfMap.view(st.freq, SPAN, "CW-USB", cw), sc = scopeFor(view);
  const f = st.freq + 700 - 590;                              // the RF that plays 590 Hz on the LSB side
  const dial2 = tap(sc, xOfTone(590, view));
  eq(dial2, f, "the tap tunes the radio to the signal's own frequency");
  eq(tone(f, dial2, -1, 700, true), 700, "the signal now plays at the 700 Hz pitch (it went to 0 Hz before)");
  check(xOfTone(700, view) < W - 100 && xOfTone(700, view) > 100, "the pitch line is inside the scope, clear of the edges");
});

test("C SSB / data: the dial is an edge, no marker, a tap makes the tapped point the carrier (unchanged)", () => {
  for (const [mode, lsb] of [["USB", false], ["LSB", true], ["DATA-USB", false], ["DATA-LSB", true],
                             ["RTTY-LSB", true], ["RTTY-USB", false], ["AM", false]]) {
    const dial = 7074000, view = AfMap.view(dial, SPAN, mode, null), sc = scopeFor(view);
    eq([view.lsb, view.tuned, view.retune], [lsb, 0, 0], "C " + mode + ": side, no marker, no retune");
    eq([view.lower, view.upper], lsb ? [dial - SPAN, dial] : [dial, dial + SPAN], "C " + mode + ": edges as before");
    const a = 1500, f = lsb ? dial - a : dial + a;
    near(sc.freqAtX(xOfTone(a, view)), f, 1e-6, "C " + mode + ": a tone reads back its RF");
    eq(tap(sc, xOfTone(a, view)), f, "C " + mode + ": a tap tunes the dial to the tapped point");
  }
});

test("D drawOverlay: the CW marker line at the pitch, labelled with the dial to 10 Hz", () => {
  const texts = [], xs = [];
  const ctx2d = new Proxy({ fillText: (t) => texts.push(String(t)), moveTo: (x) => xs.push(x),
                            measureText: (t) => ({ width: String(t).length * 6 }) },
                          { get: (t, k) => (k in t ? t[k] : () => {}), set: (t, k, v) => { t[k] = v; return true; } });
  for (const [mode, side] of [["CW-LSB", -1], ["CW-USB", 1]]) {
    texts.length = 0; xs.length = 0;
    const st = { freq: 7020910, key_pitch: 40, cw_disp: 1 };
    const view = AfMap.view(st.freq, SPAN, mode, AfMap.cw(FT991A, st, mode));
    eq(view.lsb, side < 0, "D " + mode + ": side");
    const sc = Object.create(Scope.prototype);
    Object.assign(sc, { octx: ctx2d, W, scopeH: 300, specH: 150, wfH: 150, meta: view, opMode: mode,
                        showBandplan: false, bandplan: [] });
    sc.drawOverlay();
    check(texts.includes("7.02091 MHz"), "D " + mode + ": the marker label reads the dial to 10 Hz (texts " + JSON.stringify(texts) + ")");
    check(xs.some((x) => Math.abs(x - xOfTone(700, view)) < 1e-6), "D " + mode + ": the marker line is at the pitch (x " + JSON.stringify(xs) + ")");
  }
});

// ---- the CW zoom -----------------------------------------------------------------------------------
const cwOf = (pitch, side, shown) => ({ side: side || 1, pitch, shown: shown !== false });
test("E the zoom: in CW with the width known, 2.5 widths centred on the pitch; otherwise 0..3600 Hz", () => {
  eq(AfMap.frame(cwOf(700), 500, SPAN), { a0: 75, span: 1250 }, "E 500 Hz at a 700 Hz pitch: 1250 Hz, the pitch in the middle");
  eq(AfMap.frame(cwOf(700), 250, SPAN), { a0: 388, span: 625 }, "E 250 Hz: 625 Hz");
  eq(AfMap.frame(cwOf(600), 50, SPAN), { a0: 400, span: 400 }, "E 50 Hz: no narrower than 400 Hz");
  eq(AfMap.frame(cwOf(800, -1), 500, SPAN), { a0: 175, span: 1250 }, "E the IC-728's 800 Hz LSB-side pitch, CW-NAR 500 Hz");
  eq(AfMap.frame(cwOf(300), 500, SPAN), { a0: 0, span: 1250 }, "E a 300 Hz pitch: from 0 Hz (no tone below it)");
  eq(AfMap.frame(cwOf(700), 1200, SPAN), { a0: 0, span: 3000 }, "E 1200 Hz: 3 kHz, from 0 Hz");
  eq(AfMap.frame(cwOf(700), 2400, SPAN), { a0: 0, span: 3600 }, "E 2400 Hz (the CW default): the whole 3.6 kHz, as before");
  eq(AfMap.frame(cwOf(700), 1400, SPAN), { a0: 0, span: 3600 }, "E 1400 Hz: wider than a CW filter (1.2 kHz): as before");
  eq(AfMap.frame(cwOf(800, -1), 2400, SPAN), { a0: 0, span: 3600 }, "E the IC-728's CW (its 2.4 kHz SSB filter): as before");
  eq(AfMap.view(7020000, 3600, "CW", cwOf(800, -1), 0, 2400).filterBw, 0, "E ... and no band drawn around the pitch (that filter isn't centred on it)");
  eq(AfMap.frame(cwOf(700), 0, SPAN), { a0: 0, span: 3600 }, "E the width not known: as before");
  eq(AfMap.frame(null, 2400, SPAN), { a0: 0, span: 3600 }, "E SSB / data: as before, whatever the width");
  const v = AfMap.view(7074000, 3600, "USB", null, 0, 2400);
  eq([v.lower, v.upper, v.tuned, v.filterBw], [7074000, 7077600, 0, 0], "E SSB: the edges as before, no band drawn");
});

test("F the zoomed view reads true and shades the passband around the marker", () => {
  let taps = 0;
  for (const side of [1, -1]) for (const shown of [true, false]) for (const pitch of [300, 600, 700, 800, 1050])
    for (const bw of [50, 250, 500, 800, 1200]) {
      const cw = cwOf(pitch, side, shown), fr = AfMap.frame(cw, bw, SPAN), dial = 14025000;
      const view = AfMap.view(dial, fr.span, "CW-ANY", cw, fr.a0, bw), sc = scopeFor(view);
      const tag = "F side " + side + (shown ? " PITCH" : " DIRECT") + " pitch " + pitch + " width " + bw;
      const xOf = (a) => { const u = (a - fr.a0) / fr.span; return (view.lsb ? 1 - u : u) * W; };
      eq([view.lsb, view.upper - view.lower, view.filterBw], [side < 0, fr.span, bw], tag + ": side, span, the width");
      near(sc.freqToX(view.tuned), xOf(pitch), 1e-6, tag + ": the marker is drawn at the pitch");
      if (fr.a0 > 0 && fr.a0 + fr.span < SPAN) near(sc.freqToX(view.tuned), W / 2, 1, tag + ": ... in the middle");
      for (let j = 0; j <= 10; j++) {
        const a = fr.a0 + fr.span * (0.04 + 0.092 * j);              // tones across the frame
        const f = (shown ? dial - side * pitch : dial) + side * a;   // the RF that plays tone a
        near(sc.freqAtX(xOf(a)), f, 1e-6, tag + ": tone " + a.toFixed(0) + " Hz reads back its RF");
        const dial2 = Math.round((sc.freqAtX(xOf(a)) + view.retune) / 10) * 10;
        near(tone(f, dial2, side, pitch, shown), pitch, 5, tag + ": a tap on it -> heard at the pitch");
        taps++;
      }
      // the band drawn: the width, centred on the marker
      const calls = [];
      const t = { fillStyle: null, fillRect: (...a) => calls.push([t.fillStyle, ...a]), measureText: (s) => ({ width: String(s).length * 6 }) };
      const ctx = new Proxy(t, { get: (o, k) => (k in o ? o[k] : () => {}), set: (o, k, v) => { o[k] = v; return true; } });
      Object.assign(sc, { octx: ctx, scopeH: 300, specH: 150, wfH: 150, opMode: side < 0 ? "CW-LSB" : "CW-USB", showBandplan: false, bandplan: [] });
      sc.drawOverlay();
      const band = calls.filter((c) => c[0] === sc.pal.pass);
      check(band.length === 1, tag + ": one passband drawn (" + band.length + ")");
      if (band.length) {
        near(band[0][3], bw / fr.span * W, 1e-6, tag + ": the band is the width");
        near(band[0][1] + band[0][3] / 2, xOf(pitch), 1e-6, tag + ": ... centred on the marker");
      }
    }
  check(taps === 2 * 2 * 5 * 5 * 11, "F covered " + taps + " taps");
});

test("G the FFT for a frame, and the passband's bins", () => {
  const a = AfMap.plan({ a0: 0, span: 3600 }, 48000, 700);
  eq([a.fftSize, a.k0, a.n], [8192, 0, 614], "G 0..3600 Hz over 700 px at 48 kHz: 8192, as before");
  eq(AfMap.plan({ a0: 0, span: 3600 }, 48000, 1400).fftSize, 16384, "G ... over 1400 px: 16384, as before");
  for (const [fr, sr, Wpx, want] of [[{ a0: 75, span: 1250 }, 48000, 700, 16384], [{ a0: 388, span: 625 }, 48000, 700, 16384],
                                     [{ a0: 400, span: 400 }, 44100, 500, 16384], [{ a0: 0, span: 3000 }, 48000, 700, 8192],
                                     [{ a0: 0, span: 1250 }, 48000, 300, 8192], [{ a0: 0, span: 3600 }, 48000, 200, 4096]]) {
    const p = AfMap.plan(fr, sr, Wpx), tag = "G " + JSON.stringify(fr) + " at " + sr + " over " + Wpx + " px";
    eq(p.fftSize, want, tag + ": the FFT size");
    near(p.k0 * p.binHz, fr.a0, p.binHz / 2, tag + ": the first bin is the frame's lowest tone");
    near(p.n * p.binHz, fr.span, p.binHz / 2, tag + ": the bins span the frame");
    check(p.k0 + p.n <= p.fftSize / 2, tag + ": inside the analyser's bins");
  }
  const cw = cwOf(700), fr = AfMap.frame(cw, 500, SPAN), p = AfMap.plan(fr, 48000, 700), pb = AfMap.pass(cw, 500, p);
  near((p.k0 + pb[0]) * p.binHz, 450, p.binHz / 2, "G the passband starts at the pitch - 250 Hz");
  near((p.k0 + pb[1]) * p.binHz, 950, p.binHz / 2, "G ... and ends at the pitch + 250 Hz");
  eq([AfMap.pass(cw, 0, p), AfMap.pass(null, 500, p)], [null, null], "G no passband when the width is not known, or not CW");
  eq([AfMap.pass(cw, 1200, p) !== null, AfMap.pass(cw, 1400, p), AfMap.pass(cw, 2400, p)], [true, null, null],
     "G a CW filter up to 1.2 kHz has its passband; a wider one none (the mean floor, as before)");
});

// ---- the noise floor --------------------------------------------------------------------------------
// Synthetic frames: each bin's power is exponential (as the analyser's are with no smoothing), through a CW
// filter of -6 dB width `real` with raised-cosine skirts T Hz wide, over the audio's own floor 50 dB down;
// tones add power to their bin (and the window's leakage beside it). Brightness is app.js's mapping
// (AF_MARGIN_DB 3, AF_RANGE_DB 30: t_af_scope.py pins them). A seeded generator: the same frames every run.
let seed = 7;
function rnd() { seed = (seed * 1664525 + 1013904223) >>> 0; return (seed + 0.5) / 4294967296; }
const bright = (v) => { const r = (v - 3) * 160 / 30; return r <= 0 ? 0 : r >= 160 ? 160 : r; };
const shape = (f, c, bw, T) => { const d = Math.abs(f - c) - bw / 2; return d <= -T / 2 ? 1 : d >= T / 2 ? 0 : 0.5 * (1 + Math.cos(Math.PI * (d + T / 2) / T)); };
function run({ pitch = 700, bw = 500, real, T = 60, shift = 0, tones = [], frames = 80, old = false }) {
  const cw = bw ? cwOf(pitch) : null, fr = AfMap.frame(cw, bw, SPAN), pl = AfMap.plan(fr, 48000, 700), n = pl.n;
  const pb = AfMap.pass(cw, bw, pl), H = Math.max(6, Math.round(250 / pl.binHz));
  const b = new Float32Array(n), out = new Float32Array(n), tmp = new Float32Array(n), acc = new Float64Array(n), fl = new Float64Array(n);
  const rw = real || bw || 1e6;
  for (let k = 0; k < frames; k++) {
    for (let i = 0; i < n; i++) {
      const f = (pl.k0 + i) * pl.binHz, g = shape(f, pitch + shift, rw, T);
      let p = (g * g + 1e-5) * -Math.log(rnd());
      for (const t of tones) { const d = Math.abs(f - t.f) / pl.binHz; if (d < 1.5) p += t.p * (d < 0.5 ? 1 : 0.35); }
      b[i] = 10 * Math.log10(p);
    }
    AfMap.floor(b, n, H, old ? null : pb, out, tmp);
    for (let i = 0; i < n; i++) { acc[i] += bright(b[i] - out[i]) / frames; fl[i] += out[i] / frames; }
  }
  const w = pb ? pb[1] - pb[0] : 0;
  // the mean over the bins of the ranges [lo, hi] that are inside the frame
  const avg = (a, ...rs) => { let s = 0, c = 0; for (const [lo, hi] of rs) for (let i = Math.max(0, lo); i <= Math.min(n - 1, hi); i++) { s += a[i]; c++; } return c ? s / c : 0; };
  const r = (...rs) => avg(acc, ...rs);
  if (!pb) return { all: r([0, n - 1]), acc, pl, n };
  const q = (x) => Math.round(x);
  return { pl, pb, n, acc,
           middle: r([q(pb[0] + 0.25 * w), q(pb[1] - 0.25 * w)]),
           rim: r([pb[0], q(pb[0] + 0.15 * w)], [q(pb[1] - 0.15 * w), pb[1]]),
           skirt: r([q(pb[0] - 0.15 * w), pb[0] - 1], [pb[1] + 1, q(pb[1] + 0.15 * w)]),
           outside: r([0, q(pb[0] - 0.15 * w)], [q(pb[1] + 0.15 * w), n - 1]),
           floorMid: avg(fl, [q(pb[0] + 0.25 * w), q(pb[1] - 0.25 * w)]),
           at: (hz) => Math.max(...[-1, 0, 1].map((d) => acc[Math.round(hz / pl.binHz) - pl.k0 + d] || 0)) };
}
const f1 = (x) => x.toFixed(1);

test("H the noise floor: measured inside the passband, no humps at its edges", () => {
  const flat = run({ bw: 0 }).all;                     // a flat band on the old path: the look to keep
  check(flat > 2 && flat < 7, "H the flat band's noise is a faint texture (" + f1(flat) + ")");
  // the reported picture: the old mean across a 500 Hz filter's edges
  const o5 = run({ bw: 500, old: true });
  check(o5.rim > 3 * o5.middle && o5.rim > 40, "H the old mean on 500 Hz: the rim lights up in two humps (rim " + f1(o5.rim) + ", middle " + f1(o5.middle) + ")");
  const o25 = run({ bw: 250, old: true });
  check(o25.middle > 60, "H the old mean on 250 Hz: the whole passband lit (" + f1(o25.middle) + ")");
  for (const [tag, o] of [["500 Hz", { bw: 500 }], ["500 Hz, soft skirts", { bw: 500, T: 150 }], ["250 Hz", { bw: 250 }],
                          ["50 Hz", { bw: 50, T: 25 }], ["800 Hz", { bw: 800 }], ["1200 Hz", { bw: 1200 }],
                          ["500 Hz at a 300 Hz pitch", { bw: 500, pitch: 300 }], ["500 Hz at 1050 Hz", { bw: 500, pitch: 1050 }]]) {
    const r = run(o);
    // (a 50 Hz filter is only 17 bins, so its floor is a little noisier and its noise a little brighter)
    near(r.middle, flat, o.bw > 50 ? 0.8 : 1.5, "H " + tag + ": the passband's noise looks like a flat band's (" + f1(r.middle) + " vs " + f1(flat) + ")");
    check(r.rim < r.middle + 2.5, "H " + tag + ": no humps: the rim no brighter than the middle (" + f1(r.rim) + " vs " + f1(r.middle) + ")");
    if (o.bw >= 250) near(r.rim, r.middle, 2.5, "H " + tag + ": ... it looks like the middle (a 50 Hz filter is all skirt: its rim is darker)");
    if (o.bw <= 800) check(r.outside < 0.5 && r.skirt < 1.5, "H " + tag + ": the outside dark (" + f1(r.outside) + ", skirt " + f1(r.skirt) + ")");
  }
  // the real filter wider / narrower than its nominal width, or off it: no hump either way
  const wide = run({ bw: 500, real: 600 });
  check(wide.skirt < wide.middle + 2 && wide.rim < wide.middle + 2.5, "H a filter 20% wider: just more of the same noise past the edges (skirt " + f1(wide.skirt) + ", rim " + f1(wide.rim) + ", middle " + f1(wide.middle) + ")");
  const nar = run({ bw: 500, real: 400 });
  check(nar.rim < nar.middle + 1 && nar.middle < flat + 3, "H a filter 20% narrower: its rim goes dark, no hump (rim " + f1(nar.rim) + ", middle " + f1(nar.middle) + ")");
  const sh = run({ bw: 500, shift: 100 });
  check(sh.middle < flat + 4 && sh.rim < sh.middle + 2.5, "H the filter 100 Hz off the pitch (IF SHIFT, not read): a little brighter, no hump (middle " + f1(sh.middle) + ", rim " + f1(sh.rim) + ")");
  // signals: shown, and they don't lift the floor
  const q = run({ bw: 250 }), t1 = run({ bw: 250, tones: [{ f: 700, p: 10 }] });
  const t3 = run({ bw: 250, tones: [{ f: 640, p: 30 }, { f: 700, p: 10 }, { f: 760, p: 30 }] });
  check(t1.at(700) > 40, "H a tone 10 dB over the noise shows bright (" + f1(t1.at(700)) + ")");
  const past = run({ bw: 500, tones: [{ f: 250, p: 10 }, { f: 1150, p: 10 }] });   // 200 Hz past each edge, 10 dB over the passband's noise
  check(past.at(250) > 40 && past.at(1150) > 40, "H outside the passband a signal that beats its noise still shows, either side (" + f1(past.at(250)) + ", " + f1(past.at(1150)) + ")");
  near(t3.at(700), t1.at(700), 6, "H ... as bright with two strong tones beside it in a 250 Hz filter");
  near(t3.floorMid, q.floorMid, 1.5, "H three tones in a 250 Hz filter lift the floor about 1 dB at most (" + f1(t3.floorMid - q.floorMid) + " dB)");
});

(async () => {
  for (const [name, fn] of pending) {
    try { await fn(); } catch (e) { failures++; checks++; console.log("FAIL " + name + " threw: " + (e && e.stack || e)); }
  }
  console.log(failures ? failures + " of " + checks + " checks FAILED" : "ALL PASS (" + checks + " checks)");
  process.exit(failures ? 1 : 0);
})();
