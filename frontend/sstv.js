/* TeleRig SSTV receive (the SSTV panel): decodes slow-scan TV pictures from the radio's audio, the PCM
 * as it arrives (window.RadioAudio.tapPcm: 16 kHz mono, before the volume). Receive only: nothing here
 * can transmit.
 *
 *   FM discriminator: the audio is mixed down around 1700 Hz, low-passed, and the phase step per
 *   sample is the tone frequency (SSTV: 1200 Hz sync, 1500 black .. 2300 Hz white).
 *   VIS header: leader 1900 Hz, start bit 1200, 7 bits LSB first + even parity (1100 = 1, 1300 = 0),
 *   stop bit 1200, 30 ms each. The leader also measures the tuning offset, taken off every tone.
 *   Lines: each line re-locks on its sync pulse (a small loop that also learns the line period), so a
 *   station whose sound card clock runs fast or slow does not slant the picture.
 *   Modes (timings: the Barber / N7CXI "Dayton paper" tables): Robot 36 / 72, Martin M1 / M2, Scottie
 *   S1 / S2 / DX, PD-50 / 90 / 120 / 160 / 180 / 240 / 290. "Auto" starts on a VIS header; a chosen
 *   mode starts at the next sync pulse (joining a picture already under way).
 *
 * window.SSTV = { MODES, Decoder } (tests, and the TX side later); window.SstvPanel = { summary(), feed(pcm, rate) }.
 */
(function () {
  "use strict";

  // ---- the modes: every time in ms from the start of a line (the line's sync is at `syncAt`) ----
  function mode(o) {
    o.lines = o.height / o.rowsPerLine;
    o.durationS = (o.first + o.lines * o.lineMs) / 1000;
    return o;
  }
  function martin(id, name, vis, scan) {
    const p = 0.572, t0 = 4.862 + p;
    return mode({ id, name, vis, width: 320, height: 256, rowsPerLine: 1, color: "GBR", syncMs: 4.862, syncAt: 0, first: 0,
      lineMs: t0 + 3 * (scan + p),
      chans: [{ k: "G", t: t0, d: scan }, { k: "B", t: t0 + scan + p, d: scan }, { k: "R", t: t0 + 2 * (scan + p), d: scan }] });
  }
  function scottie(id, name, vis, scan) {
    const syncAt = 3 + 2 * scan;
    return mode({ id, name, vis, width: 320, height: 256, rowsPerLine: 1, color: "GBR", syncMs: 9, syncAt, first: 9,
      lineMs: 13.5 + 3 * scan,
      chans: [{ k: "G", t: 1.5, d: scan }, { k: "B", t: 3 + scan, d: scan }, { k: "R", t: syncAt + 10.5, d: scan }] });
  }
  function pd(id, name, vis, width, height, scan) {
    const t0 = 22.08;
    return mode({ id, name, vis, width, height, rowsPerLine: 2, color: "PD", syncMs: 20, syncAt: 0, first: 0,
      lineMs: t0 + 4 * scan,
      chans: [{ k: "Y0", t: t0, d: scan }, { k: "Cr", t: t0 + scan, d: scan }, { k: "Cb", t: t0 + 2 * scan, d: scan },
              { k: "Y1", t: t0 + 3 * scan, d: scan }] });
  }
  const MODES = [
    mode({ id: "r36", name: "Robot 36", vis: 8, width: 320, height: 240, rowsPerLine: 1, color: "R36", syncMs: 9, syncAt: 0, first: 0,
      lineMs: 150, chans: [{ k: "Y", t: 12, d: 88 }, { k: "S", t: 101, d: 2.5 }, { k: "C", t: 106, d: 44 }] }),
    mode({ id: "r72", name: "Robot 72", vis: 12, width: 320, height: 240, rowsPerLine: 1, color: "YCC", syncMs: 9, syncAt: 0, first: 0,
      lineMs: 300, chans: [{ k: "Y", t: 12, d: 138 }, { k: "Cr", t: 156, d: 69 }, { k: "Cb", t: 231, d: 69 }] }),
    martin("m1", "Martin M1", 44, 146.432),
    martin("m2", "Martin M2", 40, 73.216),
    scottie("s1", "Scottie S1", 60, 138.24),
    scottie("s2", "Scottie S2", 56, 88.064),
    scottie("sdx", "Scottie DX", 76, 345.6),
    pd("pd50", "PD-50", 93, 320, 256, 91.52),
    pd("pd90", "PD-90", 99, 320, 256, 170.24),
    pd("pd120", "PD-120", 95, 640, 496, 121.6),
    pd("pd160", "PD-160", 98, 512, 400, 195.584),
    pd("pd180", "PD-180", 96, 640, 496, 183.04),
    pd("pd240", "PD-240", 97, 640, 496, 244.48),
    pd("pd290", "PD-290", 94, 800, 616, 228.8),
  ];
  const BY_VIS = new Map(MODES.map((m) => [m.vis, m]));
  const BY_ID = new Map(MODES.map((m) => [m.id, m]));

  // ---- the decoder ----
  const FC = 1700;                                   // mixer centre: the middle of 1100 .. 2300 Hz
  const LOST_LINES = 10;                             // this many lines with no sync in a row: the signal is gone
  function lowpass(n, cutoff, fs) {                  // windowed sinc (Blackman), unity gain at DC
    const h = new Float32Array(n), m = (n - 1) / 2, fc = cutoff / fs;
    let s = 0;
    for (let i = 0; i < n; i++) {
      const x = i - m, w = 0.42 - 0.5 * Math.cos((2 * Math.PI * i) / (n - 1)) + 0.08 * Math.cos((4 * Math.PI * i) / (n - 1));
      h[i] = (x === 0 ? 2 * fc : Math.sin(2 * Math.PI * fc * x) / (Math.PI * x)) * w;
      s += h[i];
    }
    for (let i = 0; i < n; i++) h[i] /= s;
    return h;
  }
  const clamp8 = (v) => (v < 0 ? 0 : v > 255 ? 255 : v);

  class Decoder {
    constructor(fs, cb) {
      this.fs = fs;
      this.hooks = cb || {};
      this.taps = lowpass(31, 900, fs);
      this.iq = new Float32Array(this.taps.length * 2);   // the mixer's I/Q history (a ring)
      this.iqPos = 0;
      this.ph = 0; this.dph = (2 * Math.PI * FC) / fs;
      this.prevI = 1; this.prevQ = 0;
      this.cap = Math.ceil(fs * 6);                     // ~6 s of frequency history
      this.f = new Float32Array(this.cap);              // tone frequency per sample (Hz)
      this.P = new Float64Array(this.cap + 1);          // prefix sums of f
      this.S = new Int32Array(this.cap + 1);            // prefix counts of "sync-ish" samples
      this.base = 0; this.n = 0;                        // absolute index of f[0]; samples held
      this.want = "auto";
      this.reset();
    }
    // -- control --
    reset() { this.state = "idle"; this.m = null; this.offset = 0; this.nextCheck = 0; this.line = 0; this.rows = 0; this.missed = 0; }
    setMode(want) { this.want = want === "auto" || BY_ID.has(want) ? want : "auto"; this.reset(); }
    ms(t) { return (t * this.fs) / 1000; }
    // -- input --
    feed(pcm) {
      const taps = this.taps, L = taps.length, iq = this.iq;
      for (let i = 0; i < pcm.length; i++) {
        const x = pcm[i] / 32768;
        iq[this.iqPos] = x * Math.cos(this.ph);
        iq[this.iqPos + L] = -x * Math.sin(this.ph);
        this.ph += this.dph; if (this.ph > 2 * Math.PI) this.ph -= 2 * Math.PI;
        let I = 0, Q = 0;
        for (let k = 0, j = this.iqPos; k < L; k++) {
          I += taps[k] * iq[j]; Q += taps[k] * iq[j + L];
          j = j === 0 ? L - 1 : j - 1;
        }
        this.iqPos = this.iqPos + 1 === L ? 0 : this.iqPos + 1;
        // the phase step between samples is the frequency offset from FC
        const re = I * this.prevI + Q * this.prevQ, im = Q * this.prevI - I * this.prevQ;
        this.prevI = I; this.prevQ = Q;
        let f = FC + (Math.atan2(im, re) * this.fs) / (2 * Math.PI);
        if (!(f > 900)) f = 900; else if (f > 2700) f = 2700;   // NaN / outliers (a vanishing signal)
        this.push(f);
      }
      this.work();
    }
    push(f) {
      if (this.n === this.cap) this.shift();
      const i = this.n;
      this.f[i] = f;
      this.P[i + 1] = this.P[i] + f;
      this.S[i + 1] = this.S[i] + (f - this.offset < 1350 ? 1 : 0);
      this.n++;
    }
    shift() {                                           // drop the older half (keep what a line needs)
      const keep = this.cap >> 1, drop = this.n - keep;
      this.f.copyWithin(0, drop, this.n);
      this.base += drop; this.n = keep;
      this.P[0] = 0; this.S[0] = 0;
      for (let i = 0; i < keep; i++) {
        this.P[i + 1] = this.P[i] + this.f[i];
        this.S[i + 1] = this.S[i] + (this.f[i] - this.offset < 1350 ? 1 : 0);
      }
    }
    end() { return this.base + this.n; }               // absolute index one past the newest sample
    mean(a, b) {                                        // mean frequency over absolute [a, b)
      a = Math.max(Math.round(a) - this.base, 0); b = Math.min(Math.round(b) - this.base, this.n);
      return b > a ? (this.P[b] - this.P[a]) / (b - a) : NaN;
    }
    syncFrac(a, b) {
      a = Math.max(Math.round(a) - this.base, 0); b = Math.min(Math.round(b) - this.base, this.n);
      return b > a ? (this.S[b] - this.S[a]) / (b - a) : 0;
    }
    // -- the state machine --
    work() {
      for (let guard = 0; guard < 4096; guard++) {
        if (this.state === "idle") { if (!this.lookForStart()) return; }
        else if (this.state === "rx") { if (!this.nextLine()) return; }
        else return;
      }
    }
    lookForStart() {                                    // returns true when a picture starts
      const end = this.end(), step = Math.max(1, Math.round(this.ms(1)));
      if (this.nextCheck < this.base) this.nextCheck = this.base;
      while (this.nextCheck + step <= end) {
        this.nextCheck += step;
        const now = this.nextCheck;
        if (this.want === "auto" ? this.vis(now) : this.forcedSync(now)) return true;
      }
      return false;
    }
    seg(now, fromMs, toMs) {                            // the middle 60 % of [now-from, now-to) ms
      const a = now - this.ms(fromMs), b = now - this.ms(toMs), pad = (b - a) * 0.2;
      return this.mean(a + pad, b - pad);
    }
    vis(now) {
      if (now - this.base < this.ms(620)) return false;
      const lead = this.seg(now, 600, 300);
      if (!(Math.abs(lead - 1900) < 200)) return false;
      const off = lead - 1900;
      const near = (v, want, tol) => Math.abs(v - off - want) < tol;
      if (!near(this.seg(now, 30, 0), 1200, 70) || !near(this.seg(now, 300, 270), 1200, 70)) return false;
      let code = 0, ones = 0;
      for (let b = 0; b < 8; b++) {
        const v = this.seg(now, 270 - 30 * b, 240 - 30 * b) - off;
        if (!(Math.abs(v - 1100) < 80 || Math.abs(v - 1300) < 80)) return false;
        const bit = v < 1200 ? 1 : 0;
        ones += bit;
        if (b < 7) code |= bit << b;
      }
      if (ones % 2) return false;                        // even parity
      // a steady leader, not noise that averages to 1900
      let ok = 0, cnt = 0;
      const a = Math.round(now - this.ms(560)) - this.base, b = Math.round(now - this.ms(340)) - this.base;
      for (let i = Math.max(0, a); i < b; i += 4) { cnt++; if (Math.abs(this.f[i] - off - 1900) < 90) ok++; }
      if (!cnt || ok / cnt < 0.7) return false;
      const m = BY_VIS.get(code);
      if (!m) { this.emit("onUnknown", { vis: code }); this.nextCheck = now + this.ms(400); return false; }   // once per header
      // The windows above tolerate a few ms; the picture's time comes from the sharp 1900 -> 1200 Hz
      // edge where the start bit begins, exactly 300 ms before the stop bit ends.
      const visEnd = this.edgeDown(now - this.ms(312), now - this.ms(288), 1550 + off) + this.ms(300);
      this.begin(m, off, visEnd + this.ms(m.first + m.syncAt), true);
      return true;
    }
    edgeDown(a, b, thr) {                               // first time the 2 ms mean falls below thr
      const h = this.ms(1);
      for (let i = Math.round(a); i <= b; i++) if (this.mean(i - h, i + h) < thr) return i;
      return Math.round((a + b) / 2);
    }
    forcedSync(now) {                                   // a chosen mode: start at the next sync pulse
      const m = BY_ID.get(this.want);
      const s0 = now - this.ms(m.syncMs);
      if (this.syncFrac(s0, now) < 0.85 || this.syncFrac(s0 - this.ms(3), s0) > 0.3) return false;
      this.begin(m, 0, s0, true);
      return true;
    }
    // firstSync: where line 0's sync starts, known to the sample (the VIS edge, or a sync just found):
    // the first line is not searched (after a VIS the stop bit and the first sync are one 1200 Hz tone)
    begin(m, off, firstSync) {
      this.m = m; this.offset = off; this.state = "rx";
      this.line = 0; this.rows = 0; this.missed = 0;
      this.anchor = firstSync; this.period = this.ms(m.lineMs); this.firstLine = true;
      this.cr = new Float32Array(m.width).fill(128); this.cb = new Float32Array(m.width).fill(128);
      // re-count the sync-ish samples with this picture's offset
      for (let i = 0; i < this.n; i++) this.S[i + 1] = this.S[i] + (this.f[i] - off < 1350 ? 1 : 0);
      this.emit("onStart", { mode: m, offsetHz: Math.round(off) });
    }
    nextLine() {                                        // returns true when a line was done
      const m = this.m, win = this.ms(3), sync = this.ms(m.syncMs);
      const pred = this.firstLine ? this.anchor : this.anchor + this.period;
      const lineStart = pred - this.ms(m.syncAt);
      if (this.end() < lineStart + this.ms(m.lineMs) + win + 2) return false;
      if (lineStart - this.base < -win) { this.finish("lost"); return false; }   // fell out of the history
      // the sync pulse near where it should be
      let anchor = pred;
      if (!this.firstLine) {
        let best = -1, bestD = 0;                        // the best match; on a tie, the one nearest the prediction
        for (let d = -win; d <= win; d += 1) {
          const fr = this.syncFrac(pred + d, pred + d + sync);
          if (fr > best + 1e-9 || (Math.abs(fr - best) <= 1e-9 && Math.abs(d) < Math.abs(bestD))) { best = fr; bestD = d; }
        }
        if (best > 0.55) {
          anchor = pred + 0.5 * bestD;
          this.period += 0.05 * bestD;
          this.missed = 0;
        } else if (++this.missed >= LOST_LINES) { this.finish("lost"); return false; }
      }
      this.anchor = anchor; this.firstLine = false;
      this.draw(anchor - this.ms(m.syncAt));
      this.line++;
      if (this.line >= m.lines) this.finish("done");
      return true;
    }
    pixels(start, ch, w) {
      const out = new Float32Array(w), a = start + this.ms(ch.t), step = this.ms(ch.d) / w;
      for (let x = 0; x < w; x++) {
        const f = this.mean(a + x * step, a + (x + 1) * step) - this.offset;
        out[x] = clamp8(((f - 1500) / 800) * 255);
      }
      return out;
    }
    draw(start) {
      const m = this.m, w = m.width, C = {};
      for (const ch of m.chans) C[ch.k] = ch.k === "S" ? this.mean(start + this.ms(ch.t), start + this.ms(ch.t + ch.d)) - this.offset
        : this.pixels(start, ch, ch.k === "C" ? w >> 1 : w);
      const rgba = () => new Uint8ClampedArray(w * 4);
      const ycc = (row, Y, cr, cb, x, crx) => {
        const y = Y[x], r = cr[crx] - 128, b = cb[crx] - 128;
        row[x * 4] = y + 1.402 * r; row[x * 4 + 1] = y - 0.344136 * b - 0.714136 * r; row[x * 4 + 2] = y + 1.772 * b; row[x * 4 + 3] = 255;
      };
      if (m.color === "GBR") {
        const row = rgba();
        for (let x = 0; x < w; x++) { row[x * 4] = C.R[x]; row[x * 4 + 1] = C.G[x]; row[x * 4 + 2] = C.B[x]; row[x * 4 + 3] = 255; }
        this.row(row);
      } else if (m.color === "YCC") {
        const row = rgba();
        for (let x = 0; x < w; x++) ycc(row, C.Y, C.Cr, C.Cb, x, x);
        this.row(row);
      } else if (m.color === "R36") {                    // chroma every other line: R-Y after 1500 Hz, B-Y after 2300 Hz
        const half = C.C;
        if (C.S < 1900) this.cr = half; else this.cb = half;
        const row = rgba();
        for (let x = 0; x < w; x++) ycc(row, C.Y, this.cr, this.cb, x, Math.min(half.length - 1, x >> 1));
        this.row(row);
      } else {                                           // PD: two rows share one chroma
        const a = rgba(), b = rgba();
        for (let x = 0; x < w; x++) { ycc(a, C.Y0, C.Cr, C.Cb, x, x); ycc(b, C.Y1, C.Cr, C.Cb, x, x); }
        this.row(a); this.row(b);
      }
    }
    row(rgba) { this.emit("onRow", { y: this.rows, rgba }); this.rows++; }
    finish(reason) {
      const m = this.m, rows = this.rows;
      this.reset();
      this.nextCheck = this.end();
      this.emit("onEnd", { mode: m, rows, reason });
    }
    emit(name, arg) { try { if (typeof this.hooks[name] === "function") this.hooks[name](arg); } catch (_) {} }
  }

  window.SSTV = { MODES, Decoder, byVis: (v) => BY_VIS.get(v) || null, byId: (id) => BY_ID.get(id) || null };

  // ---- the panel ----
  if (typeof document === "undefined") return;           // the decoder alone (tests, a worker)
  const $ = (id) => document.getElementById(id);
  const tile = document.querySelector('.grid-stack-item[data-tile="sstv"]');
  const canvas = $("sstvCanvas"), status = $("sstvStatus"), modeSel = $("sstvMode"), gallery = $("sstvGallery");
  if (!tile || !canvas || !status) return;
  const KEY = "telerig.sstv.mode", KEEP = 12;
  const g2d = (canvas.getContext && canvas.getContext("2d")) || null;
  let dec = null, rate = 0, cur = null, rowsDone = 0, unknown = null, lastNote = "";
  const pics = [];                                        // {url, file, name, li}

  const audio = () => window.RadioAudio || null;
  const rxOn = () => { const A = audio(); try { return !!(A && A.on()); } catch (_) { return false; } };
  const onGrid = () => !!(tile.parentElement && tile.parentElement.id === "grid");
  function radio() { try { return (window.RadioControl && RadioControl.state()) || {}; } catch (_) { return {}; } }
  const pad = (n) => String(n).padStart(2, "0");
  const utc = (d) => pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes()) + "Z";
  const stamp = (d) => d.getUTCFullYear() + pad(d.getUTCMonth() + 1) + pad(d.getUTCDate()) + "-" + pad(d.getUTCHours()) +
    pad(d.getUTCMinutes()) + pad(d.getUTCSeconds()) + "Z";
  const safe = (t) => String(t || "").replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "");

  for (const m of MODES) {                                // the mode menu: Auto first
    const o = document.createElement("option"); o.value = m.id; o.textContent = m.name; modeSel && modeSel.appendChild(o);
  }
  let want = "auto";
  try { const v = localStorage.getItem(KEY); if (v && (v === "auto" || BY_ID.has(v))) want = v; } catch (_) {}
  if (modeSel) modeSel.value = want;

  function blank(w, h) {
    canvas.width = w; canvas.height = h;
    if (g2d) { g2d.fillStyle = "#000"; g2d.fillRect(0, 0, w, h); }
  }
  function say(text) { if (status.textContent !== text) status.textContent = text; }
  function render() {
    if (cur) {
      const st = cur.freqText ? " · " + cur.freqText : "";
      say("Receiving " + cur.mode.name + " · line " + Math.min(rowsDone, cur.mode.height) + " of " + cur.mode.height + st);
    } else if (!rxOn()) say("Turn on RX in the Audio panel to receive SSTV.");
    else if (lastNote) say(lastNote);
    else say(want === "auto" ? "Listening for an SSTV picture (its VIS header picks the mode)."
                             : "Listening for " + BY_ID.get(want).name + ": it starts at the next sync pulse.");
  }
  const hooks = {
    onStart: ({ mode, offsetHz }) => {
      const st = radio(), now = new Date(Date.now());
      cur = { mode, at: now, offsetHz, freqText: st.connected && st.freq ? (st.freq / 1e6).toFixed(3) + " MHz " + (st.mode_name || "") : "" };
      cur.freqText = cur.freqText.trim();
      rowsDone = 0; unknown = null; lastNote = "";
      blank(mode.width, mode.height);
      try { TR.srLive("SSTV picture starting, " + mode.name); } catch (_) {}
      try { document.dispatchEvent(new CustomEvent("tr:digital", { detail: { mode: "sstv" } })); } catch (_) {}   // digital.js: a dot on SSTV's key
      render();
    },
    onRow: ({ y, rgba }) => {
      if (!cur) return;
      if (g2d && typeof ImageData === "function") g2d.putImageData(new ImageData(rgba, cur.mode.width, 1), 0, y);
      rowsDone = y + 1;
      if (y % 8 === 0) render();
    },
    onEnd: ({ mode, rows, reason }) => {
      const c = cur;
      cur = null;
      if (c && rows > 0) keep(c, rows);
      lastNote = reason === "done" ? "Received " + mode.name + " (" + rows + " lines) at " + utc(c ? c.at : new Date(Date.now())) + "."
        : mode.name + " ended early: the signal went away at line " + rows + " of " + mode.height + ".";
      try { TR.srLive(lastNote); } catch (_) {}
      render();
    },
    onUnknown: ({ vis }) => { if (!cur) { lastNote = "An SSTV picture in a mode TeleRig can't decode (VIS " + vis + ")."; render(); } },
  };
  function keep(c, rows) {                                // the picture into the strip, newest first
    const name = c.mode.name + (c.freqText ? " · " + c.freqText : "") + " · " + utc(c.at);
    const file = ["TeleRig_SSTV", safe(c.mode.name), c.freqText ? safe(c.freqText.split(" ")[0]) + "MHz" : "", stamp(c.at)]
      .filter(Boolean).join("_") + ".png";
    const add = (url) => {
      const a = document.createElement("a");
      a.className = "sstv-pic"; a.href = url; a.download = file; a.title = "Download " + name;
      const img = document.createElement("img"); img.src = url; img.alt = name + (rows < c.mode.height ? " (part)" : "");
      a.appendChild(img);
      if (gallery) gallery.insertBefore(a, gallery.firstChild);
      pics.unshift({ url, file, name, el: a });
      while (pics.length > KEEP) { const old = pics.pop(); try { URL.revokeObjectURL(old.url); } catch (_) {} if (old.el.parentNode) old.el.parentNode.removeChild(old.el); }
    };
    if (canvas.toBlob) canvas.toBlob((b) => { if (b) add(URL.createObjectURL(b)); }, "image/png");
  }
  function feed(pcm, r) {
    if (!onGrid()) return;
    if (!dec || r !== rate) { rate = r; dec = new Decoder(r, hooks); dec.setMode(want); }
    dec.feed(pcm);
  }
  if (modeSel) modeSel.addEventListener("change", () => {
    want = modeSel.value === "auto" || BY_ID.has(modeSel.value) ? modeSel.value : "auto";
    try { localStorage.setItem(KEY, want); } catch (_) {}
    if (dec) dec.setMode(want);
    cur = null; lastNote = ""; render();
  });
  const clear = $("sstvClear");
  if (clear) clear.addEventListener("click", () => { if (dec) dec.setMode(want); cur = null; lastNote = ""; blank(canvas.width, canvas.height); render(); });
  (function tap() {                                       // app.js loads first; wait for it if needed
    const A = audio();
    if (A && typeof A.tapPcm === "function") A.tapPcm(feed); else setTimeout(tap, 500);
  })();
  blank(320, 256);
  setInterval(render, 1000);
  render();

  window.SstvPanel = {
    summary: () => (cur ? cur.mode.name + " " + Math.min(rowsDone, cur.mode.height) + "/" + cur.mode.height
      : pics.length ? pics.length + (pics.length === 1 ? " picture" : " pictures") : ""),
    // decode audio handed over directly (int16 mono, its rate) as if the radio sent it: a check of the
    // panel, a recording later. It only decodes.
    feed: (pcm, r) => { if (pcm && pcm.length && r > 0) feed(pcm, r); },
  };
})();
