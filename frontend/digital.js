/* TeleRig Modem window (the "sstv" tile, titled Modem; its id stays "sstv" so saved layouts keep it): CW
 * (cwtool.js: the neural decoder, the sidetone, CW TX), RTTY (rtty.js), FT8 (ft8.js, ft8-decode.js) and SSTV
 * (sstv.js receive, sstv-tx.js send). One mode is shown at a time, but every decoder listens while the
 * window is on the grid (CW's while its Decode key is on), so switching never loses a picture, a line of
 * text or a slot; a mode that has news while hidden gets a dot on its key.
 *
 * The audio strip above RTTY and FT8: a small waterfall of the radio's audio from 0 to 3 kHz (the 16 kHz
 * PCM tap, a 1024-point FFT), with the mode's own markers (RTTY: mark and space; FT8: the last slot's
 * messages). A click, or the arrow keys on the focused strip, tells the mode where: RTTY tunes there.
 *
 * While this page sends (an SSTV picture, a CW message) the window stays on that mode and the others
 * can't be shown: its STOP stays in view. RTTY and FT8 receive only; SSTV's Send tab is sstv-tx.js's own
 * and CW's TX cwtool.js's, both on the PTT rules.
 *
 * tr:modem {mode}: a mode came into view: the page opened on it, the window came onto the grid showing
 * it (added, a layout reset, another device's layout), or it was chosen (its key, or the window held on
 * it while it sends). CW starts its decoder then (cwtool.js): every mode decodes without being asked.
 * Folding the window and opening it again is not a new view (its modes keep listening while folded).
 *
 * The text screens (CW's chat, RTTY's text, FT8's list) scroll inside the window. At the end a screen
 * follows what comes in (cwtool.js, rtty.js and ft8.js scroll it only then); scrolled back it stays where
 * it was left, and it comes back to that place, or to the end it was following, when it is shown again.
 *
 * window.DigitalPanel = { mode(), show(id), feed(pcm, rate), summary() }
 */
(function () {
  "use strict";
  if (typeof document === "undefined") return;
  const $ = (id) => document.getElementById(id);
  const MODES = ["cw", "rtty", "ft8", "sstv"];             // the keys' order; the first is the default
  const keys = { cw: $("dmCw"), rtty: $("dmRtty"), ft8: $("dmFt8"), sstv: $("dmSstv") };
  const panes = { cw: $("dmPaneCw"), rtty: $("dmPaneRtty"), ft8: $("dmPaneFt8"), sstv: $("dmPaneSstv") };
  if (MODES.some((m) => !keys[m] || !panes[m])) return;
  const tile = panes.sstv.closest(".grid-stack-item");
  const KEY = "telerig.digital.mode";
  const ls = (k, v) => { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (_) {} return null; };
  // a transmission from this page: the mode sending it (its STOP), or null
  const busy = (o) => { try { return !!(o && typeof o.busy === "function" && o.busy()); } catch (_) { return false; } };
  const sending = () => (busy(window.SstvTx) ? "sstv" : busy(window.CwPanel) ? "cw" : null);
  const WAIT = { sstv: "A picture is being sent: STOP it first", cw: "CW is being sent: STOP it first" };
  let cur = MODES.includes(ls(KEY)) ? ls(KEY) : MODES[0];
  const onGrid = () => !!(tile && tile.parentElement && tile.parentElement.id === "grid");
  let viewed = null, wasOnGrid = false;                   // the mode last announced in view; the window then on the grid
  function announce() {                                    // tr:modem: the mode in view, once per new view
    const g = onGrid();
    if (g && (!wasOnGrid || viewed !== cur)) {
      viewed = cur;
      try { document.dispatchEvent(new CustomEvent("tr:modem", { detail: { mode: cur } })); } catch (_) {}
    }
    wasOnGrid = g;
  }

  function render() {
    const lock = sending();
    if (lock) cur = lock;                                  // the mode sending stays in view
    for (const m of MODES) {
      const on = m === cur, k = keys[m];
      k.classList.toggle("on", on);
      k.setAttribute("aria-selected", on ? "true" : "false");
      k.tabIndex = on ? 0 : -1;
      if (on) k.classList.remove("news");
      const off = !!lock && !on;
      k.disabled = off;
      k.title = off ? WAIT[lock] : "";
      panes[m].hidden = !on;
    }
    announce();
  }
  function show(m) {
    const lock = sending();
    if (!MODES.includes(m) || (lock && m !== lock)) return false;
    cur = m; ls(KEY, m); render();
    for (const s of strips) s.paint(true);
    return true;
  }
  for (const m of MODES) {
    keys[m].addEventListener("click", () => show(m));
    keys[m].addEventListener("keydown", (e) => {            // the tablist's arrow keys
      const i = MODES.indexOf(m), d = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
      if (!d) return;
      e.preventDefault();
      const n = MODES[(i + d + MODES.length) % MODES.length];
      if (show(n)) keys[n].focus();
    });
  }
  // a mode with news while another is shown: a dot on its key (cwtool.js, rtty.js, ft8.js, sstv.js send tr:digital)
  document.addEventListener("tr:digital", (e) => {
    const m = e && e.detail && e.detail.mode;
    if (MODES.includes(m) && m !== cur) keys[m].classList.add("news");
  });
  // the window came onto the grid (or left it): layout.js's events
  document.addEventListener("tr:tile", (e) => { if (e && e.detail && e.detail.id === "sstv") announce(); });
  document.addEventListener("tr:layout", () => announce());

  // ---- the text screens keep their place ----
  // The browser forgets a hidden box's scroll position (another mode shown, the window folded or moved
  // on the grid), so each screen notes where the operator left it: at the end (it follows), or a place.
  // Shown again, or resized, it goes back there. Its own script scrolls it as text comes in, only while
  // it is at the end.
  const END_PX = 24;                                       // == rtty.js / ft8.js / cwtool.js: "at the end"
  function keepPlace(el) {
    if (!el || typeof el.addEventListener !== "function") return;
    let follow = true, top = 0;
    el.addEventListener("scroll", () => {
      if (!el.clientHeight) return;                        // hidden: the browser's reset, not the operator
      follow = el.scrollHeight - el.scrollTop - el.clientHeight < END_PX;
      top = el.scrollTop;
    });
    if (typeof ResizeObserver === "function") {
      new ResizeObserver(() => { if (el.clientHeight) el.scrollTop = follow ? el.scrollHeight : top; }).observe(el);
    }
  }
  for (const id of ["cwOut", "rttyText", "ft8List"]) keepPlace($(id));

  // ---- the audio strip ----
  const FFT_N = 1024, HOP = 512, ROWS_EVERY = 4, F_TOP = 3000;
  const LUT = {                                            // waterfall.js's colours: the night/day instrument, the radio's screen
    night: [[0, [2, 6, 18]], [0.18, [12, 28, 110]], [0.36, [10, 110, 200]], [0.52, [10, 200, 200]], [0.66, [40, 210, 90]],
            [0.8, [240, 220, 40]], [0.92, [240, 90, 30]], [1, [255, 245, 230]]],
    screen: [[0, [12, 26, 102]], [0.2, [29, 53, 181]], [0.38, [40, 110, 230]], [0.52, [22, 180, 232]], [0.66, [63, 210, 107]],
             [0.8, [242, 211, 59]], [0.92, [240, 82, 46]], [1, [255, 240, 230]]],
  };
  function colour(lut, v) {
    v = v < 0 ? 0 : v > 1 ? 1 : v;
    for (let i = 1; i < lut.length; i++) {
      if (v <= lut[i][0]) {
        const [a, ca] = lut[i - 1], [b, cb] = lut[i], f = (v - a) / (b - a || 1);
        return [0, 1, 2].map((j) => Math.round(ca[j] + (cb[j] - ca[j]) * f));
      }
    }
    return lut[lut.length - 1][1];
  }
  const theme = () => { try { return window.TR && TR.theme && TR.theme() === "screen" ? "screen" : "night"; } catch (_) { return "night"; } };
  const fft = (window.FT8 && window.FT8.makeFft) ? window.FT8.makeFft(FFT_N) : null;
  const win = new Float64Array(FFT_N);
  for (let i = 0; i < FFT_N; i++) win[i] = 0.5 - 0.5 * Math.cos((2 * Math.PI * i) / FFT_N);

  function Strip(canvas, mode, opts) {
    this.c = canvas; this.mode = mode; this.o = opts || {};
    this.g = canvas && canvas.getContext ? canvas.getContext("2d") : null;
    this.rows = null; this.w = 0; this.h = 0; this.last = null; this.binHz = 0;
    if (!canvas) return;
    canvas.addEventListener("click", (e) => {
      const r = canvas.getBoundingClientRect ? canvas.getBoundingClientRect() : { left: 0, width: canvas.width || 1 };
      const hz = ((e.clientX - r.left) / (r.width || 1)) * F_TOP;
      if (this.o.onTune) this.o.onTune(hz, this.spectrum());
      this.paint(true);
    });
    canvas.addEventListener("keydown", (e) => {            // the strip, focused: arrows nudge the tuning 10 Hz
      const d = e.key === "ArrowRight" || e.key === "ArrowUp" ? 10 : e.key === "ArrowLeft" || e.key === "ArrowDown" ? -10 : 0;
      if (!d || !this.o.onNudge) return;
      e.preventDefault();
      this.o.onNudge(d);
      this.paint(true);
    });
  }
  Strip.prototype.visible = function () {
    return !!(this.c && tile && tile.parentElement && tile.parentElement.id === "grid" && cur === this.mode && this.c.offsetParent !== null);
  };
  Strip.prototype.spectrum = function () { return this.last ? { db: this.last, binHz: this.binHz } : null; };
  Strip.prototype.size = function () {
    const dpr = window.devicePixelRatio || 1, cw = Math.max(1, Math.round((this.c.clientWidth || 300) * dpr));
    const ch = Math.max(1, Math.round((this.c.clientHeight || 48) * dpr));
    if (cw !== this.w || ch !== this.h) {
      this.w = this.c.width = cw; this.h = this.c.height = ch;
      this.rows = this.g && this.g.createImageData ? this.g.createImageData(cw, ch) : null;
    }
  };
  Strip.prototype.row = function (db) {                    // one new line at the top, the rest scrolls down
    this.last = db;
    if (!this.visible() || !this.g) return;
    this.size();
    if (!this.rows) return;
    const W = this.w, H = this.h, px = this.rows.data, lut = LUT[theme()];
    px.copyWithin(W * 4, 0, W * (H - 1) * 4);
    const sorted = Array.from(db).sort((a, b) => a - b), floor = sorted[Math.floor(sorted.length * 0.2)], span = 40;
    for (let x = 0; x < W; x++) {
      const k = Math.min(db.length - 1, Math.floor(((x + 0.5) / W) * (F_TOP / this.binHz)));
      const [r, g, b] = colour(lut, (db[k] - floor) / span);
      px[x * 4] = r; px[x * 4 + 1] = g; px[x * 4 + 2] = b; px[x * 4 + 3] = 255;
    }
    this.paint();
  };
  Strip.prototype.paint = function (force) {
    if (!this.g || (!force && !this.visible()) || this.c.offsetParent === null) return;
    if (!this.rows) {                                      // no audio yet: the empty strip, its scale and markers
      this.size();
      if (!this.rows) return;
      const [r, g, b] = colour(LUT[theme()], 0), px = this.rows.data;
      for (let i = 0; i < px.length; i += 4) { px[i] = r; px[i + 1] = g; px[i + 2] = b; px[i + 3] = 255; }
    }
    const W = this.w, H = this.h, dpr = window.devicePixelRatio || 1;
    this.g.putImageData(this.rows, 0, 0);
    this.g.font = Math.round(9 * dpr) + "px " + "ui-monospace, monospace";
    this.g.textBaseline = "top";
    for (let f = 500; f < F_TOP; f += 500) {                // the scale: a tick every 500 Hz, kHz written
      const x = Math.round((f / F_TOP) * W) + 0.5;
      this.g.fillStyle = "rgba(255,255,255,.35)";
      this.g.fillRect(x, H - Math.round(4 * dpr), 1, Math.round(4 * dpr));
      if (f % 1000 === 0) { this.g.fillStyle = "rgba(255,255,255,.55)"; this.g.fillText(f / 1000 + "k", x + 2, H - Math.round(11 * dpr)); }
    }
    const marks = (this.o.markers && this.o.markers()) || [];
    for (const m of marks) {                               // the mode's markers
      const x = Math.round((m.hz / F_TOP) * W);
      this.g.fillStyle = m.dim ? "rgba(255,154,46,.55)" : "#ff9a2e";
      this.g.fillRect(x - Math.round(dpr / 2), 0, Math.max(1, Math.round(dpr * (m.dim ? 1 : 2))), m.dim ? Math.round(6 * dpr) : H);
      if (m.label) { this.g.fillStyle = "#ffb15a"; this.g.fillText(m.label, x + Math.round(3 * dpr), Math.round(2 * dpr)); }
    }
  };

  const strips = [];
  const R = () => window.RttyPanel || null, F = () => window.Ft8Panel || null;
  strips.push(new Strip($("rttyStrip"), "rtty", {
    markers: () => (R() && R().markers ? R().markers() : []),
    onTune: (hz, sp) => { if (R() && R().tune) R().tune(hz, sp); },
    onNudge: (d) => { if (R() && R().nudge) R().nudge(d); },
  }));
  strips.push(new Strip($("ft8Strip"), "ft8", { markers: () => (F() && F().markers ? F().markers() : []) }));

  // the radio's audio -> spectra, only while a strip is on view
  let buf = null, fill = 0, acc = null, accN = 0, rate = 0;
  function feed(pcm, r) {
    if (!fft || !strips.some((s) => s.visible())) { fill = 0; accN = 0; return; }
    if (r !== rate || !buf) { rate = r; buf = new Float64Array(FFT_N); fill = 0; acc = null; accN = 0; }
    const binHz = r / FFT_N, nb = Math.min(FFT_N / 2, Math.ceil(F_TOP / binHz) + 1);
    for (let i = 0; i < pcm.length; i++) {
      buf[fill++] = pcm[i] / 32768;
      if (fill < FFT_N) continue;
      const td = new Float64Array(FFT_N);
      for (let k = 0; k < FFT_N; k++) td[k] = buf[k] * win[k];
      const [re, im] = fft(td);
      if (!acc || acc.length !== nb) { acc = new Float64Array(nb); accN = 0; }
      for (let k = 0; k < nb; k++) acc[k] += re[k] * re[k] + im[k] * im[k];
      buf.copyWithin(0, HOP); fill = FFT_N - HOP;
      if (++accN >= ROWS_EVERY) {
        const db = new Float64Array(nb);
        for (let k = 0; k < nb; k++) db[k] = 10 * Math.log10(acc[k] / accN + 1e-12);
        acc.fill(0); accN = 0;
        for (const s of strips) { s.binHz = binHz; if (s.mode === cur) s.row(db); }
      }
    }
  }
  (function tap() {                                        // app.js loads first; wait for it if needed
    const A = window.RadioAudio;
    if (A && typeof A.tapPcm === "function") A.tapPcm(feed); else setTimeout(tap, 500);
  })();

  render();
  setInterval(() => { render(); for (const s of strips) s.paint(); }, 1000);   // the send lock; markers moved
  window.DigitalPanel = {
    mode: () => cur,
    show,
    // audio handed over directly (int16 mono, its rate) as if the radio sent it, to RTTY, FT8, SSTV and the
    // strip (CW listens on the audio graph, not this tap): a check of the window, as SstvPanel.feed. It only
    // decodes.
    feed: (pcm, r) => {
      if (!pcm || !pcm.length || !(r > 0)) return;
      feed(pcm, r);
      for (const p of [window.SstvPanel, window.RttyPanel, window.Ft8Panel]) { try { if (p && p.feed) p.feed(pcm, r); } catch (_) {} }
    },
    summary: () => {
      const s = (o) => { try { return (o && typeof o.summary === "function" && o.summary()) || ""; } catch (_) { return ""; } };
      if (cur === "cw") return s(window.CwPanel);
      if (cur === "rtty") return s(window.RttyPanel);
      if (cur === "ft8") return s(window.Ft8Panel);
      return s(window.SstvTx) || s(window.SstvPanel);
    },
  };
})();
