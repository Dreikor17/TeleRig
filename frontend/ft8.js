/* TeleRig FT8 receive (the digital window's FT8 tab): FT8 decoded in this browser from the radio's audio
 * (window.RadioAudio.tapPcm: 16 kHz mono, before the volume), a slot at a time. Receive only: nothing here
 * keys the radio or sends anything to it.
 *
 * The audio is kept for the last 40 s with the time each piece arrived (the earliest arrival over the last
 * few seconds anchors the sample clock to this computer's clock, so a late network packet doesn't move
 * it). When a 15 s slot (on UTC) has arrived, with PRE_S before it and POST_S after it, the 21 s go to
 * the decoder (ft8-decode.js) in a worker; its messages are listed under the slot's time, newest at the
 * bottom. DT includes the audio's own delay on its way here and this computer's clock error: the wider
 * window still decodes a message about 4 s late or early (a 15 s one stopped at 2.5 s), and the status
 * line names a DT that large, so the operator sees why.
 *
 * The status line helps when nothing shows: with RX off it is empty (the window's own line says so,
 * digital.js); with the radio in a mode FT8 is not heard in, it names the mode (FT8 is sent in USB, so
 * USB or DATA-USB); with the audio arriving with gaps (fewer samples than the time they cover, over the
 * last 30 s) it says so, as FT8 needs it unbroken; after EMPTY_SLOTS slots in a row with nothing decoded
 * it says what to check.
 *
 * A message that names the operator's callsign (DigitalPanel.call(), also inside <...> or with a / part)
 * is marked in the list, shows even under "CQ only", and the callsign stands at the end of the list's
 * headings; tr:call (the callsign changed) marks the list again.
 *
 * window.Ft8Panel = { summary(), feed(pcm, rate), text() } (the window; tests; text(): the list,
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
  const PRE_S = 3, POST_S = 3;                             // the window around a slot: a late or early message fits
  const RATE_S = 30, GAP_PCT = 2;                          // audio short of its time by over 2 % in 30 s: gaps
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
  let counts = [];                                          // { at, total }: how much had arrived when, over RATE_S
  function reset(r) { rate = r; ring = new Float32Array(r * KEEP_S); total = 0; first = 0; anchors = []; counts = []; }
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
    counts.push({ at: now, total });
    while (counts.length > 2 && now - counts[1].at > RATE_S * 1000) counts.shift();
    schedule(now);
  }
  // the audio's shortfall, in percent of the time it covers over the last RATE_S (0 while there is too
  // little to say): a stream that drops pieces delivers fewer samples than the seconds that pass
  function gapPct() {
    if (counts.length < 2 || !rate) return 0;
    const a = counts[0], b = counts[counts.length - 1], secs = (b.at - a.at) / 1000;
    if (secs < RATE_S / 2) return 0;
    const got = (b.total - a.total) / rate;
    return Math.max(0, Math.round((1 - got / secs) * 100));
  }

  // ---- the slots ----
  let done = 0, busy = false, lastSlot = null, lastCount = 0, lastDt = 0, note = "", empty = 0;
  function schedule(now) {
    if (busy || !ring || !anchors.length) return;
    // the newest slot whose window (PRE_S before it to POST_S after it) has ended
    const start = Math.floor((now - SLOT_MS - POST_S * 1000 - LATE_MS) / SLOT_MS) * SLOT_MS;
    if (start <= done) return;
    const len = (PRE_S + 15 + POST_S) * rate, a = idxAt(start - PRE_S * 1000), b = a + len;
    if (b > total) return;                                  // not all here yet
    done = start;
    const s0 = a + PRE_S * rate, s1 = s0 + 15 * rate;       // the slot itself, inside the window
    if (s1 - Math.max(s0, first) < MIN_S * rate) return;    // too little of the slot heard
    const s = new Float32Array(len);
    for (let i = Math.max(a, first); i < b; i++) s[i - a] = ring[i % ring.length];
    decode(start, s, -PRE_S);
  }
  let worker = null, workerBroken = false, jobs = new Map(), seq = 0;
  function workerUrl() {                                    // the page's own copy, with its ?v= (never a stale one)
    try {
      const sc = [...document.querySelectorAll("script")].find((s) => /\/static\/ft8-decode\.js(\?|$)/.test(s.getAttribute("src") || ""));
      if (sc) return sc.src || sc.getAttribute("src");
    } catch (_) {}
    return "/static/ft8-decode.js";
  }
  function decode(start, samples, t0) {
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
    if (worker) { worker.postMessage({ id, rate, samples, t0 }, [samples.buffer]); return; }
    // no worker here: the decoder on this page, a moment later (it takes a fraction of a second)
    setTimeout(() => {
      let decodes = null, error = null;
      try { decodes = window.FT8.decodeSlot(samples, rate, t0); } catch (err) { error = String(err && err.message || err); }
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
  // the middle DT of a slot's messages: how late (or early) this computer hears the slots
  function medianDt(decodes) {
    const v = decodes.map((d) => d.dt).sort((x, y) => x - y), n = v.length;
    return n ? (n % 2 ? v[(n - 1) / 2] : (v[n / 2 - 1] + v[n / 2]) / 2) : 0;
  }
  function show(start, decodes) {
    lastSlot = start; lastCount = decodes.length;
    if (decodes.length) lastDt = medianDt(decodes);
    empty = decodes.length ? 0 : empty + 1;
    note = decodes.length ? ""
      : empty >= EMPTY_SLOTS ? "Nothing decoded for " + empty * 15 + " s. Check the frequency (the Calling list in the Spots panel has each band's FT8 dial), USB or DATA-USB, and this computer's clock."
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
  if (clearBtn) clearBtn.addEventListener("click", () => { while (list.firstChild) list.removeChild(list.firstChild); rows = 0; render(); });
  // FT8 is sent in USB: heard in USB, or in a data mode on USB (an Icom's DATA is USB with its switch on)
  const usbMode = (m) => /^(USB|DATA-USB|USB-D)$/.test(String(m || ""));

  function say(t) { if (status.textContent !== t) status.textContent = t; }
  function render() {
    if (!rxOn()) { say(""); return; }                       // RX is off: the window's own line says so (digital.js)
    const st = radio();
    if (st.connected && st.mode_name && !usbMode(st.mode_name)) {
      say("The radio is in " + st.mode_name + ": FT8 is heard in USB or DATA-USB (an FT8 row of the Calling list sets it).");
      return;
    }
    const gaps = gapPct();
    if (gaps >= GAP_PCT) {
      say("The radio's audio is arriving with gaps (" + gaps + " % missing over the last " + RATE_S + " s): FT8 needs it unbroken.");
      return;
    }
    if (note) { say(note); return; }
    // the first slot decoded once its window has passed: POST_S after the slot's end
    const next = (Math.floor(Date.now() / SLOT_MS) + 1) * SLOT_MS + POST_S * 1000;
    const late = lastCount && Math.abs(lastDt) >= 1.5
      ? " DT " + (lastDt > 0 ? "+" : "") + lastDt.toFixed(1) + " s: this computer's clock or the audio's delay." : "";
    say(lastSlot === null ? "Listening: the first slot is decoded after " + hms(next) + " UTC."
                          : lastCount + (lastCount === 1 ? " message" : " messages") + " at " + hms(lastSlot) + " UTC." + late);
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
