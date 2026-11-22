/* CW decoder / coder — the CW mode of the Modem window (the "sstv" tile, digital.js); outside a tile, the
 * old overlay tool that popped up over the waterfall.
 *
 * In the window: the decoder runs while its Decode key is on and the window is on the grid, whichever
 * mode is shown (collapsed too, like the window's other modes); new text puts a dot on the CW key. It
 * decodes without being asked, as the window's other modes do: CW coming into view (the page opening on
 * it, the window brought onto the grid showing it, the CW key; digital.js's tr:modem) turns Decode on.
 * Decode turns it off, until CW next comes into view. The scope's CW button brings CW into view and
 * starts the decoder. While this tab sends a CW message the window stays on CW, its STOP in view
 * (CwPanel.busy()).
 *
 * The decoder starts at once (the model loads in its worker) but listens only once the page has its
 * audio, which a click makes (RX, the mic, Play): it never makes the audio context itself. One made
 * before any click would stay paused, and the mic would then send silence.
 *
 * One line under the bar (#cwStatus) says where the decoder stands: off, loading, listening, copying,
 * or why it can't. With RX off it says nothing: the window's own line does (digital.js). Decode off and
 * on again keeps the chat (Clear empties it). WPM and the sidetone's pitch are kept by this browser.
 *
 * Decoder: a NEURAL decoder (DeepCW). It taps the pre-volume RX bus
 * (RadioAudio.bus()), resamples to 3200 Hz, and re-runs the ONNX model in
 * cw-worker.js over a growing window, committing text at pauses. Far more
 * robust than a timing decoder under QSB/QRM/weak signals.
 *
 * Coder: types -> Morse -> a soft-keyed sidetone. The sidetone (Play) never keys the radio; it
 * feeds the same rxBus, so playing it self-tests the decoder.
 *
 * CW TX: for operators (role >= operator, TX permission) the TX button DOES key the radio: it sends
 * the typed message once as a `cw_tx` claim. An Icom sends it with its own keyer (CI-V 17); the
 * FT-991A is keyed by the server on the DTR line of its Standard USB port. The claim goes through the
 * server's TX arbiter like PTT: one owner at a time, heartbeats, a per-message cap, the radio's CW
 * deadline and its TOT. The same button stops it: one tap for this tab's own CW (cw_stop), two taps
 * for someone else's (tx_stop, F18). A tap on it while it is or just was a STOP control never starts
 * a transmission (below). Viewers never see it (data-tx, role channel).
 *
 * Quick messages: CQ, DE and 73 write a message with the operator's callsign (DigitalPanel.call()) into
 * the message field, where it can be changed; they send nothing. CQ is a whole message; DE and 73 are
 * added to what is there. With no callsign yet, CQ and DE ask for it (the field beside them).
 *
 * window.CwPanel = { busy(), summary(), text() } (digital.js: the send lock; the collapsed window's
 * summary; the chat as text, for Copy)
 *
 * SPDX-License-Identifier: AGPL-3.0-only
 * The decode path links the AGPL-3.0 DeepCW model (github.com/e04/deepcw-engine),
 * so the CW feature is AGPL-3.0. See cw-worker.js. onnxruntime-web is MIT.
 */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const RA = () => window.RadioAudio || null;
  const RC = () => window.RadioControl || null;   // WS command channel + live radio state
  const CW_WORKER_V = "2";                       // bump when cw-worker.js changes (cache-bust)

  const MORSE = {
    A: ".-", B: "-...", C: "-.-.", D: "-..", E: ".", F: "..-.", G: "--.", H: "....",
    I: "..", J: ".---", K: "-.-", L: ".-..", M: "--", N: "-.", O: "---", P: ".--.",
    Q: "--.-", R: ".-.", S: "...", T: "-", U: "..-", V: "...-", W: ".--", X: "-..-",
    Y: "-.--", Z: "--..",
    "0": "-----", "1": ".----", "2": "..---", "3": "...--", "4": "....-", "5": ".....",
    "6": "-....", "7": "--...", "8": "---..", "9": "----.",
    ".": ".-.-.-", ",": "--..--", "?": "..--..", "/": "-..-.", "=": "-...-", "+": ".-.-.",
    "-": "-....-", ":": "---...", "(": "-.--.", ")": "-.--.-", '"': ".-..-.", "@": ".--.-.",
    "'": ".----.",
  };

  const panel = $("cwTool"), btn = $("cwToolBtn"), head = $("cwHead");
  if (!panel || !btn) return;
  let open = false;
  // Tile mode (§3.18): the panel is the Modem window's CW pane. Its visibility belongs to RadioLayout
  // and the window's mode keys; `open` only means "the decoder runs". Outside a tile it is the old
  // floating panel.
  const inTile = !!panel.closest(".grid-stack-item, #tileParking");
  const WIN = "sstv";                              // the Modem window's tile id (kept from the SSTV panel)
  const Lay = () => window.RadioLayout || null;
  const Dig = () => window.DigitalPanel || null;

  // ---- show / hide ----
  function setOpen(v) {
    open = !!v;
    if (inTile) panel.hidden = false; else panel.hidden = !open;
    btn.classList.toggle("active", open);
    const db = $("cwDecodeBtn");
    if (db) { db.setAttribute("aria-pressed", open ? "true" : "false"); db.classList.toggle("on", open); }
    if (open) startDecoder(); else stopDecoder();
  }
  // the scope's CW button: CW brought into view first (a hidden or collapsed window shown, persisted,
  // make-room; then its CW mode), and the decoder started if it isn't running; with CW already in view
  // it toggles the decoder. While this page sends an SSTV picture the window stays on it.
  btn.addEventListener("click", () => {
    const L = Lay(), D = Dig();
    if (!inTile || !L || typeof L.visible !== "function") { setOpen(!open); return; }
    if (L.visible(WIN) && (!D || D.mode() === "cw")) { setOpen(!open); return; }
    if (L.showTile(WIN) === false) return;
    if (D && D.show("cw") === false) return;
    if (!open) setOpen(true);
  });
  if ($("cwDecodeBtn")) $("cwDecodeBtn").addEventListener("click", () => setOpen(!open));
  // the window left the grid: stop the decoder (collapsed, it keeps listening like the window's other modes)
  document.addEventListener("tr:tile", (e) => {
    const d = e.detail || {};
    if (d.id === WIN && d.visible === false && open) setOpen(false);
  });
  // CW came into view (digital.js): it decodes by default
  document.addEventListener("tr:modem", (e) => {
    if (inTile && e && e.detail && e.detail.mode === "cw" && !open) setOpen(true);
  });

  // ---- drag the panel by its header (floating mode only) ----
  if (!inTile) (function () {
    let down = false, sx = 0, sy = 0, baseL = 0, baseT = 0;
    const prect = () => (panel.offsetParent || panel.parentElement).getBoundingClientRect();
    head.addEventListener("pointerdown", (e) => {
      if (e.target.closest(".tp-close")) return;
      const p = prect(), r = panel.getBoundingClientRect();
      baseL = r.left - p.left; baseT = r.top - p.top;
      panel.style.left = baseL + "px"; panel.style.top = baseT + "px";
      panel.style.right = "auto"; panel.style.bottom = "auto";
      sx = e.clientX; sy = e.clientY; down = true;
      try { head.setPointerCapture(e.pointerId); } catch (_) {}
      e.preventDefault();
    });
    head.addEventListener("pointermove", (e) => {
      if (!down || !(e.buttons & 1)) return;
      const p = prect();
      let x = baseL + (e.clientX - sx), y = baseT + (e.clientY - sy);
      x = Math.max(0, Math.min(p.width - panel.offsetWidth, x));
      y = Math.max(0, Math.min(p.height - 30, y));
      panel.style.left = x + "px"; panel.style.top = y + "px";
    });
    const end = () => { down = false; };
    head.addEventListener("pointerup", end);
    head.addEventListener("pointercancel", end);
    head.addEventListener("lostpointercapture", end);
  })();

  // ---- resize the panel by dragging any edge / corner (floating mode only) ----
  if (!inTile) (function () {
    const MINW = 240, MINH = 160;
    const prect = () => (panel.offsetParent || panel.parentElement).getBoundingClientRect();
    for (const dir of ["n", "s", "e", "w", "ne", "nw", "se", "sw"]) {
      const h = document.createElement("div");
      h.className = "tp-resize tp-resize-" + dir;
      h.dataset.dir = dir;
      panel.appendChild(h);                          // absolute -> stays out of the flex flow
    }
    let dir = null, sx = 0, sy = 0, sw = 0, sh = 0, sl = 0, st = 0;
    panel.addEventListener("pointerdown", (e) => {
      const t = e.target.closest(".tp-resize"); if (!t) return;
      dir = t.dataset.dir;
      const p = prect(), r = panel.getBoundingClientRect();
      sx = e.clientX; sy = e.clientY; sw = r.width; sh = r.height;
      sl = r.left - p.left; st = r.top - p.top;
      panel.style.left = sl + "px"; panel.style.top = st + "px";   // pin to left/top so resize is predictable
      panel.style.right = "auto"; panel.style.bottom = "auto";
      panel.style.width = sw + "px"; panel.style.height = sh + "px";
      try { t.setPointerCapture(e.pointerId); } catch (_) {}
      e.preventDefault(); e.stopPropagation();
    });
    panel.addEventListener("pointermove", (e) => {
      if (!dir || !(e.buttons & 1)) return;
      const p = prect();
      const dx = e.clientX - sx, dy = e.clientY - sy;
      let w = sw, h = sh, l = sl, t = st;
      if (dir.includes("e")) w = sw + dx;
      if (dir.includes("s")) h = sh + dy;
      if (dir.includes("w")) { w = sw - dx; l = sl + dx; }
      if (dir.includes("n")) { h = sh - dy; t = st + dy; }
      if (w < MINW) { if (dir.includes("w")) l -= (MINW - w); w = MINW; }   // keep the anchored edge fixed
      if (h < MINH) { if (dir.includes("n")) t -= (MINH - h); h = MINH; }
      w = Math.min(w, p.width); h = Math.min(h, p.height);                  // never exceed the scope pane
      l = Math.max(0, Math.min(l, p.width - w));
      t = Math.max(0, Math.min(t, p.height - h));
      panel.style.width = w + "px"; panel.style.height = h + "px";
      panel.style.left = l + "px"; panel.style.top = t + "px";
    });
    const end = () => { dir = null; };
    panel.addEventListener("pointerup", end);
    panel.addEventListener("pointercancel", end);
    panel.addEventListener("lostpointercapture", end);
  })();

  // ---- neural decoder: live streaming over a growing buffer, committing at word gaps ----
  // Ported from e04/web-deep-cw-decoder (src/hooks/useStreamingDecode.ts): keep a buffer of
  // UNcommitted audio; each tick decode it and show the running decode as a live preview; when a
  // settled word gap is found (a word-space span at least TAIL_GUARD before the end), commit the
  // text up to that gap and slide the buffer PAST it. Because the cut lands in a word gap and the
  // in-progress word's audio is retained, the live stream decodes with no mid-word seam.
  const SR = 3200;                               // model sample rate
  const INFER_MS = 1000;                         // re-decode cadence
  const MIN_PAD_S = 5;                           // pad short analyses to the model's min length
  const MIN_PENDING = 2 * SR;                    // need this much audio before decoding
  const MIN_CONFIRMED = 2 * SR;                  // earliest sample we'll commit up to
  const TAIL_GUARD = (1.25 * SR) | 0;            // never commit text inside the freshest tail (may still change)
  const MAX_SEGMENT = 18 * SR;                   // force a commit before the model's input grows too long
  const SILENCE = 1.5e-3;                        // |sample| below this across the buffer = no RX signal
  const RX_SPLIT_MS = 5000;                      // a pause this long in the received text starts a new RX bubble

  let worker = null, workerReady = false, HOP = 48;      // HOP (frame -> sample) comes from the worker
  let capNode = null, ticker = 0, workletReady = false;  // realtime capture worklet
  let buf = new Float32Array(21 * SR), pendingLen = 0;   // UNcommitted audio @ 3200 Hz (room above MAX_SEGMENT)
  let pendingText = "", lastPeak = 0;
  let messages = [], rxOpen = false;                     // texting chat log: {dir:"rx"|"tx", text, t}
  let inflight = false, analysisLen = 0, reqId = 0;
  const out = $("cwOut"), statusEl = $("cwStatus");

  // ---- chat log helpers: decoded RX = grey bubbles left, transmitted = blue bubbles right ----
  function fmtT() { const d = new Date(); return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0"); }
  function trimMsgs() { if (messages.length > 80) messages.splice(0, messages.length - 80); }
  function openRx() { if (!rxOpen) { messages.push({ dir: "rx", text: "", t: fmtT() }); rxOpen = true; trimMsgs(); } }
  function closeRx() {                                    // finalize the current RX bubble (on silence / before a TX)
    if (!rxOpen) return;
    const m = messages[messages.length - 1];
    if (m && m.dir === "rx" && !m.text) messages.pop();  // drop an RX bubble that never confirmed any text
    rxOpen = false;
  }
  // New characters after RX_SPLIT_MS or more without any (committed or in the live preview) go into a new
  // bubble, with its own time: a band with steady noise never goes quiet enough to close one. Only new
  // characters split: text committed later was already shown in the bubble it stays in.
  let lastRxAt = 0, lastPreview = "";
  function splitIfPaused() {
    const now = Date.now(), m = messages[messages.length - 1];
    if (rxOpen && m && m.dir === "rx" && m.text && lastRxAt && now - lastRxAt >= RX_SPLIT_MS) { closeRx(); openRx(); }
    lastRxAt = now;
  }
  function addRxText(seg) {                               // append a committed segment to the open RX bubble
    if (!seg) return;
    openRx();
    const m = messages[messages.length - 1];
    m.text += (m.text ? " " : "") + seg;
    if (m.text.length > 500) m.text = m.text.slice(-480);
    try { document.dispatchEvent(new CustomEvent("tr:digital", { detail: { mode: "cw" } })); } catch (_) {}   // digital.js: a dot on CW's key
  }
  function addTx(text) {                                  // a transmitted message = its own blue bubble
    const t = String(text || "").toUpperCase().trim();
    if (!t) return;
    closeRx();
    messages.push({ dir: "tx", text: t, t: fmtT() });
    trimMsgs(); render();
  }

  // The line under the bar: where the decoder stands. RX off and no audio of the page's own to hear
  // (the sidetone): nothing here, the window's own line says RX is off and has its key.
  let modelFailed = false;
  function statusText() {
    if (!open) return "The decoder is off.";
    if (modelFailed) return "The decoder could not be loaded.";
    if (capFailed) return "This browser can't pass the audio to the decoder.";
    if (!workerReady) return "Loading the decoder…";
    const ra = RA(), rxOn = !!(ra && ra.on && ra.on());
    if (!rxOn && !capNode) return "";
    return pendingText ? "Copying." : "Listening for CW.";
  }
  function showStatus() { if (statusEl) { const t = statusText(); if (statusEl.textContent !== t) statusEl.textContent = t; } }

  function ensureWorker() {
    if (worker) return;
    worker = new Worker("/static/cw-worker.js?v=" + CW_WORKER_V);
    worker.onmessage = (e) => {
      const m = e.data;
      if (m.type === "ready") { workerReady = true; if (m.hop) HOP = m.hop; render(); }
      else if (m.type === "error") { modelFailed = true; showStatus(); }
      else if (m.type === "result") onResult(m);
    };
    worker.onerror = () => { modelFailed = true; showStatus(); };
  }

  // The decoder: the model (its worker) and the 1 s tick start at once; the capture on the RX bus joins
  // when the page has its audio (attach(): now, at each tick, and before Play's sidetone).
  let moduleLoading = null, capFailed = false;           // the capture worklet's module loading; the capture failed
  function startDecoder() {
    ensureWorker();
    stopCapture();
    capFailed = false;
    pendingLen = 0; pendingText = ""; lastPeak = 0; lastRxAt = 0; lastPreview = "";   // the chat stays: Clear empties it
    inflight = false; analysisLen = 0;
    render();
    if (!ticker) ticker = setInterval(tick, INFER_MS);
    attach();
  }
  // the capture, if the decoder is on and the page has its audio; the promise settles once it is in (or
  // can't be). The worklet's module loads once: calls meanwhile wait for it, then check again.
  function attach() {
    const ra = RA(), ctx = ra && ra.ctx(), bus = ra && ra.bus();
    if (!open || capNode || capFailed || !ctx || !bus) return Promise.resolve();   // no audio yet: never made here
    if (!workletReady) {
      if (!moduleLoading) {
        let p;
        try { p = ctx.audioWorklet.addModule("/static/cw-capture-worklet.js?v=" + CW_WORKER_V); } catch (err) { p = Promise.reject(err); }
        moduleLoading = p.then(() => { workletReady = true; moduleLoading = null; },
                               () => { moduleLoading = null; failCapture(); });
      }
      return moduleLoading.then(() => attach());
    }
    try {
      capNode = new AudioWorkletNode(ctx, "cw-capture", { processorOptions: { targetSR: SR } });
      capNode.port.onmessage = (e) => appendSamples(e.data);
      bus.connect(capNode); capNode.connect(ctx.destination);   // worklet emits no output -> silent
    } catch (err) {
      stopCapture(); failCapture();
    }
    return Promise.resolve();
  }
  function failCapture() { capFailed = true; showStatus(); }   // tried again when Decode is next turned on
  function stopDecoder() { stopCapture(); if (ticker) { clearInterval(ticker); ticker = 0; } pendingText = ""; closeRx(); render(); }
  function stopCapture() {
    if (capNode) { try { capNode.disconnect(); capNode.port.onmessage = null; } catch (_) {} capNode = null; }
  }

  // 3200 Hz chunks from the realtime capture worklet -> append to the pending (uncommitted) buffer
  function appendSamples(arr) {
    for (let i = 0; i < arr.length; i++) {
      if (pendingLen >= buf.length) break;       // bounded; tick force-commits well before this
      buf[pendingLen++] = arr[i];
    }
  }

  function tick() {
    if (!capNode) attach();                       // the page's audio may have started (RX, the mic)
    if (!workerReady) { render(); return; }
    if (inflight) return;
    if (pendingLen < MIN_PENDING) { render(); return; }
    let pk = 0;                                   // RX-present gate: peak across the whole pending buffer
    for (let i = 0; i < pendingLen; i++) { const a = buf[i] < 0 ? -buf[i] : buf[i]; if (a > pk) pk = a; }
    lastPeak = pk;
    if (pk < SILENCE) {                           // no signal: keep a short lead, never decode pure noise
      const keep = Math.min(pendingLen, (0.5 * SR) | 0);
      if (pendingLen > keep) { buf.copyWithin(0, pendingLen - keep, pendingLen); pendingLen = keep; }
      pendingText = ""; closeRx(); render(); return;     // RX went quiet -> finalize the current bubble
    }
    openRx();                                     // RX signal present -> ensure a bubble to receive it
    analysisLen = Math.min(pendingLen, MAX_SEGMENT);
    const L = Math.max(analysisLen, MIN_PAD_S * SR);     // pad short analyses with trailing silence
    const a = new Float32Array(L);
    a.set(buf.subarray(0, analysisLen));
    const g = Math.max(0.1, Math.min(100, 0.5 / pk));    // level-normalize (the model has no input AGC)
    if (g !== 1) for (let i = 0; i < analysisLen; i++) a[i] *= g;
    inflight = true;
    worker.postMessage({ type: "decode", id: ++reqId, audio: a }, [a.buffer]);
  }

  // The latest word-space gap whose mid-sample is settled: >= MIN_CONFIRMED in, and (unless we're
  // forcing because the buffer is too long) at least TAIL_GUARD before the analyzed end.
  function findSplit(spans, allowNearEnd) {
    const maxCommit = allowNearEnd ? analysisLen : Math.max(MIN_CONFIRMED, analysisLen - TAIL_GUARD);
    for (let i = spans.length - 1; i >= 0; i--) {
      const sample = Math.round(((spans[i].startFrame + spans[i].endFrame) / 2) * HOP);
      if (sample >= MIN_CONFIRMED && sample <= maxCommit) return { sample, endFrame: spans[i].endFrame };
    }
    return null;
  }
  function trimToFrame(spans, endFrame) {        // text of the characters that end at/before a frame
    let s = "";
    for (let i = 0; i < spans.length; i++) if (spans[i].endFrame <= endFrame) s += spans[i].char;
    return s;
  }
  function norm(s) { return s.replace(/\s+/g, " ").trim(); }

  function onResult(m) {
    inflight = false;
    if (m.err) { return; }
    pendingText = norm(m.text || "");            // running decode of the pending buffer = the live preview
    if (pendingText && pendingText !== lastPreview) splitIfPaused();   // new characters: after a pause, a new bubble
    lastPreview = pendingText;
    const ws = m.wordSpaceSpans || [], cs = m.characterSpans || [];
    let split = findSplit(ws, false);
    if (!split && pendingLen >= MAX_SEGMENT) split = findSplit(ws, true);
    const confirmedLen = split ? split.sample : (pendingLen >= MAX_SEGMENT ? analysisLen : 0);
    if (confirmedLen >= MIN_CONFIRMED) {
      const segText = split ? norm(trimToFrame(cs, split.endFrame)) : pendingText;   // forced: whole analysis
      if (segText) { addRxText(segText); lastRxAt = Date.now(); }   // append to the current grey RX bubble
      buf.copyWithin(0, confirmedLen, pendingLen); pendingLen -= confirmedLen;       // slide past committed audio
      pendingText = "";                          // cleared so committed text never appears twice in one tick
    }
    render();
  }

  // The chat scrolls inside the window: at its end it follows what comes in; scrolled back it stays (and
  // digital.js brings it back to that place when CW is shown again). It is written again only when it
  // changed, so a selection in it lasts.
  let chatHtml = null;
  function render() {
    if (out) {
      let html = "";
      for (let i = 0; i < messages.length; i++) {
        const m = messages[i];
        let inner = esc(m.text);
        if (m.dir === "rx" && rxOpen && i === messages.length - 1 && pendingText)   // live decode tail
          inner += (m.text ? " " : "") + '<span class="cw-live">' + esc(pendingText) + "</span>";
        if (!inner) continue;                     // skip an empty (just-opened) RX bubble
        html += '<div class="cw-msg-wrap ' + m.dir + '"><div class="cw-msg-lbl">' + m.t +
          '</div><div class="cw-msg ' + m.dir + '">' + inner + "</div></div>";
      }
      if (html !== chatHtml) {
        const atEnd = out.scrollHeight - out.scrollTop - out.clientHeight < 24;   // == digital.js END_PX
        out.innerHTML = html;
        chatHtml = html;
        if (atEnd) out.scrollTop = out.scrollHeight;
      }
    }
    showStatus();
  }
  function esc(s) { return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }

  $("cwClear").addEventListener("click", () => { messages = []; rxOpen = false; pendingText = ""; lastRxAt = 0; lastPreview = ""; render(); });

  // ---- the speed and the sidetone's pitch: kept by this browser; the pitch shown beside its slider ----
  const KEY_WPM = "telerig.cw.wpm", KEY_TONE = "telerig.cw.tone";
  const ls = (k, v) => { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, String(v)); } catch (_) {} return null; };
  const wpmSet = $("cwWpmSet"), toneSet = $("cwToneSet"), toneVal = $("cwToneVal");
  const within = (v, lo, hi, dflt) => (v >= lo && v <= hi ? v : dflt);
  function showTone() { if (toneVal && toneSet) toneVal.textContent = within(+toneSet.value, 400, 1000, 600) + " Hz"; }
  if (wpmSet) {
    const kept = Math.round(+ls(KEY_WPM));
    if (kept >= 5 && kept <= 40) wpmSet.value = String(kept);
    wpmSet.addEventListener("change", () => { const v = Math.round(+wpmSet.value); if (v >= 5 && v <= 40) ls(KEY_WPM, v); });
  }
  if (toneSet) {
    const kept = Math.round(+ls(KEY_TONE));
    if (kept >= 400 && kept <= 1000) toneSet.value = String(kept);
    toneSet.addEventListener("input", showTone);
    toneSet.addEventListener("change", () => { showTone(); ls(KEY_TONE, within(Math.round(+toneSet.value), 400, 1000, 600)); });
    showTone();
  }

  // ---- quick messages: into the message field, to be read and changed before anything is done with it ----
  const MSG_MAX = 160;                                   // the field's own limit
  function myCall() {                                    // "" outside the Modem window, or before a callsign is known
    try { return Dig().call() || ""; } catch (_) { return ""; }
  }
  function quick(text, fresh) {
    const el = $("cwSend");
    if (!el) return;
    const was = fresh ? "" : String(el.value || "").replace(/\s+$/, "");
    el.value = ((was ? was + " " : "") + text).slice(0, MSG_MAX);
  }
  function askCall() {                                   // no callsign yet: its field, and why
    const el = $("cwCall"), tr = window.TR;
    if (el && typeof el.focus === "function") el.focus();
    try { if (tr && typeof tr.toast === "function") tr.toast("Type your callsign first, in the field beside these keys.", "info"); } catch (_) {}
  }
  const QUICK = {
    cwQCq: (c) => (c ? ["CQ CQ CQ DE " + c + " " + c + " K", true] : null),
    cwQDe: (c) => (c ? ["DE " + c, false] : null),
    cwQ73: (c) => [c ? "TU 73 DE " + c + " SK" : "TU 73 SK", false],
  };
  for (const id of Object.keys(QUICK)) {
    const b = $(id);
    if (b) b.addEventListener("click", () => {
      const m = QUICK[id](myCall());
      if (m) quick(m[0], m[1]); else askCall();
    });
  }

  // ---- coder (text -> Morse sidetone; never transmits) ----
  $("cwPlay").addEventListener("click", () => playMorse($("cwSend").value));
  $("cwSend").addEventListener("keydown", (e) => { if (e.key === "Enter") playMorse($("cwSend").value); });
  async function playMorse(text) {
    const ra = RA(); if (!ra) return;
    ra.ensure();                                  // a click: the page's audio may start here
    const ctx = ra.ctx(), bus = ra.bus();
    if (!ctx || !bus || !text) return;
    await attach();                               // the decoder, if on, hears the sidetone from its start
    const wpm = Math.max(5, Math.min(40, +$("cwWpmSet").value || 18));
    const dot = 1.2 / wpm;
    const tone = +$("cwToneSet").value || 600;
    const osc = ctx.createOscillator(); osc.type = "sine"; osc.frequency.value = tone;
    const g = ctx.createGain(); g.gain.value = 0.0001;
    osc.connect(g); g.connect(bus);              // -> rxBus -> speakers AND the decoder's tap
    let t = ctx.currentTime + 0.05;
    for (const chRaw of text.toUpperCase()) {
      if (chRaw === " ") { t += dot * 7; continue; }
      const m = MORSE[chRaw]; if (!m) continue;
      for (const el of m) {
        const dur = el === "-" ? dot * 3 : dot;
        g.gain.setValueAtTime(0, t);                        // raised-edge keying to true 0/1
        g.gain.linearRampToValueAtTime(0.3, t + 0.005);
        g.gain.setValueAtTime(0.3, t + dur - 0.005);
        g.gain.linearRampToValueAtTime(0, t + dur);
        t += dur + dot;
      }
      t += dot * 2;
    }
    osc.start(); osc.stop(t + 0.1);
  }

  // ---- transmit (operator-triggered): key the message on the radio as CW ----
  // Same act as pressing PTT — one bounded message, fully operator-controlled, a TX claim
  // in the server's arbiter. The backend keys the radio (Icom keyer / FT-991A DTR line) at
  // the WPM and drops back to RX. Press again to stop. Bound by the per-message cap, the
  // radio's CW deadline and TOT.
  const txBtn = $("cwTx"), txRow = $("cwTxRow"), txHint = $("cwTxHint");
  const TX = () => window.RadioTx || null;        // server-derived TX ownership (app.js, G-1)
  const TRx = () => window.TR || null;
  function setTxHint(s) {
    if (!txRow || !txHint) return;
    txHint.textContent = s || "";
    txRow.hidden = !s;
  }
  function txOwner() { const t = TX(); return t ? t.owner() : null; }
  function mineTx() { const tr = TRx(); return !!(tr && tr.isTxOwner && tr.isTxOwner()); }
  function ownerName(o) { return o ? (o.external ? "FRONT PANEL" : (o.user || "someone")) : ""; }
  // Someone else's CW (F18): a non-owner's cw_stop would only get `busy`, so the button becomes a
  // two-tap STOP: the first tap arms it for 3 s ("Tap again to stop <USER>"), the second sends tx_stop.
  // The arm is bound to the owner of the first tap (TR.stopArm, the top-bar STOP's rule, I23): any
  // owner change disarms it (every tx frame and every state poll), and the second tap sends tx_stop
  // only while that same owner still sends CW.
  //
  // A tap on this button while it is, or just was, a STOP control never starts a transmission. The
  // button keeps its STOP role for STOP_GUARD_MS after this tab last saw a CW message on the air
  // (every 400 ms state poll and every tap) and after every tap: inside that window a tap with no CW
  // on the air sends nothing, and each such tap extends the window. So when the CW message ends
  // between the two taps of a STOP double-tap (our own cw_stop landed, or someone else's message
  // finished while armed), or just before a single STOP tap, the tap can't re-send the typed text as
  // a new cw_tx. A message is sent only by a tap after STOP_GUARD_MS with nothing on the air.
  const STOP_GUARD_MS = 3000;
  let armedHint = "", stopRoleUntil = 0;
  function keepStopRole(now) { stopRoleUntil = Math.max(stopRoleUntil, now + STOP_GUARD_MS); }
  const inStopRole = (now) => now < stopRoleUntil;
  const TRo = TRx();
  const stopArm = TRo && typeof TRo.stopArm === "function" ? TRo.stopArm((o) => {
    if (o) { armedHint = "Tap again to stop " + ownerName(o); setTxHint(armedHint); return; }
    if (txHint && txHint.textContent === armedHint) setTxHint("");
    armedHint = "";
  }) : null;                                       // no tr.js: another user's CW simply can't be stopped here
  const armed = () => !!(stopArm && stopArm.armed());
  function disarm() { if (stopArm) stopArm.disarm(); }
  document.addEventListener("tr:tx", () => { if (stopArm) stopArm.check(txOwner()); });
  if (txBtn) {
    txBtn.addEventListener("click", () => {
      const rc = RC(); if (!rc) return;
      const st = rc.state() || {};
      const o = txOwner();
      const now = Date.now();
      if (st.cw_tx) {                                // a CW message is on the air: this tap is a STOP tap
        keepStopRole(now);
        if (mineTx() || !o) { disarm(); rc.send({ action: "cw_stop" }); return; }   // ours (or nobody's)
        if (!stopArm) return;
        const r = stopArm.tap(o);                    // "arm" | "fire" (same owner still sending) | "none"
        if (r === "fire") rc.send({ action: "tx_stop" });
        return;
      }
      if (armed() || inStopRole(now)) {              // the CW being stopped ended: this tap sends nothing
        disarm();
        keepStopRole(now);                           // a further tap of the same burst only stops, too
        const msg = "nothing sent — that CW message already ended";
        setTxHint(msg);
        setTimeout(() => { if (txHint && txHint.textContent === msg) setTxHint(""); }, STOP_GUARD_MS);
        syncTx();
        return;
      }
      if (o && !mineTx()) { setTxHint("busy — " + ownerName(o) + " is transmitting"); return; }
      if (!st.connected) { setTxHint("connect a radio to transmit"); return; }
      if (!st.cw_tx_ready) { setTxHint("CW key port not found — check the radio's 2nd USB (Standard) COM port"); return; }
      // any CW mode: Icom CW / CW-R, Yaesu CW-USB / CW-LSB
      const m = st.mode_name || "";
      if (!m.startsWith("CW")) { setTxHint("set the radio to a CW mode to transmit"); return; }
      const text = $("cwSend").value;
      if (!text.trim()) { setTxHint("type a message first"); return; }
      const wpm = Math.max(5, Math.min(40, +$("cwWpmSet").value || 18));
      setTxHint("");
      addTx(text);                                  // show our sent message as a blue bubble
      const t = TX();                               // the claim carries a seq; heartbeats start with it
      rc.send({ action: "cw_tx", text, wpm, seq: t ? t.nextSeq() : undefined });
    });
  }
  // busy / denied answers to our cw_tx (forwarded by app.js as DOM events)
  document.addEventListener("tr:busy", (e) => {
    const m = e.detail || {};
    if (m.action === "cw_tx" || m.action === "cw_stop") {
      setTxHint("busy — " + (m.owner === "FRONT PANEL" ? "the radio's front panel" : (m.owner || "someone")) + " is transmitting");
    }
  });
  document.addEventListener("tr:denied", (e) => {
    const m = e.detail || {};
    if (m.action !== "cw_tx") return;
    setTxHint({ role: "not permitted for your role", not_connected: "connect a radio to transmit",
                unsupported: "CW TX is not available now (mode / key port)", invalid: "nothing to send after filtering",
                connecting: "the radio is switching — try again" }[m.reason] || ("refused: " + m.reason));
  });
  // keep the TX button in step with the live radio state (shown only when the
  // connected radio supports CW message TX; reflects the transmitting state)
  function syncTx() {
    if (!txBtn) return;
    const st = (RC() && RC().state()) || {};
    const supported = !!st.has_cw_tx;
    txBtn.hidden = !supported;
    if (!supported) { disarm(); setTxHint(""); return; }
    const tx = !!st.cw_tx;
    const ready = !!st.cw_tx_ready;
    const o = txOwner(), other = o && !mineTx() ? o : null;
    const now = Date.now();
    if (tx) keepStopRole(now);                      // the button is a STOP control now (and just after)
    const guard = !tx && inStopRole(now);
    if (stopArm) stopArm.check(o);                  // the armed owner gone (a missed tx event) -> disarm
    if (!tx && armed()) disarm();
    txBtn.textContent = tx ? "■" : "TX";
    txBtn.classList.toggle("on", tx);
    // someone else holds TX (not CW): nothing to send and nothing to stop here (top-bar STOP does that).
    // Right after a CW message (stopped or ended) the button stays inert for the STOP guard window.
    txBtn.disabled = tx ? false : (guard || !!other || !st.connected || !ready);
    txBtn.title = tx ? (other ? "Tap twice to stop " + ownerName(other) : "Stop transmitting")
      : guard ? "The CW message just ended — TX is available again in a moment"
      : other ? ownerName(other) + " is transmitting"
      : !st.connected ? "Connect a radio to transmit"
      : !ready ? "CW key port not found"
      : "Transmit this message as CW on the radio";
    if (armed()) return;                            // keep "Tap again to stop …" visible while armed
    const h = txHint ? txHint.textContent : "";
    if (tx) setTxHint(other ? ownerName(other) + " is sending CW" : "on air — sending CW");
    else if (st.connected && !ready) setTxHint("CW key port not found — check the radio's 2nd USB (Standard) COM port");
    // the CW message ended: its "… sending CW" hint goes, even while the same or another user still
    // holds a non-CW claim (PTT, VOX, TUNE). A "busy — …" hint stays while someone else transmits.
    else if (/^on air — sending CW$| is sending CW$/.test(h)) setTxHint("");
    else if (!other && (/^busy — /.test(h) || h.indexOf("CW key port") === 0)) setTxHint("");
  }
  setInterval(syncTx, 400);
  syncTx();
  showStatus();                                    // before CW first comes into view: the decoder is off

  // the Modem window (digital.js): busy() = this tab's own CW message is on the air (the window stays on
  // CW, its STOP in view); summary() = the collapsed window's line while CW is its mode
  const sendingCw = () => !!((RC() && RC().state()) || {}).cw_tx && mineTx();
  window.CwPanel = {
    busy: sendingCw,
    summary: () => {
      if (sendingCw()) return "CW on the air";
      if (!open) return "CW decoder off";
      for (let i = messages.length - 1; i >= 0; i--) {
        const m = messages[i];
        if (m.dir === "rx" && m.text) return "CW " + m.text.replace(/\s+/g, " ").trim().slice(-18).trim();
      }
      return "CW decoder on";
    },
    // the chat as text, one message a line: its time, RX or TX, the text (what is still being decoded is left out)
    text: () => messages.filter((m) => m.text).map((m) => m.t + " " + (m.dir === "tx" ? "TX" : "RX") + "  " + m.text).join("\n"),
  };

  // diagnostic: window.__cwdiag() -> live decode internals (peak / dominant Hz / raw text)
  window.__cwdiag = function () {
    let domF = 0, domE = -1;
    const N = Math.min(pendingLen, SR);             // last ~1 s of the pending buffer
    if (N > 128) {
      const start = pendingLen - N;
      for (let f = 250; f <= 1500; f += 25) {       // coarse Goertzel scan for the dominant tone
        const coeff = 2 * Math.cos(2 * Math.PI * f / SR);
        let s1 = 0, s2 = 0;
        for (let i = 0; i < N; i++) { const s0 = buf[start + i] + coeff * s1 - s2; s2 = s1; s1 = s0; }
        const e = s1 * s1 + s2 * s2 - coeff * s1 * s2;
        if (e > domE) { domE = e; domF = f; }
      }
    }
    return { peak: +lastPeak.toFixed(4), pendingLen, sec: +(pendingLen / SR).toFixed(2),
             domHz: domF, inBand: domF >= 400 && domF <= 1200,
             pending: pendingText, committed: messages.map((m) => m.text).join(" ").slice(-48), ready: workerReady };
  };
})();
