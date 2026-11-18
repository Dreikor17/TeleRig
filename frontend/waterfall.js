/* Scope: spectrum + scrolling waterfall + channel/filter overlay.
   Mirrors the IC-9700 bandscope look. */
(function (global) {
  "use strict";

  const AMP_MAX = 160;            // documented 27 00 data range
  const FLOOR_DB = 22;            // approximate noise floor amplitude
  const STRIP_H = 16;             // band-plan strip height (px) along the spectrum bottom

  function buildLUT() {
    // Icom-style waterfall colormap: dark navy -> blue -> cyan -> green -> yellow -> red -> white
    const stops = [
      [0.00, [2, 6, 18]],
      [0.18, [12, 28, 110]],
      [0.36, [10, 110, 200]],
      [0.52, [10, 200, 200]],
      [0.66, [40, 210, 90]],
      [0.80, [240, 220, 40]],
      [0.92, [240, 90, 30]],
      [1.00, [255, 245, 230]],
    ];
    const lut = new Uint8Array(256 * 3);
    for (let i = 0; i < 256; i++) {
      const t = i / 255;
      let a = stops[0], b = stops[stops.length - 1];
      for (let s = 0; s < stops.length - 1; s++) {
        if (t >= stops[s][0] && t <= stops[s + 1][0]) { a = stops[s]; b = stops[s + 1]; break; }
      }
      const f = (t - a[0]) / Math.max(1e-6, b[0] - a[0]);
      lut[i * 3] = a[1][0] + (b[1][0] - a[1][0]) * f;
      lut[i * 3 + 1] = a[1][1] + (b[1][1] - a[1][1]) * f;
      lut[i * 3 + 2] = a[1][2] + (b[1][2] - a[1][2]) * f;
    }
    return lut;
  }

  class Scope {
    constructor(spectrum, waterfall, overlay, wrap) {
      this.spec = spectrum;
      this.wf = waterfall;
      this.ov = overlay;
      this.wrap = wrap;
      this.sctx = spectrum.getContext("2d");
      this.wctx = waterfall.getContext("2d", { willReadFrequently: false });
      this.octx = overlay.getContext("2d");
      this.lut = buildLUT();
      this.opMode = "USB";
      this.meta = { mode: 0, center: 0, span: 50000, lower: 0, upper: 0, tuned: 0, filterBw: 2400 };
      this.lastData = null;
      this.maxHold = null;
      this.bandplan = null;        // [{lo,hi (Hz), label, desc, kind}]
      this.showBandplan = false;
      this.resize();
      window.addEventListener("resize", () => this.resize());
    }

    resize() {
      const cw = Math.floor(this.wrap.clientWidth), ch = Math.floor(this.wrap.clientHeight);
      // Parked or collapsed (0x0): keep everything as it is. Only the FIRST call (the constructor,
      // possibly on a hidden scope) takes the fallbacks below, so W/rowBuf/specH/wfH always exist.
      if (this._sized && (cw === 0 || ch === 0)) return;
      const w = Math.max(120, cw);
      const dpr = window.devicePixelRatio || 1;
      // read the laid-out heights so the waterfall fills its pane (falls back to fixed)
      const specH = Math.max(40, Math.floor(this.spec.clientHeight) || 150);
      const wfH = Math.max(40, Math.floor(this.wf.clientHeight) || 240);
      // overlay spans the FULL scope incl. the split gap so canvas-Y == display-Y;
      // otherwise the band-plan strip (drawn at the spectrum bottom) bleeds onto the
      // split handle that sits between the spectrum and waterfall.
      const scopeH = Math.max(specH + wfH, ch || 0);
      // Mobile browsers fire window 'resize' when the address bar shows/hides while
      // scrolling. Rebuilding here wipes the waterfall, so bail when nothing changed.
      if (this._sized && w === this.W && specH === this.specH && wfH === this.wfH &&
          scopeH === this.scopeH && dpr === this._dpr) return;
      // Keep the waterfall history across a resize: copy it aside, then stretch it horizontally
      // to the new width (rows stay 1:1, so a shorter waterfall keeps the newest rows).
      let old = null;
      if (this._sized && this.wf.width > 0 && this.wf.height > 0) {
        old = document.createElement("canvas");
        old.width = this.wf.width; old.height = this.wf.height;
        old.getContext("2d").drawImage(this.wf, 0, 0);
      }
      const oldMax = this.maxHold;
      this._sized = true; this._dpr = dpr;
      this.W = w; this.specH = specH; this.wfH = wfH; this.scopeH = scopeH;
      for (const [c, h] of [[this.spec, this.specH], [this.ov, this.scopeH]]) {
        c.width = w * dpr; c.height = h * dpr;
        c.getContext("2d").setTransform(dpr, 0, 0, dpr, 0, 0);
      }
      // waterfall stays 1:1 so putImageData rows line up crisply
      this.wf.width = w; this.wf.height = this.wfH;
      this.wctx.fillStyle = "#020c14"; this.wctx.fillRect(0, 0, w, this.wfH);
      if (old) this.wctx.drawImage(old, 0, 0, old.width, old.height, 0, 0, w, old.height);
      this.rowBuf = this.wctx.createImageData(w, 1);
      this.maxHold = null;
      if (oldMax && oldMax.length > 1) {             // resample the max-hold trace to the new width
        const mh = new Float32Array(w), k = (oldMax.length - 1) / Math.max(1, w - 1);
        for (let x = 0; x < w; x++) {
          const f = x * k, i = Math.floor(f), t = f - i;
          mh[x] = oldMax[i] * (1 - t) + oldMax[Math.min(oldMax.length - 1, i + 1)] * t;
        }
        this.maxHold = mh;
      }
      if (this.lastData) this.drawSpectrum(this.lastData);
      this.drawOverlay();
    }

    // No drawing work (and no TypeError) while the scope is parked or collapsed.
    _hidden() { return !this.rowBuf || this.wrap.clientWidth === 0; }

    setOpMode(m) { if (m) { this.opMode = m; this.drawOverlay(); } }

    clear() {
      this.lastData = null;
      this.maxHold = null;
      this.meta = { mode: 0, center: 0, span: this.meta.span || 50000, lower: 0, upper: 0, tuned: 0, filterBw: 0 };
      this.wctx.fillStyle = "#020c14"; this.wctx.fillRect(0, 0, this.W, this.wfH);
      this.sctx.clearRect(0, 0, this.W, this.specH);
      this.octx.clearRect(0, 0, this.W, this.scopeH || (this.specH + this.wfH));
    }

    // No-scope radios (e.g. Yaesu FT-991A CAT): no sweeps, but keep the band-plan
    // strip + tuned marker + freq labels aligned to the current frequency.
    showStatic(center, span) {
      this.lastData = null; this.maxHold = null;
      this.meta = { mode: 0, center: center || 0, span: span || 200000, lower: 0, upper: 0, tuned: center || 0, filterBw: 0 };
      this.sctx.fillStyle = "#020c14"; this.sctx.fillRect(0, 0, this.W, this.specH);
      this.wctx.fillStyle = "#020c14"; this.wctx.fillRect(0, 0, this.W, this.wfH);
      this.drawOverlay();
    }

    freqToX(f) {
      const m = this.meta;
      let lo, hi;
      if (m.mode === 1 && m.lower && m.upper) { lo = m.lower; hi = m.upper; }
      else { const span = m.span || 50000; const vc = m.tuned || m.center || f; lo = vc - span / 2; hi = vc + span / 2; }
      if (hi <= lo) return this.W / 2;
      return ((f - lo) / (hi - lo)) * this.W;
    }

    visibleRange() {
      const m = this.meta;
      if (m.mode === 1 && m.lower && m.upper) return [m.lower, m.upper];
      // center mode: the VIEW rides the tuned freq (always dead-center), not the
      // radio's reported sweep center (which lags a tune and made the view snap).
      const span = m.span || 50000, c = m.tuned || m.center || 0;
      return [c - span / 2, c + span / 2];
    }

    // frequency (Hz) at an output pixel x — the exact inverse of freqToX over the
    // visible range, so it's correct for both the CI-V scope and the AF scope.
    freqAtX(px) {
      const [lo, hi] = this.visibleRange();
      const x = Math.max(0, Math.min(this.W, px));
      return lo + (x / this.W) * (hi - lo);
    }

    // sweep value at output pixel x. The VIEW is centered on the tuned freq while the
    // SWEEP is centered on its own center_hz, so during a tune the data is offset to
    // keep the tuned freq dead-center (returns -1 where x falls outside the sweep).
    _dataAtX(data, x) {
      const m = this.meta, n = data.length || 1;
      let viewLo, viewHi, sweepLo, sweepHi;
      if (m.mode === 1 && m.lower && m.upper) {
        viewLo = sweepLo = m.lower; viewHi = sweepHi = m.upper;
      } else {
        const span = m.span || 50000;
        const vc = m.tuned || m.center || 0, sc = m.center || (m.tuned || 0);
        viewLo = vc - span / 2; viewHi = vc + span / 2;
        sweepLo = sc - span / 2; sweepHi = sc + span / 2;
      }
      const freq = viewLo + (x / this.W) * (viewHi - viewLo);
      const idx = ((freq - sweepLo) / (sweepHi - sweepLo)) * n;
      return (idx < 0 || idx >= n) ? -1 : data[idx | 0];
    }

    // distinct band-plan kinds currently visible (for the color-key legend), in display order
    visibleKinds() {
      if (!this.bandplan) return [];
      const [lo, hi] = this.visibleRange();
      const seen = [];
      for (const seg of this.bandplan) {
        if (seg.hi <= lo || seg.lo >= hi) continue;
        if (seen.indexOf(seg.kind) < 0) seen.push(seg.kind);
      }
      return seen;
    }

    // band-plan segment under a canvas-space point (for hover tooltips), else null
    bandplanSegAt(px, py) {
      if (!this.showBandplan || !this.bandplan) return null;
      const sy = this.specH - STRIP_H - 3;
      if (py < sy - 2 || py > sy + STRIP_H + 2) return null;
      const [lo, hi] = this.visibleRange();
      for (const seg of this.bandplan) {
        if (seg.hi <= lo || seg.lo >= hi) continue;
        if (px >= this.freqToX(seg.lo) && px <= this.freqToX(seg.hi)) return seg;
      }
      return null;
    }

    pushSweep(meta, data) {
      this.meta = meta;
      this.lastData = data;
      if (this._hidden()) return;
      this.scrollWaterfall(data);
      this.drawSpectrum(data);
      this.drawOverlay();
    }

    scrollWaterfall(data) {
      if (this._hidden()) return;
      const w = this.W, ctx = this.wctx;
      ctx.drawImage(this.wf, 0, 1);              // shift history down 1px
      const row = this.rowBuf.data;
      for (let x = 0; x < w; x++) {
        const v = this._dataAtX(data, x), o = x * 4;
        if (v < 0) { row[o] = 2; row[o + 1] = 12; row[o + 2] = 20; row[o + 3] = 255; continue; }  // off-sweep -> background
        const idx = Math.min(255, Math.round((v / AMP_MAX) * 255)) * 3;
        row[o] = this.lut[idx]; row[o + 1] = this.lut[idx + 1];
        row[o + 2] = this.lut[idx + 2]; row[o + 3] = 255;
      }
      ctx.putImageData(this.rowBuf, 0, 0);
    }

    drawSpectrum(data) {
      if (this._hidden()) return;
      const ctx = this.sctx, w = this.W, h = this.specH, n = data.length || 1;
      ctx.clearRect(0, 0, w, h);
      ctx.fillStyle = "#020c14"; ctx.fillRect(0, 0, w, h);
      // grid
      ctx.strokeStyle = "rgba(20,90,130,.45)"; ctx.lineWidth = 1;
      ctx.beginPath();
      for (let i = 1; i < 10; i++) { const x = (i / 10) * w; ctx.moveTo(x, 0); ctx.lineTo(x, h); }
      for (let i = 1; i < 4; i++) { const y = (i / 4) * h; ctx.moveTo(0, y); ctx.lineTo(w, y); }
      ctx.stroke();

      if (!this.maxHold || this.maxHold.length !== w) this.maxHold = new Float32Array(w);
      const yOf = v => h - (Math.min(AMP_MAX, v) / AMP_MAX) * (h - 4) - 2;

      // filled spectrum
      ctx.beginPath(); ctx.moveTo(0, h);
      for (let x = 0; x < w; x++) {
        const dv = this._dataAtX(data, x), v = dv < 0 ? 0 : dv;
        ctx.lineTo(x, yOf(v));
        this.maxHold[x] = Math.max(v, this.maxHold[x] * 0.985);
      }
      ctx.lineTo(w, h); ctx.closePath();
      const grad = ctx.createLinearGradient(0, 0, 0, h);
      grad.addColorStop(0, "rgba(60,210,170,.55)");
      grad.addColorStop(1, "rgba(20,90,120,.10)");
      ctx.fillStyle = grad; ctx.fill();
      // line
      ctx.beginPath();
      for (let x = 0; x < w; x++) { const dv = this._dataAtX(data, x), v = dv < 0 ? 0 : dv; x ? ctx.lineTo(x, yOf(v)) : ctx.moveTo(x, yOf(v)); }
      ctx.strokeStyle = "#5ff0c8"; ctx.lineWidth = 1.2; ctx.stroke();
      // max hold
      ctx.beginPath();
      for (let x = 0; x < w; x++) { const y = yOf(this.maxHold[x]); x ? ctx.lineTo(x, y) : ctx.moveTo(x, y); }
      ctx.strokeStyle = "rgba(255,200,120,.55)"; ctx.lineWidth = 1; ctx.stroke();
    }

    drawOverlay() {
      const ctx = this.octx, w = this.W, h = this.scopeH || (this.specH + this.wfH), m = this.meta;
      ctx.clearRect(0, 0, w, h);
      // filter passband band (offset by mode)
      const bw = m.filterBw || 0, tuned = m.tuned || 0;   // explicit 0 (AF scope) -> no marker
      // freqToX centers the view on the tuned freq, so in center mode freqToX(tuned)
      // is always mid-screen and the marker/filter ride the center with zero jitter.
      if (bw > 0 && tuned) {
        let loF, hiF;
        if (this.opMode === "USB" || this.opMode === "DV" || this.opMode === "RTTY-R") { loF = tuned; hiF = tuned + bw; }
        else if (this.opMode === "LSB" || this.opMode === "RTTY") { loF = tuned - bw; hiF = tuned; }
        else { loF = tuned - bw / 2; hiF = tuned + bw / 2; }
        const x0 = this.freqToX(loF), x1 = this.freqToX(hiF);
        ctx.fillStyle = "rgba(90,200,255,.16)";
        ctx.fillRect(Math.min(x0, x1), 0, Math.abs(x1 - x0), h);
        ctx.strokeStyle = "rgba(120,220,255,.5)"; ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(x0, 0); ctx.lineTo(x0, h); ctx.moveTo(x1, 0); ctx.lineTo(x1, h); ctx.stroke();
      }
      // tuned channel marker
      if (tuned) {
        const xc = this.freqToX(tuned);
        ctx.strokeStyle = "#ff9a2e"; ctx.lineWidth = 1.4;
        ctx.beginPath(); ctx.moveTo(xc, 0); ctx.lineTo(xc, h); ctx.stroke();
        // little triangle at top
        ctx.fillStyle = "#ff9a2e";
        ctx.beginPath(); ctx.moveTo(xc - 5, 0); ctx.lineTo(xc + 5, 0); ctx.lineTo(xc, 8); ctx.closePath(); ctx.fill();
      }
      if (m.out) {
        ctx.fillStyle = "rgba(255,80,80,.9)"; ctx.font = "11px monospace";
        ctx.fillText("OUT OF RANGE", w / 2 - 40, this.specH / 2);
      }
      // frequency scale: the CENTRE (tuned) freq by the marker, so the frequency shown on the
      // scope is explicit and verifiable. The left/right EDGE frequencies are the HTML labels
      // #lblLeft/#lblRight over the scope (app.js updateScopeLabels, same visible range); they are
      // drawn only there, never also on this canvas (that drew every edge twice, one over the other).
      const [flo, fhi] = this.visibleRange();
      if (fhi > flo) {
        ctx.font = "10px Consolas, monospace";
        ctx.textBaseline = "top";
        if (tuned) {                                            // centre indicator = the tuned freq
          const cx = Math.max(40, Math.min(w - 40, this.freqToX(tuned)));
          const dec = fhi - flo <= 10000 ? 5 : 4;                // 10 Hz digits on a narrow view (AF / CW)
          ctx.textAlign = "center"; ctx.fillStyle = "#ffb15a";
          ctx.fillText((tuned / 1e6).toFixed(dec) + " MHz", cx, 9);
        }
        ctx.textAlign = "left"; ctx.textBaseline = "alphabetic";
      }
      this.drawBandplan();
    }

    // The label text that fits `maxW` px: the whole label, else the label cut short with "…" (at
    // least 3 characters kept), else "" (no label). Clipping an over-long label to its segment cut
    // words in half and ran neighbouring labels together on a narrow scope.
    fitLabel(ctx, label, maxW) {
      label = String(label || "");
      if (!label || maxW <= 0) return "";
      if (ctx.measureText(label).width <= maxW) return label;
      for (let n = label.length - 1; n >= 3; n--) {
        const t = label.slice(0, n).trimEnd() + "…";
        if (ctx.measureText(t).width <= maxW) return t;
      }
      return "";
    }

    drawBandplan() {
      this.bpLabels = [];                       // what was drawn (segment x0/x1, text, width)
      if (!this.showBandplan || !this.bandplan || !this.bandplan.length) return;
      const ctx = this.octx, w = this.W;
      const [lo, hi] = this.visibleRange();
      const sy = this.specH - STRIP_H - 3;     // 3px clear of the spectrum/waterfall split below
      const colors = global.BANDPLAN_COLORS || {};
      ctx.font = "10px Consolas, monospace";
      ctx.textBaseline = "middle";
      for (const seg of this.bandplan) {
        if (seg.hi <= lo || seg.lo >= hi) continue;
        const x0 = Math.max(0, this.freqToX(seg.lo));
        const x1 = Math.min(w, this.freqToX(seg.hi));
        const bw = x1 - x0;
        if (bw <= 0.5) continue;
        const base = colors[seg.kind] || "rgba(150,160,180,";
        ctx.fillStyle = base + "0.30)";
        ctx.fillRect(x0, sy, bw, STRIP_H);
        ctx.strokeStyle = base + "0.85)"; ctx.lineWidth = 1;
        ctx.strokeRect(x0 + 0.5, sy + 0.5, Math.max(0, bw - 1), STRIP_H - 1);
        const text = this.fitLabel(ctx, seg.label, bw - 6);   // 3 px clear of each edge
        if (text) {
          const cx = (x0 + x1) / 2;
          ctx.fillStyle = "rgba(255,255,255,.95)";
          ctx.textAlign = "center";
          ctx.fillText(text, cx, sy + STRIP_H / 2 + 0.5);
          this.bpLabels.push({ label: seg.label, text, x0, x1, cx, w: ctx.measureText(text).width });
        }
      }
      ctx.textBaseline = "alphabetic"; ctx.textAlign = "left";
    }
  }

  global.Scope = Scope;

  // ---- the audio (AF) scope's frequency map, for radios with no scope over CAT (app.js) ----
  // The AF scope is an FFT of the RX audio: a tone of a Hz (0..span) comes from RF carrier + a on a
  // USB-side mode and carrier - a on an LSB-side mode (drawn mirrored, so RF still rises left to
  // right). The carrier point is the dial, EXCEPT in CW: there the radio shows the frequency it hears
  // AT ITS CW PITCH (the zero beat: Yaesu's factory CW FREQ DISPLAY = PITCH; the IC-728 always), so
  // the carrier sits `pitch` Hz away from the dial, and the dial is the tuned marker, drawn at the
  // pitch. A signal on the marker is heard at the pitch, inside the radio's CW filter and the CW
  // decoder's window, and a reply there is zero-beat. Pure functions (tests/af_scope_test.js).
  const AF_LSB_MODES = ["LSB", "CW-LSB", "RTTY-LSB", "DATA-LSB", "CW-R", "RTTY", "DATA-L"];
  global.AfMap = {
    // {side, pitch, shown} for a CW mode on this radio, else null. radio = the profile JSON
    // (cw_sides, cw_pitch_hz); s = the live state (freq, key_pitch, cw_bfo, cw_disp).
    cw(radio, s, mode) {
      let side = ((radio && radio.cw_sides) || {})[mode];
      if (!side) return null;
      s = s || {};
      if (s.cw_bfo === 0) side = 1;                            // FT-891 menu 07-07 CW BFO: USB
      else if (s.cw_bfo === 1) side = -1;                      //   LSB
      else if (s.cw_bfo === 2) side = (s.freq || 0) < 10e6 ? -1 : 1;   // AUTO: LSB on 7 MHz and below
      const code = s.key_pitch == null ? 40 : s.key_pitch;     // Yaesu KP 00-75 = 300-1050 Hz (40 = 700)
      const pitch = (radio && radio.cw_pitch_hz) || 300 + 10 * code;
      return { side: side < 0 ? -1 : 1, pitch, shown: s.cw_disp !== 0 };   // unread -> factory PITCH
    },
    // The sweep meta for Scope.pushSweep (fixed edges, mode 1), plus lsb (mirror the FFT bins) and
    // retune (dial - marker): a tap at RF f tunes to f + retune, so the tapped signal lands ON the
    // marker — at the pitch in CW, as the radio itself would put it.
    view(dial, span, mode, cw) {
      const lsb = cw ? cw.side < 0 : AF_LSB_MODES.includes(mode);
      let carrier = dial, marker = 0;
      if (cw) {
        if (cw.shown) carrier = dial - cw.side * cw.pitch;
        marker = carrier + cw.side * cw.pitch;                  // = dial when the dial shows the zero beat
      }
      const lower = lsb ? carrier - span : carrier;
      return { mode: 1, center: lower + span / 2, span, lower, upper: lower + span,
               tuned: marker, filterBw: 0, retune: marker ? dial - marker : 0, lsb };
    },
  };

  global.fmtHz = function (hz) {
    if (!hz) return "—";
    const mhz = hz / 1e6;
    return mhz.toFixed(mhz >= 1000 ? 4 : 3);
  };
})(window);
