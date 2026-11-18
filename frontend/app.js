/* TeleRig — UI controller (the session layer, permissions and close codes live in tr.js) */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const TR = window.TR;                           // tr.js loads first (index.html script order)
  const Layout = () => window.RadioLayout;        // the tr.js stub until layout.js (B3) replaces it

  const scope = new Scope($("spectrum"), $("waterfall"), $("overlay"), $("scopeWrap"), $("bandStrip"));
  // the scope's colours follow the page theme (the head script set html.day / html.screen already)
  if (TR && typeof TR.theme === "function") scope.setTheme(TR.theme());
  document.addEventListener("tr:theme", (e) => scope.setTheme((e.detail && e.detail.theme) || "night"));

  let ws = null, state = {}, step = 25000;
  let blanked = false;                            // after a radio change: VFO zeroed + waterfall cleared until reconnect
  let radios = [], currentRadio = null;          // radio profiles from /api/radios
  const LEVELS = ["af", "rf", "sql", "rfpwr", "nb_level", "nr_level", "pbt1", "pbt2", "mnotch_pos",
                  "mic", "comp_level", "vox_gain", "mon_level", "cw_pitch", "keyer_speed"];

  // ---- frequency formatting (Icom dotted readout) ----
  function formatFreq(hz) {
    hz = Math.max(0, Math.round(hz));
    const mhz = Math.floor(hz / 1e6);
    const rem = hz % 1e6;
    const khz = String(Math.floor(rem / 1000)).padStart(3, "0");
    const h = String(rem % 1000).padStart(3, "0");
    return `${mhz}.${khz}.${h}`;
  }

  // ---- websocket ----
  // Close codes (4401 / 4403 / 4429 / link lost) are handled by tr.js; this only (re)opens.
  function connectWS() {
    if (TR._leaving) return;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const sock = new WebSocket(`${proto}://${location.host}/ws`);
    ws = sock;
    sock.binaryType = "arraybuffer";
    sock.onmessage = onMessage;
    sock.onclose = (e) => {
      if (ws !== sock) return;
      ws = null;
      txOnSocketClosed();
      TR.handleClose(e.code, e.reason, connectWS);
    };
  }

  // Actions a viewer may send: the heartbeat, menu reads (F23), its own RX audio, mic off. A viewer's
  // radio action would only draw `denied role`, so it is never sent at all (the server decides anyway).
  const VIEWER_OK = new Set(["tx_hb", "menu_read", "menu_read_group", "rx_audio"]);
  function viewerMay(o) { return VIEWER_OK.has(o.action) || (o.action === "mic" && o.on === false); }

  function rawSend(obj) {
    if (ws && ws.readyState === 1) { ws.send(JSON.stringify(obj)); return true; }
    return false;
  }
  function send(obj) {
    if (!obj || typeof obj.action !== "string") return false;
    if (!TR.can("operate") && !viewerMay(obj)) return false;
    const k = claimOf(obj);
    if (k) return txSendClaim(obj, k);           // TX claims: seq + heartbeats (G-1)
    if (obj.action === "ptt" && obj.tx === false) pttGuard();   // our PTT ends: re-key guard
    return rawSend(obj);
  }

  function onMessage(ev) {
    if (typeof ev.data === "string") {
      let msg = null;
      try { msg = JSON.parse(ev.data); } catch (_) { return; }
      handleFrame(msg);
      return;
    }
    const tag = new Uint8Array(ev.data, 0, 1)[0];
    if (tag === 0x53) parseScope(ev.data);        // 'S' scope sweep
    else if (tag === 0x41) playAudio(ev.data);    // 'A' RX audio
  }
  function handleFrame(msg, meta) {
    if (!msg || typeof msg !== "object") return;
    switch (msg.type) {
      case "state": updateState(msg); break;
      case "meter": updateMeter(msg); break;
      case "menu": updateMenu(msg.values || {}); break;
      case "hello": onHello(msg, meta); break;
      case "tx": onTxFrame(msg); break;
      case "tx_revoked": onTxRevoked(msg); break;
      case "busy": onBusy(msg); break;
      case "bridge": onBridgeFrame(msg); break;
      case "denied": onDenied(msg); break;
      case "error": onErrorFrame(msg); break;
      default: break;
    }
  }
  TR._onFrame = handleFrame;                      // TR._inject (sim-only) feeds frames here

  // ---- the WSJT-X bridge status (bridge.js draws it): from hello / /api/status and `bridge` frames ----
  function emitBridge() {
    document.dispatchEvent(new CustomEvent("tr:bridge", { detail: (TR.status && TR.status.bridge) || null }));
  }
  function onBridgeFrame(m) {
    if (!m.bridge || typeof m.bridge !== "object") return;
    if (!TR.status || typeof TR.status !== "object") TR.status = {};
    TR.status.bridge = m.bridge;
    emitBridge();
  }

  // ---- hello: this connection's identity, role, status and the TX owner (§2.11) ----
  function onHello(m, meta) {
    const injected = !!(meta && meta.injected);
    if (typeof m.conn === "number") TR.myConn = m.conn;
    if (typeof m.user === "string") TR.user = m.user;
    if (m.role) TR.setRole(m.role, m.perms);
    if (m.status && typeof m.status === "object") TR.status = m.status;
    if (typeof m.hb_interval === "number" && m.hb_interval > 0) hbMs = Math.max(500, Math.min(5000, m.hb_interval * 1000));
    if (!injected) TR.linkUp();
    if (m.tx && typeof m.tx === "object") {     // authoritative snapshot: nothing of ours is pending now
      pendingClaims.clear(); pttPending = null; heldTx = null;
      applyTx({ type: "tx", owner: m.tx.owner || null });
    }
    if (!injected) {
      if (wasOwnedAtDrop) { wasOwnedAtDrop = false; TR.toast("Transmission ended (connection lost)", "warn"); }
      // RX audio and the mic are per connection on the server: re-assert them on the new socket.
      // Never re-key (G-1 g): a claim is only ever sent by an operator's own click.
      if ($("audioBtn") && $("audioBtn").classList.contains("active")) send({ action: "rx_audio", on: true });
      if (micOn) send({ action: "mic", on: true });
    }
    applyStatusUi();
    try { Layout().applyGates && Layout().applyGates(); } catch (_) {}   // the role may gate the PTT tile (§3.8)
  }

  // ---- TX: server-derived ownership (G-1, AR-8), claim seq + heartbeats (§2.13, AR-46) ----
  // Ownership comes ONLY from the server's tx frames. A claim sent by this tab is "pending" until a
  // tx/busy/denied frame answers it (req.seq >= its seq) or for 3 s. tx frames that predate the
  // answer are held back, so a stale frame can never flip the button and make a click re-key.
  let txSeq = 0;
  const pendingClaims = new Map();               // seq -> {kind, at}
  let pttPending = null;                         // {seq, at}: this tab's PTT claim awaiting its answer
  let answeredSeq = 0;                           // highest req.seq of ours the server has answered
  let heldTx = null;                             // the newest tx frame that arrived while a claim was pending
  let txSince = 0;                               // performance.now() when the current TX session started
  let hbTimer = 0, hbMs = 2000;
  let wasOwnedAtDrop = false;
  let failsafeSent = false;
  // The re-key guard (asymmetric, fail-safe): this tab sends no ptt:true before pttGuardUntil. It is
  // set PTT_REKEY_GUARD_MS ahead whenever this tab's own PTT ends (it sent ptt:false on any path, or
  // a tx frame / a socket close took its ptt claim away) and pushed out again by every press it
  // refuses, like cwtool.js's STOP_GUARD_MS: a burst of STOP clicks (a double-click, a slow
  // double-click, clicks every 300 ms, the session ending as STOP is clicked) never turns into a new
  // key. It only ever holds back KEYING; an unkey is never held back (I4).
  let pttGuardUntil = -1e9;
  const PTT_REKEY_GUARD_MS = 800;
  function pttGuard(now) {
    const t = (now === undefined ? performance.now() : now) + PTT_REKEY_GUARD_MS;
    if (t > pttGuardUntil) pttGuardUntil = t;
  }
  // The tab is hidden or being left (pagehide): a claim granted now is released at once (G-1 c),
  // and no claim is SENT at all (txSendClaim), so a deferred key (a touch hold whose 300 ms timer
  // fires after the hide) can never key the radio from a page nobody is looking at.
  let pageGone = false;
  function pageAway() { return pageGone || !!document.hidden; }

  function nextSeq() { return ++txSeq; }
  function claimOf(o) {
    if (o.action === "ptt" && o.tx === true) return "ptt";
    if (o.action === "cw_tx") return "cw";
    if (o.action === "tune_atu") return "tune";
    if (o.action === "rx_func" && o.name === "vox" && o.on === true) return "vox";
    return null;
  }
  function txOwner() { return (TR.tx && TR.tx.owner) || null; }
  function holds(k) {
    const o = txOwner();
    return TR.isTxOwner() && !!o && Array.isArray(o.claims) && o.claims.includes(k);
  }
  function ownedPtt() { return holds("ptt"); }
  function ownedOrPending() { return TR.isTxOwner() || pendingClaims.size > 0; }
  function otherOwner() { const o = txOwner(); return o && !TR.isTxOwner() ? o : null; }
  function ownerName(o) { return o ? (o.external ? "FRONT PANEL" : (o.user || "someone")) : ""; }
  function isMine(req) {
    return !!req && TR.myConn != null && req.conn === TR.myConn && typeof req.seq === "number";
  }

  function txSendClaim(obj, k) {
    // Never KEY from a hidden or departing page (G-1 c): whatever asks — a deferred press, a timer,
    // another module through RadioControl.send — nothing is sent. Unkeys are never held back.
    if (pageAway()) return false;
    if (holds(k)) {                              // never re-send a held claim (F2, F19, I17)
      // Tuning is not observable: the server holds the tune claim until its 20 s cap. Say so
      // instead of silently ignoring the press.
      if (k === "tune") TR.toast("Tuning is still running — TUNE can be pressed again 20 s after it started.", "info");
      return false;
    }
    if (!ws || ws.readyState !== 1) return false;
    const seq = (typeof obj.seq === "number" && obj.seq > 0) ? obj.seq : nextSeq();
    if (seq > txSeq) txSeq = seq;
    const now = performance.now();
    pendingClaims.set(seq, { kind: k, at: now });
    if (k === "ptt") pttPending = { seq, at: now };
    rawSend(Object.assign({}, obj, { seq }));
    startHb(true);                               // tx_hb from the moment the claim is sent (AR-46)
    renderTx();
    return true;
  }
  function resolvePending(seq) {
    if (seq > answeredSeq) answeredSeq = seq;
    for (const s of [...pendingClaims.keys()]) if (s <= seq) pendingClaims.delete(s);
    if (pttPending && pttPending.seq <= seq) pttPending = null;
  }
  function flushHeld() {
    if (!pendingClaims.size && heldTx) { const f = heldTx; heldTx = null; applyTx(f); }
  }
  // A claim nobody answered within 3 s is given up, and released at the same time: if its grant is
  // only late, the release (queued behind the claim on the same socket) unkeys it again, so a late
  // grant never leaves the radio keyed while this tab shows it unkeyed. The server ignores the
  // auto release of a claim that lost.
  const EXPIRED_RELEASE = { ptt: { action: "ptt", tx: false, auto: true }, cw: { action: "cw_stop", auto: true },
                            vox: { action: "rx_func", name: "vox", on: false, auto: true } };
  function expirePending() {
    const now = performance.now();
    let changed = false;
    for (const [s, p] of pendingClaims) {
      if (now - p.at <= 3000) continue;
      pendingClaims.delete(s); changed = true;
      if (EXPIRED_RELEASE[p.kind]) send(Object.assign({}, EXPIRED_RELEASE[p.kind]));
    }
    if (pttPending && now - pttPending.at > 3000) { pttPending = null; changed = true; }
    if (changed) { flushHeld(); renderTx(); }
  }

  function onTxFrame(m) {
    const req = m.req;
    if (isMine(req) && req.seq < answeredSeq) return;            // older than an answer we already have
    if (pendingClaims.size) {
      const oldest = Math.min(...pendingClaims.keys());
      if (!(isMine(req) && req.seq >= oldest)) { heldTx = m; return; }   // predates our claim (AR-8)
      resolvePending(req.seq);
      heldTx = null;
    } else if (isMine(req) && req.seq > answeredSeq) {
      answeredSeq = req.seq;
    }
    applyTx(m);
    // the grant of a hold-to-talk claim whose key is already up (pttKeyEnd sent its release too;
    // this covers a grant that overtakes it): unkey now
    if (isMine(req) && req.seq <= pttKeyDeadSeq && ownedPtt()) send({ action: "ptt", tx: false, auto: true });
  }
  function answerFromReply(m) {                  // a busy / denied frame answering one of our claims
    if (!isMine(m.req) || m.req.seq < answeredSeq) return;
    resolvePending(m.req.seq);
    flushHeld();
    renderTx();
  }
  function applyTx(m) {
    const o = m.owner && typeof m.owner === "object" ? m.owner : null;
    const hadPtt = ownedPtt();
    TR.tx = { owner: o };
    if (hadPtt && !ownedPtt()) pttGuard();       // our PTT ended (any reason): re-key guard
    txSince = o ? performance.now() - Math.max(0, +o.elapsed_ms || 0) : 0;
    if (!ownedPtt()) failsafeSent = false;
    if (TR.isTxOwner()) startHb();
    renderTx();
    document.dispatchEvent(new CustomEvent("tr:tx", { detail: TR.tx }));
    // A claim that was still pending when the tab was hidden (or left) is granted now: hiding
    // released only what was held then, so release this one too (the same auto path).
    if (TR.isTxOwner() && pageAway()) autoRelease();
  }
  function onTxRevoked(m) {
    const by = m.by ? " by " + m.by : "";
    const why = {
      stopped: "Your transmission was stopped" + by + ".",
      heartbeat: "Transmission ended — the link to the server stalled.",
      timeout: m.claim === "cw" ? "CW stopped — the message ran past its time limit."
                                : "Transmission ended — the 120 s limit was reached.",
      radio_change: "Transmission ended — the radio was switched" + by + ".",
      radio_disconnect: "Transmission ended — the radio was disconnected" + by + ".",
      admin_switch: "Transmission ended — an admin switched the radio" + by + ".",
      server_locked: "Transmission ended — the server is locked.",
      shutdown: "Transmission ended — the server is shutting down.",
    }[m.reason] || "Transmission ended.";
    TR.toast(why, "warn", { force: true });
    TR.srLive(why);
  }
  function txOnSocketClosed() {
    if (ownedOrPending()) wasOwnedAtDrop = true;   // the server released our claims with the socket
    if (ownedPtt() || pttPending) pttGuard();
    pendingClaims.clear(); pttPending = null; heldTx = null; answeredSeq = 0;
    TR.myConn = null;
    TR.tx = { owner: null };
    txSince = 0;
    stopHb();
    renderTx();
  }

  // A claim gets its first tx_hb HB_FIRST_MS after it is sent (well inside 250 ms), then one every
  // hbMs: the interval restarts from that first beat, so the server never waits a whole interval.
  const HB_FIRST_MS = 150;
  let hbFirst = 0;
  function startHb(claimSent) {
    if (claimSent) {
      clearTimeout(hbFirst);
      hbFirst = setTimeout(() => {
        hbFirst = 0;
        if (hbTimer) { clearInterval(hbTimer); hbTimer = 0; }
        hbTick();                                // sends, or stops if the claim is already gone
        if (pendingClaims.size || TR.isTxOwner()) startHb();
      }, HB_FIRST_MS);
      return;
    }
    if (!hbTimer) hbTimer = setInterval(hbTick, hbMs);
  }
  function stopHb() {
    if (hbTimer) { clearInterval(hbTimer); hbTimer = 0; }
    if (hbFirst) { clearTimeout(hbFirst); hbFirst = 0; }
  }
  function hbTick() {
    expirePending();
    if (pendingClaims.size || TR.isTxOwner()) rawSend({ action: "tx_hb" });   // never touches the radio (I16)
    else stopHb();
  }

  // The owner's STOP (top-bar chip, FAB): unkey what this tab holds. A claim still pending is
  // released with auto:true, which the server ignores if the claim lost (busy).
  function txStop() {
    const o = txOwner(), claims = (TR.isTxOwner() && o && o.claims) || [];
    if (claims.includes("ptt")) send({ action: "ptt", tx: false });
    else if (pttPending) send({ action: "ptt", tx: false, auto: true });
    if (claims.includes("cw")) send({ action: "cw_stop" });
    if (claims.includes("vox")) send({ action: "rx_func", name: "vox", on: false });
    if (claims.includes("tune")) send({ action: "tx_stop" });   // tune has no stop primitive
    pttPending = null;
    renderTx();
  }
  // Leaving the page or hiding the tab while transmitting releases (auto) and stops the local mic.
  function autoRelease() {
    let did = false;
    if (ownedPtt() || pttPending) { send({ action: "ptt", tx: false, auto: true }); did = true; }
    if (holds("cw")) { send({ action: "cw_stop", auto: true }); did = true; }
    if (holds("vox")) { send({ action: "rx_func", name: "vox", on: false, auto: true }); did = true; }
    pttPending = null;
    if (did && micOn) { stopMic(); $("micBtn").classList.remove("on"); send({ action: "mic", on: false }); }
    if (did) renderTx();
  }

  // ---- denied / busy / error frames (control queue, to this connection only) ----
  const DENY_TEXT = {
    role: "Not permitted for your role.",
    not_connected: "Connect a radio first.",
    connecting: "The radio is switching — try again in a moment.",
    unsupported: "This radio doesn't support that.",
    critical_admin: "Only an admin can change this setting.",
    rate_limited: "Too many commands — slow down a little.",
    not_owner: "Key PTT first — this works only while you transmit.",
    invalid: "The server rejected that command.",
  };
  function onBusy(m) {
    answerFromReply(m);
    document.dispatchEvent(new CustomEvent("tr:busy", { detail: m }));
    if (m.action === "menu_read_group") { menuBusyRetry(m.retry_after); return; }
    if (m.action === "menu_write") revertPendingMenu();
    const o = m.owner;
    TR.toast(o === "FRONT PANEL" ? "The radio is keyed from its front panel."
             : (o ? o + " is on the air — try again when they finish." : "Busy — try again in a moment."), "warn");
  }
  function onDenied(m) {
    answerFromReply(m);
    document.dispatchEvent(new CustomEvent("tr:denied", { detail: m }));
    if (m.action === "menu_write" && handleMenuDenied(m)) return;
    const t = DENY_TEXT[m.reason];
    if (t) TR.toast(t, "warn");
  }
  function onErrorFrame(m) {
    const code = String(m.code || "");
    if (code.indexOf("rx_") === 0) {             // host RX capture refused / failed: RX is off for us
      audioOn = false; stopAfScope();
      $("audioBtn").classList.remove("active");
    }
    if ((code.indexOf("tx_device") === 0 || (code === "tx_failed" && /^could not open/i.test(m.message || ""))) && micOn) {
      stopMic(); $("micBtn").classList.remove("on");
    }
    TR.toast(m.message || code || "Server error", "error");
  }

  function parseScope(buf) {
    if (blanked) return;                          // radio changed: ignore sweeps so the waterfall stays cleared
    const dv = new DataView(buf);
    if (dv.getUint8(0) !== 0x53) return;
    const npoints = dv.getUint16(3, true);
    const meta = {
      mode: dv.getUint8(1),
      out: dv.getUint8(2) === 1,
      center: dv.getUint32(5, true),
      span: dv.getUint32(9, true),
      lower: dv.getUint32(13, true),
      upper: dv.getUint32(17, true),
      tuned: dv.getUint32(21, true),
      filterBw: dv.getUint32(25, true),
    };
    const data = new Uint8Array(buf, 29, npoints);
    scope.pushSweep(meta, data);
    updateScopeLabels(meta);
  }

  function updateScopeLabels(m) {
    let lo, hi;
    if (m.mode === 1) { lo = m.lower; hi = m.upper; }
    else { const vc = m.tuned || m.center; lo = vc - m.span / 2; hi = vc + m.span / 2; }
    // center freq label removed — the tuned-channel marker sits there and overlapped it
    $("lblLeft").textContent = formatFreq(lo);
    $("lblRight").textContent = formatFreq(hi);
  }

  // ---- state -> UI ----
  // Meter render reads the live `state`; driven both by full-state frames and the fast
  // 'meter' channel (which carries only meter/meter_val/smeter/smeter_s between full frames).
  function renderMeter() {
    const s = state || {};
    const isS = (s.meter || "S") === "S";
    const mmax = isS ? 240 : (s.meter_max || 255);
    const mf = $("meterFill"), mv = $("meterVal");
    if (mf) mf.style.width = Math.min(100, (s.meter_val || 0) / mmax * 100) + "%";
    if (mv) mv.textContent = isS ? (s.smeter_s || "S0") : (s.meter + " " + (s.meter_val || 0));
  }
  function updateMeter(m) {
    state.meter = m.meter; state.meter_val = m.meter_val;
    state.smeter = m.smeter; state.smeter_s = m.smeter_s;
    renderMeter();
    layoutSummaries();
  }
  // collapsed tiles show a one-line summary in their header (§3.11); layout.js only touches collapsed ones
  function layoutSummaries() { try { const L = Layout(); if (L.summaries) L.summaries(); } catch (_) {} }
  // VFO tile cues (§4.2): the operating VFO / split colour the tile border; .dual sizes the digits
  function setBandsCues(s) {
    const b = $("bands");
    if (!b) return;
    const dual = $("rowSub") ? $("rowSub").style.display !== "none" : false;
    b.classList.toggle("dual", dual);
    if (s) {
      b.classList.toggle("sub-active", dual && s.active_band === "sub");
      b.classList.toggle("split", (s.split || 0) > 0);
    }
  }
  function updateState(s) {
    const justConnected = !state.connected && s.connected;
    const connChanged = !!state.connected !== !!s.connected || (s.connected && state.radio !== s.radio);
    state = s;
    // A fresh connection un-blanks the readout here (not just in doConnect's .then): a minimal
    // radio (IC-728) emits state only ONCE on connect — no meter/scope stream to re-trigger a
    // render — so if this frame raced ahead of the connect handler, the guard below would drop
    // it and the freq would never appear. Clearing blanked on the connected frame itself is race-free.
    if (justConnected) { blanked = false; onMenuReconnect(); }   // drop stale menu cache + refresh open groups
    // G-4: the SERVER's radio wins. Another user (an admin) switched the radio: render its profile.
    if (s.connected && s.radio && radios.some(p => p.id === s.radio) && (!currentRadio || currentRadio.id !== s.radio)) {
      $("radioSel").value = s.radio;
      renderRadio(selectedRadio());
      renderRadioHelp();
    }
    // connected / disconnected / switched: transport kind, pins and host audio follow the server (G-6)
    if (connChanged) refreshStatus();
    // dual-watch band readout (MAIN/SUB); single-rx radios show MAIN only
    $("rowSub").style.display = s.dual_watch ? "" : "none";
    setBandsCues(s);
    if (blanked) {                                  // radio just changed: keep VFOs zeroed until a fresh connect
      renderFreq($("mainFreq"), 0, false);
      renderFreq($("subFreq"), 0, false);
    } else {
      const data = (s.data || 0) > 0;             // DATA mode belongs to the operating band
      fillBand("main", s.main, s.active_band !== "sub", data && s.active_band !== "sub");
      fillBand("sub", s.sub, s.active_band === "sub", data && s.active_band === "sub");
    }
    // no-scope radios (Yaesu CAT): no sweeps. While RX audio runs we draw an AF
    // spectrum (afTimer); otherwise drive the band plan + freq labels from the freq.
    if (!blanked && currentRadio && currentRadio.has_scope === false && !afTimer) {
      scope.showStatic(s.freq, 200000);
      updateScopeLabels(scope.meta);
    }
    $("rowMain").classList.toggle("active", s.active_band !== "sub");
    $("rowSub").classList.toggle("active", s.active_band === "sub");
    setInd("mainInd", s, s.active_band !== "sub");
    setInd("subInd", s, s.active_band === "sub");

    // multi-meter (S live; TX meters wired for M3). The value also arrives on the fast
    // lightweight 'meter' channel between full-state frames — render via the shared helper.
    renderMeter();
    setActive(".m-btn", b => b.dataset.meter === (s.meter || "S"));

    // RX toggles
    { const pb = $("preampBtn"); if (pb) {
        const labels = (currentRadio && currentRadio.preamp_labels) || ["OFF", "P.AMP"];
        pb.textContent = labels[s.preamp || 0] || labels[0];
        pb.classList.toggle("on", (s.preamp || 0) > 0);
      } }
    $("attBtn").classList.toggle("on", (s.att || 0) > 0);
    $("lockBtn").classList.toggle("on", !!s.lock);
    { const tb = $("tunerBtn"); if (tb) tb.classList.toggle("on", (s.tuner || 0) > 0); }
    // ATAS manual-tune controls next to PTT: shown only when TUNER SELECT = ATAS (value 3)
    { const at = $("atasTune"); if (at) at.hidden = (s.tuner_select !== 3);
      const ah = $("atasHint"); if (ah) ah.textContent = s.ptt ? "tuning · watch SWR" : "key PTT · watch SWR";
      const tuning = s.tuner_select === 3 && !!s.ptt;
      at && at.querySelectorAll(".atas-btn").forEach(b => b.classList.toggle("armed", tuning)); }

    // connection
    const on = !!s.connected;
    $("led").classList.toggle("on", on);
    $("connlabel").textContent = on ? (s.transport || "Connected") : "Disconnected";
    applyAudioLabel();
    $("modelLabel").textContent = on ? (s.radio_name || "") : (currentRadio ? currentRadio.name : "");
    applyRadioSelLock();

    // TX tag = the RADIO's TX/RX. The PTT button follows the server's TX owner (tx frames, AR-60),
    // never s.ptt: another user's transmission must not light this tab's PTT red.
    $("txTag").textContent = s.ptt ? "TX" : "RX";
    $("txTag").classList.toggle("on", !!s.ptt);
    renderPtt();

    // span + scope mode
    $("spanVal").textContent = s.span_label || "";
    $("btnCenter").classList.toggle("active", !!s.scope_center);
    $("btnFixed").classList.toggle("active", !s.scope_center);

    // active buttons
    markBandMode(s);
    setActive(".filt", b => b.dataset.filter === String(s.filter));
    { const db = $("dataBtn"); if (db) {             // Icom DATA mode: only SSB / AM / FM have a -D variant
        db.classList.toggle("on", (s.data || 0) > 0);
        db.classList.toggle("dim", !DATA_MODES.includes(s.mode_name));
      } }

    // RX DSP toggles + AGC
    for (const f of ["nb", "nr", "anotch", "mnotch"]) {
      const el = $(f + "Btn");
      if (el) el.classList.toggle("on", (s[f] || 0) > 0);
    }
    setActive(".agc", b => +b.dataset.mode === (s.agc || 2));
    setActive(".mnw", b => +b.dataset.width === (s.mnotch_w || 0));

    // TX toggles + TBW + SPLIT/RIT (M3)
    for (const f of ["comp", "vox", "mon"]) {
      const el = $(f + "Btn");
      if (el) el.classList.toggle("on", (s[f] || 0) > 0);
    }
    setActive(".tbw", b => +b.dataset.w === (s.tbw || 0));
    setActive(".dup", b => +b.dataset.mode === (s.duplex || 0));
    if ($("splitBtn")) $("splitBtn").classList.toggle("on", (s.split || 0) > 0);
    if ($("ritBtn")) $("ritBtn").classList.toggle("on", (s.rit || 0) > 0);
    if ($("ritVal")) $("ritVal").textContent = (s.rit_freq > 0 ? "+" : "") + (s.rit_freq || 0) + " Hz";

    // NAR/WIDE toggle + FM Tone/DCS/shift + the extra operating controls (Yaesu)
    if ($("narBtn")) $("narBtn").classList.toggle("on", (s.narrow || 0) > 0);
    syncFmPanel(s);
    syncExtPanel(s);
    syncIcomCw(s);

    // level sliders (don't fight an active drag) + value readouts
    for (const t of LEVELS) {
      const el = $(t);
      if (el && document.activeElement !== el && s[t] != null) { el.value = s[t]; setFill(el); }
      if (s[t] != null) $(t + "Val").textContent = fmtLevel(t, s[t]);
    }

    // Keep the marker + filter passband tracking the tuned freq between real CI-V
    // scope sweeps. The AF audio scope (no-scope radios with RX audio on) owns the
    // overlay and draws its own marker (CW: the dial, at the pitch; no marker in the
    // other modes, where the dial sits at an edge), so skip this while it runs —
    // otherwise every state update stamps a marker the next AF frame moves, i.e. it flickers.
    if (!afTimer) {
      scope.setOpMode(s.mode_name);
      scope.meta.tuned = s.freq;
      scope.meta.filterBw = s.filter_bw;
      scope.drawOverlay();
    }
    layoutSummaries();
  }

  const BAND_LABEL = { "144": "2 m", "430": "70 cm", "1200": "23 cm" };
  function renderFreq(el, hz, interactive) {
    if (!el) return;
    if (el._hz === hz && el._int === interactive) return;   // skip when unchanged (avoids hover flicker)
    el._hz = hz; el._int = interactive;
    el.classList.toggle("tunable", !!interactive);
    const s = formatFreq(hz), ndig = (s.match(/\d/g) || []).length;
    let html = "", di = 0;
    for (const ch of s) {
      if (ch >= "0" && ch <= "9") {
        html += '<span class="fd" data-place="' + Math.pow(10, ndig - 1 - di) + '">' + ch + "</span>";
        di++;
      } else {
        html += '<span class="fdsep">' + ch + "</span>";
      }
    }
    el.innerHTML = html;
  }
  function fillBand(name, b, active, data) {
    if (!b) return;
    renderFreq($(name + "Freq"), b.freq, !!active);
    $(name + "Mode").textContent = (b.mode_name || "") + (data && b.mode_name ? "-D" : "");   // USB-D, like the radio
    $(name + "Fil").textContent = b.filter_name || "";
    const bn = bandOf(b.freq);
    const lbl = $(name + "Band");
    if (lbl) lbl.textContent = BAND_LABEL[bn] || (bn ? bn + " m" : "");
  }

  // function-indicator strip (lit, like the TFT) — only the operating band's DSP is tracked
  function setInd(elId, s, isActive) {
    const el = $(elId);
    if (!el) return;
    if (!isActive) { el.innerHTML = ""; return; }
    const inds = [["AGC-" + ({ 1: "F", 2: "M", 3: "S" }[s.agc || 2]), "amber"]];
    if ((s.preamp || 0) > 0) inds.push(["P.AMP", ""]);
    if ((s.att || 0) > 0) inds.push(["ATT", ""]);
    if (s.nb) inds.push(["NB", ""]);
    if (s.nr) inds.push(["NR", ""]);
    if (s.anotch) inds.push(["AN", ""]);
    if (s.mnotch) inds.push(["MN", ""]);
    el.innerHTML = inds.map(([t, c]) => '<span class="ind ' + c + '">' + t + "</span>").join("");
  }

  const DATA_MODES = ["LSB", "USB", "AM", "FM"];   // the modes with a DATA (-D) variant (backend Radio.DATA_MODES)
  function bandOf(hz) {
    if (!currentRadio) return "";
    for (const b of currentRadio.bands) if (hz >= b.lo && hz <= b.hi) return b.name;
    return "";
  }
  function setActive(sel, pred) {
    document.querySelectorAll(sel).forEach(b => b.classList.toggle("active", pred(b)));
  }
  // the lit BAND and MODE keys. Called on every state frame AND by renderRadio, which rebuilds those
  // keys unlit: after a reload the first state frame can arrive before /api/radios has answered (no
  // keys yet), and a quiet radio sends no other until the freq or mode changes.
  function markBandMode(s) {
    setActive(".band", b => b.dataset.band === String(bandOf(s.freq)));
    setActive(".mode", b => b.dataset.mode === s.mode_name);
  }

  // hover tooltips: just what each abbreviation stands for (no descriptions)
  const TITLES = {
    "NB": "Noise Blanker", "NR": "Noise Reduction", "A-NOTCH": "Auto Notch", "M-NOTCH": "Manual Notch",
    "NOTCH": "Manual Notch", "AGC": "Automatic Gain Control",
    "PBT1": "Passband Tuning 1", "PBT2": "Passband Tuning 2",
    "P.AMP": "Preamplifier", "ATT": "Attenuator", "LOCK": "Dial Lock",
    "COMP": "Speech Compressor", "VOX": "Voice-Operated Transmit", "MON": "Monitor",
    "TBW": "Transmit Bandwidth", "RIT": "Receiver Incremental Tuning",
    "SPLIT": "Split", "DUP": "Duplex", "SIMP": "Simplex", "PTT": "Push To Talk",
    "AF": "Audio Gain", "RF": "RF Gain", "SQL": "Squelch", "PWR": "RF Power",
    "MIC": "Microphone Gain", "VOL": "Volume",
    "FIL1": "Filter 1", "FIL2": "Filter 2", "FIL3": "Filter 3",
    "LSB": "Lower Sideband", "USB": "Upper Sideband", "CW": "Continuous Wave", "CW-R": "CW Reverse",
    "CW-USB": "CW (USB side)", "CW-LSB": "CW (LSB side)",
    "AM": "Amplitude Modulation", "FM": "Frequency Modulation", "FM-N": "FM Narrow", "AM-N": "AM Narrow",
    "RTTY": "Radio Teletype", "RTTY-R": "RTTY Reverse",
    "RTTY-LSB": "RTTY (LSB)", "RTTY-USB": "RTTY (USB)",
    "DATA-LSB": "Data (LSB)", "DATA-USB": "Data (USB)", "DATA-FM": "Data (FM)", "C4FM": "C4FM Digital",
    "DV": "Digital Voice", "DD": "Digital Data",
    "S": "S-meter", "PO": "Power Output", "SWR": "Standing Wave Ratio", "ALC": "Auto Level Control",
    "Vd": "Drain Voltage", "Id": "Drain Current", "CENT": "Center", "FIX": "Fixed",
    "STEP": "Tuning Step",
  };
  function applyTitles(root) {
    const r = root || document;
    r.querySelectorAll(".key,.m-btn,.sc-btn,.step-label").forEach(el => {
      const t = (el.textContent || "").trim();
      if (TITLES[t] && !el.title) el.title = TITLES[t];
    });
    r.querySelectorAll(".sl").forEach(row => {
      const lab = row.querySelector("span");
      const t = lab && (lab.textContent || "").trim();
      if (t && TITLES[t] && !row.title) row.title = TITLES[t];
    });
  }

  // ---- FM Tone / DCS (CTCSS + DCS + repeater shift), Yaesu operating controls ----
  // index = the Yaesu CN "number" for that tone/code (must match the backend tables).
  const CTCSS_TONES = ["67.0", "69.3", "71.9", "74.4", "77.0", "79.7", "82.5", "85.4", "88.5", "91.5",
    "94.8", "97.4", "100.0", "103.5", "107.2", "110.9", "114.8", "118.8", "123.0", "127.3", "131.8",
    "136.5", "141.3", "146.2", "151.4", "156.7", "159.8", "162.2", "165.5", "167.9", "171.3", "173.8",
    "177.3", "179.9", "183.5", "186.2", "189.9", "192.8", "196.6", "199.5", "203.5", "206.5", "210.7",
    "218.1", "225.7", "229.1", "233.6", "241.8", "250.3", "254.1"];
  const DCS_CODES = ["023", "025", "026", "031", "032", "036", "043", "047", "051", "053", "054", "065",
    "071", "072", "073", "074", "114", "115", "116", "122", "125", "131", "132", "134", "143", "145",
    "152", "155", "156", "162", "165", "172", "174", "205", "212", "223", "225", "226", "243", "244",
    "245", "246", "251", "252", "255", "261", "263", "265", "266", "271", "274", "306", "311", "315",
    "325", "331", "332", "343", "346", "351", "356", "364", "365", "371", "411", "412", "413", "423",
    "431", "432", "445", "446", "452", "454", "455", "462", "464", "465", "466", "503", "506", "516",
    "523", "526", "532", "546", "565", "606", "612", "624", "627", "631", "632", "654", "662", "664",
    "703", "712", "723", "731", "732", "734", "743", "754"];
  const TONE_MODES = [[0, "OFF"], [2, "TONE"], [1, "TSQL"], [3, "DCS"], [4, "DCS-ENC"]]; // [wire value, label]
  const RPT_SHIFTS = [[0, "SIMPLEX"], [1, "+ SHIFT"], [2, "- SHIFT"]];
  const FM_MODES = ["FM", "FM-N", "DATA-FM", "C4FM"];
  let fmWired = false;

  function setupFmControls() {
    if (fmWired) return; fmWired = true;
    const opt = (v, t) => { const o = document.createElement("option"); o.value = v; o.textContent = t; return o; };
    const tm = $("toneMode"), tf = $("toneFreq"), dc = $("dcsCode"), rs = $("rptShift");
    if (!tm) return;
    TONE_MODES.forEach(([v, l]) => tm.appendChild(opt(v, l)));
    RPT_SHIFTS.forEach(([v, l]) => rs.appendChild(opt(v, l)));
    CTCSS_TONES.forEach((hz, i) => tf.appendChild(opt(i, hz + " Hz")));
    DCS_CODES.forEach((c, i) => dc.appendChild(opt(i, c)));
    tm.addEventListener("change", () => send({ action: "tone_mode", value: +tm.value }));
    tf.addEventListener("change", () => send({ action: "tone_freq", idx: +tf.value }));
    dc.addEventListener("change", () => send({ action: "dcs_code", idx: +dc.value }));
    rs.addEventListener("change", () => send({ action: "rpt_shift", value: +rs.value }));
  }

  const FM_FAMILY = ["FM", "FM-N", "DATA-FM", "C4FM"];
  function syncFmPanel(s) {
    const cap = (currentRadio && currentRadio.capabilities) || {};
    const show = !!cap.fm_tone && FM_FAMILY.includes(s.mode_name);
    const g = $("fmGrp"); if (g) g.style.display = show ? "block" : "none";   // beat the #fmGrp{display:none} default
    if (!show) return;
    // DCS + repeater-shift only where the radio has them (Yaesu); Icom does tone/TSQL only,
    // and uses the SPLIT·RIT duplex control for shift.
    const dcs = cap.fm_dcs !== false;
    [...$("toneMode").options].forEach(o => { if (o.value === "3" || o.value === "4") o.hidden = !dcs; });
    const hideRow = (id, hide) => { const e = $(id); if (e && e.parentElement) e.parentElement.style.display = hide ? "none" : ""; };
    hideRow("dcsCode", !dcs); hideRow("rptShift", !dcs);
    const set = (id, v) => { const el = $(id); if (el && document.activeElement !== el) el.value = String(v); };
    set("toneMode", s.tone_mode || 0); set("toneFreq", s.tone_freq || 0);
    set("dcsCode", s.dcs_code || 0); set("rptShift", s.rpt_shift || 0);
  }

  // ---- Icom CW / filter group: APF, break-in, CW pitch/speed, filter shape ----
  const APF_LABELS = ["OFF", "WIDE", "MID", "NAR"], BKIN_LABELS = ["OFF", "SEMI", "FULL"];
  function syncIcomCw(s) {
    const cap = (currentRadio && currentRadio.capabilities) || {};
    const g = $("icomCwGrp"); if (!g) return;
    const cw = ["CW", "CW-R"].includes(s.mode_name);       // Icom CW mode names
    g.style.display = (cap.icom_cw && cw) ? "" : "none";
    if (!(cap.icom_cw && cw)) return;
    const apf = s.apf || 0, bkin = s.bkin || 0;
    const ab = $("apfCycBtn"); if (ab) { ab.textContent = "APF: " + APF_LABELS[apf]; ab.classList.toggle("on", apf > 0); }
    const bb = $("bkinCycBtn"); if (bb) { bb.textContent = "BK-IN: " + BKIN_LABELS[bkin]; bb.classList.toggle("on", bkin > 0); }
    const fs = $("fshapeBtn"); if (fs) { fs.textContent = (s.filter_shape ? "SOFT" : "SHARP"); fs.classList.toggle("on", !!s.filter_shape); }
  }

  // ---- extra operating controls: WIDTH / CONTOUR / APF / CW / TXW / scan (Yaesu) ----
  const SSB_MODES = ["LSB", "USB"], CW_MODES = ["CW-USB", "CW-LSB"];
  const RTTYDATA_MODES = ["RTTY-LSB", "RTTY-USB", "DATA-LSB", "DATA-USB"];
  const WIDTH_MODES = [...SSB_MODES, ...CW_MODES, ...RTTYDATA_MODES];
  const CONTOUR_MODES = [...WIDTH_MODES, "AM"];
  // SH DSP bandwidth per context — [code, Hz]; keyed by mode class then narrow(1)/wide(0).
  // From the FT-991A CAT manual WIDTH table. NAR on => narrow column, NAR off => wide column.
  // lists are sorted ascending by Hz so the -/+ stepper moves narrower/wider monotonically.
  const SH_VALID = {
    ssb: { 1: [[1, 200], [2, 400], [3, 600], [4, 850], [5, 1100], [6, 1350], [0, 1500], [7, 1500], [8, 1650], [9, 1800]],
           0: [[9, 1800], [10, 1950], [11, 2100], [12, 2200], [13, 2300], [0, 2400], [14, 2400], [15, 2500], [16, 2600], [17, 2700], [18, 2800], [19, 2900], [20, 3000], [21, 3200]] },
    cw:  { 1: [[1, 50], [2, 100], [3, 150], [4, 200], [5, 250], [6, 300], [7, 350], [8, 400], [9, 450], [0, 500], [10, 500]],
           0: [[10, 500], [11, 800], [12, 1200], [13, 1400], [14, 1700], [15, 2000], [0, 2400], [16, 2400], [17, 3000]] },
    data:{ 1: [[1, 50], [2, 100], [3, 150], [4, 200], [5, 250], [0, 300], [6, 300], [7, 350], [8, 400], [9, 450], [10, 500]],
           0: [[0, 500], [10, 500], [11, 800], [12, 1200], [13, 1400], [14, 1700], [15, 2000], [16, 2400], [17, 3000]] },
  };
  function widthClass(m) { return CW_MODES.includes(m) ? "cw" : RTTYDATA_MODES.includes(m) ? "data" : "ssb"; }
  function widthList() { return (SH_VALID[widthClass(state.mode_name)] || {})[(state.narrow || 0) ? 1 : 0] || []; }
  function stepWidth(delta) {
    const list = widthList(); if (!list.length) return;
    let i = list.findIndex(([c]) => c === (state.width || 0)); if (i < 0) i = 0;
    i = Math.max(0, Math.min(list.length - 1, i + delta));
    send({ action: "width", code: list[i][0] });
  }

  let extWired = false;
  function setupExtControls() {
    if (extWired) return; extWired = true;
    // sliders with their own actions: update readout live (input), send on release (change)
    const wire = (id, act, key, fmt) => {
      const el = $(id); if (!el) return;
      const vv = $(id + "Val");
      el.addEventListener("input", () => { if (vv) vv.textContent = fmt(+el.value); if (typeof setFill === "function") setFill(el); });
      el.addEventListener("change", () => send({ action: act, [key]: +el.value }));
    };
    wire("contour_freq", "contour_freq", "hz", v => v + " Hz");
    wire("apf_freq", "apf_freq", "v", v => ((v - 25) * 10) + " Hz");
    wire("key_speed", "key_speed", "wpm", v => v + " wpm");
    wire("key_pitch", "key_pitch", "code", v => (300 + v * 10) + " Hz");
  }

  function syncExtPanel(s) {
    const cap = (currentRadio && currentRadio.capabilities) || {};
    const ext = !!cap.ext_ops, mode = s.mode_name || "";
    const disp = (id, on) => { const e = $(id); if (e) e.style.display = on ? "" : "none"; };
    disp("widthGrp", ext && WIDTH_MODES.includes(mode));
    disp("contourGrp", ext && CONTOUR_MODES.includes(mode));
    disp("apfGrp", ext && CW_MODES.includes(mode));
    disp("cwGrp", ext && CW_MODES.includes(mode));
    disp("opsGrp", ext);
    disp("paramEqBtn", ext); disp("txwBtn", ext); disp("qsplitBtn", ext);
    if (!ext) return;
    const tog = (id, v) => { const e = $(id); if (e) e.classList.toggle("on", (v || 0) > 0); };
    tog("contourBtn", s.contour); tog("apfBtn", s.apf); tog("bkinBtn", s.bkin); tog("keyerBtn", s.keyer);
    tog("spotBtn", s.spot); tog("paramEqBtn", s.param_eq); tog("txwBtn", s.txw); tog("fastBtn", s.fast);
    const sl = (id, v, fmt) => {
      const e = $(id); if (e && document.activeElement !== e) { e.value = v; if (typeof setFill === "function") setFill(e); }
      const vv = $(id + "Val"); if (vv) vv.textContent = fmt(v);
    };
    sl("contour_freq", s.contour_freq || 10, v => v + " Hz");
    sl("apf_freq", s.apf_freq == null ? 25 : s.apf_freq, v => ((v - 25) * 10) + " Hz");
    sl("key_speed", s.key_speed || 20, v => v + " wpm");
    sl("key_pitch", s.key_pitch == null ? 40 : s.key_pitch, v => (300 + v * 10) + " Hz");
    const wv = $("widthVal"); if (wv) { const e = widthList().find(([c]) => c === (s.width || 0)); wv.textContent = e ? e[1] + " Hz" : "—"; }
    setActive(".scan", b => +b.dataset.dir === (s.scan || 0));
  }

  // ---- button delegation ----
  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-act]");
    if (!b) return;
    if (!TR.can("operate") || b.disabled) return;   // JS gate (cosmetic; the server is the authority)
    const act = b.dataset.act;
    if (act === "band") send({ action: "band", band: b.dataset.band });
    else if (act === "select_band") send({ action: "select_band", band: b.dataset.band });
    else if (act === "meter") send({ action: "set_meter", meter: b.dataset.meter });
    else if (act === "toggle") {
      const fn = b.dataset.fn;
      if (fn === "preamp") {                              // cycle through the radio's preamp states
        const labels = (currentRadio && currentRadio.preamp_labels) || ["OFF", "P.AMP"];
        send({ action: "preamp", level: ((state.preamp || 0) + 1) % labels.length });
      } else {
        // generic on/off toggle: att/tuner/narrow/contour/apf/bkin/keyer/spot/param_eq/txw/fast
        const on = fn === "lock" ? !state.lock : !((state[fn] || 0) > 0);
        send({ action: fn, on });
      }
    }
    else if (act === "tune_atu") send({ action: "tune_atu" });
    else if (act === "width_d") stepWidth(+b.dataset.d);
    else if (act === "scan") send({ action: "scan", dir: +b.dataset.dir });
    else if (act === "zero_in") send({ action: "zero_in" });
    else if (act === "quick_split") send({ action: "quick_split" });
    else if (act === "apf_cyc") send({ action: "apf_lvl", v: (((state.apf || 0) + 1) % 4) });   // Icom APF OFF/WIDE/MID/NAR
    else if (act === "bkin_cyc") send({ action: "bkin_lvl", v: (((state.bkin || 0) + 1) % 3) });  // Icom BK-IN OFF/SEMI/FULL
    else if (act === "rxfunc") send({ action: "rx_func", name: b.dataset.fn, on: !((state[b.dataset.fn] || 0) > 0) });
    else if (act === "agc") send({ action: "agc", mode: +b.dataset.mode });
    else if (act === "mnotch_w") send({ action: "mnotch_w", width: +b.dataset.width });
    else if (act === "tbw") send({ action: "tbw", w: +b.dataset.w });
    else if (act === "duplex") send({ action: "duplex", mode: +b.dataset.mode });
    else if (act === "splittog") send({ action: "split", on: !((state.split || 0) > 0) });
    else if (act === "rittog") send({ action: "rit", on: !((state.rit || 0) > 0) });
    else if (act === "rit_d") { const v = Math.max(-9999, Math.min(9999, (state.rit_freq || 0) + (+b.dataset.d))); state.rit_freq = v; send({ action: "rit_freq", hz: v }); }
    else if (act === "rit_z") { state.rit_freq = 0; send({ action: "rit_freq", hz: 0 }); }
    else if (act === "mode") send({ action: "set_mode", mode: b.dataset.mode });
    else if (act === "datamode") {
      if (DATA_MODES.includes(state.mode_name)) send({ action: "data_mode", on: !((state.data || 0) > 0) });
      else TR.toast("DATA mode exists only in LSB, USB, AM and FM.", "info");
    }
    else if (act === "filter") send({ action: "set_filter", filter: +b.dataset.filter });
    else if (act === "vfo") send({ action: "vfo", code: +b.dataset.code });
    else if (act === "mem_recall") { const ch = Math.max(1, Math.min(99, +($("memCh") || {}).value || 1)); send({ action: "mem_recall", ch }); }
    else if (act === "mem_to_vfo") send({ action: "mem_to_vfo" });
    else if (act === "atas_step") {                    // ATAS motor step (only acts while keyed)
      send({ action: "atas_step", dir: +b.dataset.dir });
      if ((state.meter || "S") !== "SWR") send({ action: "set_meter", meter: "SWR" });   // watch SWR while tuning
    }
    else if (act === "scope_mode") send({ action: "scope_mode", center: b.dataset.center === "1" });
  });

  // span +/- cycle through documented spans
  const SPANS = [2500, 5000, 10000, 25000, 50000, 100000, 250000, 500000];
  $("spanUp").onclick = () => stepSpan(+1);
  $("spanDn").onclick = () => stepSpan(-1);
  function stepSpan(dir) {
    let i = SPANS.indexOf(state.span || 50000);
    if (i < 0) i = 3;
    i = Math.max(0, Math.min(SPANS.length - 1, i + dir));
    send({ action: "set_span", span: SPANS[i] });
  }

  // frequency entry
  $("freqSet").onclick = setFreqFromEntry;
  $("freqEntry").addEventListener("keydown", e => { if (e.key === "Enter") setFreqFromEntry(); });
  function setFreqFromEntry() {
    const v = parseFloat($("freqEntry").value);
    if (!isNaN(v)) send({ action: "set_freq", hz: Math.round(v * 1e6) });
  }

  // step select
  $("step").onchange = () => { step = +$("step").value; };

  // level sliders (power shown as %, others 0–255)
  function fmtLevel(t, v) {
    if (t === "rfpwr") return Math.round(v / 255 * 100) + "%";
    if (t === "cw_pitch") return (300 + Math.round(v / 255 * 600)) + " Hz";   // Icom 14 09: 300-900 Hz
    if (t === "keyer_speed") return (6 + Math.round(v / 255 * 42)) + " wpm";    // Icom 14 0C: 6-48 WPM
    return "" + v;
  }
  function setFill(el) {
    const mn = +el.min || 0, mx = +el.max || 100;
    el.style.setProperty("--p", Math.round((el.value - mn) / (mx - mn) * 100) + "%");
  }

  // ---- rAF-coalesced senders (AR-48) ----
  // While a slider, a wheel or a drag-tune moves, at most one message per animation frame goes out
  // (latest value per key; tune deltas are summed). The trailing value is always sent: on `change`,
  // on pointerup and at the wheel's end. A timeout backs up rAF (it pauses in background tabs).
  const rafPending = new Map();                  // key -> latest message
  let tuneAcc = 0, rafId = 0, rafTimer = 0;
  function scheduleFlush() {
    if (!rafId) rafId = requestAnimationFrame(flushSoon);
    if (!rafTimer) rafTimer = setTimeout(flushSoon, 120);
  }
  function flushSoon() {
    if (rafId) { cancelAnimationFrame(rafId); rafId = 0; }
    if (rafTimer) { clearTimeout(rafTimer); rafTimer = 0; }
    flushTune();
    const msgs = [...rafPending.values()];
    rafPending.clear();
    for (const m of msgs) send(m);
  }
  function sendSoon(key, msg) { rafPending.set(key, msg); scheduleFlush(); }
  function sendNow(key, msg) { rafPending.delete(key); send(msg); }
  function flushKey(key) { if (rafPending.has(key)) sendNow(key, rafPending.get(key)); }
  function queueTune(delta) { tuneAcc += delta; scheduleFlush(); }
  function flushTune() { if (tuneAcc) { const d = tuneAcc; tuneAcc = 0; send({ action: "tune", delta: d }); } }

  for (const t of LEVELS) {
    const el = $(t);
    el.addEventListener("input", () => {
      $(t + "Val").textContent = fmtLevel(t, +el.value);
      sendSoon("lvl:" + t, { action: "set_level", target: t, value: +el.value });
    });
    el.addEventListener("change", () => sendNow("lvl:" + t, { action: "set_level", target: t, value: +el.value }));
  }
  // every slider (incl. VOL): the visual fill, and the wheel only when the slider is focused AND
  // hovered (§3.17); leaving it with no button pressed blurs it, so the next wheel scrolls the page.
  // RF power never takes the wheel.
  document.querySelectorAll(".sl input[type=range]").forEach(el => {
    setFill(el);
    el.addEventListener("input", () => setFill(el));
    el.addEventListener("pointerleave", (e) => { if (!e.buttons && document.activeElement === el) el.blur(); });
    if (el.id === "rfpwr") return;
    let wheelEnd = 0;
    el.addEventListener("wheel", e => {
      if (document.activeElement !== el || !el.matches(":hover")) return;
      if (!TR.can("operate") && el.closest('[data-gate~="operate"]')) return;
      e.preventDefault();
      const span = (+el.max || 100) - (+el.min || 0);
      const d = Math.max(1, Math.round(span / 64)) * (e.deltaY < 0 ? 1 : -1);
      el.value = Math.max(+el.min, Math.min(+el.max, (+el.value) + d));
      el.dispatchEvent(new Event("input"));
      clearTimeout(wheelEnd);
      wheelEnd = setTimeout(() => el.dispatchEvent(new Event("change")), 250);   // the trailing value
    }, { passive: false });
  });

  // ---- PTT (G-1): owner-aware, from the server's tx frames ----
  // Mouse, pen and keyboard: click toggles. Touch: hold 300 ms to key (a scroll, cancel or early
  // release keys nothing); when owned, touching unkeys at once; the compat click after a touch is
  // swallowed (the touch already acted at its pointerdown).
  // The rule is asymmetric and fail-safe (I4, G-1 c): while this tab owns ptt or has a ptt claim
  // pending, EVERY press — any click (whatever its e.detail), Enter, Space, a pen or touch tap, a
  // key's auto-repeat — sends ptt:false. Only KEYING is ever held back: an idle click that is the
  // 2nd+ click of a burst (e.detail > 1), an auto-repeated key, and any press inside the re-key guard
  // (pttGuardUntil, after this tab's own PTT ended or a refused press) send nothing. So a STOP can
  // never be swallowed, and a double-click / a burst of STOP clicks can never re-key.
  const ptt = $("pttBtn");
  let cancelPttHold = () => {};                  // a touch hold still timing (set below); hide/pagehide cancel it
  function pttLeft() {
    const since = ownedPtt() ? txSince : (pttPending ? pttPending.at : performance.now());
    return (state.ptt_tot || 120) - Math.floor((performance.now() - since) / 1000);
  }
  function renderPtt() {
    if (!ptt) return;
    const o = txOwner(), mine = TR.isTxOwner(), keyed = ownedPtt() || !!pttPending;
    ptt.classList.remove("on");                  // the red .on look never comes from the radio's state (AR-60)
    ptt.classList.toggle("tx-mine", ownedPtt());
    ptt.classList.toggle("tx-busy", !!o && !mine && !o.external);
    ptt.classList.toggle("tx-external", !!o && !mine && !!o.external);
    ptt.setAttribute("aria-pressed", keyed ? "true" : "false");
    let label = "PTT";
    if (keyed) label = "ON AIR " + Math.max(0, pttLeft()) + "s — STOP";
    else if (o && !mine) label = o.external ? "RADIO TX" : "BUSY · " + (o.user || "?");
    if (ptt.textContent !== label) ptt.textContent = label;
  }
  function pttRelease() {
    if (ownedPtt()) send({ action: "ptt", tx: false });
    else if (pttPending) send({ action: "ptt", tx: false, auto: true });   // ignored if our claim lost
    pttPending = null;
    renderTx();
  }
  // One press on the PTT button: a click (mouse, pen, keyboard, assistive tech; `detail` is the
  // pointer click count, 0 from the keyboard), a touch on an owned / busy button, or a completed
  // 300 ms touch hold.
  function pttPress(detail) {
    if (ownedPtt() || pttPending) { pttRelease(); return; }   // ours: every press unkeys, never refused
    // From here on the press can only KEY (or hint), and only keying is ever refused:
    const now = performance.now();
    if (detail > 1 || now < pttGuardUntil) {     // the tail of a double-click / a STOP burst
      pttGuard(now);                             // each refused press keeps the guard shut (cwtool.js)
      return;
    }
    const o = otherOwner();
    if (o) {
      TR.toast((o.external ? "The radio is keyed from its front panel" : o.user + " is transmitting") +
               " — use STOP in the top bar.", "warn");
      return;
    }
    if (!TR.can("tx")) return;
    send({ action: "ptt", tx: true });           // seq + heartbeats added by send() (txSendClaim)
  }
  function tickTx() {
    if (ownedPtt() && !failsafeSent && pttLeft() <= 0) {   // client failsafe (the server enforces too)
      failsafeSent = true;
      send({ action: "ptt", tx: false, auto: true });
    }
    renderPtt(); renderOnAir(); renderFab();
  }
  setInterval(tickTx, 400);
  if (ptt) {
    // The compat click a browser fires after a touch is not a second press: that touch already acted
    // at its pointerdown (unkeyed, hinted, or started its hold), and the click would otherwise unkey
    // a hold that just keyed. Pointer clicks are swallowed from the touch's pointerdown until
    // SWALLOW_MS after ITS pointerup/pointercancel, however long the finger was held. A mouse/pen
    // press ends the window at once (its click is a real one), and only a touch-made click is ever
    // swallowed: a keyboard / assistive-tech click is always a real press, so it always unkeys an
    // owned PTT.
    const SWALLOW_MS = 800;
    let touchTimer = 0, touchHeld = false, touchId = null, swallowClickUntil = 0;
    const cancelHold = () => { touchHeld = false; clearTimeout(touchTimer); };
    // Hiding the tab / leaving the page ends a hold that has not keyed yet: it never keys, not even
    // when the tab is shown again with the finger still down (lift and hold again). The touch's
    // compat-click window is left as it is, so its click stays swallowed.
    cancelPttHold = cancelHold;
    const touchEnded = (e) => {
      if (touchId === null || e.pointerId !== touchId) return;
      touchId = null;
      swallowClickUntil = performance.now() + SWALLOW_MS;
    };
    ptt.addEventListener("pointerdown", (e) => {
      if (e.pointerType !== "touch") { touchId = null; swallowClickUntil = 0; return; }
      touchId = e.pointerId;
      // until this touch ends (touchEnded); bounded so a lost pointerup can't block clicks forever
      swallowClickUntil = performance.now() + 130000;
      if (ownedPtt() || pttPending || otherOwner()) { pttPress(1); return; }  // unkey at once / busy hint
      cancelHold();
      touchHeld = true;
      touchTimer = setTimeout(() => { const go = touchHeld; touchHeld = false; if (go) pttPress(1); }, 300);
    });
    ptt.addEventListener("pointerup", cancelHold);
    ptt.addEventListener("pointercancel", cancelHold);
    ptt.addEventListener("pointerleave", cancelHold);
    // the end of the touch, wherever the finger is lifted (window capture also sees a pointerup
    // that lands outside the button)
    window.addEventListener("pointerup", touchEnded, true);
    window.addEventListener("pointercancel", touchEnded, true);
    window.addEventListener("scroll", cancelHold, { capture: true, passive: true });
    ptt.addEventListener("click", (e) => {
      const detail = +e.detail || 0;
      // the compat click of a touch: pointerType "touch" (a PointerEvent click), or a plain
      // MouseEvent click (no pointerType) with a click count; "" / detail 0 is a keyboard or
      // assistive-tech click and "mouse" / "pen" a real one: those are never swallowed
      const touchy = e.pointerType === "touch" || (e.pointerType === undefined && detail !== 0);
      if (touchy && performance.now() < swallowClickUntil) { e.preventDefault(); return; }
      pttPress(detail);                          // owned/pending: unkeys at ANY detail; idle: may key
    });
    // A held Enter/Space auto-repeats. A repeat never KEYS (it would chatter the TX); while this tab
    // owns or is claiming ptt it is let through, so it unkeys like any other press (I4).
    ptt.addEventListener("keydown", (e) => {
      if (!e.repeat || !(e.key === "Enter" || e.key === " " || e.key === "Spacebar")) return;
      if (!(ownedPtt() || pttPending)) e.preventDefault();
    });
  }
  // Hiding the tab or leaving the page releases what this tab holds; a claim still pending then is
  // released when its grant lands (applyTx -> pageAway()). A touch hold still timing is cancelled,
  // and while hidden / gone no claim is sent at all (txSendClaim). Back from the bfcache (pageshow)
  // or visible again, presses and grants work as usual.
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) { cancelPttHold(); pttKeyEnd(); autoRelease(); }
    else pageGone = false;
  });
  window.addEventListener("pagehide", () => { pageGone = true; cancelPttHold(); pttKeyEnd(); autoRelease(); });
  window.addEventListener("pageshow", () => { pageGone = false; });

  // ---- PTT hotkey: a key on THIS device (user menu > PTT hotkey…, device.js) ----
  // localStorage "telerig.pttkey" = {"code": <KeyboardEvent.code>, "mode": "hold" | "toggle", "label"}.
  // The key follows the PTT button's asymmetric, fail-safe rule (I4, G-1 c):
  //  - while this tab owns ptt or has a ptt claim pending, a press of the key ALWAYS unkeys, also
  //    with a dialog open and as part of a chord (the one exception: a key that types, pressed in a
  //    text field, is text);
  //  - only KEYING is held back: the re-key guard, another owner and a hidden page (all inside
  //    pttPress / txSendClaim), and here a text field with a key that types, a shortcut chord
  //    (Ctrl/Alt/Meta + key), a STOP control holding the focus, any visible dialog / confirm bar /
  //    menu, Arrange mode, Space while a control has the focus from the KEYBOARD (Space is that
  //    control's key; a button that only got the focus from a click does not keep it), and a held
  //    key's auto-repeat;
  //  - "hold": key down = PTT, key up = unkey, and losing the window focus, hiding the tab or a
  //    changed hotkey unkey too. The release is sent whatever happened to the claim meanwhile (an
  //    unanswered claim is given up after 3 s): a grant that lands after the key went up is
  //    released, never left keyed. "toggle": each press is one click on PTT.
  //  - Shift, Ctrl and Alt can't be the hotkey (they are part of other shortcuts; AltGr reports
  //    Ctrl + Alt and types characters): a function key or a foot switch that sends one is the choice.
  // A key this code acts on is kept from scrolling the page or clicking the focused button; a STOP
  // control always gets its key.
  const PTT_KEY_STORE = "telerig.pttkey";
  const PTT_KEY_DENY = new Set(["Unidentified", "Escape", "Tab", "Enter", "NumpadEnter", "Backspace", "Delete",
    "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End", "PageUp", "PageDown", "F5", "F11", "F12",
    "MetaLeft", "MetaRight", "OSLeft", "OSRight", "ContextMenu", "PrintScreen", "CapsLock", "NumLock",
    "ScrollLock", "Fn", "FnLock", "Power", "Sleep", "WakeUp",
    "ShiftLeft", "ShiftRight", "ControlLeft", "ControlRight", "AltLeft", "AltRight", "AltGraph"]);
  const PTT_KEY_STOPS = "#onAirStop, #txStopFab, #cwTx";     // STOP controls: their keys are their own
  function pttKeyAllowed(code) {
    return typeof code === "string" && /^[A-Za-z0-9]{1,32}$/.test(code) && !PTT_KEY_DENY.has(code);
  }
  // true for a key that puts text into a field (letters, digits, Space, punctuation, the numpad)
  function pttKeyTypes(code) { return !(/^F\d{1,2}$/.test(code) || code === "Pause"); }
  let pttKey = null;                               // {code, mode, label} | null
  let pttKeyHeld = false;                          // hold mode: this press keyed (or is claiming) and is still down
  let pttKeySeq = 0;                               // hold mode: the seq of the claim the held key sent
  let pttKeyDeadSeq = 0;                           // the newest held claim whose key is already up
  let pttKeyDown = false;                          // we took the key-down: its key-up is ours too
  let pttPtrAt = -1e9, pttFocusKbd = false;        // was the focus last moved by a pointer or by the keyboard?
  document.addEventListener("pointerdown", () => { pttPtrAt = performance.now(); }, true);
  document.addEventListener("focusin", () => { pttFocusKbd = performance.now() - pttPtrAt > 600; }, true);
  // The held key is up (key-up, window blur, hidden tab, a changed hotkey): ALWAYS send the release
  // for the claim it sent, owned or not (pending, given up, or lost: the server ignores an auto
  // release of a claim that is not ours), and remember it, so a grant that still lands is unkeyed.
  function pttKeyEnd() {
    const seq = pttKeySeq;
    pttKeyHeld = false; pttKeySeq = 0;
    if (!seq) return;
    if (seq > pttKeyDeadSeq) pttKeyDeadSeq = seq;
    if (ownedPtt()) send({ action: "ptt", tx: false });
    else if (seq > answeredSeq) send({ action: "ptt", tx: false, auto: true });   // unanswered: a grant may still land
    pttPending = null;
    renderTx();
  }
  function loadPttKey() {
    let k = null;
    try {
      const raw = JSON.parse(localStorage.getItem(PTT_KEY_STORE) || "null");
      if (raw && pttKeyAllowed(raw.code))
        k = { code: raw.code, mode: raw.mode === "toggle" ? "toggle" : "hold",
              label: typeof raw.label === "string" && raw.label ? raw.label.slice(0, 24) : raw.code };
    } catch (_) {}
    pttKeyEnd();                                   // the key changed under a held press
    pttKeyDown = false;
    pttKey = k;
    if (ptt) ptt.title = k ? "PTT — hotkey: " + k.label + (k.mode === "hold" ? " (hold to talk)" : " (press to toggle)") : "";
  }
  function pttKeyTyping(el) {                      // the focus is somewhere that takes typed text
    if (!el || el === document.body || el === document) return false;
    const tag = String(el.tagName || "").toUpperCase();
    if (tag === "TEXTAREA" || tag === "SELECT" || el.isContentEditable === true) return true;
    if (tag !== "INPUT") return false;
    const t = String(el.type || "text").toLowerCase();
    return !["button", "submit", "reset", "checkbox", "radio", "range", "color", "file", "image"].includes(t);
  }
  function pttKeyVisible(el) { return !!el && !el.hidden && (typeof el.getClientRects !== "function" || el.getClientRects().length > 0); }
  function pttKeyBlocked() {                       // the key belongs to a dialog / menu / Arrange mode: never KEY
    const b = document.body;
    if (b && b.classList && (b.classList.contains("tr-modal-open") || b.classList.contains("arranging"))) return true;
    if (TR.menu && typeof TR.menu.isOpen === "function" && TR.menu.isOpen()) return true;
    // any rendered dialog, confirm bar or menu, modal or not (a popover too)
    const open = document.querySelectorAll('.tr-confirm, [role="dialog"], [role="alertdialog"], [role="menu"]');
    for (const el of open) if (pttKeyVisible(el)) return true;
    return false;
  }
  function pttKeyOnStop(el) { return !!(el && typeof el.closest === "function" && el.closest(PTT_KEY_STOPS)); }
  function pttKeySpaceForControl(e) {              // Space for a control that has the focus from the keyboard
    if (pttKey.code !== "Space" || !pttFocusKbd) return false;
    const el = e.target;
    if (!el || el === document.body || el === document || typeof el.getAttribute !== "function") return false;
    const tag = String(el.tagName || "").toUpperCase();
    if (["BUTTON", "A", "SUMMARY", "INPUT", "SELECT", "TEXTAREA"].includes(tag)) return true;
    return /^(button|switch|checkbox|radio|tab|menuitem|menuitemcheckbox|menuitemradio|option|slider|link)$/.test(el.getAttribute("role") || "");
  }
  function onPttKeyDown(e) {
    if (!pttKey || e.code !== pttKey.code) return;
    if (pttKeyTypes(pttKey.code) && pttKeyTyping(e.target)) return;   // text for the field
    const mine = ownedPtt() || !!pttPending;
    const onStop = pttKeyOnStop(e.target);
    if (!mine) {                                    // only KEYING is held back (I4):
      if (e.ctrlKey || e.altKey || e.metaKey) return;                  // a shortcut chord, not the hotkey
      if (onStop || pttKeyBlocked()) return;                           // a STOP control / an open panel keeps its key
      if (pttKeySpaceForControl(e)) return;                            // Space is that control's key
    }
    if (!onStop) { e.preventDefault(); e.stopPropagation(); }          // a STOP control still gets its key
    pttKeyDown = true;
    if (e.repeat) return;                                           // a held key neither keys nor unkeys again
    if (mine) { pttKeyHeld = false; pttKeySeq = 0; pttRelease(); return; }   // a press always unkeys (I4)
    pttPress(0);                                                    // the guard / busy / hidden-page rules are in there
    if (pttKey.mode === "hold" && pttPending) { pttKeyHeld = true; pttKeySeq = pttPending.seq; }
  }
  function onPttKeyUp(e) {
    if (!pttKey || e.code !== pttKey.code) return;
    if (pttKeyDown && !pttKeyOnStop(e.target)) { e.preventDefault(); e.stopPropagation(); }   // Space's key-up would click the focused button
    pttKeyDown = false;
    pttKeyEnd();                                                    // hold: key up = unkey, whatever the claim did meanwhile
  }
  document.addEventListener("keydown", onPttKeyDown, true);
  document.addEventListener("keyup", onPttKeyUp, true);
  window.addEventListener("blur", () => {                           // the key-up will not reach this window
    pttKeyDown = false;
    pttKeyEnd();
  });
  document.addEventListener("tr:pttkey", loadPttKey);              // device.js saved a new key
  window.addEventListener("storage", (e) => { if (e && e.key === PTT_KEY_STORE) loadPttKey(); });
  loadPttKey();

  // ---- ON-AIR chip, two-tap STOP and the STOP FAB (§4.5) ----
  function fmtMmSs(ms) {
    const s = Math.max(0, Math.floor(ms / 1000));
    return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
  }
  // The two-tap STOP is armed for ONE owner: the owner's conn (EXTERNAL = -1) and name at the first
  // tap. The second tap sends tx_stop only while that same owner still holds TX; any other owner (or
  // none) disarms it, so a stale arm can never stop someone else's transmission (D-16, I23). The rule
  // is TR.stopArm (tr.js), shared with the CW STOP and Admin → Activity; this only draws the chip.
  const topStop = TR.stopArm((o) => {             // armed for owner o (3 s), or disarmed (null)
    const b = $("onAirStop"), l = $("onAirStopLbl");
    if (b) b.classList.toggle("armed", !!o);
    const text = o ? "Tap again to stop " + ownerName(o) : "STOP";
    if (l && l.textContent !== text) l.textContent = text;
    if (o) TR.srLive("Tap STOP again within 3 seconds to stop " + ownerName(o));
  });
  function disarmStop() { topStop.disarm(); }
  function checkStopArm() { topStop.check(txOwner()); }   // every tx frame: the armed owner gone -> disarm
  function renderOnAir() {
    const chip = $("onAir");
    if (!chip) return;
    const o = txOwner();
    checkStopArm();
    if (!o) { if (!chip.hidden) chip.hidden = true; disarmStop(); return; }
    chip.hidden = false;
    chip.classList.toggle("mine", TR.isTxOwner());
    const who = TR.isTxOwner() ? "YOU" : ownerName(o);
    const w = $("onAirWho"), t = $("onAirTime");
    if (w && w.textContent !== " · " + who) w.textContent = " · " + who;
    if (t) t.textContent = " · " + fmtMmSs(performance.now() - txSince);
    const tip = "ON AIR · " + who;                // the chip text may be cut with an ellipsis (narrow bar)
    if (chip.title !== tip) chip.title = tip;
  }
  if ($("onAirStop")) $("onAirStop").addEventListener("click", () => {
    const o = txOwner();
    if (!o) { disarmStop(); return; }
    if (TR.isTxOwner()) { disarmStop(); txStop(); return; }          // the owner: one tap
    if (!TR.can("tx")) return;
    // first tap: arm for o; second tap: "fire" only for the owner the first tap was armed for (F18)
    if (topStop.tap(o) === "fire") send({ action: "tx_stop" });
  });
  let pttVisible = true;                          // the IntersectionObserver's view of #pttBtn
  if (ptt && "IntersectionObserver" in window) {
    new IntersectionObserver((entries) => {
      for (const en of entries) pttVisible = en.isIntersecting && en.intersectionRatio >= 0.5;
      renderFab();
    }, { threshold: [0, 0.5, 1] }).observe(ptt);
  }
  function renderFab() {
    const f = $("txStopFab");
    if (!f) return;
    const show = TR.isTxOwner() && !pttVisible;   // PTT off-screen, parked or collapsed
    if (f.hidden === show) f.hidden = !show;
    const t = $("txStopFabTime");
    if (show && t) t.textContent = fmtMmSs(performance.now() - txSince);
  }
  if ($("txStopFab")) $("txStopFab").addEventListener("click", () => txStop());
  // While someone else owns TX, the TX toggles are disabled (F18): the only way to stop them is
  // the two-tap STOP. (#cwTx is handled by cwtool.js, which keeps its two-tap STOP for CW.)
  function renderTxLocks() {
    const o = otherOwner();
    const els = [$("voxBtn"), $("tuneBtn"), ...document.querySelectorAll("#atasTune .atas-btn")];
    for (const el of els) {
      if (!el) continue;
      if (el.dataset.trTitle === undefined) el.dataset.trTitle = el.title || "";
      el.disabled = !!o;
      el.title = o ? (o.external ? "The radio is keyed from its front panel" : o.user + " is transmitting")
                   : el.dataset.trTitle;
    }
  }
  function renderTx() { renderPtt(); renderOnAir(); renderTxLocks(); renderFab(); }

  // ---- wheel and touch guards (§3.17, G-12, G-13) ----
  let lastScrollTs = 0;
  window.addEventListener("scroll", () => { lastScrollTs = performance.now(); }, { capture: true, passive: true });
  function scrollableAncestor(el) {
    for (let p = el.parentElement; p; p = p.parentElement) {
      const oy = getComputedStyle(p).overflowY;
      if ((oy === "auto" || oy === "scroll") && p.scrollHeight > p.clientHeight + 1) return p;
    }
    const se = document.scrollingElement;
    return se && se.scrollHeight > se.clientHeight + 1 ? se : null;
  }
  // Inside something that scrolls, the wheel tunes only after the pointer rested 400 ms on the
  // element with no scrolling, so a quick wheel over the scope keeps scrolling the page.
  function hoverIntent(el) {
    if (!scrollableAncestor(el)) return true;
    const t = performance.now();
    return el._hoverSince > 0 && t - el._hoverSince >= 400 && t - lastScrollTs >= 400;
  }
  function trackHover(el) {
    if (!el) return;
    el._hoverSince = 0;
    el.addEventListener("pointerenter", () => { el._hoverSince = performance.now(); });
    el.addEventListener("pointerleave", () => { el._hoverSince = 0; });
  }

  // ---- tuning on the scope: tap to select a frequency, drag/slide to spin ----
  // Positions are scaled from CSS pixels to the scope's drawing width (scope.W), and a tap uses the
  // scope's own pixel->frequency map, so the edges tune to exactly the edge labels.
  const wrap = $("scopeWrap");
  trackHover(wrap);
  let scDown = false, scStartPx = 0, scLastPx = 0, scLastCss = 0, scMoved = 0, scAccum = 0;
  let scDownAt = 0, scType = "mouse";
  function scopePos(e) {
    const r = $("overlay").getBoundingClientRect();
    return { px: (e.clientX - r.left) * scope.W / (r.width || 1), css: e.clientX - r.left };
  }
  wrap.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 || !e.isPrimary || !TR.can("operate") || Layout().arranging()) return;
    if (e.pointerType === "touch" && performance.now() - lastScrollTs < 300) return;   // a scroll, not a tap
    if (e.pointerType === "mouse") e.preventDefault();
    scDownAt = performance.now(); scType = e.pointerType || "mouse";
    const p = scopePos(e);
    scDown = true; scMoved = 0; scAccum = 0;
    scStartPx = scLastPx = p.px; scLastCss = p.css;
    try { wrap.setPointerCapture(e.pointerId); } catch (_) {}
  });
  wrap.addEventListener("pointermove", (e) => {
    if (!scDown) return;
    const p = scopePos(e);
    const dx = p.px - scLastPx;
    scMoved += Math.abs(p.css - scLastCss);
    scLastPx = p.px; scLastCss = p.css;
    if (scMoved > 6) {                                // a drag -> pan the frequency
      const [lo, hi] = scope.visibleRange();
      scAccum += -(dx / scope.W) * (hi - lo);         // drag right -> tune down (content follows finger)
      const n = (scAccum / step) | 0;
      if (n !== 0) { queueTune(n * step); scAccum -= n * step; }
    }
  });
  function scEnd(e) {
    if (!scDown) return; scDown = false;
    try { wrap.releasePointerCapture(e.pointerId); } catch (_) {}
    if (scMoved > 6) { flushTune(); return; }         // the trailing drag delta
    // A touch tap must be short (a long press is not a tap); mouse and pen keep any duration (AR-59).
    if (scType === "touch" && performance.now() - scDownAt >= 600) return;
    // Fine snap (10 Hz on a narrow view): lands where you tapped. On the AF scope in CW, `retune`
    // puts the tapped signal ON the marker (heard at the pitch), not at the carrier point (AfMap).
    const [vlo, vhi] = scope.visibleRange(), q = vhi - vlo <= 10000 ? 10 : 100;
    const hz = Math.round((scope.freqAtX(scStartPx) + (scope.meta.retune || 0)) / q) * q;
    if (hz > 0) send({ action: "set_freq", hz });
  }
  wrap.addEventListener("pointerup", scEnd);
  wrap.addEventListener("pointercancel", () => { scDown = false; flushTune(); });
  let scWheelEnd = 0;
  wrap.addEventListener("wheel", (e) => {
    if (!TR.can("operate") || Layout().arranging() || !hoverIntent(wrap)) return;   // the page scrolls
    e.preventDefault();
    queueTune((e.deltaY < 0 ? 1 : -1) * step);
    clearTimeout(scWheelEnd);
    scWheelEnd = setTimeout(flushTune, 150);          // the wheel's end: the trailing delta
  }, { passive: false });

  // ---- per-digit VFO tuning (click the readout digits up/down, or scroll them) ----
  const bandsEl = $("bands");
  trackHover(bandsEl);
  function digitDelta(d, up, viaWheel) {
    const place = +d.dataset.place || 0;
    if (!place) return;
    const row = d.closest(".bandrow");
    if (row && !row.classList.contains("active")) {   // clicking the non-operating band selects it first
      send({ action: "select_band", band: row.dataset.band });
      return;
    }
    const hz = Math.max(0, (state.freq || 0) + (up ? place : -place));
    state.freq = hz;                                  // optimistic so rapid clicks/scrolls accumulate
    if (viaWheel) sendSoon("freq", { action: "set_freq", hz });
    else sendNow("freq", { action: "set_freq", hz });
  }
  if (bandsEl) {
    let digitWheelEnd = 0;
    bandsEl.addEventListener("click", (e) => {
      const d = e.target.closest(".fd"); if (!d) return;
      e.stopPropagation();                            // don't also fire the row's band-select
      if (!TR.can("operate") || Layout().arranging()) return;
      digitDelta(d, e.offsetY < d.clientHeight / 2, false);
    });
    bandsEl.addEventListener("wheel", (e) => {
      const d = e.target.closest(".fd"); if (!d) return;
      if (!TR.can("operate") || Layout().arranging() || !hoverIntent(bandsEl)) return;
      e.preventDefault();
      digitDelta(d, e.deltaY < 0, true);
      clearTimeout(digitWheelEnd);
      digitWheelEnd = setTimeout(() => flushKey("freq"), 150);
    }, { passive: false });
  }

  // The no-scope notice (FT-991A, FT-891, IC-728) sits in the middle of the WATERFALL, so it never
  // covers the band-plan strip drawn along the spectrum's bottom edge, whatever the split.
  function placeNoScope() {
    const ns = $("noScope"), wf = $("waterfall");
    if (!ns || ns.hidden || !wf || !wf.offsetHeight) return;
    const top = wf.offsetTop + Math.max(6, (wf.offsetHeight - ns.offsetHeight) / 2);
    ns.style.top = Math.round(top) + "px";
    ns.style.bottom = "auto";
  }

  // ---- spectrum / waterfall split (§3.16) ----
  // The split is a FRACTION of the scope height, clamped to BOUNDS.split before it is applied or
  // saved, per device. It is applied where the waterfall flexes to fill a layout-sized scope (the
  // scope tile on both devices); where the scope's height followed its content the fraction would
  // feed back into the height, so there the spectrum keeps its CSS / dragged pixel height.
  const SPLIT_BOUNDS = (TR.boot.layout_bounds && Array.isArray(TR.boot.layout_bounds.split))
    ? TR.boot.layout_bounds.split : [0.15, 0.85];
  function clampSplit(f) {
    f = +f;
    if (!isFinite(f) || f <= 0) f = 0.38;
    return Math.min(SPLIT_BOUNDS[1], Math.max(SPLIT_BOUNDS[0], f));
  }
  function splitDevice() { return matchMedia("(min-width: 900px)").matches ? "desktop" : "phone"; }
  const splitByDev = (function () {
    const L = Layout();                           // layout.js knows the stored (or cached) fraction
    if (L && typeof L.scopeSplit === "function")
      return { desktop: clampSplit(L.scopeSplit("desktop")), phone: clampSplit(L.scopeSplit("phone")) };
    const s = ((TR.boot.layout || {}).scope_split) || {};
    return { desktop: clampSplit(s.desktop), phone: clampSplit(s.phone) };
  })();
  // reset / undo / the server's echo / a device switch hand over a new fraction (layout.js)
  document.addEventListener("tr:split", (e) => {
    const d = e.detail || {};
    if (d.device !== "desktop" && d.device !== "phone") return;
    splitByDev[d.device] = clampSplit(d.fraction);
    if (d.device === splitDevice()) { applySplit(); scope.resize(); placeNoScope(); }
  });
  let specSetBy = null;                           // "apply" | "drag" | null: who set the inline height
  function splitApplies() {
    const wf = $("waterfall");
    return !!wf && parseFloat(getComputedStyle(wf).flexGrow) > 0;
  }
  function applySplit() {
    const sw = $("scopeWrap"), spec = $("spectrum");
    if (!sw || !spec) return;
    if (!splitApplies()) {
      if (specSetBy === "apply") { spec.style.height = ""; specSetBy = null; }
      return;
    }
    const H = sw.clientHeight;
    if (!H) return;                               // parked / collapsed: nothing to fit
    const px = Math.max(60, Math.min(H - 110, Math.round(H * clampSplit(splitByDev[splitDevice()]))));
    spec.style.height = px + "px";
    specSetBy = "apply";
  }
  (function () {
    const split = $("scopeSplit"), spec = $("spectrum"), sw = $("scopeWrap");
    if (!split || !spec || !sw) return;
    let down = false;
    split.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      down = true; split.classList.add("drag");
      try { split.setPointerCapture(e.pointerId); } catch (_) {}
      // keep this gesture on the split: don't let the wrap's tuning handler also
      // start (it would steal the pointer capture, so our pointerup never fires).
      e.preventDefault(); e.stopPropagation();
    });
    split.addEventListener("pointermove", (e) => {
      if (!down || !(e.buttons & 1)) return;   // ignore plain hover moves after release
      e.stopPropagation();
      const r = sw.getBoundingClientRect();
      spec.style.height = Math.max(60, Math.min(r.height - 110, e.clientY - r.top)) + "px";   // height only
      specSetBy = "drag";
      placeNoScope();
    });
    const end = (e) => {
      if (!down) return; down = false; split.classList.remove("drag");
      try { split.releasePointerCapture(e.pointerId); } catch (_) {}
      const H = sw.clientHeight;
      if (H > 0) {
        const f = clampSplit(spec.offsetHeight / H);   // clamped BEFORE it is applied or saved (AR-26)
        splitByDev[splitDevice()] = f;
        applySplit();
        try { Layout().setScopeSplit(f); } catch (_) {}
      }
      scope.resize();                                  // once, at the end of the drag
      placeNoScope();
    };
    split.addEventListener("pointerup", end);
    split.addEventListener("pointercancel", end);
    split.addEventListener("lostpointercapture", end);   // safety: capture lost -> stop dragging
  })();
  // Re-fit the scope when its box changes (150 ms trailing debounce); a tile resize gesture reports
  // its final size at tr:resizestop, so the observer is paused in between (§3.16).
  (function () {
    let t = 0, paused = false;
    const refit = () => { applySplit(); scope.resize(); placeNoScope(); };
    document.addEventListener("tr:resizestart", () => { paused = true; });
    document.addEventListener("tr:resizestop", () => { paused = false; refit(); });
    document.addEventListener("tr:layout", () => {
      refit();
      if (afTimer && rxBus) startAfScope(rxBus);       // the FFT size follows the scope width
    });
    if ("ResizeObserver" in window && $("scopeWrap")) {
      new ResizeObserver(() => {
        if (paused) return;
        clearTimeout(t);
        t = setTimeout(refit, 150);
      }).observe($("scopeWrap"));
    }
  })();

  // (the day / night theme lives in tr.js: TR.applyTheme, html.day, set pre-paint by the head script)

  // ---- band plan (ARRL band plan on VHF/UHF, FCC license-class on HF) ----
  // Drawn in its own strip just above the scope (#bandStrip, shown while BAND PLAN is on), so it
  // never covers the spectrum; hovering it (or tapping it on a phone) names the segment.
  (function () {
    const btn = $("bandPlanBtn"), tip = $("bandTip"), ov = $("overlay"), wrap = $("scopeWrap"), strip = $("bandStrip");
    if (!btn || !ov || !wrap) return;
    scope.bandplan = (window.BANDPLAN || []).map((s) => ({ ...s, lo: s.lo * 1e6, hi: s.hi * 1e6 }));
    const colors = window.BANDPLAN_COLORS || {}, klabel = window.BANDPLAN_KIND_LABEL || {};
    const legend = $("bandLegend"), hint = $("scopeHint");

    function updateLegend() {
      if (!legend) return;
      if (!scope.showBandplan) { legend.hidden = true; legend._sig = null; if (hint) hint.hidden = false; return; }
      const kinds = scope.visibleKinds(), sig = kinds.join(",");
      if (sig === legend._sig) return;            // unchanged -> skip re-render
      legend._sig = sig;
      legend.innerHTML = kinds.map((k) =>
        '<span class="lg"><span class="lg-sw" style="background:' + ((colors[k] || "rgba(150,160,180,") + "0.85)") +
        '"></span>' + (klabel[k] || k) + "</span>").join("");
      legend.hidden = kinds.length === 0;
      if (hint) hint.hidden = kinds.length > 0;   // swap the hint out for the legend when we have one
    }

    function apply(on) {
      scope.showBandplan = on;
      if (strip) strip.hidden = !on;                // the scope re-fits to the space left (ResizeObserver)
      btn.classList.toggle("active", on);
      if (!on && tip) tip.hidden = true;
      scope.drawOverlay();
      updateLegend();
      try { localStorage.setItem("telerig.bandplan", on ? "1" : "0"); } catch (_) {}
    }
    btn.addEventListener("click", () => apply(!scope.showBandplan));
    let saved = "1";                                // band plan ON by default
    try { saved = localStorage.getItem("telerig.bandplan") || "1"; } catch (_) {}
    apply(saved === "1");
    setInterval(updateLegend, 600);               // refresh the color key as you tune / change span

    const hf = $("hoverFreq");
    wrap.addEventListener("mousemove", (e) => {
      const r = ov.getBoundingClientRect();
      const px = (e.clientX - r.left) * (scope.W / r.width);
      // cursor frequency readout — always, over the whole spectrum + waterfall
      if (hf) {
        const f = scope.freqAtX(px), [vlo, vhi] = scope.visibleRange();
        if (f > 0) {
          hf.textContent = (f / 1e6).toFixed(vhi - vlo <= 10000 ? 5 : 4) + " MHz";   // 10 Hz on a narrow view
          hf.hidden = false;
          let x = e.clientX + 12, y = e.clientY - 28;
          if (x + hf.offsetWidth > window.innerWidth - 8) x = e.clientX - hf.offsetWidth - 12;
          if (y < 4) y = e.clientY + 16;
          hf.style.left = x + "px"; hf.style.top = y + "px";
        } else hf.hidden = true;
      }
      // band-plan segment tooltip: on the strip above the scope (below); drawn over the spectrum
      // only when the page has no strip
      if (strip) return;
      const py = (e.clientY - r.top) * ((scope.scopeH || (scope.specH + scope.wfH)) / r.height);
      bandTip(e, px, py);
    });
    let tipT = 0;
    function bandTip(e, px, py) {
      if (!scope.showBandplan || !tip) return;
      const seg = scope.bandplanSegAt(px, py);
      if (!seg) { tip.hidden = true; return; }
      const base = colors[seg.kind] || "rgba(150,160,180,";
      tip.innerHTML =
        '<div class="bt-h"><span class="bt-sw" style="background:' + base + '0.9)"></span>' + seg.label + "</div>" +
        '<div class="bt-rng">' + (seg.lo / 1e6).toFixed(3) + " – " + (seg.hi / 1e6).toFixed(3) +
        " MHz · " + (klabel[seg.kind] || seg.kind) + "</div>" +
        '<div class="bt-d">' + seg.desc + "</div>";
      tip.hidden = false;
      const tw = tip.offsetWidth, th = tip.offsetHeight;
      let x = e.clientX + 14, y = e.clientY + 14;
      if (x + tw > window.innerWidth - 8) x = e.clientX - tw - 14;
      if (y + th > window.innerHeight - 8) y = e.clientY - th - 14;
      tip.style.left = x + "px"; tip.style.top = y + "px";
      clearTimeout(tipT);
      if (e.type === "click") tipT = setTimeout(() => { tip.hidden = true; }, 4000);   // a tap: no mouseleave follows
    }
    if (strip) {
      const onStrip = (e) => {
        const r = strip.getBoundingClientRect();
        bandTip(e, (e.clientX - r.left) * (scope.W / (r.width || 1)), 0);
      };
      strip.addEventListener("mousemove", onStrip);
      strip.addEventListener("click", onStrip);
      strip.addEventListener("mouseleave", () => { clearTimeout(tipT); if (tip) tip.hidden = true; });
    }
    wrap.addEventListener("mouseleave", () => { if (tip && !strip) tip.hidden = true; if (hf) hf.hidden = true; });
  })();

  // ---- radio profiles (bands/modes/steps render from the selected radio) ----
  function selectedRadio() { return radios.find(p => p.id === $("radioSel").value) || radios[0] || null; }
  // ---- SET menu (Setup tab): data-driven accordion from currentRadio.menu ----
  let menuItems = {}, menuCache = {};
  function renderMenu(p) {
    const root = $("menuRoot"); if (!root) return;
    menuItems = {}; menuCache = {}; root.innerHTML = "";
    const items = (p && p.menu) || [];
    if (!items.length) { root.hidden = true; return; }
    root.hidden = false;
    for (const it of items) menuItems[it.num] = it;
    const order = [], byGroup = {};
    for (const it of items) {
      if (!byGroup[it.group]) { byGroup[it.group] = []; order.push(it.group); }
      byGroup[it.group].push(it);
    }
    const title = document.createElement("div");
    title.className = "grp-title";
    title.innerHTML = 'SET MENU <span class="menu-hint">tap a section to read it from the radio</span>';
    root.appendChild(title);
    for (const g of order) {
      const sec = document.createElement("div"); sec.className = "menu-grp"; sec.dataset.group = g;
      // gate-exempt: viewers may open groups and read the values (F23); the inputs stay gated
      const head = document.createElement("button"); head.className = "menu-ghead gate-exempt"; head.type = "button";
      head.innerHTML = `<span>${rhEsc(g)}</span><span class="menu-count">${byGroup[g].length}</span>`;
      const body = document.createElement("div"); body.className = "menu-gbody hide";
      head.addEventListener("click", () => toggleMenuGroup(g, sec, body, byGroup[g]));
      sec.appendChild(head); sec.appendChild(body); root.appendChild(sec);
    }
  }
  function toggleMenuGroup(g, sec, body, items) {
    const open = body.classList.toggle("hide") === false;
    sec.classList.toggle("open", open);
    if (!open) return;
    if (!body._built) { for (const it of items) body.appendChild(buildMenuItem(it)); body._built = true; }
    if (state.connected) send({ action: "menu_read_group", group: g });   // lazy: read this group only
  }
  // Menu gating (§4.6, F8, AR-13, AR-14): critical items are admin-only (locked with "admin only" for
  // everyone else); writing TX TOT = OFF asks twice and sends confirm:"tot_off"; routing PTT to an
  // RTS/DTR line warns and sends confirm:"tx_route". The server decides all of it again.
  const LOCK_SVG = '<svg class="mi-lock" aria-hidden="true"><use href="#i-lock"/></svg>';   // inline sprite (§4.4)
  function menuLocked(it) { return !TR.can("menu") || (!!it.critical && !TR.can("menu_critical")); }
  function buildMenuItem(it) {
    const row = document.createElement("div");
    const locked = menuLocked(it);
    row.className = "menu-item" + (it.critical ? " critical" : "") + (locked && it.critical ? " locked" : "");
    row.dataset.num = it.num;
    const name = document.createElement("div"); name.className = "mi-name";
    const adminOnly = it.critical && !TR.can("menu_critical")
      ? ' <span class="mi-admin" title="Only an admin can change this setting">' + LOCK_SVG + "admin only</span>" : "";
    name.innerHTML = `<span>${rhEsc(it.name)}${it.critical ? ' <span class="mi-warn" title="connection / transmit-sensitive">&#9888;</span>' : ''}${adminOnly}</span>` +
                     `<span class="mi-num">${String(it.num).padStart(3, "0")}</span>`;
    if (it.note) name.title = it.note;
    row.appendChild(name);
    if (it.readonly) {
      const ro = document.createElement("span"); ro.className = "mi-ro"; ro.dataset.role = "val"; ro.textContent = "—";
      row.appendChild(ro);
    } else if (it.kind === "enum") {
      const ctl = document.createElement("div"); ctl.className = "mi-ctl";
      const sel = document.createElement("select"); sel.dataset.role = "input";
      for (let i = 0; i < it.options.length; i++) {
        const o = document.createElement("option"); o.value = i; o.textContent = it.options[i];
        if (/reserved/i.test(it.options[i])) o.disabled = true;   // documented index gap — not selectable
        sel.appendChild(o);
      }
      sel.disabled = locked;
      sel.addEventListener("change", () => writeMenu(it, +sel.value));
      ctl.appendChild(sel); row.appendChild(ctl);
    } else {
      const ctl = document.createElement("div"); ctl.className = "mi-ctl";
      const r = document.createElement("input"); r.type = "range"; r.dataset.role = "input";
      r.min = it.min; r.max = it.max; r.step = it.step || 1;
      r.value = (it.kind === "signed-int") ? Math.max(it.min, Math.min(it.max, 0)) : it.min;
      r.disabled = locked;
      const val = document.createElement("span"); val.className = "mi-val"; val.dataset.role = "val"; val.textContent = "—";
      const show = () => { val.textContent = r.value + (it.unit ? " " + it.unit : ""); };
      r.addEventListener("input", show);
      r.addEventListener("change", () => writeMenu(it, +r.value));
      ctl.appendChild(r); ctl.appendChild(val); row.appendChild(ctl);
    }
    if (it.num in menuCache) applyMenuValue(it, row, menuCache[it.num]);
    return row;
  }
  function menuOption(it, value) {
    const o = it.kind === "enum" ? (it.options || [])[value] : undefined;
    return String(o === undefined ? value : o);
  }
  function isTotOff(it, value) {                 // the server decides by the encoded bytes; this asks first
    return !!it.tx_tot && (it.kind === "enum" ? menuOption(it, value).trim().toUpperCase() === "OFF" : +value === 0);
  }
  function routesToLine(it, value) {
    return !!it.tx_route && it.kind === "enum" && /RTS|DTR/i.test(menuOption(it, value));
  }
  const NNN = (it) => String(it.num).padStart(3, "0");
  const TOT_Q = (it) => `Turn "${it.name}" (menu ${NNN(it)}) OFF? The radio's own transmit time-out is the ` +
    "hardware backstop if a transmission ever sticks.";
  const ROUTE_Q = (it, value) => `Set "${it.name}" to ${menuOption(it, value)}? Routing PTT to RTS/DTR can key ` +
    "the radio immediately while TeleRig holds its serial port open.";
  let pendingMenu = null;                        // {it, value, confirm, at}: the last write, until answered
  async function writeMenu(it, value) {
    if (!state.connected || menuLocked(it)) { revertMenuItem(it); return; }
    let confirmTag = null;
    if (it.critical && !(await TR.confirm(`Change "${it.name}" (menu ${NNN(it)})? ` +
        (it.note || "This is a connection / transmit-sensitive setting."), { ok: "Change" }))) {
      revertMenuItem(it); return;                // cancelled -> restore the real value
    }
    if (isTotOff(it, value)) {                   // the second confirm (F8)
      if (!(await TR.confirm(TOT_Q(it), { ok: "Turn it off", danger: true }))) { revertMenuItem(it); return; }
      confirmTag = "tot_off";
    }
    if (routesToLine(it, value)) {
      if (!(await TR.confirm(ROUTE_Q(it, value), { ok: "Set it", danger: true }))) { revertMenuItem(it); return; }
      confirmTag = "tx_route";
    }
    sendMenuWrite(it, value, confirmTag);
  }
  function sendMenuWrite(it, value, confirmTag) {
    const msg = { action: "menu_write", num: it.num, value: value };
    if (confirmTag) msg.confirm = confirmTag;
    pendingMenu = { it, value, confirm: confirmTag, at: Date.now() };
    send(msg);
  }
  function revertPendingMenu() {
    const p = pendingMenu;
    pendingMenu = null;
    if (p) revertMenuItem(p.it);
  }
  // A denied menu_write: the server asks for a confirm we did not send (its encoder decided the
  // value is TOT=OFF / an RTS-DTR route) -> ask and resend once; anything else -> repaint the item.
  function handleMenuDenied(m) {
    const p = pendingMenu;
    if (p && Date.now() - p.at < 10000 && !p.confirm &&
        (m.reason === "tot_off_confirm" || m.reason === "tx_route_confirm")) {
      pendingMenu = null;
      const tag = m.reason === "tot_off_confirm" ? "tot_off" : "tx_route";
      const q = tag === "tot_off" ? TOT_Q(p.it) : ROUTE_Q(p.it, p.value);
      TR.confirm(q, { ok: tag === "tot_off" ? "Turn it off" : "Set it", danger: true }).then((ok) => {
        if (ok && state.connected) sendMenuWrite(p.it, p.value, tag); else revertMenuItem(p.it);
      });
      return true;
    }
    revertPendingMenu();
    return false;
  }
  function revertMenuItem(it) {
    if (it.num in menuCache) refreshMenuItem(it.num);                  // repaint from last known value
    else if (state.connected) send({ action: "menu_read", num: it.num });  // none cached -> ask the radio
  }
  function onMenuReconnect() {
    menuCache = {};                                                   // stale values from the old session
    readOpenMenuGroups();
  }
  function readOpenMenuGroups() {
    document.querySelectorAll(".menu-grp.open").forEach(sec =>
      send({ action: "menu_read_group", group: sec.dataset.group }));   // refresh whatever is open
  }
  // The menu queue answers busy {retry_after} when it is full (AR-32): retry the open groups ONCE.
  let menuRetryTimer = 0, menuRetried = false;
  function menuBusyRetry(after) {
    if (menuRetryTimer || menuRetried) return;
    const ms = Math.max(500, Math.min(10000, (+after || 2) * 1000));
    menuRetryTimer = setTimeout(() => { menuRetryTimer = 0; menuRetried = true; readOpenMenuGroups(); }, ms);
  }
  function updateMenu(values) {
    menuRetried = false;                         // the queue is serving again
    if (pendingMenu && String(pendingMenu.it.num) in values) pendingMenu = null;
    for (const k in values) { menuCache[+k] = values[k]; refreshMenuItem(+k); }
  }
  function refreshMenuItem(num) {
    const it = menuItems[num]; if (!it || !(num in menuCache)) return;
    const row = document.querySelector(`.menu-item[data-num="${num}"]`);
    if (row) applyMenuValue(it, row, menuCache[num]);
  }
  function applyMenuValue(it, row, value) {
    const inp = row.querySelector('[data-role="input"]'), valEl = row.querySelector('[data-role="val"]');
    if (it.readonly) { if (valEl) valEl.textContent = value; return; }
    if (it.kind === "enum") {
      if (inp && document.activeElement !== inp) {
        const idx = (typeof value === "number") ? value : it.options.indexOf(value);
        if (idx >= 0) inp.value = idx;
      }
    } else {
      if (inp && document.activeElement !== inp) inp.value = value;
      if (valEl) valEl.textContent = value + (it.unit ? " " + it.unit : "");
    }
  }

  async function loadRadios() {
    try {
      const j = await TR.api("/api/radios");
      radios = j.radios || [];
      const sel = $("radioSel"); sel.innerHTML = "";
      for (const p of radios) {
        const o = document.createElement("option"); o.value = p.id;
        o.textContent = (p.make ? p.make + " " : "") + p.name; sel.appendChild(o);
      }
    } catch (_) { radios = []; }
    return radios;
  }
  function renderRadio(p) {
    if (!p) return;
    currentRadio = p;
    const label = (p.make ? p.make + " " : "") + p.name;
    $("modelLabel").textContent = state.connected ? (state.radio_name || label) : label;
    const br = $("bandRow"); br.innerHTML = ""; br.classList.toggle("band-grid", p.bands.length > 4);
    for (const b of p.bands) {
      const btn = document.createElement("button");
      btn.className = "key band"; btn.dataset.act = "band"; btn.dataset.band = b.name;
      btn.textContent = b.name; br.appendChild(btn);
    }
    const mr = $("modeRow"); mr.innerHTML = "";
    for (const name of p.modes) {
      const btn = document.createElement("button");
      btn.className = "key mode"; btn.dataset.act = "mode"; btn.dataset.mode = name;
      btn.textContent = name; mr.appendChild(btn);
    }
    const st = $("step"); st.innerHTML = "";
    for (const s of p.steps) {
      const o = document.createElement("option"); o.value = s.v; o.textContent = s.label;
      if (s.v === p.default_step) o.selected = true; st.appendChild(o);
    }
    step = p.default_step;
    // capability-driven gating: show only what the radio supports (falls back to flat flags)
    const cap = p.capabilities || {};
    const capHas = (k, flat) => (k in cap ? cap[k] : flat);
    const sub = $("rowSub"); if (sub) sub.style.display = capHas("dual_watch", p.dual_watch) ? "" : "none";
    setBandsCues(state.connected ? state : null);
    const pa = $("preampBtn"); if (pa) {
      pa.style.display = capHas("preamp", p.has_preamp) ? "" : "none";
      const pl = p.preamp_labels || ["OFF", "P.AMP"];
      pa.textContent = pl[state.preamp || 0] || pl[0];
    }
    const at = $("attBtn"); if (at) at.style.display = capHas("att", p.has_att) ? "" : "none";
    const nb = $("narBtn"); if (nb) nb.style.display = capHas("narrow", false) ? "" : "none";
    { const dr = $("dataRow"); if (dr) dr.hidden = !capHas("data_mode", false); }   // Icom DATA switch (1A 06)
    // FM Tone/DCS group: base-hide unless the rig supports it (updateState shows it in FM modes)
    const fg = $("fmGrp"); if (fg && !capHas("fm_tone", false)) fg.style.display = "none";
    const tu = $("tunerBtn"); if (tu) tu.style.display = capHas("tuner", p.has_tuner) ? "" : "none";
    const tn = $("tuneBtn"); if (tn) tn.style.display = capHas("tuner", p.has_tuner) ? "" : "none";
    // hide the whole ANTENNA TUNER group (now in the TX panel) when the rig has no internal ATU
    const tg = $("tunerGrp"); if (tg) tg.style.display = capHas("tuner", p.has_tuner) ? "" : "none";
    // VFO A/B select: hidden when the rig has no CAT active-VFO selector (FT-991A); A=B/SWAP stay
    const vfoSel = capHas("vfo_select", true);
    document.querySelectorAll('[data-act="vfo"][data-code="0"], [data-act="vfo"][data-code="1"]')
      .forEach(b => { b.style.display = vfoSel ? "" : "none"; });
    // RX-DSP + TX-function buttons: only those the rig reports
    document.querySelectorAll('[data-act="rxfunc"][data-fn]').forEach(b => {
      const fn = b.dataset.fn;
      const list = ["nb", "nr", "anotch", "mnotch"].includes(fn) ? cap.rx_dsp : cap.tx_funcs;
      if (Array.isArray(list)) b.style.display = list.includes(fn) ? "" : "none";
    });
    // meter buttons: only the meters the rig supports
    if (Array.isArray(cap.meters)) document.querySelectorAll('[data-act="meter"][data-meter]')
      .forEach(b => { b.style.display = cap.meters.includes(b.dataset.meter) ? "" : "none"; });
    // no-scope radios (Yaesu CAT): hide the scope-only controls, show the notice
    const noScope = p.has_scope === false;
    const ns = $("noScope"); if (ns) ns.hidden = !noScope;
    placeNoScope();
    const soc = $("scopeOnlyCtl"); if (soc) soc.hidden = noScope;
    if (p.default_baud) $("baud").value = p.default_baud;
    // COM-only radios (FT-991A, IC-7300 family): no RS-BA1 LAN — hide that option
    const lanOpt = $("transport").querySelector('option[value="lan"]');
    if (lanOpt) lanOpt.hidden = p.has_network === false;
    if (p.has_network === false && $("transport").value === "lan") {
      $("transport").value = "sim";
    }
    // transmit-less rigs (IC-728: no CI-V key): hide the PTT button + the TX-audio (mic) path
    const canPtt = capHas("ptt", true);
    { const pb = $("pttBtn"); if (pb) pb.style.display = canPtt ? "" : "none"; }
    { const mb = $("micBtn"); if (mb) mb.style.display = canPtt ? "" : "none"; }
    { const mid = $("micInDev"); if (mid && mid.closest(".sl")) mid.closest(".sl").style.display = canPtt ? "" : "none"; }
    { const mm = $("micMeter"); if (mm) mm.style.display = canPtt ? "" : "none"; }
    // meter block: hide entirely when the rig exposes no meters over the control link (S-bar included);
    // the METER tile itself is gated by layout.js (§3.8)
    { const mtr = $("meterBlock");
      if (mtr) mtr.style.display = (Array.isArray(cap.meters) && cap.meters.length === 0) ? "none" : ""; }
    // control-panel tabs: hide the ones the rig can't drive + the Settings tab when it has no menu
    const tabOn = { rx: capHas("tab_rx", true), tx: capHas("tab_tx", true),
                    dsp: capHas("tab_dsp", true), setup: !!(p.menu && p.menu.length) };
    document.querySelectorAll(".controls .tab").forEach(t => {
      const name = t.getAttribute("data-t");
      if (name in tabOn) t.style.display = tabOn[name] ? "" : "none";
    });
    // if the currently-active tab just got hidden, fall back to the Radio tab
    const activeTab = document.querySelector(".controls .tab.on");
    if (activeTab && activeTab.style.display === "none") {
      const rt = document.querySelector('.controls .tab[data-t="radio"]');
      if (rt) rt.click();
    }
    // Radio-tab MEMORY recall group (the OPERATING group is already gated by syncExtPanel/ext_ops)
    { const mg = $("memGrp"); if (mg) mg.hidden = !capHas("memory", false); }
    // VFO A=B / SWAP: hide when the rig has no CI-V equalize/exchange (old CI-V only selects A/B)
    document.querySelectorAll('[data-act="vfo"][data-code="160"], [data-act="vfo"][data-code="176"]')
      .forEach(b => { b.style.display = capHas("vfo_swap", true) ? "" : "none"; });
    updateConnFields();
    renderMenu(p);
    syncExtPanel(state);         // show/hide the WIDTH/CONTOUR/APF/CW/ops groups for this radio+mode
    syncIcomCw(state);           // Icom CW/filter group visibility for this radio+mode
    // light the new keys from the state we already have, but only the connected radio's own state:
    // a radio picked while disconnected must not show another rig's (or a stale) band and mode
    if (state.connected && state.radio === p.id) markBandMode(state);
    applyTitles();
    applyStatusUi();
    try { Layout().setRadio(p); } catch (_) {}
  }
  // G-4 / F20: while a radio is connected, only an admin may switch it, and only after an inline
  // confirm (it disconnects the radio for everyone). An operator's change is reverted before
  // anything else happens.
  async function othersOnline() {
    try {
      const j = await TR.api("/api/admin/sessions", { noRedirect: true });
      return new Set((j.sessions || []).map((s) => s.user).filter((u) => u && u !== TR.user)).size;
    } catch (_) { return null; }
  }
  async function adminDisconnect(confirmText) {
    try {
      await TR.api("/api/disconnect", { method: "POST", json: { confirm: true }, quiet: true });
      return true;
    } catch (e) {
      if (e.status === 409 && e.code === "busy") {      // someone transmits: stopping them is explicit
        const ok = await TR.confirm((e.body.owner || "Someone") + " is transmitting — stop them and " + confirmText + "?",
                                    { ok: "Stop and continue", danger: true });
        if (!ok) return false;
        try { await TR.api("/api/disconnect", { method: "POST", json: { confirm: true, force: true } }); return true; }
        catch (e2) { TR.toast(e2.message, "error"); return false; }
      }
      TR.toast("Disconnect failed: " + e.message, "error");
      return false;
    }
  }
  let radioSelBusy = false;
  $("radioSel").addEventListener("change", async () => {
    const sel = $("radioSel"), want = sel.value;
    if (state.connected && currentRadio && want !== currentRadio.id) {
      sel.value = currentRadio.id;                 // revert until (and unless) the switch is confirmed
      if (!TR.can("switch_radio")) {
        TR.toast("Only an admin can switch the radio while it's connected", "warn");
        return;
      }
      if (radioSelBusy) return;
      radioSelBusy = true;
      try {
        const n = await othersOnline();
        const ok = await TR.confirm("Switching radio disconnects it for everyone" +
                                    (n ? " (" + n + " other user" + (n === 1 ? "" : "s") + " online)" : "") + ". Continue?",
                                    { ok: "Switch radio", danger: true });
        if (!ok || !(await adminDisconnect("switch the radio"))) return;
      } finally { radioSelBusy = false; }
      sel.value = want;
      $("conn").classList.add("open");             // reopen the connect controls so they can reconnect
    }
    renderRadio(selectedRadio());
    renderRadioHelp();
    applyConnForRadio($("radioSel").value);         // restore THIS radio's last connection + settings + audio
    CONNS.last = $("radioSel").value; persistConns();
    stopAllAudio();                                 // drop any audio/AF-scope from the old radio
    blanked = true;                                 // new radio: blank the readout + waterfall until reconnect
    scope.clear();
    renderFreq($("mainFreq"), 0, false);
    renderFreq($("subFreq"), 0, false);
    $("lblLeft").textContent = $("lblRight").textContent = "";
    applyStatusUi();
  });

  // ---- connection help popover ("?" beside the radio picker) ----
  function rhEsc(s) { return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }
  function renderRadioHelp() {
    const help = $("radioHelp"), p = selectedRadio();
    if (!help || !p) return;
    const secs = p.connect_help || [];
    let html = '<div class="rh-title">' + rhEsc((p.make ? p.make + " " : "") + p.name) +
      ' <small>— set these on the radio</small></div>';
    if (!secs.length) {
      html += '<div class="rh-sec">No special radio settings needed — just connect.</div>';
    } else {
      for (const s of secs) {
        html += '<div class="rh-sec"><div class="rh-h">' + rhEsc(s.title) + '</div><ul>' +
          (s.items || []).map((it) => "<li>" + rhEsc(it) + "</li>").join("") + "</ul></div>";
      }
    }
    help.innerHTML = html;
  }
  function toggleRadioHelp(force) {
    const help = $("radioHelp"), btn = $("radioHelpBtn");
    if (!help || !btn) return;
    const show = force === undefined ? help.hidden : force;
    if (show) renderRadioHelp();
    help.hidden = !show;
    btn.classList.toggle("on", show);
  }
  $("radioHelpBtn").addEventListener("click", (e) => { e.stopPropagation(); toggleRadioHelp(); });
  document.addEventListener("click", (e) => {
    const help = $("radioHelp");
    if (help && !help.hidden && !e.target.closest(".radio-pick")) toggleRadioHelp(false);
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") toggleRadioHelp(false); });

  // ---- connection controls ----
  async function loadPorts() {
    if (!TR.can("connect")) return null;            // viewers have no connect form (operator route)
    try {
      const j = await TR.api("/api/ports", { noRedirect: true });
      const sel = $("transport");
      sel.querySelectorAll("option:not([value='sim']):not([value='lan'])").forEach(o => o.remove());
      const lanOpt = sel.querySelector("option[value='lan']");
      for (const p of (j.ports || [])) {
        const o = document.createElement("option");
        o.value = p.device; o.dataset.kind = "serial";
        // keep the DISTINGUISHING part of the description (e.g. "Enhanced COM Port") —
        // the chip name is the same on both Yaesu ports, and CAT is only on Enhanced.
        let desc = p.description || "";
        const colon = desc.indexOf(":");
        if (colon >= 0) desc = desc.slice(colon + 1).trim();
        o.textContent = (`${p.device} — ${desc}` || p.device).slice(0, 64);
        o.title = `${p.device} — ${p.description}`;
        if (/enhanced/i.test(p.description || "")) o.dataset.enhanced = "1";
        sel.insertBefore(o, lanOpt);
      }
      return j;
    } catch (_) { return null; }
  }

  // The LOCAL #transport select only builds connect requests. What is connected comes from the
  // server (status.transport_kind, G-6 / AR-50): audio rows, the "• USB device" label, host audio.
  function localTransportKind() {
    const sel = $("transport"), opt = sel.options[sel.selectedIndex];
    if (!opt) return "sim";
    return opt.dataset.kind === "serial" ? "serial" : opt.value;
  }
  function currentTransportKind() {
    const st = TR.status || {};
    if (st.connected && st.transport_kind && st.transport_kind !== "none") return st.transport_kind;
    return localTransportKind();
  }
  function updateConnFields() {                     // the connect form's own fields
    const k = localTransportKind();
    $("baud").hidden = k !== "serial";
    $("lanFields").hidden = k !== "lan";
    applyTransportUi();
  }
  let shownKind = null;
  function applyTransportUi() {
    const kind = currentTransportKind();
    const devs = $("audioDevs");
    if (devs) devs.hidden = !(kind === "serial" || kind === "lan");   // Mic In: COM + LAN radios; sim: none
    if ((kind === "serial" || kind === "lan") && kind !== shownKind) loadAudioDevices();
    shownKind = kind;
    applyAudioLabel();
    applyHostAudioInfo();
  }
  function applyAudioLabel() {
    const note = $("audioAvail");
    if (!note) return;
    const kind = currentTransportKind();
    const t = state.audio ? "• available" : (kind === "serial" ? "• USB device" : (kind === "lan" ? "• network" : ""));
    if (note.textContent !== t) note.textContent = t;
  }
  // Host sound cards (serial radios): which card the server uses, the admin's pin or the server's
  // automatic CODEC choice (F22); chosen by the server, never by this browser. For an admin the line
  // is a link that opens Admin → Radio & audio (G-5).
  let hostAudioShown = null;
  function applyHostAudioInfo() {
    const el = $("hostAudioInfo");
    if (!el) return;
    if (currentTransportKind() !== "serial") { el.hidden = true; return; }
    const ha = (TR.status && TR.status.host_audio) || {};
    const rx = ha.rx_pinned || ha.rx_default;
    const admin = TR.can("audio_pin");
    const text = rx ? "Host audio: " + rx + (ha.rx_pinned ? "" : " (automatic)")
      : (admin ? "Host audio not set up — choose the devices in Admin → Radio & audio"
               : "Host audio not set up — ask an admin");
    const key = (admin ? "a|" : "u|") + text;
    if (hostAudioShown !== key || !el.firstChild) {
      hostAudioShown = key;
      el.textContent = "";
      if (admin) {
        const a = document.createElement("a");
        a.href = "#"; a.className = "ha-link"; a.textContent = text;
        a.title = "Open Admin → Radio & audio";
        a.addEventListener("click", (e) => {
          e.preventDefault();
          if (window.TRAdmin) window.TRAdmin.open("audio");
        });
        el.appendChild(a);
      } else {
        el.textContent = text;
      }
    }
    el.hidden = false;
  }
  // Admin → Radio & audio changed a pin or the transport pin: re-read the status (G-6) so this line
  // and the connection form follow at once.
  document.addEventListener("tr:refresh-status", () => { refreshStatus(); });
  $("transport").addEventListener("change", () => { updateConnFields(); saveConn(); });

  // ---- server status (G-6): bootstrap, then hello, then re-read on connect / disconnect ----
  let statusSeq = 0;
  async function refreshStatus() {
    const my = ++statusSeq;
    try {
      const j = await TR.api("/api/status", { noRedirect: true });
      if (my === statusSeq && j && typeof j === "object") { TR.status = j; applyStatusUi(); }
    } catch (_) {}
  }
  function applyStatusUi() {
    applyTransportUi();
    applyOperatorForm();
    emitBridge();
  }
  function applyRadioSelLock() {                    // F20: only an admin switches a connected radio
    const sel = $("radioSel");
    if (!sel) return;
    const lock = !!state.connected && !TR.can("switch_radio");
    if (sel.disabled !== lock) sel.disabled = lock;
    sel.title = lock ? "Only an admin can switch radios while connected" : "Radio model";
  }
  // F20 operator connect form: the current radio only, over the transport an admin pinned (shown
  // read-only, pre-filled from status.pinned_transport, never credentials). Admins: unrestricted.
  let connecting = false, lanPort = 50001;
  function applyOperatorForm() {
    applyRadioSelLock();
    if (!TR.can("connect")) return;
    const btn = $("connectBtn"), fields = ["transport", "baud", "lanHost"];
    if (TR.can("switch_radio")) {
      for (const id of fields) if ($(id)) $(id).disabled = false;
      lanPort = 50001;
      if (!connecting) { btn.disabled = false; btn.title = "Connect"; }
      return;
    }
    const st = TR.status || {}, sel = $("radioSel");
    const pin = st.pinned_transport && typeof st.pinned_transport === "object" ? st.pinned_transport : null;
    const current = !!st.last_radio && sel.value === st.last_radio;
    for (const id of fields) if ($(id)) $(id).disabled = true;
    if (pin && current) prefillFromPin(pin);
    if (!connecting) btn.disabled = !(pin && current && !state.connected);
    btn.title = !pin ? "Ask an admin to connect this radio once"
      : (!current ? "Only an admin can switch the radio"
      : (state.connected ? "Already connected" : "Connect over the transport an admin set up"));
  }
  function prefillFromPin(pin) {
    const sel = $("transport");
    if (pin.transport === "serial" && pin.port) {
      let o = [...sel.options].find((x) => x.dataset.kind === "serial" && x.value === pin.port);
      if (!o) {                                     // not enumerated here: show it anyway (read-only)
        o = document.createElement("option");
        o.value = pin.port; o.dataset.kind = "serial"; o.textContent = pin.port + " (pinned)";
        sel.insertBefore(o, sel.querySelector("option[value='lan']"));
      }
      sel.value = pin.port;
      if (pin.baud) $("baud").value = pin.baud;
    } else if (pin.transport === "lan") {
      const lo = sel.querySelector("option[value='lan']");
      if (lo) { lo.hidden = false; sel.value = "lan"; }
      if (pin.host) $("lanHost").value = pin.host;
      lanPort = +pin.port || 50001;
    } else {
      sel.value = "sim";
    }
    const k = localTransportKind();
    $("baud").hidden = k !== "serial";
    $("lanFields").hidden = k !== "lan";
  }

  // ---- per-radio connection memory (transport + settings + this browser's mic) ----
  const CONNS_KEY = "telerig.conns";
  let CONNS = { last: null, radios: {} };
  let wantMicInDev = null;                          // the Mic In device to restore once the list populates
  (function loadConns() {
    try { const v = JSON.parse(localStorage.getItem(CONNS_KEY)); if (v && v.radios) CONNS = v; } catch (_) {}
    if (!CONNS.radios) CONNS.radios = {};
    if (!Object.keys(CONNS.radios).length) {        // migrate the old single-config key
      try {
        const old = JSON.parse(localStorage.getItem("telerig.conn") || localStorage.getItem("icomwebop.conn") || "null");
        if (old && old.radio) { CONNS.radios[old.radio] = { transport: old.transport, host: old.host, user: old.user, pass: old.pass, baud: old.baud }; CONNS.last = old.radio; }
      } catch (_) {}
    }
  })();
  function persistConns() { try { localStorage.setItem(CONNS_KEY, JSON.stringify(CONNS)); } catch (_) {} }

  function buildConnectBody() {
    const radio = $("radioSel").value;
    const sel = $("transport"), opt = sel.options[sel.selectedIndex];
    if (!opt || opt.value === "sim") return { transport: "sim", radio };
    if (opt.value === "lan") return {
      transport: "lan", radio, host: $("lanHost").value.trim(), port: lanPort,
      user: $("lanUser").value, password: $("lanPass").value,
    };
    return { transport: "serial", radio, port: opt.value, baud: +$("baud").value || 115200 };
  }
  function currentConf() {
    const sel = $("transport"), opt = sel.options[sel.selectedIndex];
    const serial = !!(opt && opt.dataset.kind === "serial");
    return {
      transport: serial ? "serial" : (opt ? opt.value : "sim"),
      port: serial ? opt.value : "",
      baud: $("baud").value,
      host: $("lanHost").value, user: $("lanUser").value, pass: $("lanPass").value,
      micInDev: $("micInDev") ? $("micInDev").value : "",
    };
  }
  function saveConn() {
    const id = $("radioSel").value; if (!id) return;
    // keep the v1 host-device picks (rxDev/micDev, query_devices indexes): Admin → Radio & audio
    // offers them as "the devices this browser used before" (§2.14); nothing else reads them
    const old = CONNS.radios[id] || {}, keep = {};
    for (const k of ["rxDev", "micDev"]) if (old[k] != null && old[k] !== "") keep[k] = old[k];
    CONNS.radios[id] = Object.assign(keep, currentConf()); CONNS.last = id; persistConns();
  }
  // restore a radio's last-used connection type + settings (+ this browser's Mic In)
  function applyConnForRadio(id) {
    const c = (CONNS.radios && CONNS.radios[id]) || {};
    if (c.host != null) $("lanHost").value = c.host;
    if (c.user != null) $("lanUser").value = c.user;
    if (c.pass != null) $("lanPass").value = c.pass;
    if (c.baud) $("baud").value = c.baud;
    const sel = $("transport");
    let v = "sim";
    if (c.transport === "serial" && c.port && [...sel.options].some((o) => o.dataset.kind === "serial" && o.value === c.port)) v = c.port;
    else if (c.transport === "lan") { const lo = sel.querySelector('option[value="lan"]'); if (lo && !lo.hidden) v = "lan"; }
    sel.value = v;
    if (v === "sim" && !c.transport) {            // manual form only: a COM-only radio pre-selects its Enhanced (CAT) port
      const p = radios.find((r) => r.id === id);
      if (p && p.has_network === false) {
        const enh = [...sel.options].find((o) => o.dataset.kind === "serial" && o.dataset.enhanced);
        if (enh) sel.value = enh.value;
      }
    }
    wantMicInDev = c.micInDev || null;
    updateConnFields();
    applyOperatorForm();                            // operators: the pinned transport wins (F20)
  }
  async function doConnect(body, silent) {
    const btn = $("connectBtn"), lbl = btn.querySelector(".lbl") || btn;   // the label span keeps the plug icon
    lbl.textContent = "Connecting…"; btn.disabled = true; connecting = true;
    try {
      await TR.api("/api/connect", { method: "POST", json: body, quiet: !!silent });
      $("conn").classList.remove("open"); blanked = false;              // fresh connection -> live data again
      if (!silent) autoStartRx();                                       // COM: auto-start RX audio (rides the Connect click)
    } catch (e) {
      if (!silent && e.code !== "busy") TR.toast("Connect failed: " + e.message, "error");
    } finally {
      connecting = false;
      lbl.textContent = "Connect"; btn.disabled = false;
      applyOperatorForm();
      refreshStatus();
    }
  }
  // Disconnect: an operator confirms inline when others are online (409 confirm_required, F20);
  // an admin may stop someone's transmission explicitly.
  async function doDisconnect() {
    let body = {};
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        await TR.api("/api/disconnect", { method: "POST", json: body, quiet: true });
        break;
      } catch (e) {
        if (e.status === 409 && e.code === "confirm_required" && !body.confirm) {
          const n = +e.body.online || 0;
          const ok = await TR.confirm((n === 1 ? "1 other is" : n + " others are") +
                                      " connected — disconnect the radio for everyone?", { ok: "Disconnect", danger: true });
          if (!ok) return;
          body = { confirm: true };
          continue;
        }
        if (e.status === 409 && e.code === "busy") {
          if (TR.can("switch_radio")) { if (await adminDisconnect("disconnect")) break; return; }
          TR.toast((e.body.owner || "Someone") + " is on the air — try again when they finish.", "warn");
          return;
        }
        TR.toast("Disconnect failed: " + e.message, "error");
        return;
      }
    }
    stopAllAudio();
    $("conn").classList.add("open");
    refreshStatus();
  }
  $("connToggle").onclick = () => $("conn").classList.toggle("open");
  $("connectBtn").onclick = () => {
    if (!TR.can("connect")) return;
    const body = buildConnectBody();
    if (body.transport === "lan" && !body.host) { TR.toast("Enter the radio's IP address.", "warn"); return; }
    saveConn();
    doConnect(body);                       // collapses the settings on success (mobile)
  };
  $("disconnectBtn").onclick = () => { if (TR.can("connect")) doDisconnect(); };
  // Enter in any connection field starts the connect; edits persist per-radio
  ["lanHost", "lanUser", "lanPass", "baud"].forEach((id) => {
    const el = $(id);
    if (!el) return;
    el.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); $("connectBtn").click(); } });
    el.addEventListener("change", saveConn);
  });
  // G-8: a page-load connect only with this browser's SAVED connection for the radio (never a
  // guessed port, no implicit simulator fallback); an operator's must equal the admin's pin.
  function savedConnBody(id) {
    const c = (CONNS.radios && CONNS.radios[id]) || null;
    if (!c || !c.transport) return null;
    if (c.transport === "sim") return { transport: "sim", radio: id };
    if (c.transport === "lan") {
      return c.host ? { transport: "lan", radio: id, host: String(c.host).trim(), port: 50001,
                        user: c.user || "", password: c.pass || "" } : null;
    }
    if (c.transport === "serial") {
      const ok = c.port && [...$("transport").options].some((o) => o.dataset.kind === "serial" && o.value === c.port);
      return ok ? { transport: "serial", radio: id, port: c.port, baud: +c.baud || 115200 } : null;
    }
    return null;
  }
  function samePin(body, pin) {
    if (!pin || typeof pin !== "object" || body.transport !== pin.transport) return false;
    if (body.transport === "serial") return body.port === pin.port && +body.baud === +pin.baud;
    if (body.transport === "lan") return body.host === pin.host && +body.port === +(pin.port || 50001);
    return true;
  }
  function maybeAutoConnect() {
    const st = TR.status || {};
    if (!TR.can("connect") || !TR.boot.autoconnect || new URLSearchParams(location.search).has("noconnect") ||
        state.connected || st.connected) return;
    const id = $("radioSel").value;
    const body = savedConnBody(id);
    if (!body) return;
    if (!TR.can("switch_radio") && (st.last_radio !== id || !samePin(body, st.pinned_transport))) return;
    doConnect(Object.assign(body, { auto: true }), true);   // a 409 (disabled / spent / connected) is silent
  }

  // ---- RX audio (Web Audio playback of 16-bit LE mono PCM) ----
  let audioCtx = null, audioGain = null, rxBus = null, playTime = 0, audioOn = false;
  let rsFifo = [], rsPos = 0;                       // continuous-resampler state (input samples + fractional read pos)
  function ensureAudioCtx() {
    if (!audioCtx) {
      audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      audioGain = audioCtx.createGain();
      audioGain.gain.value = (+$("vol").value || 80) / 100;
      audioGain.connect(audioCtx.destination);
      rxBus = audioCtx.createGain();               // pre-volume RX bus: tools (CW, AF) tap here
      rxBus.connect(audioGain);
    }
    if (audioCtx.state === "suspended") audioCtx.resume();
  }
  function startAudio() {                 // LAN / WS RX (jitter-buffered PCM)
    ensureAudioCtx(); playTime = 0; rsFifo = []; rsPos = 0; audioOn = true;
  }
  // minimal audio API for overlay tools (e.g. the CW decoder/coder in cwtool.js)
  // on(): is RX audio actually running? (the RX audio button is active for both serial + LAN paths).
  // Tools use this for status instead of guessing from the instantaneous audio level.
  window.RadioAudio = { ensure: ensureAudioCtx, ctx: () => audioCtx, bus: () => rxBus, state: () => state,
    on: () => !!($("audioBtn") && $("audioBtn").classList.contains("active")) };
  // control channel for overlay tools (CW TX): send a WS command + read live state
  window.RadioControl = { send: (o) => send(o), state: () => state, radios: () => radios.slice() };
  function playAudio(buf) {
    if (!audioOn || !audioCtx) return;
    const rate = new DataView(buf).getUint16(2, true) || 16000;
    const pcm = new Int16Array(buf, 4);            // header is 4 bytes
    const n = pcm.length; if (!n) return;
    for (let i = 0; i < n; i++) rsFifo.push(pcm[i] / 32768);   // queue this chunk's samples
    // Resample source-rate -> context-rate with a fractional read position that carries
    // ACROSS chunks, so consecutive scheduled buffers join seamlessly (no per-chunk
    // resampler edge clicks, which were the main source of the artifacting).
    const ctxRate = audioCtx.sampleRate, ratio = rate / ctxRate;
    const out = [];
    while (rsPos + 1 < rsFifo.length) {
      const i0 = rsPos | 0, f = rsPos - i0;
      out.push(rsFifo[i0] * (1 - f) + rsFifo[i0 + 1] * f);
      rsPos += ratio;
    }
    const drop = rsPos | 0;
    if (drop > 0) { rsFifo.splice(0, drop); rsPos -= drop; }   // keep only the fractional remainder
    if (!out.length) return;
    const ab = audioCtx.createBuffer(1, out.length, ctxRate);
    ab.getChannelData(0).set(out);
    const src = audioCtx.createBufferSource(); src.buffer = ab; src.connect(rxBus);
    const now = audioCtx.currentTime;
    if (playTime < now + 0.06) playTime = now + 0.18;        // prime / rebuild the jitter buffer on underrun
    else if (playTime > now + 0.5) playTime = now + 0.2;     // bound runaway latency without a hard gap
    src.start(playTime); playTime += ab.duration;
  }

  // ---- radio audio devices ------------------------------------------------------
  // Over a COM/CAT connection the control link carries no audio — the radio's audio is a USB sound
  // card on the HOST. The server picks it (an admin's pin, or the F22 automatic CODEC) and streams
  // it to every RX subscriber; this browser only chooses its own microphone (Mic In) for TX audio.
  // Needs a secure context (HTTPS or localhost) for the microphone, like any getUserMedia.
  function fillDevs(sel, list, kind, withDefault) {
    if (!sel) return;
    const prev = sel.value; sel.innerHTML = "";
    if (withDefault) {                             // leading "system default" entry (value "" -> getUserMedia default).
      const o = document.createElement("option");  // Mic In uses this so it never auto-picks the radio's RX CODEC.
      o.value = ""; o.textContent = "Default " + kind.toLowerCase(); sel.appendChild(o);
    }
    if (!list.length && !withDefault) {
      const o = document.createElement("option"); o.value = ""; o.textContent = "(no " + kind.toLowerCase() + " devices)";
      sel.appendChild(o); return;
    }
    list.forEach((d, i) => {
      const o = document.createElement("option");
      o.value = d.deviceId;
      o.textContent = d.label || (kind + " " + (i + 1));
      sel.appendChild(o);
    });
    if (prev) sel.value = prev;
  }
  async function loadAudioDevices() {              // this browser's microphones only (G-5)
    if (navigator.mediaDevices && navigator.mediaDevices.enumerateDevices) {
      let devs = [];
      try { devs = await navigator.mediaDevices.enumerateDevices(); } catch (_) { devs = []; }
      fillDevs($("micInDev"), devs.filter((d) => d.kind === "audioinput"), "Mic", true);   // Mic In = this client's mic
    }
    const mi = $("micInDev");                      // restore the remembered Mic In for this radio
    if (mi && wantMicInDev && [...mi.options].some((o) => o.value === wantMicInDev)) mi.value = wantMicInDev;
  }
  if (navigator.mediaDevices && navigator.mediaDevices.addEventListener) {
    navigator.mediaDevices.addEventListener("devicechange", loadAudioDevices);
  }

  // Clearer message for the failure modes the Mic In deviceId constraint can raise.
  function micErr(e) {
    const n = e && e.name;
    if (n === "OverconstrainedError" || n === "NotFoundError")
      return "The selected Mic In device isn't available — pick another under Mic In.\n(" + e + ")";
    if (n === "NotReadableError")
      return "The microphone is in use by another app and can't be opened.\n(" + e + ")";
    return "Microphone access denied: " + e;
  }
  // ---- audio (AF) spectrum from RX, for radios with no CAT band scope (FT-991A) ----
  // FFT the RX audio and feed the existing spectrum + waterfall. Audio Hz is mapped
  // to RF by the mode's sideband (USB: dial+af, LSB: dial-af) and, in CW, the radio's
  // pitch (AfMap, waterfall.js), so the labels, cursor readout, taps, tuned marker and
  // band plan all read real frequencies — a mini panadapter of the passband.
  let afTimer = 0, afAnalyser = null;
  const AF_MAX = 3600;                          // Hz of the RX audio passband to show (SSB/CW/data occupied width)
  const AF_RANGE_DB = 30;                       // dB above the LOCAL noise floor mapped to full brightness
  const AF_MARGIN_DB = 3;                       // headroom that keeps the flattened noise floor dark
  function afEligible() { return !!(currentRadio && currentRadio.has_scope === false); }
  function startAfScope(srcNode) {
    stopAfScope();
    ensureAudioCtx();
    const sr = audioCtx.sampleRate;
    afAnalyser = audioCtx.createAnalyser();
    // Size the FFT so the passband spans ~1 bin per output pixel — maximum useful detail without
    // over/under-sampling (fftSize ~= W*sr/AF_MAX; e.g. 8192 @ 48 kHz over a ~700 px scope).
    const W = (scope && scope.W) || 700;
    const fft = 1 << Math.round(Math.log2(Math.max(2048, W * sr / AF_MAX)));
    afAnalyser.fftSize = Math.max(4096, Math.min(16384, fft));
    afAnalyser.smoothingTimeConstant = 0;        // no temporal smoothing — crisp, like WSJT
    try { srcNode.connect(afAnalyser); } catch (_) { afAnalyser = null; return; }
    const bins = afAnalyser.frequencyBinCount, buf = new Float32Array(bins);
    const nyq = sr / 2;
    const n = Math.max(64, Math.min(bins, Math.round(AF_MAX / nyq * bins)));   // bins inside the passband
    const data = new Uint8Array(n), b = new Float32Array(n), bl = new Float32Array(n);
    const binHz = sr / afAnalyser.fftSize;
    const H = Math.max(6, Math.round(250 / binHz));   // baseline half-window (~250 Hz of bins)
    const ns = $("noScope"); if (ns) ns.hidden = true;
    const badge = $("afBadge"); if (badge) badge.hidden = false;
    afTimer = setInterval(() => {
      if (!scope.wrap.clientWidth || !scope.wrap.clientHeight) return;   // scope parked / collapsed: no work
      afAnalyser.getFloatFrequencyData(buf);     // raw dB (full dynamic range), not the lossy 0-255 byte path
      const mode = state.mode_name || "USB";
      const view = AfMap.view(state.freq || 0, AF_MAX, mode, AfMap.cw(currentRadio, state, mode));
      const lsb = view.lsb;                      // LSB side: mirror, so RF rises left to right
      for (let i = 0; i < n; i++) { const d = buf[lsb ? (n - 1 - i) : i]; b[i] = d > -200 ? d : -160; }
      // Per-frequency noise baseline via a wide boxcar mean (O(n) sliding window). Subtracting it
      // flattens the radio's shaped passband so the floor goes dark and only energy ABOVE the local
      // noise shows — the clean WSJT look (vs a single global floor that leaves the hump bright).
      let sum = 0, cnt = 0;
      for (let j = 0; j <= H && j < n; j++) { sum += b[j]; cnt++; }
      for (let i = 0; i < n; i++) {
        bl[i] = sum / cnt;
        const rem = i - H; if (rem >= 0) { sum -= b[rem]; cnt--; }
        const add = i + 1 + H; if (add < n) { sum += b[add]; cnt++; }
      }
      const gain = 160 / AF_RANGE_DB;
      for (let i = 0; i < n; i++) {
        const r = (b[i] - bl[i] - AF_MARGIN_DB) * gain;        // dB above the local noise floor -> 0..160
        data[i] = r <= 0 ? 0 : r >= 160 ? 160 : r | 0;
      }
      // Fixed edges. In CW the dial is the tuned marker, at the pitch; in the other modes the dial
      // sits at an edge, so tuned:0 (no marker). The labels read the edges.
      scope.opMode = mode;
      scope.pushSweep(view, data);
      updateScopeLabels(scope.meta);
    }, 40);                                       // ~25 fps
  }
  function stopAfScope() {
    if (afTimer) { clearInterval(afTimer); afTimer = 0; }
    if (afAnalyser) { try { afAnalyser.disconnect(); } catch (_) {} afAnalyser = null; }
    const badge = $("afBadge"); if (badge) badge.hidden = true;
    if (afEligible()) { const ns = $("noScope"); if (ns) ns.hidden = false; placeNoScope(); }
  }
  // stop every audio path + reset the buttons (on radio change / disconnect). The server keeps RX
  // and mic subscriptions per connection (G-5): tell it, then stop locally.
  function stopAllAudio() {
    const rxWas = $("audioBtn").classList.contains("active");
    audioOn = false;
    if (rxWas) send({ action: "rx_audio", on: false });
    if (micOn || $("micBtn").classList.contains("on")) send({ action: "mic", on: false });
    stopMic(); stopAfScope();
    $("audioBtn").classList.remove("active");
    $("micBtn").classList.remove("on");
  }

  if ($("micInDev")) $("micInDev").addEventListener("change", () => {
    wantMicInDev = $("micInDev").value || null; saveConn();
    if (micOn) { stopMic(); startMic(); }                         // re-open the client mic on the new input
  });

  // ---- TX mic (capture -> 16 kHz mono s16le -> server -> radio) ----
  // NOTE: this only streams audio to the radio's modulator; the radio transmits
  // ONLY when PTT is engaged AND its MOD Input is set to LAN.
  let micStream = null, micSrc = null, micProc = null, micSink = null, micOn = false;
  async function startMic() {
    // Toasts, never alert(): a modal dialog freezes the tab, and with it the TX heartbeat.
    if (!window.isSecureContext || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      TR.toast("Microphone (TX) needs a secure HTTPS connection. You're on " + location.protocol +
        " — browsers only allow the mic over HTTPS (or localhost). Open the https:// (Tailscale) address. " +
        "RX audio still works over HTTP.", "error", { ms: 9000 });
      return false;
    }
    try {
      const micId = $("micInDev") ? $("micInDev").value : "";
      micStream = await navigator.mediaDevices.getUserMedia(
        { audio: { deviceId: micId ? { exact: micId } : undefined, channelCount: 1, echoCancellation: true, noiseSuppression: true } });
    } catch (e) { TR.toast(micErr(e), "error", { ms: 7000 }); return false; }
    loadAudioDevices();                  // mic labels are available now permission is granted
    if (!audioCtx) startAudio();
    micSrc = audioCtx.createMediaStreamSource(micStream);
    micProc = audioCtx.createScriptProcessor(4096, 1, 1);
    micSink = audioCtx.createGain(); micSink.gain.value = 0;   // silent sink (no mic monitor)
    const inRate = audioCtx.sampleRate, outRate = 16000, ratio = inRate / outRate;
    micProc.onaudioprocess = (e) => {
      if (!micOn) return;
      const input = e.inputBuffer.getChannelData(0);
      const outLen = Math.floor(input.length / ratio);
      const out = new Int16Array(outLen);
      let peak = 0;
      for (let i = 0; i < outLen; i++) {
        const s = input[(i * ratio) | 0];
        const a = s < 0 ? -s : s; if (a > peak) peak = a;
        out[i] = s < -1 ? -32768 : s > 1 ? 32767 : (s * 32767) | 0;
      }
      const ml = $("micLevel");
      if (ml) ml.style.width = Math.min(100, Math.round(Math.sqrt(peak) * 100)) + "%";   // perceptual level
      if (ws && ws.readyState === 1) ws.send(out.buffer);
    };
    micSrc.connect(micProc); micProc.connect(micSink); micSink.connect(audioCtx.destination);
    micOn = true; return true;
  }
  function stopMic() {
    micOn = false;
    for (const n of [micProc, micSrc, micSink]) { try { n && n.disconnect(); } catch (_) {} }
    if (micStream) micStream.getTracks().forEach(t => t.stop());
    micProc = micSrc = micSink = micStream = null;
    const ml = $("micLevel"); if (ml) ml.style.width = "0%";
  }

  // Turn RX audio ON (the RX button, and the auto-start after a COM connect). Every transport
  // subscribes on the server (G-5): LAN audio flows to subscribers only, and a serial radio's host
  // capture starts with the first subscriber (the admin's pin or the automatic CODEC, F22). If the
  // server cannot capture, it answers with an error frame and RX is switched off again here.
  function startRxAudio() {
    if ($("audioBtn").classList.contains("active")) return true;
    send({ action: "rx_audio", on: true });
    startAudio();                                       // play the WS PCM (host capture, or LAN radio)
    if (afEligible()) startAfScope(rxBus);              // FT-991A etc.: AF spectrum tapped off the RX bus
    $("audioBtn").classList.add("active");
    return true;
  }
  // Auto-start RX audio after a (user-initiated) COM/serial connect. Only after a real Connect click,
  // so the AudioContext resume rides that user gesture. Waits briefly for the connected state.
  function autoStartRx(tries = 10) {
    if ($("audioBtn").classList.contains("active")) return;
    if (state.connected && currentTransportKind() === "serial") { startRxAudio(); return; }
    if (tries > 0) setTimeout(() => autoStartRx(tries - 1), 250);
  }
  $("audioBtn").onclick = () => {
    if ($("audioBtn").classList.contains("active")) {   // RX off
      send({ action: "rx_audio", on: false });
      audioOn = false; stopAfScope();
      $("audioBtn").classList.remove("active");
    } else {                                            // RX on
      startRxAudio();
    }
  };
  // Mic: non-exclusive (F12). "mic on" tells the server this connection streams microphone audio;
  // the server forwards it only while this connection owns TX with ptt/vox (I10).
  $("micBtn").onclick = async () => {
    if ($("micBtn").classList.contains("on")) {         // mic off
      send({ action: "mic", on: false });
      stopMic();
      $("micBtn").classList.remove("on");
    } else {                                            // mic on
      if (!TR.can("tx")) return;
      send({ action: "mic", on: true });
      const ok = await startMic();                      // capture the CLIENT mic -> WS -> server -> radio
      if (ok) $("micBtn").classList.add("on");
      else send({ action: "mic", on: false });
    }
  };
  $("vol").oninput = (e) => { if (audioGain) audioGain.gain.value = (+e.target.value) / 100; };

  // (Remote access — Tailscale Serve — lives in Admin → Remote now: admin.js, §5.4, G-20.)

  // ---- update check (latest GitHub release) ----
  (function () {
    const link = $("verUpdate");
    if (!link) return;
    function check(retry) {
      TR.api("/api/version", { noRedirect: true }).then((v) => {
        // source of truth = backend __version__; a dev install shows its label ("0.3.0-dev+<commit>")
        if (v.current) { const n = $("verNow"); if (n) n.textContent = "v" + (v.label || v.current); }
        if (v.update_available && v.url) {
          link.href = v.url;
          link.textContent = "↑ " + (v.latest || "update");
          link.title = "A newer release (" + v.latest + ") is available — click for the release notes";
          link.hidden = false;
        } else if (!v.latest && retry) {
          setTimeout(() => check(false), 3500);   // backend hadn't finished its GitHub check yet
        }
      }).catch(() => {});
    }
    check(true);
  })();

  // ---- RadioScope / RadioTx / RadioControl: for cwtool.js and layout.js (B3) ----
  window.RadioScope = scope;                       // §0.1: the spectrum / waterfall / overlay
  window.RadioTx = {
    ownedPtt: () => ownedPtt(),
    ownedOrPending: () => ownedOrPending(),
    holds: (k) => holds(k),
    owner: () => txOwner(),
    nextSeq: () => nextSeq(),
    stop: () => txStop(),
    hotkey: () => (pttKey ? { code: pttKey.code, mode: pttKey.mode, label: pttKey.label } : null),
    hotkeyAllowed: (code) => pttKeyAllowed(code),
  };
  // Sim-only test hook (like TR._inject): mark a PTT claim as pending WITHOUT sending anything, so the
  // AR-8 stale-frame check can be exercised with injected frames and no transmit command at all.
  if (TR.boot.sim_only === true) {
    window.RadioTx._simPending = (seq) => {
      seq = +seq || nextSeq();
      if (seq > txSeq) txSeq = seq;
      const now = performance.now();
      pendingClaims.set(seq, { kind: "ptt", at: now });
      pttPending = { seq, at: now };
      renderTx();
      return seq;
    };
  }

  // ---- boot ----
  requestAnimationFrame(() => { applySplit(); scope.resize(); });   // re-measure once the console layout settles
  window.addEventListener("load", () => { applySplit(); scope.resize(); placeNoScope(); });
  setupFmControls();                                     // populate + wire the FM Tone/DCS selects once
  setupExtControls();                                    // wire the WIDTH/CONTOUR/APF/CW sliders once
  renderTx();
  applyRadioSelLock();
  connectWS();
  loadRadios().then(() => {
    // the server's connected radio, else this browser's last, else the first profile (AR-44, G-4)
    const st = TR.status || {};
    const init = (st.connected && st.radio) || TR.initialRadio;
    if (init && radios.some(p => p.id === init)) $("radioSel").value = init;
    renderRadio(selectedRadio());
    return loadPorts();                            // serial COM options now exist (operators/admins)
  }).then(() => {
    if (state.connected && state.radio && currentRadio && state.radio !== currentRadio.id &&
        radios.some(p => p.id === state.radio)) {  // the radio changed while we loaded
      $("radioSel").value = state.radio; renderRadio(selectedRadio());
    }
    applyConnForRadio($("radioSel").value);        // restore this radio's last connection type + settings
    if ((TR.status || {}).connected || state.connected) {
      $("conn").classList.remove("open");          // already connected: collapse settings (mobile)
      return;
    }
    maybeAutoConnect();                            // G-8: only a saved, matching connection; never a guess
  });
})();
