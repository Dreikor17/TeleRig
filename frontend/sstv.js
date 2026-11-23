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
 *   S1 / S2 / DX, PD-50 / 90 / 120 / 160 / 180 / 240 / 290. "Auto" starts on a VIS header, or joins a
 *   picture already under way (its header missed: tuned in late, or too weak to read) by the rhythm of
 *   its sync pulses: four in a row a line apart (to 1 %, a pulse or two missed allowed) with the sync's
 *   length name the mode, and their tone the tuning offset. A chosen mode starts at the next sync pulse.
 *   A start is a picture only once its lines prove it: 4 of the first 12 line syncs must arrive where the
 *   timing puts them, as real pulses (low inside, the porch and picture around them). Until then the
 *   decoder holds the rows back and the panel shows nothing; a start that fails is dropped (onFalse).
 *   Noise, voice or a tone that passes for a header, or a chosen mode started on them, never shows.
 *
 * window.SSTV = { MODES, Decoder } (tests, and the TX side later);
 * window.SstvPanel = { summary(), feed(pcm, rate) }. The pictures received go into a strip: click one to
 * view it large (Export PNG saves it, Delete deletes it: the strip has no X of its own, 2026-11-23).
 *
 * The strip is kept by this browser (IndexedDB "telerig-sstv": each picture's PNG with its name, time and
 * size, under its file name), so a reload, a closed tab or a restart doesn't lose it: the pictures come
 * back, newest first, until they are deleted or the newest KEEP push them out. Two tabs of one browser
 * that received the same picture keep one copy, and a delete in either takes it out for both at their
 * next load. A browser with no such store (or one that refuses it, as some private windows do) keeps
 * them for the page's life only, as before. The pictures stay in the browser: nothing is sent anywhere.
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
  // The trial a start goes through (see the top): CONFIRM good syncs in the first CONFIRM_OF lines searched.
  // A good sync: at least SYNC_GOOD of the pulse below 1350 Hz, and at most SYNC_AROUND of the 2 ms on either
  // side (the porch and the picture are 1500 Hz and up). Measured: a real picture weak enough that its VIS is
  // barely read still gives >= 0.86 inside and <= 0.16 around; noise and voice give 0.22 .. 1 around.
  const CONFIRM = 4, CONFIRM_OF = 12, SYNC_GOOD = 0.7, SYNC_AROUND = 0.25;
  const SKIP_HEADER = 30;                            // ms: after a start, the search for the next one begins this far on
  // Joining a picture under way (rhythm()): RHYTHM pulses in a row, each a whole number (1..3) of the mode's
  // lines after the others to RHYTHM_TOL of a line, each as long as its sync (to 30 % or 1.5 ms); a pulse is
  // 3.5 .. 26 ms of the sync tone (the syncs are 4.9 .. 20 ms; a VIS bit run is 30 ms or more); the tone
  // within JOIN_OFFSET Hz of 1200.
  const RHYTHM = 4, RHYTHM_TOL = 0.01, JOIN_OFFSET = 150;
  // A start joined so needs CONFIRM_JOIN good syncs of its first CONFIRM_OF lines (a picture under way has a
  // sync on every line), not CONFIRM: a pulse every other line is no picture.
  const CONFIRM_JOIN = 8;
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
  // One line of a picture from its tones: the rows it makes (RGBA). `t`: { mean(a, b) the mean tone over the
  // samples [a, b), ms(t) ms -> samples, offset (Hz, the tuning), cr / cb (Robot 36's colour pair, kept) }.
  // The live decoder and a picture found in the recording draw the same way.
  function pixels(t, start, ch, w) {
    const out = new Float32Array(w), a = start + t.ms(ch.t), step = t.ms(ch.d) / w;
    for (let x = 0; x < w; x++) {
      const f = t.mean(a + x * step, a + (x + 1) * step) - t.offset;
      out[x] = clamp8(((f - 1500) / 800) * 255);
    }
    return out;
  }
  function lineRows(m, t, start) {
    const w = m.width, C = {};
    for (const ch of m.chans) C[ch.k] = ch.k === "S" ? t.mean(start + t.ms(ch.t), start + t.ms(ch.t + ch.d)) - t.offset
      : pixels(t, start, ch, ch.k === "C" ? w >> 1 : w);
    const rgba = () => new Uint8ClampedArray(w * 4);
    const ycc = (row, Y, cr, cb, x, crx) => {
      const y = Y[x], r = cr[crx] - 128, b = cb[crx] - 128;
      row[x * 4] = y + 1.402 * r; row[x * 4 + 1] = y - 0.344136 * b - 0.714136 * r; row[x * 4 + 2] = y + 1.772 * b; row[x * 4 + 3] = 255;
    };
    if (m.color === "GBR") {
      const row = rgba();
      for (let x = 0; x < w; x++) { row[x * 4] = C.R[x]; row[x * 4 + 1] = C.G[x]; row[x * 4 + 2] = C.B[x]; row[x * 4 + 3] = 255; }
      return [row];
    }
    if (m.color === "YCC") {
      const row = rgba();
      for (let x = 0; x < w; x++) ycc(row, C.Y, C.Cr, C.Cb, x, x);
      return [row];
    }
    if (m.color === "R36") {                             // chroma every other line: R-Y after 1500 Hz, B-Y after 2300 Hz
      const half = C.C;
      if (C.S < 1900) t.cr = half; else t.cb = half;
      const row = rgba();
      for (let x = 0; x < w; x++) ycc(row, C.Y, t.cr, t.cb, x, Math.min(half.length - 1, x >> 1));
      return [row];
    }
    const a = rgba(), b = rgba();                        // PD: two rows share one chroma
    for (let x = 0; x < w; x++) { ycc(a, C.Y0, C.Cr, C.Cb, x, x); ycc(b, C.Y1, C.Cr, C.Cb, x, x); }
    return [a, b];
  }
  // The recording: every tone the decoder measured (Int16 Hz a sample), the last TAPE_S seconds: the longest
  // mode, PD-290, is 289 s. find() looks back through it (see there).
  const TAPE_S = 300;

  class Decoder {
    // opts.tapeS: seconds of recording kept (0: none)
    constructor(fs, cb, opts) {
      this.fs = fs;
      this.hooks = cb || {};
      const tapeS = opts && opts.tapeS >= 0 ? opts.tapeS : TAPE_S;
      this.tapeCap = Math.ceil(fs * tapeS);
      this.tape = this.tapeCap > 0 ? new Int16Array(this.tapeCap) : null;
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
    reset() {
      this.state = "idle"; this.m = null; this.offset = 0; this.nextCheck = 0; this.line = 0; this.rows = 0; this.missed = 0; this.trial = null;
      this.pulses = []; this.inPulse = false; this.pulseStart = 0;        // the sync pulses heard while idle (rhythm())
    }
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
      if (this.tape) this.tape[(this.base + this.n) % this.tapeCap] = f + 0.5;   // rounded (f is 900 .. 2700)
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
    // the recording from absolute sample `from` (or as far back as it goes) to now: { f (Int16 Hz, oldest
    // first), from (the absolute index of f[0]), fs }
    recording(from) {
      const end = this.end(), a = this.tape ? Math.max(from || 0, end - this.tapeCap, 0) : end, n = end - a;
      const f = new Int16Array(n), i0 = a % (this.tapeCap || 1), first = Math.min(n, this.tapeCap - i0);
      if (n > 0) { f.set(this.tape.subarray(i0, i0 + first)); if (first < n) f.set(this.tape.subarray(0, n - first), first); }
      return { f, from: a, fs: this.fs };
    }
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
        else if (this.state === "rx") {
          // a start still on trial gives way to a header heard meanwhile (a real picture has no header in it)
          if (this.trial && this.want === "auto" && this.lookForStart()) continue;
          if (!this.nextLine()) return;
        }
        else return;
      }
    }
    lookForStart() {                                    // returns true when a picture starts
      const end = this.end(), step = Math.max(1, Math.round(this.ms(1)));
      if (this.nextCheck < this.base) this.nextCheck = this.base;
      while (this.nextCheck + step <= end) {
        this.nextCheck += step;
        const now = this.nextCheck;
        // a header always; the rhythm only while idle (a start on trial keeps its own pulses)
        if (this.want === "auto" ? this.vis(now) || (this.state === "idle" && this.rhythm(now)) : this.forcedSync(now)) return true;
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
      this.begin(m, off, visEnd + this.ms(m.first + m.syncAt), now);
      return true;
    }
    edgeDown(a, b, thr) {                               // first time the 2 ms mean falls below thr
      const h = this.ms(1);
      for (let i = Math.round(a); i <= b; i++) if (this.mean(i - h, i + h) < thr) return i;
      return Math.round((a + b) / 2);
    }
    // Auto, no header heard: a picture already under way, joined by the rhythm of its sync pulses. Each ms the
    // last ms is looked at: a run of sync-ish samples is a pulse. At a pulse's end, the pulses held (the last
    // 8) are tried against every mode: RHYTHM of them in a row (this one and the ones before it), each a
    // whole number of lines from this one, as long as the mode's sync. The picture starts at this pulse, the
    // tuning offset from the pulses' own tone, on trial as any start (its lines must prove it).
    rhythm(now) {
      const h = this.ms(1), fr = this.syncFrac(now - h, now);
      if (!this.inPulse) { if (fr >= 0.8) { this.inPulse = true; this.pulseStart = now - h; } return false; }
      if (fr > 0.2) return false;
      this.inPulse = false;
      const at = this.pulseStart, len = now - h - at, lenMs = (len * 1000) / this.fs;
      if (lenMs < 3.5 || lenMs > 26) return false;                      // not a line sync
      const p = this.pulses;
      p.push({ at, ms: lenMs, hz: this.mean(at + len * 0.2, at + len * 0.8) });
      while (p.length > 8) p.shift();
      if (p.length < RHYTHM) return false;
      const fits = (m, q) => Math.abs(q.ms - m.syncMs) <= Math.max(1.5, 0.3 * m.syncMs);
      let best = null;
      for (const m of MODES) {
        if (!fits(m, p[p.length - 1])) continue;
        const per = this.ms(m.lineMs), run = [p[p.length - 1]];
        for (let i = p.length - 2; i >= 0 && run.length < RHYTHM; i--) {
          const d = run[run.length - 1].at - p[i].at, k = Math.round(d / per);
          if (k >= 1 && k <= 3 && Math.abs(d - k * per) <= per * RHYTHM_TOL && fits(m, p[i])) run.push(p[i]);
          else if (d > 3.5 * per) break;
        }
        if (run.length < RHYTHM) continue;
        // a pulse missed at most once in the run: a train at twice a mode's line is not that mode
        const span = run[0].at - run[run.length - 1].at, lines = Math.round(span / per), err = Math.abs(span / per - lines);
        if (lines > RHYTHM) continue;
        if (!best || err < best.err) best = { m, run, err };
      }
      if (!best) return false;
      const tones = best.run.map((q) => q.hz).sort((a, b) => a - b), off = tones[tones.length >> 1] - 1200;
      if (!(Math.abs(off) <= JOIN_OFFSET)) return false;
      this.begin(best.m, off, at, now, true);
      return true;
    }
    forcedSync(now) {                                   // a chosen mode: start at the next sync pulse
      const m = BY_ID.get(this.want);
      const s0 = now - this.ms(m.syncMs);
      if (this.syncFrac(s0, now) < 0.85 || this.syncFrac(s0 - this.ms(3), s0) > 0.3) return false;
      this.begin(m, 0, s0, now);
      return true;
    }
    // firstSync: where line 0's sync starts, known to the sample (the VIS edge, or a sync just found):
    // the first line is not searched (after a VIS the stop bit and the first sync are one 1200 Hz tone).
    // found: where the header or sync was found. The picture starts on trial: onStart waits for its lines.
    begin(m, off, firstSync, found, joined) {
      if (this.trial) this.emit("onFalse", { mode: this.m, lines: this.line });    // a new header replaces a start on trial
      this.m = m; this.offset = off; this.state = "rx"; this.pulses = []; this.inPulse = false;
      this.line = 0; this.rows = 0; this.missed = 0;
      this.anchor = firstSync; this.period = this.ms(m.lineMs); this.firstLine = true; this.picFrom = Math.round(firstSync);
      this.cr = new Float32Array(m.width).fill(128); this.cb = new Float32Array(m.width).fill(128);
      // re-count the sync-ish samples with this picture's offset
      for (let i = 0; i < this.n; i++) this.S[i + 1] = this.S[i] + (this.f[i] - off < 1350 ? 1 : 0);
      this.trial = { good: 0, seen: 0, rows: [], offsetHz: Math.round(off), joined: !!joined, need: joined ? CONFIRM_JOIN : CONFIRM };
      this.nextCheck = found + this.ms(SKIP_HEADER);   // not this header again
    }
    judge(at, sync, best) {                             // on trial: is the sync found at `at` a real line sync?
      const t = this.trial, edge = this.ms(0.5), side = this.ms(2.5);
      const around = Math.max(this.syncFrac(at - side, at - edge), this.syncFrac(at + sync + edge, at + sync + side));
      t.seen++;
      if (best >= SYNC_GOOD && around <= SYNC_AROUND) t.good++;
    }
    confirm() {                                         // a picture after all: it starts now, with the rows held back
      const t = this.trial;
      this.trial = null;
      this.emit("onStart", { mode: this.m, offsetHz: t.offsetHz, joined: t.joined, from: this.picFrom });
      for (const r of t.rows) this.emit("onRow", r);
    }
    drop() {                                            // not a picture: forget it; the search goes on from where it was
      const m = this.m, lines = this.line, next = this.nextCheck;
      this.reset();
      this.nextCheck = next;
      this.emit("onFalse", { mode: m, lines });
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
        if (this.trial) this.judge(pred + bestD, sync, best);
        if (best > 0.55) {
          anchor = pred + 0.5 * bestD;
          this.period += 0.05 * bestD;
          this.missed = 0;
        } else if (++this.missed >= LOST_LINES) { this.finish("lost"); return false; }
      }
      this.anchor = anchor; this.firstLine = false;
      this.draw(anchor - this.ms(m.syncAt));
      this.line++;
      const t = this.trial;
      if (t && t.good >= t.need) this.confirm();
      else if (t && t.seen - t.good > CONFIRM_OF - t.need) { this.drop(); return true; }   // it can't get there now
      if (this.line >= m.lines) this.finish("done");
      return true;
    }
    draw(start) { for (const r of lineRows(this.m, this, start)) this.row(r); }
    row(rgba) {
      const r = { y: this.rows++, rgba };
      if (this.trial) this.trial.rows.push(r); else this.emit("onRow", r);
    }
    finish(reason) {
      if (this.trial) { this.drop(); return; }          // never shown, so it never ends either
      const m = this.m, rows = this.rows, from = this.picFrom;
      this.reset();
      this.nextCheck = this.end();
      // from / to: the picture's first line sync and now, absolute samples (where it lies in the recording)
      this.emit("onEnd", { mode: m, rows, reason, from, to: this.end() });
    }
    emit(name, arg) { try { if (typeof this.hooks[name] === "function") this.hooks[name](arg); } catch (_) {} }
  }

  // ---- a picture found in the recording, however weak (SSTV.find, SSTV.render) ----
  // The live decoder needs clean syncs to start (a header, or four pulses in a row) and gives a picture up
  // after LOST_LINES lines with none. Looking back through the recording, a picture is found by the rhythm
  // of all its syncs at once, so a sync lost in the static on most lines still counts on the others:
  //   1. the mean tone per BIN_MS; per sync length, each place's contrast: the tone AROUND_MS either side
  //      less the tone inside (a sync is a dip: 1200 Hz between the porch and the picture, both 1500 and up)
  //   2. per mode, the CAND strongest dips of each line-long stretch
  //   3. the line grid most of them sit on (to TOL_MS): TRIES pairs of strong dips a whole number of lines
  //      apart, at a period within SLANT of the mode's (the sender's clock), then least squares on its dips
  //   4. the picture: the run of lines with a dip on the grid, never GAP lines without; at most a picture
  //      long. It must hold FIND_MIN syncs, on FIND_DENSITY of its lines; the modes are scored syncs x
  //      share (a mode at half the line, Robot 36 for a Robot 72, holds the same syncs on half its lines)
  //   5. the sync's own place: the tone folded over those lines at the grid's period, the lowest sync-long
  //      stretch (a dark stripe in the picture lines up as well as the syncs do, but it is 1500 Hz and up).
  //      The tuning: the commonest tone inside the syncs, less 1200 (within FIND_OFFSET). A line next to
  //      the run whose sync still reads below EDGE_HZ joins it.
  //   6. where the lines go: after the picture's header, where its first line says the header ends (or
  //      opts.first: where the live decoder read it); else, if the picture ended inside the recording, its
  //      last line is the picture's last (a picture caught late); else its first line is the top (as when
  //      the live decoder joins one under way).
  // find(rec = Decoder.recording(), opts) -> the best picture, or null. opts: want ("auto" or a mode id),
  // skip ([[from, to], ...] absolute samples already decoded: no dip there counts), first (above).
  const BIN_MS = 0.25, AROUND_MS = 1, CAND = 3, CAND_MIN = 100, SLANT = 0.003, TOL_MS = 1, TRIES = 400, GAP = 24;
  const FIND_MIN = 16, FIND_DENSITY = 0.3, FIND_OFFSET = 250, EDGE_HZ = 1450, END_IN = 8, ISO = 4, ISO_MIN = 4, DEPTH_HZ = 200;
  function findPicture(rec, opts) {
    opts = opts || {};
    const f = rec.f, fs = rec.fs, n = f.length, Q = Math.max(1, Math.round((fs * BIN_MS) / 1000)), nb = Math.floor(n / Q);
    const toBins = (ms) => (ms * fs) / 1000 / Q, msS = (ms) => (ms * fs) / 1000;
    if (nb < toBins(3000)) return null;                   // under 3 s: nothing to find
    const B = new Float64Array(nb + 1);                   // prefix sums of the mean tone per bin
    for (let j = 0, i = 0; j < nb; j++) { let s = 0; for (let k = 0; k < Q; k++) s += f[i++]; B[j + 1] = B[j] + s / Q; }
    const skip = (opts.skip || []).map((r) => [(r[0] - rec.from) / Q, (r[1] - rec.from) / Q]).filter((r) => r[1] > 0 && r[0] < nb);
    const skipped = (j) => skip.some((r) => j >= r[0] && j < r[1]);
    const sMean = (x0, x1) => {                           // the mean tone over samples [x0, x1)
      x0 = Math.max(0, Math.round(x0)); x1 = Math.min(n, Math.round(x1));
      if (x1 <= x0) return NaN;
      let s = 0;
      for (let i = x0; i < x1; i++) s += f[i];
      return s / (x1 - x0);
    };
    const E = Math.max(1, Math.round(toBins(AROUND_MS))), tol = toBins(TOL_MS), peaks = new Map();
    let dq = null;
    // 1. the dips of one sync length: each place's contrast (the lower side of it, less the tone inside: a
    // sync has the porch after it and the picture or a porch before, a long 1200 Hz tone has neither), kept
    // where it is the deepest within R either side and at least CAND_MIN. In order: { j (bins), v (Hz) }.
    function peaksOf(L) {
      let p = peaks.get(L);
      if (p) return p;
      const c = new Float32Array(nb), R = Math.max(L, Math.round(toBins(2))), pj = [], pv = [];
      for (let j = E; j + L + E <= nb; j++) {
        const before = (B[j] - B[j - E]) / E, after = (B[j + L + E] - B[j + L]) / E;
        c[j] = Math.min(before, after) - (B[j + L] - B[j]) / L;
      }
      dq = dq || new Int32Array(nb);
      let h = 0, t = 0, next = 0;
      for (let j = 0; j < nb; j++) {                      // the deepest within [j - R, j + R]: a sliding maximum
        for (const hi = Math.min(nb - 1, j + R); next <= hi; next++) { while (t > h && c[dq[t - 1]] <= c[next]) t--; dq[t++] = next; }
        while (dq[h] < j - R) h++;
        if (dq[h] === j && c[j] >= CAND_MIN && !skipped(j)) { pj.push(j); pv.push(c[j]); }
      }
      p = { j: pj, v: pv };
      peaks.set(L, p);
      return p;
    }
    // 2. the CAND deepest dips of each line-long stretch, in order (bins); tops: the deepest of each stretch
    function dips(P0, L) {
      const p = peaksOf(L), at = [], tops = [], sel = [];
      let w = -1;
      const flush = () => {
        if (!sel.length) return;
        let top = sel[0];
        for (const x of sel) if (p.v[x] > p.v[top]) top = x;
        sel.sort((x, y) => x - y);
        for (const x of sel) { if (x === top) tops.push(at.length); at.push(p.j[x]); }
        sel.length = 0;
      };
      for (let i = 0; i < p.j.length; i++) {
        const wi = Math.floor(p.j[i] / P0);
        if (wi !== w) { flush(); w = wi; }
        if (sel.length < CAND) { sel.push(i); continue; }
        let lo = 0;
        for (let q = 1; q < sel.length; q++) if (p.v[sel[q]] < p.v[sel[lo]]) lo = q;
        if (p.v[i] > p.v[sel[lo]]) sel[lo] = i;
      }
      flush();
      return { at, tops };
    }
    function count(at, a, P) {                            // lines of the grid a + k P with a dip within tol
      let k = 0, last = NaN;
      for (let i = 0; i < at.length; i++) { const r = (at[i] - a) / P, kk = Math.round(r); if (kk !== last && Math.abs(r - kk) * P <= tol) { k++; last = kk; } }
      return k;
    }
    function onGrid(at, a, P) {                           // those lines (k) and their dips (the nearest one)
      const ks = [], ts = [];
      let last = NaN, lastD = 0;
      for (let i = 0; i < at.length; i++) {
        const r = (at[i] - a) / P, k = Math.round(r), d = Math.abs(r - k) * P;
        if (d > tol) continue;
        if (k === last) { if (d < lastD) { ts[ts.length - 1] = at[i]; lastD = d; } continue; }
        ks.push(k); ts.push(at[i]); last = k; lastD = d;
      }
      return { ks, ts };
    }
    function lsq(ks, ts) {                                // t = a + P k, least squares
      const N = ks.length;
      let mk = 0, mt = 0, kk = 0, kt = 0;
      for (let i = 0; i < N; i++) { mk += ks[i]; mt += ts[i]; }
      mk /= N; mt /= N;
      for (let i = 0; i < N; i++) { const d = ks[i] - mk; kk += d * d; kt += d * (ts[i] - mt); }
      return kk > 0 ? { a: mt - (kt / kk) * mk, P: kt / kk } : null;
    }
    function longest(ks, lines) {                         // 4. the run of lines with syncs: [k1, k2, syncs]
      let best = null, i0 = 0;
      for (let i = 1; i <= ks.length; i++) {
        if (i < ks.length && ks[i] - ks[i - 1] <= GAP) continue;
        let lo = i0, w = null;                            // longer than a picture: the picture-long stretch holding the most
        for (let hi = i0; hi < i; hi++) {
          while (ks[hi] - ks[lo] >= lines) lo++;
          if (!w || hi - lo + 1 > w[2]) w = [ks[lo], ks[hi], hi - lo + 1];
        }
        if (w && (!best || w[2] > best[2])) best = w;
        i0 = i;
      }
      return best;
    }
    function fitMode(m) {
      const why = (r) => { if (opts.why) opts.why.push(m.id + ": " + r); return null; };   // opts.why: why each mode isn't the picture
      const L = Math.max(1, Math.round(toBins(m.syncMs))), P0 = toBins(m.lineMs);
      const { at, tops } = dips(P0, L);
      if (tops.length < FIND_MIN) return why("dips " + tops.length);
      let seed = (m.vis * 2654435761) >>> 0;              // 3. the same audio, the same answer
      const rand = () => { seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0; return seed / 4294967296; };
      let g = null;
      for (let it = 0; it < TRIES; it++) {
        const i = Math.floor(rand() * tops.length), room = Math.min(150, tops.length - 1 - i);
        if (room < 2) continue;
        const ta = at[tops[i]], tb = at[tops[i + 2 + Math.floor(rand() * (room - 1))]], k = Math.round((tb - ta) / P0);
        if (k < 1 || Math.abs((tb - ta) / k / P0 - 1) > SLANT) continue;
        const s = count(at, ta, (tb - ta) / k);
        if (!g || s > g.s) g = { a: ta, P: (tb - ta) / k, s };
      }
      if (!g || g.s < FIND_MIN) return why("grid " + (g ? g.s : 0));
      let a = g.a, P = g.P;
      const refine = () => {
        for (let r = 0; r < 3; r++) {
          const { ks, ts } = onGrid(at, a, P);
          const ft = ks.length >= 3 ? lsq(ks, ts) : null;
          if (!ft || Math.abs(ft.P / P0 - 1) > SLANT * 1.5) return;
          a = ft.a; P = ft.P;
        }
      };
      refine();
      let span = longest(onGrid(at, a, P).ks, m.lines);
      if (!span) return why("run");
      // 5. the sync's own place: the lowest sync-long stretch of the line, folded over the run's lines
      const nP = Math.max(L + 1, Math.round(P)), H = nP >> 1, prof = new Float64Array(nP);
      let folded = 0;
      for (let k = span[0]; k <= span[1]; k++) {
        const s0 = Math.round(a + k * P) - H;
        if (s0 < 0 || s0 + nP > nb) continue;
        for (let d = 0; d < nP; d++) prof[d] += B[s0 + d + 1] - B[s0 + d];
        folded++;
      }
      if (!folded) return null;
      let win = 0, low = Infinity, lowAt = H;
      for (let d = 0; d < L; d++) win += prof[d % nP];
      for (let d = 0; d < nP; d++) { if (win < low) { low = win; lowAt = d; } win += prof[(d + L) % nP] - prof[d]; }
      let ks = onGrid(at, a, P).ks;
      if (Math.abs(lowAt - H) > tol) {                    // the grid sat on something else: move it to the syncs
        a += lowAt - H;
        refine();
        const k2s = onGrid(at, a, P).ks, again = longest(k2s, m.lines);
        if (again && again[2] >= span[2] / 2) { span = again; ks = k2s; }
      }
      // every other line's "sync" a different tone: the line is twice this long (a Robot 36 grid on a Robot 72)
      let ev = 0, ne = 0, od = 0, no = 0;
      for (let k = span[0]; k <= span[1]; k++) {
        const s = Math.round(a + k * P);
        if (s < 0 || s + L > nb) continue;
        const v = (B[s + L] - B[s]) / L;
        if (k & 1) { od += v; no++; } else { ev += v; ne++; }
      }
      if (ne && no && Math.abs(ev / ne - od / no) > 150) return why("every other line " + Math.round(ev / ne - od / no));
      // the syncs well below the rest of the line (noise has dips that line up too, but shallow ones)
      let all = 0;
      for (let d = 0; d < nP; d++) all += prof[d];
      const depth = all / nP / folded - low / L / folded;
      if (!(depth >= DEPTH_HZ)) return why("syncs " + Math.round(depth) + " Hz below the line");
      // the tuning: the median of the syncs' own tone (the middle of each; in static one sample's tone is
      // anything, a sync's mean still tells), less 1200
      const Ls = L * Q, tones = [];
      for (let k = span[0]; k <= span[1]; k++) { const s = (a + k * P) * Q, v = sMean(s + 0.2 * Ls, s + 0.8 * Ls); if (v === v) tones.push(v); }
      tones.sort((x, y) => x - y);
      const off = tones[tones.length >> 1] - 1200;
      if (!(Math.abs(off) <= FIND_OFFSET)) return why("offset " + Math.round(off));
      // the lines the grid holds in the recording (whole lines)
      const syncAt = toBins(m.syncAt), kMin = Math.ceil((syncAt - a) / P), kMax = Math.floor((nb - toBins(m.lineMs) + syncAt - a) / P);
      const reads = (k, hz) => k >= kMin && k <= kMax && sMean((a + k * P) * Q + 0.2 * Ls, (a + k * P) * Q + 0.8 * Ls) - off < hz;
      // the run's ends: dips that line up by chance (noise after the picture) are no syncs of it. A cluster
      // of fewer than ISO_MIN dips at an end, more than ISO lines from the rest, is dropped; then the end
      // comes in to the last line whose sync reads as one (below EDGE_HZ), unless none of END_IN does (a
      // picture deep in the static: its ends as the dips have them); then out over lines that read clearly
      // (below EDGE_HZ - 100: noise rarely does)
      const run = ks.filter((k) => k >= span[0] && k <= span[1]);
      let i1 = 0, i2 = run.length - 1;
      for (;;) {
        let j = i2;
        while (j > i1 && run[j] - run[j - 1] <= ISO) j--;
        if (j > i1 && i2 - j + 1 < ISO_MIN) i2 = j - 1; else break;
      }
      for (;;) {
        let j = i1;
        while (j < i2 && run[j + 1] - run[j] <= ISO) j++;
        if (j < i2 && j - i1 + 1 < ISO_MIN) i1 = j + 1; else break;
      }
      const trim = (k, dir, stop) => { for (let i = 0; i < END_IN && k - dir * i !== stop; i++) if (reads(k - dir * i, EDGE_HZ)) return k - dir * i; return k; };
      let k1 = run[i1], k2 = run[i2];
      k2 = trim(k2, 1, k1); k1 = trim(k1, -1, k2);
      while (k2 - k1 + 1 < m.lines && reads(k2 + 1, EDGE_HZ - 100)) k2++;
      while (k2 - k1 + 1 < m.lines && reads(k1 - 1, EDGE_HZ - 100)) k1--;
      const on = new Set(ks);
      let syncs = 0;
      for (let k = k1; k <= k2; k++) if (on.has(k) || reads(k, EDGE_HZ)) syncs++;
      const density = syncs / (k2 - k1 + 1);
      if (syncs < FIND_MIN || density < FIND_DENSITY) return why("syncs " + syncs + " share " + density.toFixed(2));
      return { m, a, P, k1, k2, kMin, kMax, off, L, syncs, density, score: syncs * density };
    }
    let best = null;
    const want = opts.want && opts.want !== "auto" ? BY_ID.get(opts.want) : null;
    for (const m of want ? [want] : MODES) { const r = fitMode(m); if (r && (!best || r.score > best.score)) best = r; }
    if (!best) return null;
    // 6. where its lines go
    const { m, a, P, k1, k2, kMin, kMax, off } = best, ended = k2 + 3 <= kMax;
    const header = (k) => {                               // the header before line k: how clear (or -Infinity)
      const end = (a + k * P) * Q - msS(m.first + m.syncAt), at = (x, y) => sMean(end + msS(x), end + msS(y)) - off;
      if (end - msS(910) < 0) return -Infinity;
      const lead = at(-570, -330), start = at(-295, -275), stop = at(-25, -5);
      let ones = 0, zeros = 0, n1 = 0, par = 0;
      for (let b = 0; b < 8; b++) {
        const bit = b < 7 ? (m.vis >> b) & 1 : par & 1;   // even parity
        par += bit;
        const v = at(-265 + 30 * b, -245 + 30 * b);
        if (bit) { ones += v; n1++; } else zeros += v;
      }
      ones /= n1; zeros /= 8 - n1;
      if (!(Math.abs(lead - 1900) < 150 && lead - start > 300 && lead - stop > 300 && zeros - ones > 80)) return -Infinity;
      return Math.min(lead - start, lead - stop, 3 * (zeros - ones));
    };
    let k0 = null, anchor = "start";
    if (opts.first != null) { k0 = Math.round(((opts.first - rec.from) / Q - a) / P); anchor = "header"; }
    else {
      let clear = -Infinity;
      for (let k = k1 - 3; k <= Math.min(k1 + 3, k2); k++) { const h = header(k); if (h > clear) { clear = h; k0 = k; } }
      if (k0 != null && clear > -Infinity) anchor = "header"; else k0 = null;
    }
    if (k0 == null) { k0 = ended ? k2 - (m.lines - 1) : k1; anchor = ended ? "end" : "start"; }
    const kFrom = Math.max(anchor === "header" ? k0 : k1, k0, kMin), kTo = Math.min(k2, k0 + m.lines - 1, kMax);
    if (kTo < kFrom) return null;
    const at = (k) => rec.from + Math.round((a + k * P) * Q - msS(m.syncAt));
    return { mode: m, offsetHz: Math.round(off), a: a * Q, P: P * Q, k0, kFrom, kTo, anchor, ended,
             syncs: best.syncs, density: best.density, from: at(kFrom), to: at(kTo + 1) };
  }
  // draw(rec, fit) -> the picture (the whole frame, black where nothing was found): { w, h, rgba, first
  // (its first row decoded), rows (how many) }
  function drawFound(rec, fit) {
    const m = fit.mode, f = rec.f, n = f.length, fs = rec.fs, w = m.width, h = m.height;
    const t = { offset: fit.offsetHz, ms: (x) => (x * fs) / 1000, cr: new Float32Array(w).fill(128), cb: new Float32Array(w).fill(128),
      mean(a, b) {
        a = Math.max(Math.round(a), 0); b = Math.min(Math.round(b), n);
        if (b <= a) return NaN;
        let s = 0;
        for (let i = a; i < b; i++) s += f[i];
        return s / (b - a);
      } };
    const rgba = new Uint8ClampedArray(w * h * 4);
    for (let i = 3; i < rgba.length; i += 4) rgba[i] = 255;
    for (let k = fit.kFrom; k <= fit.kTo; k++) {
      const rows = lineRows(m, t, fit.a + k * fit.P - t.ms(m.syncAt)), y = (k - fit.k0) * m.rowsPerLine;
      rows.forEach((r, i) => rgba.set(r, (y + i) * w * 4));
    }
    return { w, h, rgba, first: (fit.kFrom - fit.k0) * m.rowsPerLine, rows: (fit.kTo - fit.kFrom + 1) * m.rowsPerLine };
  }

  window.SSTV = { MODES, Decoder, find: findPicture, draw: drawFound, TAPE_S, byVis: (v) => BY_VIS.get(v) || null, byId: (id) => BY_ID.get(id) || null };

  // ---- the panel ----
  if (typeof document === "undefined") return;           // the decoder alone (tests, a worker)
  const $ = (id) => document.getElementById(id);
  const tile = document.querySelector('.grid-stack-item[data-tile="sstv"]');
  const canvas = $("sstvCanvas"), status = $("sstvStatus"), modeSel = $("sstvMode"), gallery = $("sstvGallery");
  if (!tile || !canvas || !status) return;
  // the strip keeps the newest KEEP pictures; one that ended early only if KEEP_PART of its lines came
  const KEY = "telerig.sstv.mode", KEEP = 12, KEEP_PART = 0.25;
  const g2d = (canvas.getContext && canvas.getContext("2d")) || null;
  let dec = null, rate = 0, cur = null, rowsDone = 0, unknown = null, lastNote = "";
  const pics = [];                                        // the strip, newest first: {url, file, name, at, w, h, rows, part, el, open}
  const known = [];                                       // the pictures kept with a span (this page's), PNG made or not yet
  const seen = [];                                        // { gen, span } of every picture the live decoder ended
  let gen = 0, lookedAt = 0;                              // gen: the decoder's timeline (a new decoder, a new one)

  // ---- the strip, kept by this browser: one record a picture, {file (its key), at (ms), name, w, h, rows, part, blob} ----
  // Everything waits for the store to open, in the order it was asked, and is dropped for good if it
  // can't be opened: the strip then lives as long as the page. Errors never reach the panel.
  const store = (function () {
    const NAME = "telerig-sstv", PICS = "pics";
    let db = null, dead = false, waiting = [];
    const fail = () => { dead = true; waiting = []; };
    function run(mode, fn) {                              // fn(the object store), in a transaction of its own
      if (dead) return;
      const go = () => { try { fn(db.transaction(PICS, mode).objectStore(PICS)); } catch (_) {} };
      if (db) go(); else waiting.push(go);
    }
    try {                                                 // a browser with no IndexedDB, or one that refuses it: caught here
      const req = window.indexedDB.open(NAME, 1);
      req.onupgradeneeded = () => { req.result.createObjectStore(PICS, { keyPath: "file" }); };
      req.onsuccess = () => { db = req.result; for (const f of waiting.splice(0)) f(); };
      req.onerror = fail;
      req.onblocked = fail;
    } catch (_) { fail(); }
    return {
      all(cb) { run("readonly", (os) => { const r = os.getAll(); r.onsuccess = () => cb(r.result); }); },
      put(rec) { run("readwrite", (os) => { os.put(rec); }); },
      del(file) { run("readwrite", (os) => { os.delete(file); }); },
    };
  })();

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
      say("Receiving " + cur.mode.name + (cur.joined ? " (joined under way)" : "") + " · line " + Math.min(rowsDone, cur.mode.height) + " of " + cur.mode.height + st);
    } else if (!rxOn()) say("");                            // RX is off: the window's own line says so (digital.js)
    else if (lastNote) say(lastNote);
    else say(want === "auto" ? "Listening for an SSTV picture: its header picks the mode, or its line timing if it is already under way."
                             : "Listening for " + BY_ID.get(want).name + ": it starts at the next sync pulse.");
  }
  const hooks = {
    onStart: ({ mode, offsetHz, joined }) => {
      const st = radio(), now = new Date(Date.now());
      cur = { mode, at: now, offsetHz, joined: !!joined, freqText: st.connected && st.freq ? (st.freq / 1e6).toFixed(3) + " MHz " + (st.mode_name || "") : "" };
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
    onEnd: ({ mode, rows, reason, from, to }) => {
      const c = cur;
      cur = null;
      // joined under way, or lost in the static: the recording may hold more of it, its lines in their places
      const kept = spans();
      seen.push({ gen, span: [from, to] });                // the searches while idle leave it out, kept or not
      if (c && (c.joined || reason !== "done")) {
        const r = fromRecording({ want: mode.id, skip: kept, first: c.joined ? null : from });
        const had = reason === "lost" ? rows - LOST_LINES * mode.rowsPerLine : rows;   // a lost one drew LOST_LINES more
        if (r && share([from, to], r.span) >= 0.5 && r.pic.rows >= had + (c.joined ? -4 : 8)) { showFound(r); return; }
      }
      const whole = reason === "done", enough = whole || rows >= mode.height * KEEP_PART;
      if (c && rows > 0 && enough) keep(c, rows, [from, to]);
      lastNote = whole ? "Received " + mode.name + " (" + rows + " lines) at " + utc(c ? c.at : new Date(Date.now())) + "."
        : mode.name + " ended early: the signal went away at line " + rows + " of " + mode.height + "." +
          (enough ? "" : " Not kept: less than a quarter of it arrived.");
      try { TR.srLive(lastNote); } catch (_) {}
      render();
    },
    onUnknown: ({ vis }) => { if (!cur) { lastNote = "An SSTV picture in a mode TeleRig can't decode (VIS " + vis + ")."; render(); } },
  };
  // span: where the picture lies in the recording (absolute samples of this decoder: gen), so the searches
  // leave it out and a better copy can take its place
  function keep(c, rows, span) {                          // the picture into the strip, newest first
    const name = c.mode.name + (c.freqText ? " · " + c.freqText : "") + " · " + utc(c.at);
    const file = ["TeleRig_SSTV", safe(c.mode.name), c.freqText ? safe(c.freqText.split(" ")[0]) + "MHz" : "", stamp(c.at)]
      .filter(Boolean).join("_") + ".png";
    const p = { file, name, at: c.at, w: c.mode.width, h: c.mode.height, rows, part: rows < c.mode.height };
    if (span) { p.span = span; p.gen = gen; known.push(p); }
    if (canvas.toBlob) canvas.toBlob((b) => {
      if (!b || p.gone) return;
      p.url = URL.createObjectURL(b);
      add(p);
      store.put({ file: p.file, at: p.at.getTime(), name: p.name, w: p.w, h: p.h, rows: p.rows, part: p.part, blob: b });   // kept by this browser too
    }, "image/png");
  }
  // ---- the recording: pictures found in the last minutes of audio (SSTV.find), however weak ----
  // After a picture the live decoder joined under way or lost in the static, the recording is searched for
  // it (above, onEnd): what came before the join and through the fades, each line in its place (one caught
  // late ends on the picture's last line). While nothing is being received, every LOOK_S of audio, it is
  // searched for a picture the live decoder never started (too weak for it): one that has ended is kept,
  // one still under way is named. Look back (#sstvLook) searches it now. The pictures kept are left out.
  const LOOK_S = 20;
  const spans = () => known.filter((p) => p.gen === gen).map((p) => p.span);
  const share = (a, b) => Math.max(0, Math.min(a[1], b[1]) - Math.max(a[0], b[0])) / Math.max(1, a[1] - a[0]);   // of a, in b
  function fromRecording(opts) {                          // { fit, pic, span } or null
    if (!dec) return null;
    try {
      const rec = dec.recording(), fit = findPicture(rec, opts);
      return fit ? { fit, pic: drawFound(rec, fit), span: [fit.from, fit.to] } : null;
    } catch (_) { return null; }
  }
  function paintFound(r) {                                // the picture found, on the canvas: { mode, at, freqText }
    blank(r.pic.w, r.pic.h);
    if (g2d && typeof ImageData === "function") g2d.putImageData(new ImageData(r.pic.rgba, r.pic.w, r.pic.h), 0, 0);
    const st = radio(), at = new Date(Date.now() - ((dec.end() - r.fit.from) / rate) * 1000);
    return { mode: r.fit.mode, at, freqText: (st.connected && st.freq ? (st.freq / 1e6).toFixed(3) + " MHz " + (st.mode_name || "") : "").trim() };
  }
  const linesText = (r) => (r.pic.rows >= r.pic.h ? "all " + r.pic.h + " lines" : "lines " + (r.pic.first + 1) + " to " + (r.pic.first + r.pic.rows) + " of " + r.pic.h);
  function showFound(r) {                                 // ... and into the strip, the partial copies it covers giving way
    const c = paintFound(r);
    for (const p of known.slice()) if (p.gen === gen && share(p.span, r.span) >= 0.5 && p.rows <= r.pic.rows) forget(p);
    keep(c, r.pic.rows, r.span);
    lastNote = "Found " + c.mode.name + " in the recording: " + linesText(r) + " (" + utc(c.at) + ").";
    try { TR.srLive(lastNote); } catch (_) {}
    try { document.dispatchEvent(new CustomEvent("tr:digital", { detail: { mode: "sstv" } })); } catch (_) {}
    render();
  }
  function look() {                                       // the search while nothing is being received
    lookedAt = dec.end();
    const r = fromRecording({ want, skip: spans().concat(seen.filter((x) => x.gen === gen).map((x) => x.span)) });
    if (!r || r.pic.rows < r.pic.h * KEEP_PART) return;  // a quarter of it at least, as the live decoder keeps
    if (r.fit.ended) { showFound(r); return; }
    lastNote = r.fit.mode.name + " under way in the static: it is decoded from the recording when it ends.";
    render();
  }
  const lookBtn = $("sstvLook");
  if (lookBtn) lookBtn.addEventListener("click", () => {
    if (cur) lastNote = "Receiving " + cur.mode.name + " now: look back once it ends.";
    else if (!dec || dec.end() < rate * 3) lastNote = "Nothing recorded yet: the recording runs while RX audio is on, the last " + Math.round(TAPE_S / 60) + " minutes.";
    else {
      const kept = spans(), r = fromRecording({ want, skip: kept }), got = Math.round(Math.min(dec.end(), rate * TAPE_S) / rate);
      if (!r) lastNote = "No " + (kept.length ? "other " : "") + "SSTV picture in the last " + Math.floor(got / 60) + ":" + pad(got % 60) + " of audio.";
      else if (r.fit.ended) { showFound(r); return; }
      else { const c = paintFound(r); lastNote = c.mode.name + " under way in the static, so far " + linesText(r) + ": it is kept when it ends."; }
    }
    try { TR.srLive(lastNote); } catch (_) {}
    render();
  });
  // the pictures this browser kept: under the ones received since the page opened, newest first
  store.all((recs) => {
    recs = recs.filter((r) => r.blob && typeof r.name === "string" && r.at > 0 && r.w > 0 && r.h > 0);
    recs.sort((a, b) => b.at - a.at);
    for (const r of recs) {
      const p = { file: r.file, name: r.name, at: new Date(r.at), w: r.w, h: r.h, rows: r.rows, part: !!r.part };
      try { p.url = URL.createObjectURL(r.blob); } catch (_) { continue; }   // its PNG is no PNG any more
      add(p, true);
    }
  });

  // ---- the strip: each picture opens the viewer (its Delete deletes it; no X on the strip) ----
  const T = () => window.TR || null;
  const icon = (n) => { const t = T(); return t && typeof t.icon === "function" ? t.icon(n) : null; };
  function add(p, older) {                                // older: one this browser kept, after the ones already shown
    const item = document.createElement("div"), open = document.createElement("button");
    const img = document.createElement("img");
    item.className = "sstv-item"; item.setAttribute("role", "listitem");
    open.className = "sstv-pic"; open.setAttribute("type", "button"); open.setAttribute("title", p.name);
    img.src = p.url; img.alt = p.name + (p.part ? " (part)" : "");
    open.appendChild(img);
    item.appendChild(open);
    open.addEventListener("click", () => view(p));
    p.el = item; p.open = open;
    if (gallery) gallery.insertBefore(item, older ? null : gallery.firstChild);
    if (older) pics.push(p); else pics.unshift(p);
    while (pics.length > KEEP) forget(pics[pics.length - 1]);
  }
  function forget(p) {                                    // out of the strip and of the browser's store, its PNG freed (and out of the viewer)
    p.gone = true;
    const k = known.indexOf(p);
    if (k >= 0) known.splice(k, 1);
    const i = pics.indexOf(p);
    if (i < 0) return;
    pics.splice(i, 1);
    if (p.el.parentNode) p.el.parentNode.removeChild(p.el);
    if (viewing === p && viewer) viewer.m.close();
    try { URL.revokeObjectURL(p.url); } catch (_) {}
    store.del(p.file);
  }
  function remove(p) {                                    // Delete in the viewer
    const i = pics.indexOf(p);
    if (i < 0) return;
    const next = pics[i + 1] || pics[i - 1] || null;      // the neighbour that takes the focus
    const focused = p.el.contains(document.activeElement) || viewing === p;
    forget(p);
    if (focused) {
      const to = next ? next.open : gallery;
      if (to === gallery && gallery && !gallery.hasAttribute("tabindex")) gallery.setAttribute("tabindex", "-1");
      if (to && typeof to.focus === "function") to.focus();
    }
    try { TR.srLive("Deleted " + p.name); } catch (_) {}
  }

  // ---- the viewer: the picture large, Export PNG, Delete (TR.modal: Esc, backdrop, focus back) ----
  let viewer = null, viewing = null;
  const ymd = (d) => d.getUTCFullYear() + "-" + pad(d.getUTCMonth() + 1) + "-" + pad(d.getUTCDate());
  const hms = (d) => pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes()) + ":" + pad(d.getUTCSeconds());
  function view(p) {
    const t = T();
    if (!t || typeof t.modal !== "function" || typeof t.h !== "function") { save(p); return; }   // no dialog here: save it
    if (t.txBlocksModal && t.txBlocksModal()) {            // the backdrop would cover the top bar's STOP
      if (typeof t.toast === "function") t.toast("Stop transmitting to open a picture.", "warn");
      return;
    }
    if (!viewer) viewer = buildViewer(t);
    viewing = p;
    viewer.show(p);
    viewer.m.open();
    viewer.first.focus();
  }
  function save(p) {
    const a = document.createElement("a");
    a.setAttribute("href", p.url); a.setAttribute("download", p.file);
    document.body.appendChild(a); a.click(); a.remove();
  }
  function buildViewer(t) {
    const h = t.h;
    const m = t.modal({ id: "sstvView", className: "sstv-view", onClose: () => { viewing = null; } });
    const name = h("span", { class: "sstv-view-name", id: m.titleId });
    const img = h("img", { class: "sstv-view-img", alt: "" });
    const cap = h("p", { class: "sstv-view-cap" });
    const exp = h("a", { class: "ctl-btn primary", id: "sstvViewExport", title: "Save the picture as a PNG file", text: "Export PNG" });
    const del = h("button", { type: "button", class: "ctl-btn danger-ghost", id: "sstvViewDelete", text: "Delete",
                              on: { click: () => { if (viewing) remove(viewing); } } });
    m.dialog.append(
      h("header", { class: "tm-head" },
        h("span", { class: "tm-chip" }, icon("tr-sstv"), h("b", { text: "SSTV" })),
        name,
        h("button", { type: "button", class: "tm-x", title: "Close", "aria-label": "Close", on: { click: () => m.close() } },
          icon("x") || "×")),
      h("div", { class: "tm-body" }, img, cap, h("div", { class: "tm-actions" }, del, exp)));
    return {
      m, first: exp,
      show(p) {
        name.textContent = p.name;
        img.src = p.url; img.alt = p.name + (p.part ? " (part)" : "");
        img.style.setProperty("aspect-ratio", p.w + " / " + p.h);      // the box before the PNG has loaded
        cap.textContent = "Received " + ymd(p.at) + " " + hms(p.at) + " UTC · " + p.w + " × " + p.h +
          (p.part ? " · " + p.rows + " of " + p.h + " lines" : "");
        exp.setAttribute("href", p.url); exp.setAttribute("download", p.file);
      },
    };
  }
  function feed(pcm, r) {
    if (!onGrid()) return;
    if (!dec || r !== rate) { rate = r; dec = new Decoder(r, hooks); dec.setMode(want); gen++; lookedAt = 0; }
    dec.feed(pcm);
    if (!cur && dec.end() - lookedAt >= LOOK_S * rate) look();   // (a start on trial isn't shown: no matter)
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
