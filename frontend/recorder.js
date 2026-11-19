/* TeleRig Recorder (the Recorder panel): records what you hear, the radio's RX audio, before the volume
 * control, so the VOL slider never changes the file and every transport records the same way (LAN PCM
 * and a serial radio's host audio alike).
 *
 *   MP3: the radio's own PCM as it arrives (window.RadioAudio.tapPcm: 16 kHz mono int16) through LAME
 *   (lamejs, frontend/vendor/lamejs, LGPL) at 48 kb/s, about a third of a megabyte a minute. A dropout
 *   of over GAP_S seconds is filled with silence, so the file keeps time with the clock.
 *   Only if the encoder is missing: MediaRecorder on the pre-volume RX bus (RadioAudio.bus()) -> WebM/
 *   Opus (Ogg/Opus where WebM is missing, MP4 on Safari) at 64 kb/s.
 *   WAV, with "Also save a lossless WAV" ticked (kept per browser): the same PCM written as it came,
 *   sample for sample, 16-bit mono at the radio's own rate (about 2 MB a minute at 16 kHz), with the
 *   same dropout silence as the MP3, so the two files line up. The copy to decode later from.
 *   A level bar (an AnalyserNode on the RX bus) runs whenever RX audio is on, so the level can be
 *   checked before recording.
 *
 * Recordings stay in this tab as Blob URLs until downloaded or deleted; leaving the page with one
 * running, or with a file of one not downloaded, asks first. Nothing is sent to the server and nothing
 * here can transmit: the only audio this panel touches is its own taps.
 *
 * window.Recorder = { summary() }   the collapsed panel's one-line summary (layout.js)
 */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const btn = $("recBtn"), lbl = $("recBtnLbl"), timeEl = $("recTime"), fill = $("recLevel"),
        hint = $("recHint"), list = $("recList"), wavChk = $("recWav");
  if (!btn || !list) return;

  const TYPES = [["audio/webm;codecs=opus", "webm"], ["audio/ogg;codecs=opus", "ogg"],
                 ["audio/webm", "webm"], ["audio/mp4", "m4a"]];
  const BITRATE = 64000;                 // Opus at 64 kb/s: plenty for SSB, CW and FM voice
  const SLICE_MS = 1000;                 // a chunk a second: the size readout moves, memory stays chunked
  const MP3_KBPS = 48;                   // MP3 at 48 kb/s: valid at 8 to 48 kHz; clean voice and CW at 16 kHz
  const GAP_S = 1.5;                     // a dropout longer than this becomes silence in the MP3 (and the WAV)
  const TICK_MS = 100;
  const SVG_NS = "http://www.w3.org/2000/svg";
  const WAV_KEY = "telerig.rec.wav";     // "1": also save a lossless WAV (this browser's choice)

  let rec = null;                        // the running recorder (Mp3Recorder, or a MediaRecorder)
  let tap = null;                        // {bus, dest} while recording
  let wav = null, wavBlob = null;        // the WAV copy while recording; its file between Stop and the list
  let wavLost = false;                   // the WAV copy failed (the main file is still kept)
  let meter = null;                      // {bus, an, buf} while RX audio is on
  let chunks = [], bytes = 0, startedAt = 0, info = null;
  let failed = "", note = "", wasOn = false;   // the last error / stop reason: shown until RX is turned on again
  const items = [];                      // {url, file, title, meta, saved, li, wav: {url, file, saved} | null}
  const done = (it) => it.saved && (!it.wav || it.wav.saved);   // every file of it downloaded

  const say = (m) => { try { TR.srLive(m); } catch (_) {} };
  const audio = () => window.RadioAudio || null;
  const rxOn = () => { const A = audio(); try { return !!(A && A.on()); } catch (_) { return false; } };
  // the WAV copy needs app.js's PCM tap (there on every path, the MediaRecorder fallback included)
  const pcmOk = () => { const A = audio(); return !!(A && typeof A.tapPcm === "function"); };
  if (wavChk) {
    try { wavChk.checked = localStorage.getItem(WAV_KEY) === "1"; } catch (_) {}
    wavChk.addEventListener("change", () => {
      try { localStorage.setItem(WAV_KEY, wavChk.checked ? "1" : "0"); } catch (_) {}
      render();
    });
  }

  function pickType() {
    const MR = window.MediaRecorder;
    if (typeof MR !== "function") return null;
    if (typeof MR.isTypeSupported === "function") {
      for (const t of TYPES) if (MR.isTypeSupported(t[0])) return t;
    }
    return ["", ""];                     // the browser's own choice; the extension follows rec.mimeType
  }
  // MP3 needs the encoder (lame.min.js) and app.js's PCM tap
  function mp3Ok() {
    const A = audio();
    return !!(window.lamejs && typeof window.lamejs.Mp3Encoder === "function" && A && typeof A.tapPcm === "function");
  }
  function extFor(mime) {
    const m = String(mime || "").toLowerCase();
    if (m.indexOf("mpeg") >= 0 || m.indexOf("mp3") >= 0) return "mp3";
    if (m.indexOf("ogg") >= 0) return "ogg";
    if (m.indexOf("mp4") >= 0 || m.indexOf("aac") >= 0) return "m4a";
    if (m.indexOf("wav") >= 0) return "wav";
    return "webm";
  }

  // ---- formatting ----
  const pad = (n, w) => String(n).padStart(w || 2, "0");
  function fmtDur(ms) {
    const s = Math.max(0, Math.floor(ms / 1000)), h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60;
    return (h ? h + ":" + pad(m) : String(m)) + ":" + pad(s % 60);
  }
  function fmtSize(b) {
    return b < 1024 * 1024 ? Math.max(1, Math.round(b / 1024)) + " KB" : (b / (1024 * 1024)).toFixed(1) + " MB";
  }
  function fmtFreq(hz) {
    hz = Math.round(+hz || 0);
    return Math.floor(hz / 1e6) + "." + pad(Math.floor(hz / 1e3) % 1000, 3) + "." + pad(hz % 1000, 3);
  }
  function utcStamp(d) {
    return d.getUTCFullYear() + pad(d.getUTCMonth() + 1) + pad(d.getUTCDate()) + "-" +
           pad(d.getUTCHours()) + pad(d.getUTCMinutes()) + pad(d.getUTCSeconds()) + "Z";
  }
  const utcHm = (d) => pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes()) + "Z";
  const safe = (s) => String(s || "").replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "");

  // what was on the air when the recording started: names the file and the list entry
  function snapshot() {
    let st = {};
    try { st = (window.RadioControl && RadioControl.state()) || {}; } catch (_) { st = {}; }
    const d = new Date(Date.now());
    const on = !!st.connected && +st.freq > 0;
    const parts = ["TeleRig"];
    if (on) parts.push(safe(st.radio_name), (st.freq / 1e6).toFixed(3) + "MHz", safe(st.mode_name));
    parts.push(utcStamp(d));
    return { file: parts.filter(Boolean).join("_"),
             title: on ? fmtFreq(st.freq) + (st.mode_name ? " " + st.mode_name : "") : "Recording",
             when: utcHm(d) };
  }

  // ---- the level bar: an analyser on the RX bus while RX audio is on ----
  function meterOn() {
    const A = audio();
    const bus = A && A.bus(), ctx = A && A.ctx();
    if (!bus || !ctx || typeof ctx.createAnalyser !== "function") return;
    if (meter && meter.bus === bus) return;
    meterOff();
    const an = ctx.createAnalyser();
    an.fftSize = 1024;
    bus.connect(an);
    meter = { bus, an, buf: new Float32Array(an.fftSize) };
  }
  function meterOff() {
    if (meter) { try { meter.bus.disconnect(meter.an); } catch (_) {} meter = null; }
    if (fill) fill.style.width = "0%";
  }
  function level() {
    if (!meter) return 0;
    const { an, buf } = meter;
    let peak = 0;
    if (typeof an.getFloatTimeDomainData === "function") {
      an.getFloatTimeDomainData(buf);
      for (let i = 0; i < buf.length; i++) { const v = Math.abs(buf[i]); if (v > peak) peak = v; }
    }
    return Math.min(100, Math.round(Math.sqrt(Math.min(1, peak)) * 100));   // perceptual, like the mic bar
  }

  // ---- the MP3 recorder: the radio's PCM through LAME. The few MediaRecorder members this file uses
  // (state, mimeType, start, stop, ondataavailable / onstop / onerror), so both paths share the rest.
  function resample(pcm, from, to) {                      // linear; only if the radio's rate ever changes
    const n = Math.max(1, Math.round(pcm.length * to / from)), out = new Int16Array(n), k = from / to;
    for (let i = 0; i < n; i++) {
      const x = i * k, i0 = Math.min(pcm.length - 1, Math.floor(x)), i1 = Math.min(pcm.length - 1, i0 + 1);
      out[i] = Math.round(pcm[i0] + (pcm[i1] - pcm[i0]) * (x - i0));
    }
    return out;
  }
  function Mp3Recorder(A) {
    const self = this;
    let enc = null, rate = 0, t0 = 0, written = 0, untap = null;
    this.state = "inactive";
    this.mimeType = "audio/mpeg";
    this.ondataavailable = null; this.onstop = null; this.onerror = null;
    const emit = (mp3) => {
      if (mp3 && mp3.length && self.ondataavailable) self.ondataavailable({ data: new Blob([mp3], { type: "audio/mpeg" }) });
    };
    function onPcm(pcm, r) {
      if (self.state !== "recording") return;
      try {
        if (!enc) {                                       // the first chunk sets the rate and the clock
          rate = r;
          enc = new window.lamejs.Mp3Encoder(1, rate, MP3_KBPS);
          t0 = Date.now() - pcm.length * 1000 / rate;
        }
        const data = r === rate ? pcm : resample(pcm, r, rate);
        const gap = Math.round((Date.now() - t0) * rate / 1000) - written - data.length;
        if (gap > rate * GAP_S) { emit(enc.encodeBuffer(new Int16Array(gap))); written += gap; }
        emit(enc.encodeBuffer(data));
        written += data.length;
      } catch (e) {
        if (self.onerror) self.onerror({ error: e });
      }
    }
    this.start = () => { untap = A.tapPcm(onPcm); self.state = "recording"; };
    this.durationMs = () => (rate ? written * 1000 / rate : 0);   // the audio in the file, not the clock
    this.stop = () => {
      if (self.state === "inactive") throw new Error("not recording");
      self.state = "inactive";
      if (untap) { untap(); untap = null; }
      try { if (enc) emit(enc.flush()); } catch (_) {}
      setTimeout(() => { if (self.onstop) self.onstop(); }, 0);
    };
  }

  // ---- the lossless copy: the same PCM as the MP3, as it came, 16-bit little-endian mono at the rate of
  // the first chunk (a later rate is resampled to it, as in the MP3), dropouts over GAP_S filled with the
  // same silence as the MP3. Held as a Blob a second; stop() returns the file, header first.
  function WavRecorder(A) {
    let rate = 0, t0 = 0, written = 0, untap = null, buf = null, n = 0;
    const parts = [];
    this.failed = false;
    const fresh = () => { buf = new DataView(new ArrayBuffer(rate * 2)); n = 0; };
    const full = () => { if (n * 2 === buf.byteLength) { parts.push(new Blob([new Uint8Array(buf.buffer)])); buf = null; } };
    function put(pcm) {
      for (let i = 0; i < pcm.length; i++) {
        if (!buf) fresh();
        buf.setInt16(n * 2, pcm[i], true);
        n++; full();
      }
    }
    function silence(k) {                                 // a new buffer is zeros already: just move on
      while (k > 0) {
        if (!buf) fresh();
        const step = Math.min(k, buf.byteLength / 2 - n);
        n += step; k -= step; full();
      }
    }
    const onPcm = (pcm, r) => {
      if (!untap || this.failed) return;
      try {
        if (!rate) { rate = r; t0 = Date.now() - pcm.length * 1000 / rate; }
        const data = r === rate ? pcm : resample(pcm, r, rate);
        const gap = Math.round((Date.now() - t0) * rate / 1000) - written - data.length;
        if (gap > rate * GAP_S) { silence(gap); written += gap; }
        put(data);
        written += data.length;
      } catch (_) { this.failed = true; }
    };
    this.start = () => { untap = A.tapPcm(onPcm); };
    this.stop = () => {
      if (untap) { untap(); untap = null; }
      if (this.failed || !written) return null;
      if (buf && n) parts.push(new Blob([new Uint8Array(buf.buffer, 0, n * 2)]));
      buf = null;
      return new Blob([wavHeader(rate, written)].concat(parts), { type: "audio/wav" });
    };
  }
  function wavHeader(rate, samples) {                     // RIFF / WAVE, one PCM "fmt " chunk, then "data"
    const h = new DataView(new ArrayBuffer(44)), size = samples * 2;
    const tag = (at, s) => { for (let i = 0; i < 4; i++) h.setUint8(at + i, s.charCodeAt(i)); };
    tag(0, "RIFF"); h.setUint32(4, 36 + size, true); tag(8, "WAVE");
    tag(12, "fmt "); h.setUint32(16, 16, true); h.setUint16(20, 1, true); h.setUint16(22, 1, true);   // PCM, mono
    h.setUint32(24, rate, true); h.setUint32(28, rate * 2, true); h.setUint16(32, 2, true); h.setUint16(34, 16, true);
    tag(36, "data"); h.setUint32(40, size, true);
    return new Uint8Array(h.buffer);
  }

  // ---- recording ----
  function start() {
    if (rec) return;
    const A = audio(), mp3 = mp3Ok(), t = mp3 ? ["audio/mpeg", "mp3"] : pickType();
    if (!t || !rxOn() || !A) { render(); return; }
    const withWav = !!(wavChk && wavChk.checked) && pcmOk();
    try { A.ensure(); } catch (_) {}
    let r, bus = null, dest = null;
    if (mp3) {
      r = new Mp3Recorder(A);
    } else {                                              // no encoder: the browser's own recorder
      const ctx = A.ctx();
      bus = A.bus();
      if (!ctx || !bus || typeof ctx.createMediaStreamDestination !== "function") {
        failed = "This browser can't record audio."; render(); return;
      }
      dest = ctx.createMediaStreamDestination();
      bus.connect(dest);
      try {
        const opts = { audioBitsPerSecond: BITRATE };
        if (t[0]) opts.mimeType = t[0];
        r = new MediaRecorder(dest.stream, opts);
      } catch (e) {
        try { bus.disconnect(dest); } catch (_) {}
        failed = "The recording could not start: " + ((e && e.message) || e) + "."; render(); return;
      }
    }
    chunks = []; bytes = 0; failed = ""; note = "";
    r.ondataavailable = (ev) => { if (ev.data && ev.data.size) { chunks.push(ev.data); bytes += ev.data.size; } };
    r.onstop = () => finish(r);
    r.onerror = (ev) => {
      const e = ev && (ev.error || ev);
      failed = "The recording stopped with an error: " + ((e && (e.message || e.name)) || "unknown") + ".";
      stop();
    };
    rec = r; tap = dest ? { bus, dest } : null; info = snapshot(); startedAt = Date.now();
    try { r.start(SLICE_MS); }
    catch (e) {
      rec = null; if (tap) { try { tap.bus.disconnect(tap.dest); } catch (_) {} } tap = null;
      failed = "The recording could not start: " + ((e && e.message) || e) + "."; render(); return;
    }
    wav = null; wavBlob = null; wavLost = false;
    if (withWav) {                                        // the lossless copy, from the same PCM
      wav = new WavRecorder(A);
      try { wav.start(); } catch (_) { wav = null; wavLost = true; }
    }
    say(wav ? "Recording started, with a WAV copy" : "Recording started");
    render();
  }
  function endWav() {                                     // the WAV stops with the main file, to the chunk
    if (!wav) return;
    const w = wav;
    wav = null;
    wavBlob = w.stop();
    if (!wavBlob && w.failed) wavLost = true;
  }
  function stop(why) {
    if (!rec) return;
    if (why) note = why;
    endWav();
    const r = rec;
    if (r.state === "inactive") { finish(r); return; }
    try { r.stop(); } catch (_) { finish(r); }       // the stop event delivers the last chunk, then finish()
  }
  function finish(r) {
    if (rec !== r) return;
    rec = null;
    endWav();
    const w = wavBlob;
    wavBlob = null;
    if (tap) { try { tap.bus.disconnect(tap.dest); } catch (_) {} tap = null; }
    const ms = r && typeof r.durationMs === "function" ? r.durationMs() : Date.now() - startedAt;
    const mime = (r && r.mimeType) || "audio/webm";
    if (!bytes) { note = note || "Nothing was recorded."; render(); return; }
    const blob = new Blob(chunks, { type: mime });
    chunks = [];
    const ext = extFor(mime);
    const it = { url: URL.createObjectURL(blob), file: info.file + "." + ext, ext, title: info.title,
                 meta: info.when + " · " + fmtDur(ms) + " · " + fmtSize(blob.size || bytes) + (w ? " · WAV " + fmtSize(w.size) : ""),
                 saved: false, li: null,
                 wav: w ? { url: URL.createObjectURL(w), file: info.file + ".wav", saved: false } : null };
    if (wavLost) note = "The WAV copy could not be written. The " + ext.toUpperCase() + " is kept.";
    items.unshift(it);
    list.insertBefore(row(it), list.firstChild);
    say("Recording stopped, " + fmtDur(ms));
    render();
  }
  function row(it) {
    const li = document.createElement("li");
    li.className = "rec-item";
    const name = document.createElement("span"); name.className = "rec-name"; name.textContent = it.title;
    const meta = document.createElement("span"); meta.className = "rec-meta"; meta.textContent = it.meta;
    const txt = document.createElement("span"); txt.className = "rec-txt"; txt.append(name, meta);
    // one file: "Download"; with the WAV copy: a link for each, named by its format
    const link = (f, label) => {
      const a = document.createElement("a");
      a.className = "key rec-save"; a.href = f.url; a.download = f.file; a.textContent = label; a.title = f.file;
      if (label !== "Download") a.setAttribute("aria-label", "Download the " + label + " of " + it.title);
      a.addEventListener("click", () => { f.saved = true; a.classList.add("saved"); li.classList.toggle("saved", done(it)); render(); });
      return a;
    };
    const links = it.wav ? [link(it, (it.ext || "audio").toUpperCase()), link(it.wav, "WAV")] : [link(it, "Download")];
    if (it.wav) li.classList.add("has-wav");
    const del = document.createElement("button");
    del.type = "button"; del.className = "tile-btn rec-del";
    del.setAttribute("aria-label", "Delete the recording " + it.title + ", " + it.meta);
    del.title = "Delete";
    const svg = document.createElementNS(SVG_NS, "svg"), use = document.createElementNS(SVG_NS, "use");
    svg.setAttribute("aria-hidden", "true");
    use.setAttribute("href", "#i-x");
    svg.appendChild(use);
    del.appendChild(svg);
    del.addEventListener("click", () => remove(it));
    li.append(txt, ...links, del);
    it.li = li;
    return li;
  }
  function remove(it) {
    const i = items.indexOf(it);
    if (i < 0) return;
    items.splice(i, 1);
    try { URL.revokeObjectURL(it.url); } catch (_) {}
    if (it.wav) { try { URL.revokeObjectURL(it.wav.url); } catch (_) {} }
    if (it.li && it.li.parentNode) it.li.parentNode.removeChild(it.li);
    render();
  }

  // ---- the panel ----
  function render() {
    const t = mp3Ok() || pickType(), on = rxOn();
    btn.disabled = !rec && (!t || !on);
    btn.classList.toggle("on", !!rec);
    btn.setAttribute("aria-pressed", rec ? "true" : "false");
    if (lbl) lbl.textContent = rec ? "Stop" : "Record";
    const use = btn.querySelector("use");
    if (use) use.setAttribute("href", rec ? "#i-square" : "#i-tr-record");
    if (timeEl) { timeEl.textContent = fmtDur(rec ? Date.now() - startedAt : 0); timeEl.classList.toggle("on", !!rec); }
    let h;
    if (!t) h = "This browser can't record audio.";
    else if (rec) h = wav ? "Recording, with a WAV copy. Stop to keep the files." : "Recording. Stop to keep the file.";
    else if (failed) h = failed;
    else if (note) h = note;
    else if (!on) h = "Turn on RX in the Audio panel to record.";
    else if (items.some((i) => !done(i))) h = "Recordings stay in this tab until you download them.";
    else h = "Records the radio's receive audio. The volume slider doesn't change it.";
    if (hint && hint.textContent !== h) hint.textContent = h;
    list.hidden = !items.length;
    if (wavChk) {                                         // chosen before Record; needs the PCM tap
      const can = !!t && pcmOk();
      wavChk.disabled = !!rec || !can;
      const opt = wavChk.closest ? wavChk.closest(".rec-opt") : null;
      if (opt) opt.classList.toggle("na", !can);
    }
  }
  function tick() {
    const on = rxOn();
    if (on && !wasOn) { note = ""; failed = ""; }          // RX turned on again: a fresh start
    wasOn = on;
    if (on) meterOn(); else meterOff();
    if (rec && !on) stop("Stopped: RX audio was turned off.");
    if (fill) fill.style.width = (on ? level() : 0) + "%";
    render();
  }

  btn.addEventListener("click", () => { if (rec) stop(); else start(); });
  window.addEventListener("beforeunload", (e) => {
    if (!rec && !items.some((i) => !done(i))) return;
    e.preventDefault();
    e.returnValue = "";                  // the browser asks before the recordings are lost
  });
  setInterval(tick, TICK_MS);
  render();

  window.Recorder = {
    summary: () => (rec ? "REC " + fmtDur(Date.now() - startedAt)
      : items.length ? items.length + (items.length === 1 ? " recording" : " recordings") : ""),
  };
})();
