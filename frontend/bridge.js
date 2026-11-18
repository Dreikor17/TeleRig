/* TeleRig — the WSJT-X bridge indicator (docs/WSJTX-BRIDGE.md). Loads after app.js and account.js.
 *
 * The bridge lets WSJT-X / JS8Call / Fldigi / a logger share the radio TeleRig holds (backend/bridge.py).
 * This file only SHOWS its state and lets an operator hold or resume its transmit:
 *
 *   #bridgeBtn   top bar, visible only while the bridge is enabled. data-state:
 *                idle (nobody connected) · live (a program is connected) · paused (transmit held) ·
 *                onair (a bridge client is transmitting) · error (the port could not be opened)
 *   #bridgePop   a small panel under the button: what is listening, who is connected (operators and
 *                admins), whether the bridge may transmit, and
 *                  Pause transmit / Resume transmit   WS `bridge_tx {on}` (operators; the server decides)
 *                  Settings…                          Administration > WSJT-X bridge (admins)
 *
 * The state comes from the server only: hello / /api/status (`status.bridge`) and `bridge` frames,
 * which app.js turns into the `tr:bridge` document event. Stopping a bridge transmission is the same
 * STOP as for any other owner (top bar); this panel's Pause does that too and then holds.
 * Pause and Resume share one button, so a RESUME is held back (asymmetric, like PTT: a pause is never
 * held back): the second click of a double-click, and any click within RESUME_GUARD_MS of a pause
 * (this tab's click, or a pause that just showed up), does nothing. While the panel is open it is
 * updated in place: it is rebuilt only when what it shows changes, the running counters are just
 * refreshed, so the button under the pointer never changes by itself.
 * All text is set with textContent.
 */
(function () {
  "use strict";
  const TR = window.TR;
  if (!TR) return;
  const $ = (id) => document.getElementById(id);
  const btn = $("bridgeBtn"), pop = $("bridgePop");
  if (!btn || !pop) return;

  let st = null;                                   // the last status.bridge
  let lastPaused = null;                           // to announce a pause once
  const RESUME_GUARD_MS = 1000;
  let resumeGuardUntil = -1e9;                     // no Resume click acts before this
  let shownSig = null;                             // what the open panel was built from
  let peerNodes = [];                              // [{ip, node}] the counters' text nodes

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }
  function icon(name) { return typeof TR.icon === "function" ? TR.icon(name) : null; }
  function plural(n, one, many) { return n + " " + (n === 1 ? one : many); }
  function ago(secs) {
    const s = Math.max(0, +secs || 0);
    if (s < 90) return s + " s";
    if (s < 5400) return Math.round(s / 60) + " min";
    return Math.round(s / 3600) + " h";
  }

  function pauseText(p) {
    if (!p) return "";
    const by = p.by ? " by " + p.by : "";
    return ({
      stopped: "stopped" + by,
      operator: "held" + by,
      timeout: "the 120 s limit was reached",
      heartbeat: "the program stopped talking to TeleRig",
      admin_switch: "an admin switched the radio" + by,
      server_locked: "the server was locked",
    })[p.reason] || "paused" + by;
  }
  function stateOf(b) {
    if (!b || !b.enabled) return "off";
    if (b.on_air) return "onair";
    if (b.error || !b.listening) return "error";
    if (b.paused) return "paused";
    return b.clients > 0 ? "live" : "idle";
  }
  const STATE_LABEL = { idle: "waiting", live: "connected", paused: "transmit paused", onair: "ON AIR", error: "not listening" };

  function summary(b) {
    const s = stateOf(b);
    if (s === "off") return "WSJT-X bridge: off";
    let t = "WSJT-X bridge: " + STATE_LABEL[s];
    if (s === "live" || s === "onair") t += " (" + plural(b.clients, "program", "programs") + ")";
    if (s === "paused") t += " — " + pauseText(b.paused);
    return t;
  }

  function render() {
    const b = st;
    const s = stateOf(b);
    const show = s !== "off";
    if (btn.hidden === show) btn.hidden = !show;
    if (!show) { close(); return; }
    btn.dataset.state = s;
    const tip = summary(b);
    if (btn.title !== tip) { btn.title = tip; btn.setAttribute("aria-label", tip); }
    if (!pop.hidden) fill();
  }

  // what the panel shows, minus the running counters (seconds connected, commands)
  function sig(b) {
    if (!b) return "";
    const peers = Array.isArray(b.peers) ? b.peers.map((p) => p.ip + (p.tx ? "*" : "")).join(",") : "";
    return [stateOf(b), b.listening, b.error_text || "", b.port, b.listen, b.clients, peers, b.ptt,
            b.paused ? b.paused.reason + "|" + (b.paused.by || "") : "", TR.can("tx"), TR.can("admin")].join(";");
  }
  function peerText(p) { return " · " + ago(p.secs) + " · " + plural(+p.cmds || 0, "command", "commands"); }
  function fill() {
    const b = st;
    const sg = sig(b);
    if (b && sg === shownSig && pop.childNodes.length) {          // same panel: refresh the counters only
      for (const p of (Array.isArray(b.peers) ? b.peers : [])) {
        const n = peerNodes.find((x) => x.ip === p.ip);
        if (n && n.node.textContent !== peerText(p)) n.node.textContent = peerText(p);
      }
      return;
    }
    shownSig = sg;
    peerNodes = [];
    pop.textContent = "";
    if (!b) return;
    const s = stateOf(b);
    const head = el("div", "bp-head");
    head.append(el("b", null, "WSJT-X BRIDGE"), el("span", "bp-tag bp-" + s, STATE_LABEL[s] || s));
    pop.append(head);

    // what is listening
    const where = b.listen === "network" ? "this PC and the allowed computers" : "programs on this PC only";
    if (b.listening) pop.append(el("p", "bp-line", "Listening on port " + b.port + " · " + where + "."));
    else pop.append(el("p", "bp-line bp-warn", b.error_text || "The bridge port could not be opened. Ask an admin (Administration > WSJT-X bridge)."));

    // who is connected
    if (!b.clients) pop.append(el("p", "bp-line bp-dim", "No program is connected."));
    else if (Array.isArray(b.peers) && b.peers.length) {
      const ul = el("ul", "bp-peers");
      for (const p of b.peers) {
        const li = el("li");
        const node = document.createTextNode(peerText(p));
        peerNodes.push({ ip: p.ip, node });
        li.append(el("code", null, p.ip), node);
        if (p.tx) li.append(el("span", "bp-tag bp-onair", "transmitting"));
        ul.append(li);
      }
      pop.append(ul);
    } else pop.append(el("p", "bp-line", plural(b.clients, "program is", "programs are") + " connected."));

    // transmit
    if (!b.ptt) pop.append(el("p", "bp-line bp-dim", "Transmit over the bridge is off: programs can tune and read the radio, not key it."));
    else if (b.paused) pop.append(el("p", "bp-line bp-warn", "Transmit over the bridge is PAUSED (" + pauseText(b.paused) + "). The program cannot key until someone resumes it."));
    else pop.append(el("p", "bp-line", "Transmit over the bridge is allowed. STOP in the top bar ends a bridge transmission like any other."));

    const acts = el("div", "bp-actions");
    if (b.ptt && TR.can("tx")) {
      const resume = !!b.paused;
      const a = el("button", "ctl-btn sm" + (resume ? " primary" : ""));
      a.type = "button";
      a.id = "bridgeTxBtn";
      const ic = icon(resume ? "tr-play" : "tr-pause");
      if (ic) a.append(ic);
      a.append(resume ? "Resume transmit" : "Pause transmit");
      a.title = resume ? "Let the bridge key the radio again" : (b.on_air ? "Stop the bridge's transmission and hold it" : "Hold transmit over the bridge");
      a.addEventListener("click", (e) => {
        const now = performance.now();
        // a RESUME is held back: the tail of a double-click, or a click just after a pause (the
        // button changed under the pointer). A pause is never held back.
        if (resume && ((+e.detail || 0) > 1 || now < resumeGuardUntil)) return;
        if (!resume) resumeGuardUntil = now + RESUME_GUARD_MS;
        const rc = window.RadioControl;
        if (!rc || !rc.send({ action: "bridge_tx", on: resume })) TR.toast("Not connected to the server — try again in a moment.", "warn");
      });
      acts.append(a);
    }
    if (TR.can("admin") && window.TRAdmin && typeof window.TRAdmin.open === "function") {
      const a = el("button", "ctl-btn sm", "Settings…");
      a.type = "button";
      a.id = "bridgeAdminBtn";
      a.addEventListener("click", () => { close(); window.TRAdmin.open("bridge"); });
      acts.append(a);
    }
    if (acts.childNodes.length) pop.append(acts);
    pop.append(el("p", "bp-hint", "In WSJT-X: Rig = Hamlib NET rigctl, Network Server = " +
                                  (b.listen === "network" ? "this PC's address" : "localhost") + ":" + b.port + "."));
  }

  // ---- open / close ------------------------------------------------------------------------------
  // While the panel is open the status is re-read every few seconds (the server only pushes a
  // `bridge` frame when something changes, not for the peers' running counters).
  let refreshT = 0;
  const refresh = () => document.dispatchEvent(new CustomEvent("tr:refresh-status"));
  function open() {
    if (!pop.hidden) return;
    if (TR.menu && TR.menu.close) TR.menu.close(false);
    fill();
    pop.hidden = false;
    btn.setAttribute("aria-expanded", "true");
    refresh();
    clearInterval(refreshT);
    refreshT = setInterval(refresh, 3000);
  }
  function close(focusBtn) {
    clearInterval(refreshT);
    refreshT = 0;
    shownSig = null;                                 // the next open builds the panel afresh
    if (pop.hidden) return;
    pop.hidden = true;
    btn.setAttribute("aria-expanded", "false");
    if (focusBtn && !btn.hidden) btn.focus();
  }
  btn.addEventListener("click", () => { if (pop.hidden) open(); else close(); });
  document.addEventListener("pointerdown", (e) => {
    if (pop.hidden) return;
    const t = e.target;
    if (t && (pop.contains(t) || btn.contains(t))) return;
    close();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !pop.hidden) { e.preventDefault(); close(true); }
  });

  // ---- updates -------------------------------------------------------------------------------------
  function update(b) {
    st = b && typeof b === "object" ? b : null;
    const p = st && st.enabled && st.paused ? st.paused : null;
    const key = p ? p.reason + "|" + (p.by || "") : null;
    if (key && key !== lastPaused) {                 // say it once per pause
      resumeGuardUntil = performance.now() + RESUME_GUARD_MS;   // the button just turned into Resume
      const msg = "WSJT-X bridge: transmit paused (" + pauseText(p) + ").";
      TR.toast(msg + (TR.can("tx") ? " Resume it from the bridge button." : ""), "warn", { force: true });
      if (typeof TR.srLive === "function") TR.srLive(msg);
    }
    lastPaused = key;
    render();
  }
  document.addEventListener("tr:bridge", (e) => update(e.detail));
  update((TR.status && TR.status.bridge) || null);

  window.TRBridge = { status: () => st, state: () => stateOf(st), open, close };
})();
