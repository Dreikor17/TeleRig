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
 *  V6. a start is a picture only once 4 of its first 12 line syncs arrive: a header followed by silence,
 *      noise or voice-band noise, a chosen mode on voice-band noise, or a header and two lines are dropped
 *      (onFalse) and never shown; the rows wait until the picture is confirmed; a real header inside a
 *      start's trial wins; interference over a real picture's first lines still lets it through
 *  V7. Auto joins a picture already under way (no header heard) by its sync rhythm: Martin M1, Scottie S1,
 *      PD-120, Robot 36 and Martin M2 from the middle of a line, each named right and matching the picture;
 *      80 Hz mistuned (measured from the syncs) and a 0.2 % fast clock still join; noise, voice-band noise,
 *      a pulse train no mode has and CW-like keying never start one
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
  const r = { starts: [], rows: [], ends: [], unknown: [], falses: [], early: 0, ys: [] };
  const d = new Decoder(FS, { onStart: (s) => r.starts.push(s),
                              onRow: (x) => { if (!r.starts.length) r.early++; r.ys.push(x.y); r.rows.push(Array.from(x.rgba)); },
                              onEnd: (e) => r.ends.push(e), onUnknown: (u) => r.unknown.push(u.vis), onFalse: (f) => r.falses.push(f) });
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
  check(r.early === 0 && r.ys.every((y, i) => y === i), `V2 ${m.name}: the start comes first, then the rows top down`);
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

// ---- V7: Auto joins a picture already under way ----
// the best line offset (picture line y0 + row) of the rows decoded, and its mean error
function bestFit(rows, m, img, lo, hi) {
  let best = { y0: -1, err: 999 };
  // (the first two rows left out: a joined Robot 36's first line has no colour pair yet)
  for (let y0 = lo; y0 <= hi; y0++) { const e = mae(rows.slice(0, 40), m, img, y0, 2); if (e < best.err) best = { y0, err: e }; }
  return best;
}
for (const [id, from] of [["m1", 30], ["s1", 30], ["pd120", 20], ["r36", 60], ["m2", 40]]) {
  const m = byId(id), img = pic(m.width, m.height);
  const segs = picture(m, img), perLine = segs.length / m.lines;
  const tail = segs.slice(Math.round(from * perLine + perLine / 3));   // a third into line `from`, no VIS
  const r = decode(audio(tail, { lead: 0.2 }).subarray(0, 30 * FS));
  eq([r.starts.map((s) => [s.mode.id, s.joined]), r.unknown], [[[id, true]], []], `V7 ${m.name} joined under way, no header: named right`);
  const fit = bestFit(r.rows, m, img, from * m.rowsPerLine, (from + 8) * m.rowsPerLine);
  // as well as from a header (V2: GBR under 3, the colour-difference modes under 5), a row's worth more for GBR
  check(r.rows.length > 20 && fit.err < (m.color === "GBR" ? 4 : 5),
        `V7 ... matching the picture from line ${fit.y0} (mean error ${fit.err.toFixed(1)}, ${r.rows.length} rows)`);
}
{
  const m = byId("s1"), img = pic(m.width, m.height);
  const segs = picture(m, img), perLine = segs.length / m.lines;
  const r = decode(audio(segs.slice(Math.round(50 * perLine + 5)), { lead: 0.2, offset: 80 }).subarray(0, 30 * FS));
  check(r.starts.length === 1 && r.starts[0].mode.id === "s1" && Math.abs(r.starts[0].offsetHz - 80) <= 10,
        `V7 Scottie S1 80 Hz high, joined: the offset from its syncs (${r.starts[0] && r.starts[0].offsetHz} Hz)`);
  const fit = bestFit(r.rows, m, img, 50, 58);
  check(fit.err < 4, `V7 ... and taken off (mean error ${fit.err.toFixed(1)})`);
  const q = byId("m2"), qimg = pic(q.width, q.height), qs = picture(q, qimg), qper = qs.length / q.lines;
  const f = decode(audio(qs.slice(Math.round(60 * qper + 4)), { lead: 0.2, fsTx: FS * 1.002 }));
  check(f.starts.length === 1 && f.starts[0].mode.id === "m2" && f.rows.length > 150,
        `V7 Martin M2 from a sender 0.2 % fast, joined: ${f.rows.length} rows`);
}
{
  // a train of 9 ms 1200 Hz pulses every 600 ms over 1500 Hz (no mode's line), and CW-like 1200 Hz keying
  const train = [];
  for (let k = 0; k < 40; k++) train.push(tone(9, 1200), tone(591, 1500));
  const rt = decode(audio(train));
  eq([rt.starts.length, rt.falses.length], [0, 0], "V7 a pulse train no mode has (every other line of Robot 72): no picture, not even tried");
  // three pulses a line apart (fewer than four), then silence: not tried
  const three = [];
  for (let k = 0; k < 3; k++) three.push(tone(9, 1200), tone(byId("s1").lineMs - 9, 1700));
  eq(decode(audio(three, { tail: 3 })).falses.length, 0, "V7 three pulses a line apart: not tried (four are needed)");
  // nine-ms pulses 3 % slower than Scottie S1's line: no mode (its line to 1 %), not tried
  const slow = [];
  for (let k = 0; k < 30; k++) slow.push(tone(9, 1200), tone(byId("s1").lineMs * 1.03 - 9, 1700));
  const rs = decode(audio(slow));
  eq([rs.starts.length, rs.falses.length], [0, 0], "V7 a line 3 % off Scottie S1's: not that mode, not tried");
  const cw = [];
  for (let k = 0; k < 60; k++) cw.push(tone(k % 3 ? 60 : 180, 1200), tone(60, 1700));
  eq(decode(audio(cw)).starts.length, 0, "V7 CW-like keying at 1200 Hz: no picture");
  eq(decode(audio([], { noise: 0.25, lead: 20, tail: 0 })).starts.length, 0, "V7 20 s of noise: no picture (no rhythm in it)");
  // Robot 72's line (9 ms every 300 ms) for five pulses, then every other line: joined, then dropped (8 of
  // 12 needed of a start joined so; this has 6)
  const broken = [];
  for (let k = 0; k < 5; k++) broken.push(tone(9, 1200), tone(291, 1500));
  for (let k = 0; k < 20; k++) broken.push(tone(9, 1200), tone(591, 1500));
  const rb = decode(audio(broken));
  eq([rb.starts.length, rb.falses.length >= 1, rb.rows.length], [0, true, 0], "V7 a rhythm that breaks after four lines: tried, dropped, nothing shown");
  // 20 ms pulses (a PD sync) at Scottie S1's line: not Scottie (its sync is 9 ms), and no PD has that line
  const pdlen = [], s1 = byId("s1").lineMs;
  for (let k = 0; k < 30; k++) pdlen.push(tone(20, 1200), tone(s1 - 20, 1700));
  const rp = decode(audio(pdlen));
  eq([rp.starts.length, rp.falses.length], [0, 0], "V7 a sync of the wrong length at a mode's line: no picture, not even tried");
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

// ---- V6: a start is a picture only once its line syncs arrive ----
let seed = 4242;
function gauss() { let s = 0; for (let k = 0; k < 6; k++) { seed = (seed * 1103515245 + 12345) & 0x7fffffff; s += seed / 0x7fffffff; } return (s - 3) * 1.4; }
const clip = (v) => Math.max(-32767, Math.min(32767, Math.round(v)));
function noise(sec, rms, lowpassHz) {                      // white, or low-passed like voice (most of it under ~1 kHz)
  const out = new Int16Array(Math.round(sec * FS)), a = lowpassHz ? Math.exp((-2 * Math.PI * lowpassHz) / FS) : 0;
  let y = 0;
  for (let i = 0; i < out.length; i++) { const x = gauss(); y = lowpassHz ? a * y + (1 - a) * x * 3 : x; out[i] = clip(y * rms * 32767); }
  return out;
}
function cat(...parts) {
  const out = new Int16Array(parts.reduce((n, p) => n + p.length, 0));
  let k = 0;
  for (const p of parts) { out.set(p, k); k += p.length; }
  return out;
}
{
  const hdr = audio(vis(byId("m2").vis), { tail: 0 });
  for (const [what, after] of [["silence", new Int16Array(12 * FS)], ["noise", noise(12, 0.25)], ["voice-band noise", noise(12, 0.3, 500)]]) {
    const r = decode(cat(hdr, after));
    eq([r.starts.length, r.rows.length, r.ends.length, r.falses.map((f) => f.mode.id)], [0, 0, 0, ["m2"]],
       `V6 a Martin M2 header, then ${what}: no picture, the start dropped`);
  }
}
{
  const r = decode(noise(30, 0.3, 500), "m2");
  check(r.starts.length === 0 && r.rows.length === 0 && r.falses.length > 0,
        `V6 Martin M2 chosen, 30 s of voice-band noise: no picture (${r.starts.length} shown, ${r.falses.length} starts dropped)`);
}
{
  const m = byId("m2"), segs = picture(m, pic(m.width, m.height)), perLine = segs.length / m.height;
  const r = decode(audio([...vis(m.vis), ...segs.slice(0, 2 * perLine)], { tail: 8 }));
  eq([r.starts.length, r.rows.length, r.falses.length], [0, 0, 1], "V6 a header and 2 lines, then nothing: dropped, never shown");
}
{
  const m = byId("r36"), pcm = audio([...vis(m.vis), ...picture(m, pic(m.width, m.height))]);
  const cut = Math.round((0.3 + 0.91 + 3.5 * 0.15) * FS);   // the lead, the header and 3.5 lines
  const r = { starts: 0, rows: 0 };
  const d = new Decoder(FS, { onStart: () => r.starts++, onRow: () => r.rows++ });
  for (let i = 0; i < cut; i += 320) d.feed(pcm.subarray(i, Math.min(cut, i + 320)));
  eq([r.starts, r.rows], [0, 0], "V6 Robot 36 after 3 lines: nothing shown yet (the rows wait)");
  for (let i = cut; i < pcm.length; i += 320) d.feed(pcm.subarray(i, i + 320));
  eq([r.starts, r.rows], [1, 240], "V6 ... the 4th good sync starts it, with every row");
}
{
  const m = byId("r36"), img = pic(m.width, m.height);
  // Scottie DX: its trial would outlast the 6 s history, so only the watch during the trial finds the real header
  const r = decode(cat(audio(vis(byId("sdx").vis), { tail: 0 }), noise(0.6, 0.05), audio([...vis(m.vis), ...picture(m, img)])));
  eq([r.falses.map((f) => f.mode.id), r.starts.map((s) => s.mode.id), r.rows.length], [["sdx"], ["r36"], 240],
     "V6 a header that led nowhere, then a real one inside its trial: the real picture");
  check(mae(r.rows, m, img) < 5, "V6 ... decoded right");
}
{
  const m = byId("r36"), img = pic(m.width, m.height);
  const pcm = audio([...vis(m.vis), ...picture(m, img)], { noise: 0.05 });
  const from = Math.round((0.3 + 0.91 + 0.15) * FS), to = from + Math.round(6 * 0.15 * FS);   // lines 1 to 6
  for (let i = from; i < to; i++) pcm[i] = clip(gauss() * 0.5 * 32767);
  const r = decode(pcm);
  eq([r.starts.length, r.rows.length, r.ends.map((e) => e.reason)], [1, 240, ["done"]],
     "V6 interference over lines 1-6 of a real picture: still a picture (4 good syncs in the first 12)");
}

console.log(failures ? `${failures} of ${checks} checks FAILED` : `ALL PASS (${checks} checks)`);
process.exit(failures ? 1 : 0);
