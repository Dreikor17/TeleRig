/* TeleRig Recorder (the Recorder panel): records what you hear, the radio's RX audio, from the
 * pre-volume RX bus app.js plays every transport into (window.RadioAudio.bus(): LAN PCM and a serial
 * radio's host audio alike), so the volume slider never changes the file.
 *
 *   MediaRecorder on a MediaStreamAudioDestinationNode -> WebM/Opus (Ogg/Opus where WebM is missing,
 *   MP4 on Safari) at 64 kb/s, about half a megabyte a minute. A level bar (an AnalyserNode on the same
 *   bus) runs whenever RX audio is on, so the level can be checked before recording.
 *
 * Recordings stay in this tab as Blob URLs until downloaded or deleted; leaving the page with one
 * running or not downloaded asks first. Nothing is sent to the server and nothing here can transmit:
 * the only audio nodes touched are this panel's own taps on the RX bus.
 *
 * window.Recorder = { summary() }   the collapsed panel's one-line summary (layout.js)
 */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const btn = $("recBtn"), lbl = $("recBtnLbl"), timeEl = $("recTime"), fill = $("recLevel"),
        hint = $("recHint"), list = $("recList");
  if (!btn || !list) return;

  const TYPES = [["audio/webm;codecs=opus", "webm"], ["audio/ogg;codecs=opus", "ogg"],
                 ["audio/webm", "webm"], ["audio/mp4", "m4a"]];
  const BITRATE = 64000;                 // Opus at 64 kb/s: plenty for SSB, CW and FM voice
  const SLICE_MS = 1000;                 // a chunk a second: the size readout moves, memory stays chunked
  const TICK_MS = 100;
  const SVG_NS = "http://www.w3.org/2000/svg";

  let rec = null;                        // the running MediaRecorder
  let tap = null;                        // {bus, dest} while recording
  let meter = null;                      // {bus, an, buf} while RX audio is on
  let chunks = [], bytes = 0, startedAt = 0, info = null;
  let failed = "", note = "", wasOn = false;   // the last error / stop reason: shown until RX is turned on again
  const items = [];                      // {url, file, title, meta, saved, li}

  const say = (m) => { try { TR.srLive(m); } catch (_) {} };
  const audio = () => window.RadioAudio || null;
  const rxOn = () => { const A = audio(); try { return !!(A && A.on()); } catch (_) { return false; } };

  function pickType() {
    const MR = window.MediaRecorder;
    if (typeof MR !== "function") return null;
    if (typeof MR.isTypeSupported === "function") {
      for (const t of TYPES) if (MR.isTypeSupported(t[0])) return t;
    }
    return ["", ""];                     // the browser's own choice; the extension follows rec.mimeType
  }
  function extFor(mime) {
    const m = String(mime || "").toLowerCase();
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

  // ---- recording ----
  function start() {
    if (rec) return;
    const t = pickType(), A = audio();
    if (!t || !rxOn() || !A) { render(); return; }
    try { A.ensure(); } catch (_) {}
    const ctx = A.ctx(), bus = A.bus();
    if (!ctx || !bus || typeof ctx.createMediaStreamDestination !== "function") {
      failed = "This browser can't record audio."; render(); return;
    }
    const dest = ctx.createMediaStreamDestination();
    bus.connect(dest);
    let r;
    try {
      const opts = { audioBitsPerSecond: BITRATE };
      if (t[0]) opts.mimeType = t[0];
      r = new MediaRecorder(dest.stream, opts);
    } catch (e) {
      try { bus.disconnect(dest); } catch (_) {}
      failed = "The recording could not start: " + ((e && e.message) || e) + "."; render(); return;
    }
    chunks = []; bytes = 0; failed = ""; note = "";
    r.ondataavailable = (ev) => { if (ev.data && ev.data.size) { chunks.push(ev.data); bytes += ev.data.size; } };
    r.onstop = () => finish(r);
    r.onerror = (ev) => {
      const e = ev && (ev.error || ev);
      failed = "The recording stopped with an error: " + ((e && (e.message || e.name)) || "unknown") + ".";
      stop();
    };
    rec = r; tap = { bus, dest }; info = snapshot(); startedAt = Date.now();
    try { r.start(SLICE_MS); }
    catch (e) {
      rec = null; try { bus.disconnect(dest); } catch (_) {} tap = null;
      failed = "The recording could not start: " + ((e && e.message) || e) + "."; render(); return;
    }
    say("Recording started");
    render();
  }
  function stop(why) {
    if (!rec) return;
    if (why) note = why;
    const r = rec;
    if (r.state === "inactive") { finish(r); return; }
    try { r.stop(); } catch (_) { finish(r); }       // the stop event delivers the last chunk, then finish()
  }
  function finish(r) {
    if (rec !== r) return;
    rec = null;
    if (tap) { try { tap.bus.disconnect(tap.dest); } catch (_) {} tap = null; }
    const ms = Date.now() - startedAt;
    const mime = (r && r.mimeType) || "audio/webm";
    if (!bytes) { note = note || "Nothing was recorded."; render(); return; }
    const blob = new Blob(chunks, { type: mime });
    chunks = [];
    const it = { url: URL.createObjectURL(blob), file: info.file + "." + extFor(mime), title: info.title,
                 meta: info.when + " · " + fmtDur(ms) + " · " + fmtSize(blob.size || bytes), saved: false, li: null };
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
    const a = document.createElement("a");
    a.className = "key rec-save"; a.href = it.url; a.download = it.file; a.textContent = "Download";
    a.title = it.file;
    a.addEventListener("click", () => { it.saved = true; li.classList.add("saved"); render(); });
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
    li.append(txt, a, del);
    it.li = li;
    return li;
  }
  function remove(it) {
    const i = items.indexOf(it);
    if (i < 0) return;
    items.splice(i, 1);
    try { URL.revokeObjectURL(it.url); } catch (_) {}
    if (it.li && it.li.parentNode) it.li.parentNode.removeChild(it.li);
    render();
  }

  // ---- the panel ----
  function render() {
    const t = pickType(), on = rxOn();
    btn.disabled = !rec && (!t || !on);
    btn.classList.toggle("on", !!rec);
    btn.setAttribute("aria-pressed", rec ? "true" : "false");
    if (lbl) lbl.textContent = rec ? "Stop" : "Record";
    const use = btn.querySelector("use");
    if (use) use.setAttribute("href", rec ? "#i-square" : "#i-tr-record");
    if (timeEl) { timeEl.textContent = fmtDur(rec ? Date.now() - startedAt : 0); timeEl.classList.toggle("on", !!rec); }
    let h;
    if (!t) h = "This browser can't record audio.";
    else if (rec) h = "Recording. Stop to keep the file.";
    else if (failed) h = failed;
    else if (note) h = note;
    else if (!on) h = "Turn on RX in the Audio panel to record.";
    else if (items.some((i) => !i.saved)) h = "Recordings stay in this tab until you download them.";
    else h = "Records the radio's receive audio. The volume slider doesn't change it.";
    if (hint && hint.textContent !== h) hint.textContent = h;
    list.hidden = !items.length;
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
    if (!rec && !items.some((i) => !i.saved)) return;
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
