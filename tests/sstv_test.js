/* SSTV receive (frontend/sstv.js): the REAL decoder in a Node vm, fed with SSTV audio made here by a
 * reference encoder (phase-continuous tones at 16 kHz, VIS header included). No browser, no radio.
 * Run by tests/t_c1.py (unit):
 *
 *     node tests/sstv_test.js           -> exit 0 and "ALL PASS", or exit 1 naming each failure
 *
 *  V1. the mode table against the published numbers (picture time, VIS code, size); VIS codes unique
 *  V2. every mode decodes from clean audio: the VIS picks the mode, the picture matches the original
 *  V3. a weak signal (noise in the passband), a mistuned one (+60 Hz, measured and taken off) and a
 *      station whose clock runs 0.2 % fast (no slant: each line re-locks on its sync)
 *  V4. a picture already under way: the chosen mode starts at the next sync pulse
 *  V5. noise alone never starts a picture; an unknown VIS is reported, not decoded; a signal that
 *      goes away ends the picture as "lost", with the lines received so far
 */
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

let failures = 0, checks = 0;
function check(cond, msg) { checks++; if (!cond) { failures++; console.log("FAIL " + msg); } }
function eq(a, b, msg) { const ja = JSON.stringify(a), jb = JSON.stringify(b); check(ja === jb, msg + ": got " + ja + ", want " + jb); }

const ctx = { console, Math, Float32Array, Float64Array, Int32Array, Int16Array, Uint8ClampedArray, Map, Set, Array, Object,
              Number, String, JSON, isNaN };
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "frontend", "sstv.js"), "utf8"), ctx, { filename: "sstv.js" });
const { MODES, Decoder } = ctx.SSTV;
const byId = (id) => MODES.find((m) => m.id === id);
const FS = 16000;                                       // == sstv_encode.FS

const { pic, tone, vis, picture, audio } = require("./sstv_encode");

function decode(pcm, want, chunk) {
  const r = { starts: [], rows: [], ends: [], unknown: [] };
  const d = new Decoder(FS, { onStart: (s) => r.starts.push(s), onRow: (x) => r.rows.push(Array.from(x.rgba)),
                              onEnd: (e) => r.ends.push(e), onUnknown: (u) => r.unknown.push(u.vis) });
  if (want) d.setMode(want);
  chunk = chunk || 320;
  for (let i = 0; i < pcm.length; i += chunk) d.feed(pcm.subarray(i, i + chunk));
  r.dec = d;
  return r;
}
// mean absolute error per channel over the rows decoded (row y of the result = row y + y0 of the picture)
function mae(rows, m, img, y0, skipRows) {
  let s = 0, n = 0;
  for (let y = skipRows || 0; y < rows.length; y++) {
    for (let x = 0; x < m.width; x++) {
      const want = img(x, y + (y0 || 0));
      for (let c = 0; c < 3; c++) { s += Math.abs(rows[y][x * 4 + c] - want[c]); n++; }
    }
  }
  return n ? s / n : 999;
}

// ---- V1: the table ----
const PUBLISHED = {   // picture time (s), VIS, width x height
  r36: [36, 8, 320, 240], r72: [72, 12, 320, 240], m1: [114.3, 44, 320, 256], m2: [58.1, 40, 320, 256],
  s1: [109.6, 60, 320, 256], s2: [71.1, 56, 320, 256], sdx: [268.9, 76, 320, 256], pd50: [49.7, 93, 320, 256],
  pd90: [90.0, 99, 320, 256], pd120: [126.1, 95, 640, 496], pd160: [160.9, 98, 512, 400], pd180: [187.1, 96, 640, 496],
  pd240: [248.0, 97, 640, 496], pd290: [288.7, 94, 800, 616] };
eq(MODES.map((m) => m.id).sort(), Object.keys(PUBLISHED).sort(), "V1 the modes");
for (const m of MODES) {
  const [t, v, w, h] = PUBLISHED[m.id];
  check(Math.abs(m.durationS - t) < 0.15, `V1 ${m.name}: ${m.durationS.toFixed(2)} s (published ${t} s)`);
  eq([m.vis, m.width, m.height], [v, w, h], `V1 ${m.name}: VIS and size`);
  const last = Math.max(...m.chans.map((c) => c.t + c.d));
  check(last <= m.lineMs + 1e-9, `V1 ${m.name}: every scan inside its line`);
}
eq(new Set(MODES.map((m) => m.vis)).size, MODES.length, "V1 VIS codes are unique");

// ---- V2: every mode, clean (the long ones: their first 25 s) ----
for (const m of MODES) {
  const img = pic(m.width, m.height);
  let pcm = audio([...vis(m.vis), ...picture(m, img)]);
  const long = m.durationS > 75;
  if (long) pcm = pcm.subarray(0, Math.round(25.6 * FS));
  const r = decode(pcm);
  eq(r.starts.map((s) => s.mode.id), [m.id], `V2 ${m.name}: the VIS picks the mode`);
  const expectRows = long ? null : m.height;
  if (expectRows) eq([r.rows.length, r.ends.map((e) => e.reason)], [expectRows, ["done"]], `V2 ${m.name}: every row, then done`);
  else check(r.rows.length > 20 && !r.ends.length, `V2 ${m.name}: rows keep coming (${r.rows.length} in 25 s)`);
  const err = mae(r.rows, m, img);
  check(err < (m.color === "GBR" ? 3 : 5), `V2 ${m.name}: the picture matches (mean error ${err.toFixed(1)} of 255)`);
}

// ---- V3: noise, offset, a fast clock ----
{
  const m = byId("m1"), img = pic(m.width, m.height);
  const r = decode(audio([...vis(m.vis), ...picture(m, img)], { noise: 0.12 }).subarray(0, 30 * FS));
  const err = mae(r.rows, m, img);
  check(r.starts.length === 1 && r.rows.length > 50 && err < 12, `V3 Martin M1 in noise: ${r.rows.length} rows, mean error ${err.toFixed(1)}`);
}
{
  const m = byId("s1"), img = pic(m.width, m.height);
  const r = decode(audio([...vis(m.vis), ...picture(m, img)], { offset: 60 }).subarray(0, 30 * FS));
  check(r.starts.length === 1 && Math.abs(r.starts[0].offsetHz - 60) <= 8, `V3 Scottie S1 60 Hz high: the offset is measured (${r.starts[0] && r.starts[0].offsetHz} Hz)`);
  const err = mae(r.rows, m, img);
  check(err < 3, `V3 ... and taken off (mean error ${err.toFixed(1)})`);
}
{
  const m = byId("m2"), img = pic(m.width, m.height);
  const r = decode(audio([...vis(m.vis), ...picture(m, img)], { fsTx: FS * 1.002 }));
  eq([r.rows.length, r.ends.map((e) => e.reason)], [256, ["done"]], "V3 Martin M2, sender clock 0.2 % fast: every row");
  const err = mae(r.rows, m, img, 0, 40);                   // after the loop has learned the period
  check(err < 4, `V3 ... and no slant (mean error ${err.toFixed(1)} below row 40)`);
}

// ---- V4: joining a picture under way ----
{
  const m = byId("m2"), img = pic(m.width, m.height);
  const segs = picture(m, img), perLine = segs.length / m.height;
  const tail = segs.slice(40 * perLine + 3);                // from the middle of line 40, no VIS
  const r = decode(audio(tail, { lead: 0.2 }), "m2");
  eq(r.starts.map((s) => s.mode.id), ["m2"], "V4 the chosen mode starts with no VIS");
  check(r.rows.length >= 213 && r.rows.length <= 216, `V4 ... at the next line (${r.rows.length} rows)`);
  const err = mae(r.rows, m, img, 41);
  check(err < 3, `V4 ... matching the picture from line 41 (mean error ${err.toFixed(1)})`);
}

// ---- V5: no false starts; an unknown VIS; a signal that goes away ----
{
  const n2 = audio([], { noise: 0.25, lead: 20, tail: 0 });
  const r = decode(n2);
  eq([r.starts.length, r.rows.length], [0, 0], "V5 20 s of noise: no picture");
  const r2 = decode(audio([...vis(33), tone(2000, 1500)]));
  eq([r2.starts.length, r2.unknown], [0, [33]], "V5 an unknown VIS (33) is reported, not decoded");
  const bad = vis(44);                                        // Martin M1 with its parity bit flipped
  bad[11] = tone(30, bad[11][1]() === 1100 ? 1300 : 1100);
  const r4 = decode(audio([...bad, ...picture(byId("m1"), pic(320, 256)).slice(0, 40)]));
  eq([r4.starts.length, r4.unknown], [0, []], "V5 a VIS with a wrong parity bit is ignored");
  const m = byId("r36"), img = pic(m.width, m.height);
  const segs = [...vis(m.vis), ...picture(m, img)];
  let ms = 0, k = 0;
  while (ms < 900 + 12000) ms += segs[k++][0];               // the VIS and about 12 s of picture
  const r3 = decode(audio(segs.slice(0, k), { tail: 4, noise: 0.002 }));
  eq(r3.ends.map((e) => e.reason), ["lost"], "V5 the signal goes away: the picture ends as lost");
  check(r3.rows.length > 70 && r3.rows.length < 90, `V5 ... with the lines received (${r3.rows.length})`);
}

console.log(failures ? `${failures} of ${checks} checks FAILED` : `ALL PASS (${checks} checks)`);
process.exit(failures ? 1 : 0);
