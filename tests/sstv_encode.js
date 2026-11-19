/* A reference SSTV encoder for the tests (tests/sstv_test.js, tests/sstv_panel_test.js, tests/sstv_tx_test.js):
 * phase-continuous tones at 16 kHz, VIS header included, for every mode in frontend/sstv.js's table, written
 * from the mode specifications (not from the decoder). frontend/sstv-tx.js's encoder must match it sample
 * for sample (tests/sstv_tx_test.js E2).
 *
 *   pic(w, h)                    a test picture: colour bars over a grey ramp, a dark box, one-row stripes
 *   vis(code), picture(m, img)   segment lists [[ms, freq(t)]]
 *   audio(segs, opts)            Int16Array; opts: fsTx (the sender's clock), offset (Hz), noise (rms), lead, tail (s)
 */
"use strict";
const FS = 16000;

// ---- a test picture: colour bars over a grey ramp, with a dark box (edges in both directions) ----
const BARS = [[255, 255, 255], [255, 255, 0], [0, 255, 255], [0, 255, 0], [255, 0, 255], [255, 0, 0], [0, 0, 255], [0, 0, 0]];
function pic(w, h) {
  return (x, y) => {
    if (y < h / 2) return BARS[Math.min(7, Math.floor((x * 8) / w))];
    if (x > w * 0.6 && x < w * 0.8 && y > h * 0.7 && y < h * 0.85) return [20, 40, 90];
    if (x < w * 0.25) return y % 2 ? [40, 40, 40] : [215, 215, 215];   // one-row stripes: rows in their places
    const g = Math.round((x / (w - 1)) * 255);
    return [g, g, g];
  };
}
const yccOf = ([r, g, b]) => [0.299 * r + 0.587 * g + 0.114 * b, 128 - 0.168736 * r - 0.331264 * g + 0.5 * b,
                              128 + 0.5 * r - 0.418688 * g - 0.081312 * b];       // Y, Cb, Cr

// ---- the reference encoder: a list of [ms, freq(t 0..1)] segments, then tones with exact timing ----
const tone = (ms, f) => [ms, () => f];
const scan = (ms, w, val) => [ms, (t) => 1500 + (Math.max(0, Math.min(255, val(Math.min(w - 1, Math.floor(t * w))))) / 255) * 800];
function vis(code) {
  const s = [tone(300, 1900), tone(10, 1200), tone(300, 1900), tone(30, 1200)];
  let ones = 0;
  for (let b = 0; b < 7; b++) { const bit = (code >> b) & 1; ones += bit; s.push(tone(30, bit ? 1100 : 1300)); }
  s.push(tone(30, ones % 2 ? 1100 : 1300));                                     // even parity
  s.push(tone(30, 1200));
  return s;
}
function picture(m, img) {
  const W = m.width, s = [], ch = (y, k) => (x) => img(x, y)["RGB".indexOf(k)];
  if (m.color === "GBR" && m.syncMs < 5) {                                     // Martin
    for (let y = 0; y < m.height; y++) {
      s.push(tone(4.862, 1200), tone(0.572, 1500));
      for (const k of "GBR") s.push(scan(m.chans[0].d, W, ch(y, k)), tone(0.572, 1500));
    }
  } else if (m.color === "GBR") {                                              // Scottie
    s.push(tone(9, 1200));
    for (let y = 0; y < m.height; y++) {
      const d = m.chans[0].d;
      s.push(tone(1.5, 1500), scan(d, W, ch(y, "G")), tone(1.5, 1500), scan(d, W, ch(y, "B")), tone(9, 1200), tone(1.5, 1500),
             scan(d, W, ch(y, "R")));
    }
  } else if (m.color === "R36") {
    for (let y = 0; y < m.height; y++) {
      const Y = (x) => yccOf(img(x, y))[0], even = y % 2 === 0, yy = even ? y : y - 1;
      const C = (x) => { const a = yccOf(img(2 * x, yy)), b = yccOf(img(2 * x, Math.min(m.height - 1, yy + 1))); return even ? (a[2] + b[2]) / 2 : (a[1] + b[1]) / 2; };
      s.push(tone(9, 1200), tone(3, 1500), scan(88, W, Y), tone(4.5, even ? 1500 : 2300), tone(1.5, 1900), scan(44, W >> 1, C));
    }
  } else if (m.color === "YCC") {                                              // Robot 72
    for (let y = 0; y < m.height; y++) {
      const c = (i) => (x) => yccOf(img(x, y))[i];
      s.push(tone(9, 1200), tone(3, 1500), scan(138, W, c(0)), tone(4.5, 1500), tone(1.5, 1900), scan(69, W, c(2)),
             tone(4.5, 2300), tone(1.5, 1900), scan(69, W, c(1)));
    }
  } else {                                                                      // PD
    const d = m.chans[0].d;
    for (let y = 0; y < m.height; y += 2) {
      const a = (x) => yccOf(img(x, y)), b = (x) => yccOf(img(x, y + 1));
      s.push(tone(20, 1200), tone(2.08, 1500), scan(d, W, (x) => a(x)[0]), scan(d, W, (x) => (a(x)[2] + b(x)[2]) / 2),
             scan(d, W, (x) => (a(x)[1] + b(x)[1]) / 2), scan(d, W, (x) => b(x)[0]));
    }
  }
  return s;
}
// opts: fsTx (the sender's clock), offset (Hz, mistuning), noise (rms, 1 = full scale), lead / tail (s of silence)
function audio(segs, opts) {
  opts = opts || {};
  const fsTx = opts.fsTx || FS, off = opts.offset || 0;
  let total = 0;
  for (const [ms] of segs) total += ms;
  const lead = Math.round((opts.lead || 0.3) * FS), n = lead + Math.ceil((total / 1000) * fsTx) + Math.round((opts.tail || 0.3) * FS);
  const out = new Int16Array(n);
  let ph = 0, si = 0, segStart = 0, i = lead;
  let rnd = 12345;
  const gauss = () => { let s = 0; for (let k = 0; k < 6; k++) { rnd = (rnd * 1103515245 + 12345) & 0x7fffffff; s += rnd / 0x7fffffff; } return (s - 3) * 1.4; };
  for (; i < n; i++) {
    const tMs = ((i - lead) / fsTx) * 1000;                   // the sender's time
    while (si < segs.length && tMs >= segStart + segs[si][0]) { segStart += segs[si][0]; si++; }
    let v = 0;
    if (si < segs.length) {
      const f = segs[si][1]((tMs - segStart) / segs[si][0]) + off;
      ph += (2 * Math.PI * f) / FS;                           // played on the receiver's clock
      v = 0.5 * Math.sin(ph);
    }
    if (opts.noise) v += opts.noise * gauss();
    out[i] = Math.max(-32767, Math.min(32767, Math.round(v * 32767)));
  }
  if (opts.noise) for (let k = 0; k < lead; k++) out[k] = Math.round(opts.noise * gauss() * 32767);
  return out;
}
module.exports = { FS, BARS, pic, yccOf, tone, scan, vis, picture, audio };
