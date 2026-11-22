/* TeleRig FT8 receive (the digital window's FT8 tab): FT8 decoded in this browser from the radio's audio
 * (window.RadioAudio.tapPcm: 16 kHz mono, before the volume), a slot at a time. Receive only: nothing here
 * keys the radio or sends anything to it.
 *
 * The audio is kept for the last 40 s with the time each piece arrived (the earliest arrival over the last
 * few seconds anchors the sample clock to this computer's clock, so a late network packet doesn't move
 * it). When a 15 s slot (on UTC) has fully arrived, it goes to the decoder (ft8-decode.js) in a worker;
 * its messages are listed under the slot's time, newest at the bottom. DT includes the audio's own delay
 * on its way here. The slots follow this computer's clock: it has to be right to about a second.
 *
 * The status line helps when nothing shows: with RX off it is empty (the window's own line says so,
 * digital.js); with the radio in a mode FT8 is not heard in, it names the mode (FT8 is sent in USB, so
 * USB or DATA-USB); after EMPTY_SLOTS slots in a row with nothing decoded it says what to check.
 *
 * A message that names the operator's callsign (DigitalPanel.call(), also inside <...> or with a / part)
 * is marked in the list, shows even under "CQ only", and the callsign stands at the end of the list's
 * headings; tr:call (the callsign changed) marks the list again.
 *
 * window.Ft8Panel = { summary(), feed(pcm, rate), markers(), text() } (the window; tests; text(): the list,
 * for the window's Copy key)
 */
(function () {
  "use strict";
  if (typeof document === "undefined") return;
  const $ = (id) => document.getElementById(id);
  const pane = $("dmPaneFt8"), list = $("ft8List"), status = $("ft8Status"), cqOnly = $("ft8CqOnly"), clearBtn = $("ft8Clear");
  const tile = pane ? pane.closest(".grid-stack-item") : null;
  if (!pane || !list || !status || !tile) return;
  const SLOT_MS = 15000, KEEP_S = 40, LATE_MS = 300, MIN_S = 10, MAX_ROWS = 400, ANCHOR_MS = 8000;
  const KEY_CQ = "telerig.ft8.cq";
  const ls = (k, v) => { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (_) {} return null; };
  const audio = () => window.RadioAudio || null;
  const rxOn = () => { const A = audio(); try { return !!(A && A.on()); } catch (_) { return false; } };
  const onGrid = () => !!(tile.parentElement && tile.parentElement.id === "grid");
  const radio = () => { try { return (window.RadioControl && RadioControl.state()) || {}; } catch (_) { return {}; } };
  const EMPTY_SLOTS = 3;                                    // 45 s with nothing decoded: say what to check
  const pad = (n) => String(n).padStart(2, "0");
  const hms = (ms) => { const d = new Date(ms); return pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes()) + ":" + pad(d.getUTCSeconds()); };

  // ---- the audio, with its clock ----
  let rate = 0, ring = null, total = 0, first = 0;          // samples received in all; the first one kept
  let anchors = [];                                         // { at, off }: arrival time less the samples' own time
  function reset(r) { rate = r; ring = new Float32Array(r * KEEP_S); total = 0; first = 0; anchors = []; }
  function offset() { let m = Infinity; for (const a of anchors) if (a.off < m) m = a.off; return m; }
  const idxAt = (ms) => Math.round(((ms - offset()) * rate) / 1000);
  function feed(pcm, r) {
    if (!onGrid()) return;
    const now = Date.now();
    if (r !== rate || !ring) reset(r);
    const off = now - ((total + pcm.length) * 1000) / rate;
    if (anchors.length && off - offset() > 2000) reset(r);  // a gap of more than 2 s: the stream started again
    for (let i = 0; i < pcm.length; i++) ring[(total + i) % ring.length] = pcm[i] / 32768;
    total += pcm.length;
    first = Math.max(first, total - ring.length);
    anchors.push({ at: now, off: now - (total * 1000) / rate });
    while (anchors.length > 1 && now - anchors[0].at > ANCHOR_MS) anchors.shift();
    schedule(now);
  }

  // ---- the slots ----
  let done = 0, busy = false, lastSlot = null, lastCount = 0, lastFreqs = [], note = "", empty = 0;
  function schedule(now) {
    if (busy || !ring || !anchors.length) return;
    const start = Math.floor((now - SLOT_MS - LATE_MS) / SLOT_MS) * SLOT_MS;   // the newest slot that has ended
    if (start <= done) return;
    const a = idxAt(start), b = a + 15 * rate;
    if (b > total) return;                                  // not all here yet
    done = start;
    const have = b - Math.max(a, first);
    if (have < MIN_S * rate || a + 15 * rate < first) return;   // too little of it heard
    const s = new Float32Array(15 * rate);
    for (let i = Math.max(a, first); i < b; i++) s[i - a] = ring[i % ring.length];
    decode(start, s);
  }
  let worker = null, workerBroken = false, jobs = new Map(), seq = 0;
  function workerUrl() {                                    // the page's own copy, with its ?v= (never a stale one)
    try {
      const sc = [...document.querySelectorAll("script")].find((s) => /\/static\/ft8-decode\.js(\?|$)/.test(s.getAttribute("src") || ""));
      if (sc) return sc.src || sc.getAttribute("src");
    } catch (_) {}
    return "/static/ft8-decode.js";
  }
  function decode(start, samples) {
    busy = true; note = "Decoding the " + hms(start) + " slot…"; render();
    const id = ++seq;
    jobs.set(id, start);
    if (!worker && !workerBroken && typeof Worker === "function") {
      try {
        worker = new Worker(workerUrl());
        worker.onmessage = (e) => finish(e.data || {});
        worker.onerror = () => { workerBroken = true; worker = null; for (const [i] of jobs) finish({ id: i, error: "worker" }); };
      } catch (_) { workerBroken = true; worker = null; }
    }
    if (worker) { worker.postMessage({ id, rate, samples }, [samples.buffer]); return; }
    // no worker here: the decoder on this page, a moment later (it takes a fraction of a second)
    setTimeout(() => {
      let decodes = null, error = null;
      try { decodes = window.FT8.decodeSlot(samples, rate); } catch (err) { error = String(err && err.message || err); }
      finish({ id, decodes, error });
    }, 0);
  }
  function finish(m) {
    const start = jobs.get(m.id);
    jobs.delete(m.id);
    busy = jobs.size > 0;
    if (start === undefined) return;
    if (m.error || !Array.isArray(m.decodes)) { note = "The " + hms(start) + " slot could not be decoded."; render(); return; }
    show(start, m.decodes);
  }

  // ---- the list ----
  let rows = 0;
  const isCq = (t) => /^CQ( |$)/.test(t);
  // the operator's callsign, and whether a message names it: one of its words, the <> of a hashed call
  // taken off, or a part of a word around a / (W9XYZ/P, VE3/W9XYZ)
  const meEl = $("ft8Me");
  function myCall() {                                      // "" without the Modem window's script, or before a callsign is known
    try { return window.DigitalPanel.call() || ""; } catch (_) { return ""; }
  }
  const names = (t, c) => !!c && String(t).replace(/[<>]/g, "").split(/\s+/).some((w) => w === c || w.split("/").includes(c));
  function remark() {                                      // the callsign changed: the rows and the heading again
    const c = myCall();
    for (const r of list.querySelectorAll(".ft8-row")) r.classList.toggle("me", names(r.querySelector(".msg").textContent, c));
    if (meEl) {
      meEl.textContent = c || "your call: set it in CW";
      meEl.classList.toggle("on", !!c);
      meEl.title = c ? "Messages that name " + c + " are marked" : "Type your callsign in CW's Your call field to have it marked here";
    }
  }
  document.addEventListener("tr:call", remark);            // digital.js: as it starts, and at every change
  function cell(cls, text) { const s = document.createElement("span"); s.className = cls; s.textContent = text; return s; }
  function show(start, decodes) {
    lastSlot = start; lastCount = decodes.length; lastFreqs = decodes.map((d) => d.freq);
    empty = decodes.length ? 0 : empty + 1;
    note = decodes.length ? ""
      : empty >= EMPTY_SLOTS ? "Nothing decoded for " + empty * 15 + " s. Check the frequency (Tune to), the mode and this computer's clock."
      : "Nothing decoded in the " + hms(start) + " slot.";
    if (decodes.length) {
      const atEnd = list.scrollHeight - list.scrollTop - list.clientHeight < 24;
      const sep = document.createElement("div");
      sep.className = "ft8-slot";
      sep.textContent = hms(start) + " UTC";
      list.appendChild(sep);
      const me = myCall();
      for (const d of decodes.slice().sort((a, b) => a.freq - b.freq)) {
        const r = document.createElement("div");
        r.className = "ft8-row" + (isCq(d.text) ? " cq" : "") + (names(d.text, me) ? " me" : "");
        r.appendChild(cell("db", (d.snr > 0 ? "+" : "") + d.snr));
        r.appendChild(cell("dt", d.dt.toFixed(1)));
        r.appendChild(cell("hz", String(d.freq)));
        r.appendChild(cell("msg", d.text));                  // text only, never markup
        list.appendChild(r);
        rows++;
      }
      while (rows > MAX_ROWS && list.firstChild) {          // the oldest slots go first
        const el = list.firstChild;
        if (el.classList && el.classList.contains("ft8-row")) rows--;
        list.removeChild(el);
      }
      if (atEnd) list.scrollTop = list.scrollHeight;
      document.dispatchEvent(new CustomEvent("tr:digital", { detail: { mode: "ft8" } }));
    }
    render();
  }
  if (cqOnly) {
    cqOnly.checked = ls(KEY_CQ) === "1";
    list.classList.toggle("cq-only", cqOnly.checked);
    cqOnly.addEventListener("change", () => { ls(KEY_CQ, cqOnly.checked ? "1" : "0"); list.classList.toggle("cq-only", cqOnly.checked); });
  }
  if (clearBtn) clearBtn.addEventListener("click", () => { while (list.firstChild) list.removeChild(list.firstChild); rows = 0; lastFreqs = []; render(); });
  // FT8 is sent in USB: heard in USB, or in a data mode on USB (an Icom's DATA is USB with its switch on)
  const usbMode = (m) => /^(USB|DATA-USB|USB-D)$/.test(String(m || ""));

  function say(t) { if (status.textContent !== t) status.textContent = t; }
  function render() {
    if (!rxOn()) { say(""); return; }                       // RX is off: the window's own line says so (digital.js)
    const st = radio();
    if (st.connected && st.mode_name && !usbMode(st.mode_name)) {
      say("The radio is in " + st.mode_name + ": FT8 is heard in USB or DATA-USB (Tune to sets it).");
      return;
    }
    if (note) { say(note); return; }
    const next = (Math.floor(Date.now() / SLOT_MS) + 1) * SLOT_MS;
    say(lastSlot === null ? "Listening: the first slot is decoded after " + hms(next) + " UTC."
                          : lastCount + (lastCount === 1 ? " message" : " messages") + " at " + hms(lastSlot) + " UTC.");
  }
  (function tap() {                                        // app.js loads first; wait for it if needed
    const A = audio();
    if (A && typeof A.tapPcm === "function") A.tapPcm(feed); else setTimeout(tap, 500);
  })();
  setInterval(() => { if (ring) schedule(Date.now()); render(); }, 500);
  render();

  window.Ft8Panel = {
    summary: () => (lastSlot === null ? "" : "FT8 " + lastCount + " at " + hms(lastSlot).slice(0, 5)),
    feed: (pcm, r) => { if (pcm && pcm.length && r > 0) feed(pcm, r); },
    markers: () => lastFreqs.map((hz) => ({ hz: hz + 25, dim: true })),   // the middle of each message's 50 Hz
    // the list as it shows, as text: a line for each slot's time, then its messages (dB, DT, Hz and the
    // message, tab-separated); under "CQ only", only the rows that show
    text: () => {
      const out = [], only = list.classList.contains("cq-only");
      let slot = null;
      for (const el of Array.from(list.children)) {
        if (el.classList.contains("ft8-slot")) { slot = el.textContent; continue; }
        if (!el.classList.contains("ft8-row") || (only && !el.classList.contains("cq") && !el.classList.contains("me"))) continue;
        if (slot !== null) { out.push(slot); slot = null; }
        out.push(Array.from(el.children).map((c) => c.textContent).join("\t"));
      }
      return out.join("\n");
    },
  };
})();
