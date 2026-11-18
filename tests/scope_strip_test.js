/* The band plan's own strip above the scope (waterfall.js drawBandplan / bandplanSegAt, #bandStrip),
 * tested on the REAL waterfall.js in a Node vm (tests/fakedom.js). No browser, no server, no radio.
 * Run by tests/t_af_scope.py (unit group):
 *
 *     node tests/scope_strip_test.js  -> exit 0 and "ALL PASS", or exit 1 naming each failure
 *
 *  A. with a strip: the band plan is drawn there (1 px from its top, 16 px tall), nothing of it on the
 *     overlay over the spectrum; each segment spans exactly the scope's own x for its frequencies
 *  B. the strip's tooltip lookup: only x matters on the strip; nothing while the band plan is off
 *  C. BAND PLAN off: the strip is cleared and nothing is drawn
 *  D. a page without a strip: drawn along the spectrum bottom as before, looked up there only
 */
"use strict";
const { makeEnv } = require("./fakedom");

let failures = 0, checks = 0;
function check(cond, msg) { checks++; if (!cond) { failures++; console.log("FAIL " + msg); } }
function eq(a, b, msg) { const ja = JSON.stringify(a), jb = JSON.stringify(b); check(ja === jb, msg + ": got " + ja + ", want " + jb); }

const env = makeEnv();
env.load("waterfall.js");
const { Scope } = env.ctx;

function recorder() {
  const calls = [];
  const target = {
    fillRect: (...a) => calls.push(["fillRect", ...a]), strokeRect: (...a) => calls.push(["strokeRect", ...a]),
    clearRect: (...a) => calls.push(["clearRect", ...a]), fillText: (t, x, y) => calls.push(["fillText", String(t), x, y]),
    measureText: (t) => ({ width: String(t).length * 6 }),
  };
  const ctx = new Proxy(target, { get: (t, k) => (k in t ? t[k] : () => {}), set: (t, k, v) => { t[k] = v; return true; } });
  return { ctx, calls, of: (n) => calls.filter((c) => c[0] === n) };
}
const PLAN = [
  { lo: 7.000e6, hi: 7.025e6, label: "Extra CW", desc: "CW only", kind: "cw" },
  { lo: 7.025e6, hi: 7.125e6, label: "CW / data", desc: "CW and data", kind: "data" },
  { lo: 7.125e6, hi: 7.300e6, label: "Phone", desc: "phone", kind: "phone" },
];
function mk(withStrip, on) {
  const ov = recorder(), st = recorder();
  const sc = Object.create(Scope.prototype);
  Object.assign(sc, { W: 600, specH: 150, wfH: 150, scopeH: 308, octx: ov.ctx, stctx: withStrip ? st.ctx : null,
                      strip: withStrip ? {} : null, meta: { mode: 1, lower: 7.0e6, upper: 7.3e6, tuned: 7.074e6 },
                      bandplan: PLAN, showBandplan: on !== false, opMode: "USB" });
  return { sc, ov, st };
}

// ---- A. drawn on the strip, aligned with the scope -------------------------------------------------
{
  const { sc, ov, st } = mk(true);
  sc.drawBandplan();
  eq(ov.of("fillRect").length + ov.of("fillText").length, 0, "A nothing of the band plan is drawn over the spectrum");
  const rects = st.of("fillRect");
  eq(rects.length, 3, "A one box per visible segment on the strip");
  eq(rects.map((r) => [r[2], r[4]]), [[1, 16], [1, 16], [1, 16]], "A 1 px from the strip's top, 16 px tall");
  eq(rects.map((r) => [Math.round(r[1]), Math.round(r[1] + r[3])]),
     PLAN.map((s) => [Math.round(sc.freqToX(s.lo)), Math.round(sc.freqToX(s.hi))]), "A each segment spans the scope's own x for its frequencies");
  eq(st.of("fillText").map((t) => t[1]), ["Extra…", "CW / data", "Phone"], "A the labels are on the strip (fitted to their box)");
  check(st.of("fillText").every((t) => t[3] > 1 && t[3] < 17), "A ... vertically inside the strip");
  check(st.of("clearRect").length >= 1, "A the strip is cleared before drawing");
}
// ---- B. the tooltip lookup on the strip ---------------------------------------------------------------
{
  const { sc } = mk(true);
  const x = (f) => sc.freqToX(f);
  eq([sc.bandplanSegAt(x(7.01e6), 0), sc.bandplanSegAt(x(7.2e6), 999)].map((s) => s && s.label), ["Extra CW", "Phone"],
     "B on the strip only x matters");
  sc.showBandplan = false;
  eq(sc.bandplanSegAt(x(7.01e6), 0), null, "B nothing while the band plan is off");
}
// ---- C. BAND PLAN off: cleared, nothing drawn -----------------------------------------------------------
{
  const { sc, ov, st } = mk(true, false);
  sc.drawBandplan();
  eq([st.of("clearRect").length >= 1, st.of("fillRect").length, ov.of("fillRect").length], [true, 0, 0], "C cleared, nothing drawn");
}
// ---- D. no strip on the page: the old place, the old lookup ---------------------------------------------
{
  const { sc, ov } = mk(false);
  sc.drawBandplan();
  const rects = ov.of("fillRect");
  eq([rects.length, rects[0] && rects[0][2]], [3, 150 - 16 - 3], "D drawn along the spectrum bottom");
  const x = sc.freqToX(7.01e6);
  eq([sc.bandplanSegAt(x, 140) && sc.bandplanSegAt(x, 140).label, sc.bandplanSegAt(x, 20)], ["Extra CW", null],
     "D looked up only on that strip");
}

console.log(failures ? failures + " of " + checks + " checks FAILED" : "ALL PASS (" + checks + " checks)");
process.exit(failures ? 1 : 0);
