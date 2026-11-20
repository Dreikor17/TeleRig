/* TeleRig RTTY receive (the digital window's RTTY tab): Baudot radioteletype decoded in this browser from
 * the radio's audio (window.RadioAudio.tapPcm: 16 kHz mono, before the volume). Receive only: nothing here
 * keys the radio or sends anything to it.
 *
 * The demodulator: the audio mixed down at the mark and at the space tone, each integrated over one bit
 * (the matched filter for a tone that lasts a bit), its envelope normalised by its own slowly decaying
 * peak (automatic threshold correction: a tone fading on one side doesn't bias the decision), mark minus
 * space. Then the start-stop framing: a start bit of space, five data bits (least significant first), a
 * stop bit of mark, each sampled where its bit ends; a stop bit that isn't mark drops the character.
 * Baudot / ITA2 with US-TTY figures, LTRS / FIGS shifts, unshift on space; a squelch keeps noise quiet.
 *
 * Mark is the higher radio frequency, as amateurs send it: on LSB (and Icom RTTY, Yaesu RTTY-LSB) the
 * lower audio tone; on USB (RTTY-R, RTTY-USB, data on USB) the higher one. `reverse` follows the radio's
 * mode by default; the Rev key swaps it.
 *
 * window.RTTY = { Decoder, PRESETS, polarityFor(mode), LTRS, FIGS }
 *   new Decoder(rate, { baud, shift, center, reverse }, onChar)  .feed(pcm)  .set(opts)  .level()
 *   window.RttyPanel = { summary(), feed(pcm, rate), tune(hz) } (the tab; tests)
 */
(function () {
  "use strict";

  // ---- Baudot (ITA2 letters, US-TTY figures); "" = no character (NUL, CR, BELL, the shifts) ----
  const LTRS = ["", "E", "\n", "A", " ", "S", "I", "U", "", "D", "R", "J", "N", "F", "C", "K",
                "T", "Z", "L", "W", "H", "Y", "P", "Q", "O", "B", "G", "", "M", "X", "V", ""];
  const FIGS = ["", "3", "\n", "-", " ", "", "8", "7", "", "$", "4", "'", ",", "!", ":", "(",
                "5", "\"", ")", "2", "#", "6", "0", "1", "9", "?", "&", "", ".", "/", ";", ""];
  const C_FIGS = 27, C_LTRS = 31, C_SPACE = 4;
  const PRESETS = [                               // id: baud, shift (Hz); the name reads "speed / shift"
    { id: "45-170", baud: 45.45, shift: 170, name: "45.45 / 170" },
    { id: "50-170", baud: 50, shift: 170, name: "50 / 170" },
    { id: "75-170", baud: 75, shift: 170, name: "75 / 170" },
    { id: "50-425", baud: 50, shift: 425, name: "50 / 425" },
    { id: "50-850", baud: 50, shift: 850, name: "50 / 850" },
  ];
  const CENTER = 2210;                            // mark 2125 / space 2295 Hz: the radios' RTTY tones
  // the radio's mode -> reversed? (mark the higher audio tone)
  const polarityFor = (mode) => /^(USB|DATA-USB|RTTY-R|RTTY-USB|USB-D)$/.test(String(mode || ""));

  function Decoder(rate, opts, onChar) {
    this.rate = rate;
    this.onChar = onChar || (() => {});
    this.o = Object.assign({ baud: 45.45, shift: 170, center: CENTER, reverse: false, squelch: 0.55 }, opts || {});
    this.reset();
  }
  Decoder.prototype.reset = function () {
    const o = this.o, r = this.rate;
    this.N = Math.max(4, Math.round(r / o.baud));          // samples per bit
    const hi = o.center + o.shift / 2, lo = o.center - o.shift / 2;
    this.fm = o.reverse ? hi : lo;                          // mark
    this.fs = o.reverse ? lo : hi;                          // space
    this.wm = (2 * Math.PI * this.fm) / r; this.ws = (2 * Math.PI * this.fs) / r;
    this.pm = 0; this.ps = 0;
    this.hist = new Float64Array(this.N * 4);               // ring of the mixed I/Q: mark I, mark Q, space I, space Q
    this.s0 = this.s1 = this.s2 = this.s3 = 0;              // their running sums over the last bit
    this.hi = 0; this.n = 0;
    this.peakM = 1e-9; this.peakS = 1e-9; this.floorM = 0; this.floorS = 0;
    this.k = 1 - Math.exp(-1 / (this.N * 16));               // a peak lets go over about 16 bits,
    this.kf = 1 - Math.exp(-1 / (this.N * 128));             // a floor rises over about 128
    this.q = 0; this.qt = 0;                                // the squelch: the decisions' quality, per bit
    this.state = 0;                                         // 0 idle (waiting for a start edge), 1 in a character
    this.t = 0; this.next = 0; this.bit = 0; this.code = 0; this.last = 1;
    this.figs = false;
    this.m = 0; this.s = 0; this.open = false;
  };
  Decoder.prototype.set = function (opts) { Object.assign(this.o, opts || {}); this.reset(); };
  Decoder.prototype.level = function () {
    return { mark: Math.min(1, this.m / this.peakM), space: Math.min(1, this.s / this.peakS), quality: this.q, open: this.open };
  };
  Decoder.prototype.emit = function (code) {
    if (code === C_LTRS) { this.figs = false; return; }
    if (code === C_FIGS) { this.figs = true; return; }
    const ch = (this.figs ? FIGS : LTRS)[code];
    if (code === C_SPACE) this.figs = false;                // unshift on space
    if (ch) this.onChar(ch);
  };
  Decoder.prototype.feed = function (pcm) {
    const scale = pcm instanceof Int16Array ? 1 / 32768 : 1, N = this.N, h = this.hist;
    let s0 = this.s0, s1 = this.s1, s2 = this.s2, s3 = this.s3;
    for (let i = 0; i < pcm.length; i++) {
      const x = pcm[i] * scale;
      // mix down at both tones and integrate over one bit (running sums over the last N samples)
      const a = x * Math.cos(this.pm), b = -x * Math.sin(this.pm), c = x * Math.cos(this.ps), e = -x * Math.sin(this.ps);
      this.pm += this.wm; this.ps += this.ws;
      if (this.pm > 1e4) this.pm %= 2 * Math.PI;
      if (this.ps > 1e4) this.ps %= 2 * Math.PI;
      const at = this.hi * 4;
      s0 += a - h[at]; s1 += b - h[at + 1]; s2 += c - h[at + 2]; s3 += e - h[at + 3];
      h[at] = a; h[at + 1] = b; h[at + 2] = c; h[at + 3] = e;
      this.hi = this.hi + 1 === N ? 0 : this.hi + 1;
      if (++this.n % (N * 4096) === 0) {                    // now and then, the sums afresh (no rounding drift)
        s0 = s1 = s2 = s3 = 0;
        for (let k = 0; k < N; k++) { s0 += h[4 * k]; s1 += h[4 * k + 1]; s2 += h[4 * k + 2]; s3 += h[4 * k + 3]; }
      }
      const m = Math.sqrt(s0 * s0 + s1 * s1) / N, s = Math.sqrt(s2 * s2 + s3 * s3) / N;
      this.m = m; this.s = s;
      // automatic threshold correction (W7AY's "optimal ATC"): each tone measured between its own floor
      // and peak (fast attack, slow release), so a fade on one side, or a tone that hasn't been sent for a
      // while, never biases the decision. A floor never sits above half its peak: a tone that has been on
      // for a long while (a steady idle mark) has no "off" level of its own yet.
      const k = this.k, kf = this.kf;
      this.peakM = m > this.peakM ? m : this.peakM + (m - this.peakM) * k;
      this.peakS = s > this.peakS ? s : this.peakS + (s - this.peakS) * k;
      this.floorM = Math.min(m < this.floorM ? m : this.floorM + (m - this.floorM) * kf, 0.5 * this.peakM);
      this.floorS = Math.min(s < this.floorS ? s : this.floorS + (s - this.floorS) * kf, 0.5 * this.peakS);
      const am = this.peakM - this.floorM, as = this.peakS - this.floorS;
      const cm = Math.min(Math.max(m, this.floorM), this.peakM) - this.floorM;
      const cs = Math.min(Math.max(s, this.floorS), this.peakS) - this.floorS;
      const d = cm * am - cs * as - 0.25 * (am * am - as * as);   // > 0 mark, < 0 space
      const bitNow = d >= 0 ? 1 : 0;
      // the squelch: how cleanly one tone stands over the other where a bit is read (once a bit while
      // idle), smoothed over a few bits; noise reads about 0.3, a copyable signal near 1
      const quality = () => { this.q += (Math.abs(m - s) / (m + s + 1e-12) - this.q) * 0.25; this.open = this.q > this.o.squelch; };
      if (this.state === 0 && ++this.qt >= N) { this.qt = 0; quality(); }
      this.t++;
      if (this.state === 0) {
        if (this.open && this.last === 1 && bitNow === 0) {  // a mark -> space edge: a start bit, maybe
          this.state = 1; this.next = this.t + Math.round(N / 2); this.bit = -1; this.code = 0;
        }
      } else if (this.t >= this.next) {
        quality();
        if (this.bit === -1) {                               // the end of the start bit: still space?
          if (bitNow !== 0) this.state = 0;
          else { this.bit = 0; this.next += N; }
        } else if (this.bit < 5) {
          this.code |= bitNow << this.bit;
          this.bit++; this.next += N;
        } else {                                             // the stop bit
          if (bitNow === 1 && this.open) this.emit(this.code);
          this.state = 0; this.qt = 0;
        }
      }
      this.last = bitNow;
    }
    this.s0 = s0; this.s1 = s1; this.s2 = s2; this.s3 = s3;
  };

  window.RTTY = { Decoder, PRESETS, polarityFor, LTRS, FIGS, CENTER };

  // ---- the tab (browser only) ----
  if (typeof document === "undefined") return;
  const $ = (id) => document.getElementById(id);
  const pane = $("dmPaneRtty"), textEl = $("rttyText"), status = $("rttyStatus"), modeSel = $("rttyMode");
  const revBtn = $("rttyRev"), clearBtn = $("rttyClear"), stripEl = $("rttyStrip");
  const tile = pane ? pane.closest(".grid-stack-item") : null;
  if (!pane || !textEl || !status || !tile) return;
  const KEY_MODE = "telerig.rtty.mode", KEY_CENTER = "telerig.rtty.center", KEY_REV = "telerig.rtty.rev", KEEP = 6000;
  const ls = (k, v) => { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (_) {} return null; };
  const audio = () => window.RadioAudio || null;
  const rxOn = () => { const A = audio(); try { return !!(A && A.on()); } catch (_) { return false; } };
  const onGrid = () => !!(tile.parentElement && tile.parentElement.id === "grid");
  const radio = () => { try { return (window.RadioControl && RadioControl.state()) || {}; } catch (_) { return {}; } };

  let preset = PRESETS.find((p) => p.id === ls(KEY_MODE)) || PRESETS[0];
  let center = +ls(KEY_CENTER);
  if (!(center >= 300 && center <= 3000)) center = CENTER;
  let manualRev = ls(KEY_REV) === "1";                     // the Rev key: swaps whatever the radio's mode says
  let dec = null, rate = 0, text = "", lastMode = null, chars = 0;
  if (modeSel) {
    for (const p of PRESETS) { const o = document.createElement("option"); o.value = p.id; o.textContent = p.name; modeSel.appendChild(o); }
    modeSel.value = preset.id;
    modeSel.addEventListener("change", () => {
      preset = PRESETS.find((p) => p.id === modeSel.value) || PRESETS[0];
      ls(KEY_MODE, preset.id); apply(); render();
    });
  }
  const reversed = () => polarityFor(radio().mode_name) !== manualRev;
  function apply() { if (dec) dec.set({ baud: preset.baud, shift: preset.shift, center, reverse: reversed() }); }
  if (revBtn) {
    revBtn.setAttribute("aria-pressed", manualRev ? "true" : "false");
    revBtn.classList.toggle("on", manualRev);
    revBtn.addEventListener("click", () => {
      manualRev = !manualRev; ls(KEY_REV, manualRev ? "1" : "0");
      revBtn.setAttribute("aria-pressed", manualRev ? "true" : "false"); revBtn.classList.toggle("on", manualRev);
      apply(); render();
    });
  }
  if (clearBtn) clearBtn.addEventListener("click", () => { text = ""; textEl.textContent = ""; chars = 0; render(); });

  function tones() {
    const hi = center + preset.shift / 2, lo = center - preset.shift / 2, rv = reversed();
    return { mark: rv ? hi : lo, space: rv ? lo : hi };
  }
  function say(t) { if (status.textContent !== t) status.textContent = t; }
  function render() {
    const tn = tones();
    if (!rxOn()) { say("Turn on RX in the Audio panel to decode RTTY."); return; }
    const lv = dec ? dec.level() : null;
    say("Mark " + Math.round(tn.mark) + " Hz, space " + Math.round(tn.space) + " Hz" + (reversed() ? " (reversed)" : "") +
        (lv && lv.open ? ". Copying." : ". Click a signal in the strip to tune to it."));
  }
  function addText(ch) {
    const atEnd = textEl.scrollHeight - textEl.scrollTop - textEl.clientHeight < 24;
    text += ch; chars++;
    if (text.length > KEEP) text = text.slice(text.length - KEEP);
    textEl.textContent = text;                             // text only, never markup
    if (atEnd) textEl.scrollTop = textEl.scrollHeight;
    if (ch.trim()) document.dispatchEvent(new CustomEvent("tr:digital", { detail: { mode: "rtty" } }));
  }
  function feed(pcm, r) {
    if (!onGrid()) return;
    if (!dec || r !== rate) { rate = r; dec = new Decoder(r, { baud: preset.baud, shift: preset.shift, center, reverse: reversed() }, addText); }
    const mode = radio().mode_name;
    if (mode !== lastMode) { lastMode = mode; apply(); }
    dec.feed(pcm);
  }
  // tune to the pair nearest a click: the strongest mark / space pair (this shift) within 150 Hz of it,
  // centred on its two peaks; with no spectrum, the click itself
  function tune(hz, spectrum) {
    let best = hz;
    if (spectrum && spectrum.db && spectrum.binHz) {
      const db = spectrum.db, bw = spectrum.binHz, half = preset.shift / 2;
      const peak = (f) => {                               // the strongest bin within one bin of f
        const k0 = Math.round(f / bw);
        let k = -1, v = -Infinity;
        for (let j = k0 - 1; j <= k0 + 1; j++) if (j >= 0 && j < db.length && db[j] > v) { v = db[j]; k = j; }
        return { k, v };
      };
      let bestV = -Infinity;
      for (let c = hz - 150; c <= hz + 150; c += 2) {
        const v = Math.min(peak(c - half).v, peak(c + half).v);
        if (v > bestV || (v === bestV && Math.abs(c - hz) < Math.abs(best - hz))) { bestV = v; best = c; }
      }
      const a = peak(best - half), b = peak(best + half);
      if (a.k >= 0 && b.k >= 0) best = ((a.k + b.k) / 2) * bw;
    }
    center = Math.max(300, Math.min(3000, Math.round(best)));
    ls(KEY_CENTER, String(center));
    apply(); render();
    return center;
  }
  (function tap() {                                        // app.js loads first; wait for it if needed
    const A = audio();
    if (A && typeof A.tapPcm === "function") A.tapPcm(feed); else setTimeout(tap, 500);
  })();
  setInterval(render, 1000);
  render();

  window.RttyPanel = {
    summary: () => { const t = text.replace(/\s+/g, " ").trim(); return t ? "RTTY " + t.slice(-18).trim() : ""; },
    feed: (pcm, r) => { if (pcm && pcm.length && r > 0) feed(pcm, r); },
    tune: (hz, spectrum) => tune(+hz, spectrum),
    nudge: (d) => tune(center + (+d || 0)),               // the strip's arrow keys: 10 Hz a press
    markers: () => { const t = tones(); return [{ hz: t.mark, label: "M" }, { hz: t.space, label: "S" }]; },
    strip: stripEl,
  };
})();
