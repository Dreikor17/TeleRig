/* TeleRig SSTV transmit (the SSTV panel's Send tab): one picture sent as slow-scan TV through the radio,
 * operator-triggered and bounded, on the same rules as PTT.
 *
 * One click on Send is one transmission of one picture, never more: nothing here repeats, answers,
 * schedules or retries a transmission. The audio is made in the page before anything is keyed (the
 * encoder below, from the mode specifications like tests/sstv_encode.js), then:
 *   1. the mic's path is taken (RadioAudio.txBegin: the server's mic flag on, the client mic muted) and
 *      the server's "ready" awaited (a serial radio's host TX device opens first);
 *   2. the ptt claim goes to the server's TX arbiter through RadioControl.send, so app.js's claim rules
 *      apply (a seq, heartbeats, never from a hidden page); busy or denied ends it there. The claim names
 *      the mode (sstv: id), so the server holds this one keying for the picture's own length (its time
 *      on the air + 5 s, backend/sstv.py) instead of a voice over's 120 s;
 *   3. once granted, the audio streams in real time, LEAD ahead, as the mic's 16 kHz frames; the server
 *      forwards them only while this tab owns TX with ptt (I10);
 *   4. after the last sample has played out (a silent tail), ptt is released and the path given back.
 * The same button is STOP from the first step on. Losing the claim (the picture's limit, another user's
 * STOP, a hidden tab, a dropped connection, a radio change) or closing the panel stops the audio at once.
 * Only modes whose whole transmission ends inside the radio's own 3-minute TX time-out, set on connect as
 * the backstop if the control link fails, are offered (MAX_ON_AIR_S; state.ptt_hold_max for the radio
 * in use). Nothing is sent by a viewer, for a radio without PTT, or in a mode that transmits no audio
 * (CW, RTTY, DV, C4FM).
 *
 * The picture never leaves the page except as the radio's audio. Nothing here parses text as HTML.
 *
 * window.SstvTx = { summary(), modes(), onAirSeconds(mode), encode(mode, imageData, level) }
 */
(function () {
  "use strict";
  const S = window.SSTV;
  if (!S || !Array.isArray(S.MODES)) return;

  // ---- the encoder: phase-continuous tones at 16 kHz, the VIS header first ----
  const FS = 16000;
  const SETTLE_MS = 300;                 // silence after the key, before the VIS: the radio settles on TX
  const TAIL_MS = 600;                   // silence after the picture: the last line plays out before unkey
  const FIT_MARGIN_S = 3;                // a mode fits when it ends this far inside the radio's longest keying (backend/sstv.py)
  const MAX_ON_AIR_S = 177 - FIT_MARGIN_S;   // the longest keying: 3 s inside the radios' 3-minute TOT
  const clamp255 = (v) => (v < 0 ? 0 : v > 255 ? 255 : v);
  const ycc = (p) => [0.299 * p[0] + 0.587 * p[1] + 0.114 * p[2],               // Y, Cb, Cr
                      128 - 0.168736 * p[0] - 0.331264 * p[1] + 0.5 * p[2],
                      128 + 0.5 * p[0] - 0.418688 * p[1] - 0.081312 * p[2]];
  const tone = (ms, f) => [ms, () => f];
  const scan = (ms, w, val) => [ms, (t) => 1500 + (clamp255(val(Math.min(w - 1, Math.floor(t * w)))) / 255) * 800];
  function vis(code) {
    const s = [tone(300, 1900), tone(10, 1200), tone(300, 1900), tone(30, 1200)];
    let ones = 0;
    for (let b = 0; b < 7; b++) { const bit = (code >> b) & 1; ones += bit; s.push(tone(30, bit ? 1100 : 1300)); }
    s.push(tone(30, ones % 2 ? 1100 : 1300));             // even parity
    s.push(tone(30, 1200));
    return s;
  }
  // the picture's segments; px(x, y) -> [r, g, b]
  function picture(m, px) {
    const W = m.width, s = [], ch = (y, i) => (x) => px(x, y)[i];
    if (m.color === "GBR" && m.syncMs < 5) {                                   // Martin
      for (let y = 0; y < m.height; y++) {
        s.push(tone(4.862, 1200), tone(0.572, 1500));
        for (const i of [1, 2, 0]) s.push(scan(m.chans[0].d, W, ch(y, i)), tone(0.572, 1500));
      }
    } else if (m.color === "GBR") {                                            // Scottie
      s.push(tone(9, 1200));
      const d = m.chans[0].d;
      for (let y = 0; y < m.height; y++) {
        s.push(tone(1.5, 1500), scan(d, W, ch(y, 1)), tone(1.5, 1500), scan(d, W, ch(y, 2)), tone(9, 1200),
               tone(1.5, 1500), scan(d, W, ch(y, 0)));
      }
    } else if (m.color === "R36") {
      for (let y = 0; y < m.height; y++) {
        const even = y % 2 === 0, yy = even ? y : y - 1;
        const Y = (x) => ycc(px(x, y))[0];
        const C = (x) => { const a = ycc(px(2 * x, yy)), b = ycc(px(2 * x, Math.min(m.height - 1, yy + 1)));
                           return even ? (a[2] + b[2]) / 2 : (a[1] + b[1]) / 2; };
        s.push(tone(9, 1200), tone(3, 1500), scan(88, W, Y), tone(4.5, even ? 1500 : 2300), tone(1.5, 1900), scan(44, W >> 1, C));
      }
    } else if (m.color === "YCC") {                                            // Robot 72
      for (let y = 0; y < m.height; y++) {
        const c = (i) => (x) => ycc(px(x, y))[i];
        s.push(tone(9, 1200), tone(3, 1500), scan(138, W, c(0)), tone(4.5, 1500), tone(1.5, 1900), scan(69, W, c(2)),
               tone(4.5, 2300), tone(1.5, 1900), scan(69, W, c(1)));
      }
    } else {                                                                   // PD
      const d = m.chans[0].d;
      for (let y = 0; y < m.height; y += 2) {
        const a = (x) => ycc(px(x, y)), b = (x) => ycc(px(x, y + 1));
        s.push(tone(20, 1200), tone(2.08, 1500), scan(d, W, (x) => a(x)[0]), scan(d, W, (x) => (a(x)[2] + b(x)[2]) / 2),
               scan(d, W, (x) => (a(x)[1] + b(x)[1]) / 2), scan(d, W, (x) => b(x)[0]));
      }
    }
    return s;
  }
  function segments(m, px) { return vis(m.vis).concat(picture(m, px)); }
  // the whole transmission: SETTLE_MS of silence, the tones, TAIL_MS of silence; level 0..1 of full scale
  function synth(segs, level) {
    let total = 0;
    for (const [ms] of segs) total += ms;
    const lead = Math.round((SETTLE_MS / 1000) * FS), n = lead + Math.ceil((total / 1000) * FS) + Math.round((TAIL_MS / 1000) * FS);
    const out = new Int16Array(n), amp = Math.max(0, Math.min(1, level));
    let ph = 0, si = 0, segStart = 0;
    for (let i = lead; i < n; i++) {
      const tMs = ((i - lead) / FS) * 1000;
      while (si < segs.length && tMs >= segStart + segs[si][0]) { segStart += segs[si][0]; si++; }
      if (si >= segs.length) break;                      // the tail: silence
      ph += (2 * Math.PI * segs[si][1]((tMs - segStart) / segs[si][0])) / FS;
      out[i] = Math.max(-32767, Math.min(32767, Math.round(amp * Math.sin(ph) * 32767)));
    }
    return out;
  }
  function encode(m, img, level) {
    const W = img.width, d = img.data;
    const px = (x, y) => { const i = (y * W + x) * 4; return [d[i], d[i + 1], d[i + 2]]; };
    return synth(segments(m, px), level);
  }
  const flat = () => [128, 128, 128];
  const SECS = new Map(S.MODES.map((m) => {
    let ms = SETTLE_MS + TAIL_MS;
    for (const [d] of segments(m, flat)) ms += d;
    return [m.id, ms / 1000];
  }));
  const onAirSeconds = (m) => SECS.get(m.id) || Infinity;
  const TX_MODES = S.MODES.filter((m) => onAirSeconds(m) <= MAX_ON_AIR_S);
  const fmtS = (s) => { s = Math.max(0, Math.ceil(s)); return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0"); };
  const fmtLeft = fmtS;

  window.SstvTx = { summary: () => "", modes: () => TX_MODES.slice(), onAirSeconds, encode };

  // ---- the panel (browser only) ----
  if (typeof document === "undefined") return;
  const $ = (id) => document.getElementById(id);
  const tabRx = $("sstvTabRx"), tabTx = $("sstvTabTx"), paneRx = $("sstvRx"), paneTx = $("sstvTx");
  const modeSel = $("sstvTxMode"), fileIn = $("sstvTxFile"), cardBtn = $("sstvTxCard"), textIn = $("sstvTxText");
  const canvas = $("sstvTxCanvas"), levelIn = $("sstvTxLevel"), levelVal = $("sstvTxLevelVal");
  const sendBtn = $("sstvTxSend"), statusEl = $("sstvTxStatus");
  if (!paneTx || !canvas || !sendBtn || !modeSel) return;
  const g2d = canvas.getContext ? canvas.getContext("2d") : null;
  const tile = canvas.closest ? canvas.closest(".grid-stack-item") : null;
  const KEY_MODE = "telerig.sstv.tx.mode", KEY_TEXT = "telerig.sstv.tx.text", KEY_LEVEL = "telerig.sstv.tx.level";
  const LEAD = Math.round(0.12 * FS);    // stream this far ahead of real time: timer jitter never starves the radio
  const CHUNK = 1024;                    // samples per binary frame (2 KB; the server takes up to 32 KB)
  const MAX_BURST = FS / 2;              // at most 0.5 s of audio per tick, whatever happens
  const STALL_SKIP = FS / 4;             // further behind than 0.25 s (the page stalled): skip to now, see tick()
  const TICK_MS = 20;
  const READY_MS = 5000, CLAIM_MS = 3500, END_MARGIN_MS = 100;
  const STOP_GUARD_MS = 3000;            // after STOP or a picture's end, a click starts nothing for this long
  const AUDIO_MODES = /^(USB|LSB|FM|AM|DATA-USB|DATA-LSB|DATA-FM)$/;

  const ls = (k, v) => { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (_) {} return null; };
  const ctl = () => window.RadioControl || null;
  const audio = () => window.RadioAudio || null;
  const tx = () => window.RadioTx || null;
  const TRo = () => window.TR || null;
  const state = () => { const c = ctl(); try { return (c && c.state()) || {}; } catch (_) { return {}; } };
  const say = (m) => { try { TRo().srLive(m); } catch (_) {} };
  const can = () => { const t = TRo(); try { return !!(t && t.can && t.can("tx")); } catch (_) { return false; } };
  const onGrid = () => { const L = window.RadioLayout; try { return !L || typeof L.visible !== "function" || L.visible("sstv"); } catch (_) { return true; } };
  const owned = () => { const t = tx(); try { return !!(t && t.ownedPtt && t.ownedPtt()); } catch (_) { return false; } };
  function otherOwner() {
    const t = tx(), T = TRo();
    let o = null;
    try { o = t && t.owner ? t.owner() : null; } catch (_) { o = null; }
    if (!o) return null;
    try { if (T && T.isTxOwner && T.isTxOwner()) return null; } catch (_) {}
    return o;
  }
  const ownerName = (o) => (o.external ? "the radio's front panel" : (o.user || "someone"));
  function profile() {
    const st = state(), c = ctl();
    let list = [];
    try { list = (c && c.radios && c.radios()) || []; } catch (_) { list = []; }
    return list.find((p) => p && p.id === st.radio) || null;
  }
  function canPtt() {
    const p = profile(), cap = (p && p.capabilities) || {};
    return "ptt" in cap ? !!cap.ptt : true;
  }

  // ---- the tabs: Receive (sstv.js) and Send ----
  function showTab(which) {
    const send = which === "tx";
    if (tabRx) { tabRx.classList.toggle("on", !send); tabRx.setAttribute("aria-selected", send ? "false" : "true"); }
    if (tabTx) { tabTx.classList.toggle("on", send); tabTx.setAttribute("aria-selected", send ? "true" : "false"); }
    if (paneRx) paneRx.hidden = send;
    paneTx.hidden = !send;
    render();
  }
  if (tabRx) tabRx.addEventListener("click", () => showTab("rx"));
  if (tabTx) tabTx.addEventListener("click", () => showTab("tx"));

  // ---- the mode menu: only what fits inside the radio's own time-out ----
  for (const m of TX_MODES) {
    const o = document.createElement("option");
    o.value = m.id;
    o.textContent = m.name + " · " + fmtS(onAirSeconds(m));
    modeSel.appendChild(o);
  }
  const saved = ls(KEY_MODE);
  modeSel.value = TX_MODES.some((m) => m.id === saved) ? saved : (TX_MODES.some((m) => m.id === "s1") ? "s1" : TX_MODES[0].id);
  const txMode = () => TX_MODES.find((m) => m.id === modeSel.value) || TX_MODES[0];
  modeSel.addEventListener("change", () => { ls(KEY_MODE, modeSel.value); paint(); });

  // ---- the picture: a chosen image (cropped to fill the mode's frame), or the test card; the text line ----
  let source = null;                     // null | "card" | an ImageBitmap / HTMLImageElement
  let img = null;                        // the ImageData to send (the preview's pixels)
  if (textIn) {
    textIn.value = ls(KEY_TEXT) || "";
    textIn.addEventListener("input", () => { ls(KEY_TEXT, textIn.value); paint(); });
  }
  if (levelIn) {
    const v = +ls(KEY_LEVEL);
    if (v >= 10 && v <= 100) levelIn.value = String(v);
    const show = () => { if (levelVal) levelVal.textContent = levelIn.value + "%"; };
    levelIn.addEventListener("input", () => { ls(KEY_LEVEL, levelIn.value); show(); });
    show();
  }
  const level = () => Math.max(10, Math.min(100, +(levelIn && levelIn.value) || 50)) / 100;
  function drawCard(W, H) {
    const bars = ["#ffffff", "#ffff00", "#00ffff", "#00ff00", "#ff00ff", "#ff0000", "#0000ff", "#000000"];
    const bh = Math.round(H * 0.6);
    for (let i = 0; i < 8; i++) { g2d.fillStyle = bars[i]; g2d.fillRect(Math.round((i * W) / 8), 0, Math.ceil(W / 8), bh); }
    const ramp = g2d.createLinearGradient ? g2d.createLinearGradient(0, 0, W, 0) : null;
    if (ramp) { ramp.addColorStop(0, "#000000"); ramp.addColorStop(1, "#ffffff"); }
    g2d.fillStyle = ramp || "#808080";
    g2d.fillRect(0, bh, W, H - bh);
  }
  function drawText(W, H) {
    const text = ((textIn && textIn.value) || "").trim();
    if (!text) return;
    const band = Math.round(H * 0.2);
    g2d.fillStyle = "rgba(0,0,0,0.62)";
    g2d.fillRect(0, H - band, W, band);
    let size = Math.round(band * 0.68);
    const font = (s) => "700 " + s + 'px "Exo 2", system-ui, sans-serif';
    g2d.font = font(size);
    while (size > 8 && g2d.measureText && g2d.measureText(text).width > W - 16) g2d.font = font(--size);
    g2d.textAlign = "center"; g2d.textBaseline = "middle";
    g2d.lineWidth = Math.max(2, Math.round(size / 8)); g2d.strokeStyle = "#000000"; g2d.fillStyle = "#ffffff";
    if (g2d.strokeText) g2d.strokeText(text, W / 2, H - band / 2);
    g2d.fillText(text, W / 2, H - band / 2);
  }
  function paint() {
    const m = txMode();
    if (canvas.width !== m.width) canvas.width = m.width;
    if (canvas.height !== m.height) canvas.height = m.height;
    img = null;
    if (!g2d) { render(); return; }
    g2d.fillStyle = "#000000";
    g2d.fillRect(0, 0, m.width, m.height);
    if (source === "card") drawCard(m.width, m.height);
    else if (source) {
      const s = Math.max(m.width / source.width, m.height / source.height), w = source.width * s, h = source.height * s;
      g2d.drawImage(source, (m.width - w) / 2, (m.height - h) / 2, w, h);   // cropped to fill, centred
    }
    if (source) {
      drawText(m.width, m.height);
      try { img = g2d.getImageData(0, 0, m.width, m.height); } catch (_) { img = null; }
    }
    render();
  }
  async function useFile(file) {
    if (!file || !/^image\//.test(file.type || "")) { note = "That isn't a picture."; render(); return; }
    if (phase !== "idle") return;                        // never swap the picture while it is going out
    try {
      source = typeof createImageBitmap === "function" ? await createImageBitmap(file) : await loadImg(file);
      note = "";
    } catch (_) { source = null; note = "That picture could not be opened."; }
    paint();
  }
  function loadImg(file) {
    return new Promise((res, rej) => {
      const u = URL.createObjectURL(file), im = new Image();
      im.onload = () => { URL.revokeObjectURL(u); res(im); };
      im.onerror = () => { URL.revokeObjectURL(u); rej(new Error("bad image")); };
      im.src = u;
    });
  }
  if (fileIn) fileIn.addEventListener("change", () => { const f = fileIn.files && fileIn.files[0]; if (f) useFile(f); fileIn.value = ""; });
  if (cardBtn) cardBtn.addEventListener("click", () => { if (phase !== "idle") return; source = "card"; note = ""; paint(); });
  paneTx.addEventListener("dragover", (e) => { e.preventDefault(); });
  paneTx.addEventListener("drop", (e) => {
    e.preventDefault();
    const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    if (f) useFile(f);
  });
  document.addEventListener("paste", (e) => {
    if (paneTx.hidden || !onGrid()) return;
    const t = e.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA")) return;   // text pastes stay text
    const items = (e.clipboardData && e.clipboardData.files) || [];
    const f = [...items].find((x) => /^image\//.test(x.type || ""));
    if (f) { e.preventDefault(); useFile(f); }
  });

  // ---- sending ----
  let phase = "idle";                    // idle | ready (the TX path opening) | claim | air
  let job = null;                        // { m, pcm, sent, t0, timer }
  let note = "";                         // the last outcome, shown while idle
  let stopRoleUntil = 0;
  const keepStopRole = () => {
    stopRoleUntil = Math.max(stopRoleUntil, Date.now() + STOP_GUARD_MS);
    setTimeout(() => { if (phase === "idle") render(); }, STOP_GUARD_MS + 20);   // Send comes back on time
  };
  function blocked() {
    const st = state();
    if (!can()) return "Only operators can send pictures.";
    if (!st.connected) return "Connect a radio to send.";
    if (!canPtt()) return "This radio can't be keyed from TeleRig.";
    if (!AUDIO_MODES.test(st.mode_name || "")) return "Set the radio to USB, LSB, FM or a data mode to send SSTV.";
    const o = otherOwner();
    if (o) return "Busy: " + ownerName(o) + " is transmitting.";
    if (tx() && tx().ownedOrPending && tx().ownedOrPending()) return "This tab is transmitting already.";
    if (onAirSeconds(txMode()) > (+st.ptt_hold_max || 120) - FIT_MARGIN_S) return "This mode is longer than the radio's TX limit: choose a shorter one.";
    if (!img) return "Choose a picture, drop or paste one here, or use the test card.";
    return "";
  }
  function clearTimer() { if (job && job.timer) { clearTimeout(job.timer); clearInterval(job.timer); job.timer = 0; } }
  function releasePtt() {
    const t = tx(), c = ctl();
    if (!c) return;
    if (owned()) c.send({ action: "ptt", tx: false });
    else if (t && t.ownedOrPending && t.ownedOrPending()) c.send({ action: "ptt", tx: false, auto: true });   // a claim still pending
  }
  function end(why) {                    // every way out: back to idle, the path given back
    clearTimer();
    const A = audio();
    if (A && A.txEnd) A.txEnd();
    phase = "idle"; job = null; note = why || "";
    keepStopRole();
    if (why) say(why);
    render();
  }
  function start() {
    const why = blocked();
    if (why) { note = why; render(); return; }
    const A = audio(), m = txMode();
    if (!A || !A.txBegin || !A.txPush || !A.txEnd || !ctl()) { note = "Sending needs the radio's audio path, which is not available here."; render(); return; }
    const pcm = encode(m, img, level());                  // all of it first: nothing on the air can wait for this
    if (!A.txBegin()) { note = "The radio's TX audio is busy in this tab."; render(); return; }
    job = { m, pcm, sent: 0, t0: 0, timer: 0 };
    phase = "ready";
    job.timer = setTimeout(() => { if (phase === "ready") end("Nothing sent: the radio's TX audio did not open."); }, READY_MS);
    note = "";
    render();
  }
  function claim() {
    clearTimer();
    phase = "claim";
    const sent = ctl().send({ action: "ptt", tx: true, sstv: job.m.id });   // the only claim this file ever sends
    if (!sent) { end("Nothing sent: the claim could not be sent (is the tab in the background?)."); return; }
    job.timer = setTimeout(() => { if (phase === "claim") { releasePtt(); end("Nothing sent: no answer from the server."); } }, CLAIM_MS);
    render();
  }
  function goAir() {
    clearTimer();
    phase = "air";
    job.t0 = performance.now();
    job.sent = 0;
    say("Sending " + job.m.name);
    job.timer = setInterval(tick, TICK_MS);
    tick();
  }
  function tick() {
    if (phase !== "air" || !job) return;
    if (!owned()) { end("Stopped: the transmission ended before the picture did."); return; }
    if (!onGrid()) { releasePtt(); end("Stopped: the SSTV panel was closed."); return; }
    const A = audio(), total = job.pcm.length, el = performance.now() - job.t0;
    const now16 = Math.floor((el * FS) / 1000), due = Math.min(total, now16 + LEAD);
    // A stalled page (a busy main thread) can't make up the lost time: the radio has already played
    // silence for it, and sending it late at speed would only be dropped (the server's 64 KB/s, the
    // radio's buffer). Skip what is past, so the rest of the picture stays in place and ends on time.
    if (now16 - job.sent > STALL_SKIP) job.sent = Math.min(total, now16);
    let n = 0;
    while (job.sent < due && n < MAX_BURST) {
      const k = Math.min(CHUNK, due - job.sent, MAX_BURST - n);
      A.txPush(job.pcm.subarray(job.sent, job.sent + k));
      job.sent += k; n += k;
    }
    if (job.sent >= total && el >= (total * 1000) / FS + END_MARGIN_MS) {
      const name = job.m.name;
      releasePtt();
      end("Sent " + name + ".");
      return;
    }
    render();
  }
  function stop(why) {
    if (phase === "claim" || phase === "air") releasePtt();
    end(why || "Stopped.");
  }
  sendBtn.addEventListener("click", () => {
    if (phase !== "idle") { stop("Stopped."); return; }  // the button is STOP while anything is under way
    if (Date.now() < stopRoleUntil) { keepStopRole(); render(); return; }   // the tail of a STOP double-click
    start();
  });
  document.addEventListener("tr:mic", (e) => {
    const m = (e && e.detail) || {};
    if (phase === "ready" && m.ready) claim();
  });
  document.addEventListener("tr:error", (e) => {
    const code = String(((e && e.detail) || {}).code || "");
    if (phase === "ready" && (code.indexOf("tx_") === 0)) end("Nothing sent: " + (((e.detail || {}).message) || "the radio's TX audio could not open."));
  });
  document.addEventListener("tr:tx", () => {
    if (phase === "claim" && owned()) { goAir(); return; }
    if (phase === "air" && !owned()) { end("Stopped: the transmission ended before the picture did."); return; }
    render();
  });
  // our claim refused: busy (someone else holds TX) or denied (role, no radio, ...); nothing was keyed
  document.addEventListener("tr:busy", (e) => {
    const m = (e && e.detail) || {};
    if (phase !== "claim" || m.action !== "ptt") return;
    end("Nothing sent: " + (m.owner === "FRONT PANEL" ? "the radio's front panel" : (m.owner || "someone")) + " is transmitting.");
  });
  document.addEventListener("tr:denied", (e) => {
    const m = (e && e.detail) || {};
    if (phase !== "claim" || m.action !== "ptt") return;
    end("Nothing sent: the server refused (" + (m.reason || "denied") + ").");
  });

  // ---- what the panel shows ----
  function render() {
    const m = job ? job.m : txMode();                    // under way: the mode being sent
    if (phase === "air" && job) {
      const left = (job.pcm.length / FS) - (performance.now() - job.t0) / 1000;
      const lineMs = m.lineMs, el = (performance.now() - job.t0) - SETTLE_MS;
      const rows = m.rowsPerLine || 1, line = Math.max(0, Math.min(m.height, Math.floor((el - 910) / lineMs) * rows));
      sendBtn.textContent = "STOP";
      setStatus("On the air: " + m.name + ", line " + line + " of " + m.height + ", " + fmtLeft(left) + " left.");
    } else if (phase === "ready" || phase === "claim") {
      sendBtn.textContent = "STOP";
      setStatus(phase === "ready" ? "Opening the radio's TX audio…" : "Asking to transmit…");
    } else {
      sendBtn.textContent = "Send · " + fmtS(onAirSeconds(m));
      const why = blocked();
      setStatus(note || why || "Ready. Watch the radio's ALC while it sends.");
    }
    const busy = phase !== "idle";
    sendBtn.classList.toggle("on", phase === "air");
    sendBtn.disabled = !busy && (!!blocked() || Date.now() < stopRoleUntil);
    modeSel.disabled = busy;
    if (fileIn) fileIn.disabled = busy;
    if (cardBtn) cardBtn.disabled = busy;
    if (textIn) textIn.disabled = busy;
    if (levelIn) levelIn.disabled = busy;
    if (tile) tile.classList.toggle("sstv-on-air", phase === "air");
  }
  function setStatus(t) { if (statusEl && statusEl.textContent !== t) statusEl.textContent = t; }

  window.SstvTx.summary = () => (phase === "air" && job
    ? "TX " + job.m.name + " " + fmtLeft(job.pcm.length / FS - (performance.now() - job.t0) / 1000) : "");
  setInterval(() => { if (phase === "idle") render(); }, 1000);   // the radio's state changes the hints
  paint();
})();
