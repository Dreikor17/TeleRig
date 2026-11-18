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

(async () => {
  for (const [name, fn] of pending) {
    try { await fn(); } catch (e) { failures++; checks++; console.log("FAIL " + name + " threw: " + (e && e.stack || e)); }
  }
  console.log(failures ? failures + " of " + checks + " checks FAILED" : "ALL PASS (" + checks + " checks)");
  process.exit(failures ? 1 : 0);
})();
