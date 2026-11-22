/* TeleRig Modem window (the "sstv" tile, titled Modem; its id stays "sstv" so saved layouts keep it): CW
 * (cwtool.js: the neural decoder, the sidetone, CW TX), RTTY (rtty.js), FT8 (ft8.js, ft8-decode.js) and SSTV
 * (sstv.js receive, sstv-tx.js send). One mode is shown at a time, but every decoder listens while the
 * window is on the grid (CW's while its Decode key is on), so switching never loses a picture, a line of
 * text or a slot; a mode that has news while hidden gets a dot on its key. Each mode's bar sits right under
 * the mode keys: no audio strip and no "Tune to" menu (the operator asked for neither, 2026-11-22): the
 * scope shows the audio, the Spots panel's CALLING view tunes.
 *
 * Copy (CW, RTTY, FT8) puts the mode's text on the clipboard: CwPanel / RttyPanel / Ft8Panel .text().
 *
 * The operator's callsign (DigitalPanel.call()): typed once into CW's "Your call" field and kept by this
 * browser; until then the account's own name when it reads as a callsign. CW's quick keys write it into
 * messages, and FT8 marks the messages that name it. tr:call {call} when it changes.
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
 * Every mode decodes the radio's audio, so while RX is off the window says so under its keys, with a key
 * that turns RX on (the Audio panel's RX key, pressed for the operator).
 *
 * The text screens (CW's chat, RTTY's text, FT8's list) scroll inside the window. At the end a screen
 * follows what comes in (cwtool.js, rtty.js and ft8.js scroll it only then); scrolled back it stays where
 * it was left, and it comes back to that place, or to the end it was following, when it is shown again.
 *
 * window.DigitalPanel = { mode(), show(id), call(), feed(pcm, rate), summary() }
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
    renderRx();
    renderFil();
    announce();
  }

  // ---- receive audio: every mode decodes the radio's audio, so the window says when RX is off and
  // offers its key: the Audio panel's own RX key, pressed for the operator (its rules, its errors) ----
  const rxBar = $("dmRx"), rxKey = $("dmRxOn");
  const rxOn = () => { const A = window.RadioAudio; try { return !!(A && A.on()); } catch (_) { return false; } };
  function renderRx() { if (rxBar) rxBar.hidden = rxOn(); }
  if (rxKey) rxKey.addEventListener("click", () => {
    const b = $("audioBtn");
    if (b && !rxOn()) b.click();
    renderRx();
  });

  // ---- the radio's data filter: a Yaesu in DATA-USB / DATA-LSB centers it by SET menu (waterfall.js
  // Passband.data), and with DATA MODE OTHERS the factory OTHER SHIFT (0 Hz) puts it on the carrier: a 3 kHz
  // filter then passes 0-1.5 kHz, and SSTV's header and picture, RTTY's tones and FT8 above that are cut
  // before TeleRig hears them (a 14.230 MHz picture, its syncs plain on the scope, never decoded). While a
  // mode is shown whose tones it cuts, or the dial reads off the carrier (OTHER DISP), the window says so
  // under its keys, with a key that mends it (the WIDTH to 3 kHz, the center to 1500 Hz, the dial on the
  // carrier: the least that does it). An operator's key (operate + menu), never a viewer's, and not while
  // this page sends. The key asks app.js (tr:datafix {need, label}), which sends to the radio what
  // Passband.dataFix finds from its own state: this window itself never sends to the radio. ----
  const NEED = { rtty: [2050, 2370, "RTTY needs 2125 and 2295 Hz"], ft8: [300, 2500, "FT8 needs 0.3 to 2.5 kHz"],
                 sstv: [1050, 2350, "SSTV needs 1.1 to 2.3 kHz"] };
  const filBar = $("dmFil"), filText = $("dmFilText"), filKey = $("dmFilFix");
  const kHz = (hz) => String(+(Math.max(0, hz) / 1000).toFixed(2));
  const can = (p) => { try { return !!(window.TR && TR.can(p)); } catch (_) { return false; } };
  function radioNow() {                                    // the connected radio's state and profile, or null
    const C = window.RadioControl;
    let st = null, rs = [];
    try { st = C && C.state(); rs = (C && typeof C.radios === "function" && C.radios()) || []; } catch (_) { return null; }
    if (!st || !st.connected) return null;
    const p = rs.find((r) => r.id === st.radio) || null;
    return p ? { st, p } : null;
  }
  function menuName(p, num) {                              // "065 OTHER SHIFT (SSB)", "08-04 OTHER SHIFT"
    const it = (p.menu || []).find((m) => m.num === num);
    const n = it && it.ex_width === 4 ? String(Math.floor(num / 100)).padStart(2, "0") + "-" + String(num % 100).padStart(2, "0")
      : String(num).padStart(3, "0");
    return n + (it ? " " + it.name : "");
  }
  let fil = null;                                          // what the bar shows: {cmds, label, need}
  function renderFil() {
    if (!filBar) return;
    const r = radioNow(), need = NEED[cur], P = window.Passband;
    const f = r && need && P && typeof P.dataFix === "function" ? P.dataFix(r.p, r.st, need) : null;
    filBar.hidden = !f;
    if (!f) { fil = null; return; }
    const b = f.band, M = r.p.data_menus || {};
    const why = b.others ? menuName(r.p, M.shift) + " " + (b.center > 0 ? "+" : "") + b.center + " Hz"
      : menuName(r.p, M.tone) + " " + b.center + " Hz";
    let t = "";
    if (f.cut) t = "The radio's data filter passes " + kHz(b.lo) + "–" + kHz(b.hi) + " kHz (" + why + ", " + P.label(b.width) + " wide): " + need[2] + ".";
    if (f.off) t += (t ? " " : "") + "Its dial reads " + kHz(Math.abs(b.disp)) + " kHz off the carrier (" + menuName(r.p, M.disp) + "), so tuning lands that far off.";
    if (filText && filText.textContent !== t) filText.textContent = t;
    const centers = f.cmds.some((c) => c.action === "menu_write" && (c.num === M.shift || c.num === M.tone));
    const widens = f.cmds.some((c) => c.action === "width");
    const label = centers ? "Center it at 1.5 kHz" + (widens ? ", 3 kHz wide" : "") : widens ? "Widen it to 3 kHz" : "Dial on the carrier";
    fil = { cmds: f.cmds, label, need: [need[0], need[1]] };
    if (!filKey) return;
    if (filKey.textContent !== label) filKey.textContent = label;
    const val = (v) => (typeof v !== "number" ? v : (v > 0 ? "+" : "") + v + " Hz");
    const sets = f.cmds.map((c) => c.action === "menu_write" ? menuName(r.p, c.num) + " to " + val(c.value)
      : c.action === "width" ? "the WIDTH to 3 kHz" : "NAR off");
    filKey.title = "Sets " + sets.join(", ") + " on the radio";
    filKey.hidden = !f.cmds.length || !can("operate") || !can("menu") || !!sending();
  }
  if (filKey) filKey.addEventListener("click", () => {
    const f = fil;
    if (!f || !f.cmds.length || !can("operate") || !can("menu") || sending()) return;
    try { document.dispatchEvent(new CustomEvent("tr:datafix", { detail: { need: f.need, label: f.label } })); } catch (_) {}
  });

  function show(m) {
    const lock = sending();
    if (!MODES.includes(m) || (lock && m !== lock)) return false;
    cur = m; ls(KEY, m); render();
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

  const R = () => window.RttyPanel || null, F = () => window.Ft8Panel || null;

  // ---- Copy: a mode's text to the clipboard ----
  const toast = (msg, kind) => { try { TR.toast(msg, kind); } catch (_) {} };
  function copyOut(text, what) {
    if (!text) { toast("Nothing to copy yet.", "info"); return; }
    const done = (ok) => toast(ok ? what + " copied." : "This browser would not copy it: select the text and copy it yourself.", ok ? "ok" : "warn");
    const byHand = () => {                                 // plain http on a LAN address: no Clipboard API there
      const ta = document.createElement("textarea"), was = document.activeElement;
      let ok = false;
      ta.className = "dm-copybuf";
      ta.setAttribute("readonly", ""); ta.setAttribute("aria-hidden", "true");
      ta.value = text;
      document.body.appendChild(ta);
      try {
        ta.select();
        if (ta.setSelectionRange) ta.setSelectionRange(0, text.length);   // an iPhone selects a read-only box only this way
        ok = !!document.execCommand("copy");
      } catch (_) {}
      ta.remove();
      if (was) was.focus();                                // selecting took the focus: back where it was (the Copy key)
      done(ok);
    };
    // the Clipboard API where the page has it (https, or this computer); refused or missing, by hand
    try { navigator.clipboard.writeText(text).then(() => done(true), byHand); return; } catch (_) {}
    byHand();
  }
  for (const [id, panel, what] of [["cwCopy", () => window.CwPanel, "The CW text"], ["rttyCopy", R, "The RTTY text"], ["ft8Copy", F, "The FT8 list"]]) {
    const b = $(id);
    if (b) b.addEventListener("click", () => {
      let t = "";
      try { t = panel().text(); } catch (_) {}              // that mode isn't on this page, or has no text to give
      copyOut(t, what);
    });
  }

  // ---- the operator's callsign ----
  const KEY_CALL = "telerig.call", callEl = $("cwCall");
  const asCall = (v) => String(v || "").toUpperCase().replace(/[^A-Z0-9/]/g, "").slice(0, 12);
  const isCall = (v) => /^(?=.*[0-9])(?=.*[A-Z])[A-Z0-9/]{3,12}$/.test(v);      // letters and a digit: ADMIN is no callsign
  const keptCall = () => { const v = asCall(ls(KEY_CALL)); return isCall(v) ? v : ""; };
  function call() {
    const user = asCall(window.TR && TR.user);             // the account's name, when it reads as a callsign
    return keptCall() || (isCall(user) ? user : "");
  }
  let lastCall = null;
  function renderCall() {
    const c = call();
    if (callEl) {
      const ph = keptCall() || !c ? "Your call" : c;       // the account's name stands in until one is typed
      if (callEl.placeholder !== ph) callEl.placeholder = ph;
    }
    if (c !== lastCall) {
      lastCall = c;
      try { document.dispatchEvent(new CustomEvent("tr:call", { detail: { call: c } })); } catch (_) {}
    }
  }
  if (callEl) {
    callEl.value = keptCall();
    callEl.addEventListener("input", () => { const v = asCall(callEl.value); if (callEl.value !== v) callEl.value = v; });
    callEl.addEventListener("change", () => {
      const v = asCall(callEl.value);
      callEl.value = isCall(v) ? v : "";                   // what is no callsign is not kept: the field empties
      ls(KEY_CALL, callEl.value);
      renderCall();
    });
  }

  // set before the first render: what it announces (tr:modem, tr:call) is answered by asking this
  window.DigitalPanel = {
    mode: () => cur,
    show,
    call,
    // audio handed over directly (int16 mono, its rate) as if the radio sent it, to RTTY, FT8 and SSTV (CW
    // listens on the audio graph, not this tap): a check of the window, as SstvPanel.feed. It only decodes.
    feed: (pcm, r) => {
      if (!pcm || !pcm.length || !(r > 0)) return;
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
  render();
  renderCall();
  setInterval(() => { render(); renderCall(); }, 1000);   // the send lock, RX, the callsign
})();
