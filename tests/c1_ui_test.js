/* C1 review carry-over fixes, tested on the REAL frontend code in a Node vm (tests/fakedom.js: a small
 * fake DOM, a fake clock, stubbed fetch / WebSocket). No browser, no server, no socket: nothing here
 * can reach a radio, and no TX control is ever clicked except the top-bar STOP chip of a FAKE page
 * whose "server" is a recording stub. Run by tests/t_c1.py (unit group):
 *
 *     node tests/c1_ui_test.js        -> exit 0 and "ALL PASS", or exit 1 naming each failure
 *
 *  A. account.js sign-out: only a 2xx is a sign-out; 401/403/429/5xx/network are handled honestly.
 *  B. admin.js: a temporary password is shown exactly once and scrubbed on the next render.
 *  C. app.js top-bar STOP: the shared TR.stopArm rule (behaviour as before).
 *  D. waterfall.js: the edge frequencies are drawn once (HTML labels), not also on the canvas.
 *  E. tr.js: the 4401 close reason role_changed lands on /login?reason=role_changed.
 */
"use strict";
const { makeEnv } = require("./fakedom");

let failures = 0, checks = 0;
const pending = [];
function check(cond, msg) { checks++; if (!cond) { failures++; console.log("FAIL " + msg); } }
function eq(a, b, msg) { const ja = JSON.stringify(a), jb = JSON.stringify(b); check(ja === jb, msg + ": got " + ja + ", want " + jb); }
function test(name, fn) { pending.push([name, fn]); }

function jsonResponse(status, body) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}
// fetch stub: routes[url] = [status, body] | (init) => [status, body] | "network" ; records every call
function fetchStub(routes, log) {
  return (url, init) => {
    const u = String(url);
    log.push({ url: u, method: (init && init.method) || "GET" });
    let r = routes[u.split("?")[0]];
    if (typeof r === "function") r = r(init);
    if (r === "network") return Promise.reject(new TypeError("Failed to fetch"));
    if (r && typeof r.then === "function") return r;
    if (!r) return Promise.resolve(jsonResponse(404, { ok: false, error: "not_found" }));
    return Promise.resolve(jsonResponse(r[0], r[1]));
  };
}

// ---- A. account.js sign-out ------------------------------------------------------------------------
function accountEnv(routes) {
  const log = [];
  const env = makeEnv({ fetch: fetchStub(routes, log) });
  for (const id of ["userBtn", "umChangePw", "umSignoutAll", "umSignout", "modalRoot", "toasts"]) env.el("div", { id });
  env.load("tr.js");
  env.ctx.TR.user = "OP1";
  env.ctx.TR.confirm = async () => true;                   // the inline confirm answered "Sign out everywhere"
  env.toasts = [];
  const realToast = env.ctx.TR.toast;
  env.ctx.TR.toast = (msg, kind, o) => { env.toasts.push([kind, String(msg)]); return realToast(msg, kind, o); };
  env.load("account.js");
  env.log = log;
  env.nav = () => env.ctx.location.replaced;
  return env;
}
const SIGN_CASES = [
  // [label, route, status/body or "network", which, expected navigation (null = stays), toast regex]
  ["logout 200", "/api/logout", [200, { ok: true }], "signOut", "/login?reason=signed_out", null],
  ["logout 401 (session already gone)", "/api/logout", [401, { ok: false, error: "auth_required" }], "signOut", "/login?reason=signed_out", null],
  ["logout 403 bad origin", "/api/logout", [403, { ok: false, error: "bad_origin", message: "Cross-site request blocked." }], "signOut", null, /^Not signed out\. Cross-site request blocked/],
  ["logout 429", "/api/logout", [429, { ok: false, error: "rate_limited", retry_after: 30 }], "signOut", null, /^Not signed out\. Too many requests/],
  ["logout 500", "/api/logout", [500, { ok: false, error: "store_error" }], "signOut", null, /^Not signed out\./],
  ["logout network error", "/api/logout", "network", "signOut", null, /^Not signed out\. Can't reach the server/],
  ["signout_all 200", "/api/me/signout_all", [200, { ok: true }], "signOutEverywhere", "/login?reason=signed_out", null],
  ["signout_all 401: gone here, NOT signed out everywhere", "/api/me/signout_all", [401, { ok: false, error: "auth_required" }], "signOutEverywhere", "/login?reason=expired", null],
  ["signout_all 403 migrated_local_only", "/api/me/signout_all", [403, { ok: false, error: "migrated_local_only", message: "Change the shared admin password on the host PC itself." }], "signOutEverywhere", null, /^Not signed out everywhere — nothing changed\. Change the shared admin password/],
  ["signout_all 403 password_change_required", "/api/me/signout_all", [403, { ok: false, error: "password_change_required" }], "signOutEverywhere", null, /^Not signed out everywhere/],
  ["signout_all 429", "/api/me/signout_all", [429, { ok: false, error: "rate_limited", retry_after: 9 }], "signOutEverywhere", null, /^Not signed out everywhere — nothing changed\. Too many requests/],
  ["signout_all 503", "/api/me/signout_all", [503, { ok: false, error: "store_busy", message: "The account store is busy; try again." }], "signOutEverywhere", null, /store is busy/],
];
for (const [label, route, resp, which, nav, toastRe] of SIGN_CASES) {
  test("A " + label, async () => {
    const env = accountEnv({ [route]: resp });
    const ret = await env.ctx.TRAccount[which]();
    await env.flush();
    eq(env.log.map((x) => x.url), [route], "A " + label + ": one POST");
    eq(env.nav(), nav ? [nav] : [], "A " + label + ": navigation");
    eq(ret, !!nav, "A " + label + ": return value");
    if (nav) {
      check(!env.toasts.some((t) => t[0] === "error"), "A " + label + ": no error toast when leaving");
    } else {
      check(env.toasts.some((t) => t[0] === "error" && toastRe.test(t[1])), "A " + label + ": error toast " + toastRe + " (got " + JSON.stringify(env.toasts) + ")");
      eq(env.ctx.TR._leaving, false, "A " + label + ": the page stays usable (TR._leaving cleared)");
    }
    check(!env.toasts.some((t) => /signed out everywhere/i.test(t[1]) && !/^Not /.test(t[1])), "A " + label + ": never claims a sign-out everywhere it didn't do");
  });
}
test("A a WebSocket close during a FAILED sign-out is handed back (the tab reconnects)", async () => {
  let release;
  const gate = new Promise((r) => { release = r; });
  const env = accountEnv({ "/api/logout": () => gate.then(() => jsonResponse(500, { ok: false, error: "store_error" })) });
  let reconnects = 0;
  const p = env.ctx.TRAccount.signOut();
  await env.flush();
  env.ctx.TR.handleClose(1006, "", () => { reconnects++; });   // the link dropped while the request ran
  eq(reconnects, 0, "A held: nothing happens while the sign-out runs");
  release();
  await p; await env.flush();
  env.clock.advance(1300);                                     // the link-lost backoff (1.2 s)
  eq(reconnects, 1, "A after the failed sign-out the held close reconnects");
  eq(env.nav(), [], "A ... and the page stayed");
});
test("A a second click while a sign-out runs is ignored", async () => {
  let release;
  const gate = new Promise((r) => { release = r; });
  const env = accountEnv({ "/api/logout": () => gate.then(() => jsonResponse(200, { ok: true })) });
  const p1 = env.ctx.TRAccount.signOut(), p2 = env.ctx.TRAccount.signOut();
  release();
  await p1; await p2; await env.flush();
  eq(env.log.length, 1, "A one request for two clicks");
  eq(env.nav(), ["/login?reason=signed_out"], "A one navigation");
});

// ---- B. admin.js: the temporary password is shown exactly once --------------------------------------
const TEMP_PW = "Tmp4Pass9xyzQ";
function adminEnv() {
  const users = [
    { uid: "41444d494e", user: "ADMIN", role: "admin", must_change: false, online: 1 },
    { uid: "4f5031", user: "OP1", role: "operator", must_change: false, online: 0 },
    { uid: "5631", user: "V1", role: "viewer", must_change: false, online: 0 },
  ];
  const routes = {
    "/api/admin/users": (init) => {
      if (init && init.method === "POST") {
        const b = JSON.parse(init.body);
        users.push({ uid: "x" + users.length, user: b.user, role: b.role, must_change: true, online: 0 });
        return [200, { ok: true, uid: "x", user: b.user, temp_password: TEMP_PW }];
      }
      return [200, { me: "ADMIN", users: users.map((u) => Object.assign({}, u)) }];
    },
    "/api/admin/users/4f5031": (init) => { users[1].role = JSON.parse(init.body).role; return [200, { ok: true }]; },
    "/api/admin/users/5631/reset_password": [200, { ok: true, temp_password: TEMP_PW + "R" }],
    "/api/admin/allowed_hosts": [200, { entries: [], current_host: "localhost", current_origin: "http://localhost:8790", behind_proxy: false }],
  };
  const log = [];
  const env = makeEnv({ fetch: fetchStub(routes, log) });
  for (const id of ["userBtn", "adminBtn", "umAdmin", "umChangePw", "umSignoutAll", "umSignout", "modalRoot"]) env.el("div", { id });
  env.load("tr.js");
  env.ctx.TR.user = "ADMIN";
  env.ctx.TR.setRole("admin", ["view", "listen", "menu_read", "operate", "tx", "connect", "menu", "admin"]);
  env.load("account.js");
  env.load("admin.js");
  env.log = log;
  env.dialog = () => env.doc.querySelector(".tm-admin");
  env.secrets = () => env.dialog().querySelectorAll(".tm-secret");
  env.text = () => env.dialog().textContent;
  env.settle = async () => { for (let i = 0; i < 6; i++) await env.flush(); };
  env.addUser = async (name) => {
    env.dialog().querySelector("#admAddUser").click();
    env.dialog().querySelector("#admNewUser").value = name;
    env.dialog().querySelector("form.tm-addform").dispatchEvent(new env.Event("submit"));
    await env.settle();
  };
  return env;
}
test("B the temp password panel appears once, then never again on a re-render", async () => {
  const env = adminEnv();
  env.ctx.TRAdmin.open("users");
  await env.settle();
  check(env.dialog() && env.dialog().querySelectorAll("tr[data-user]").length === 3, "B the Users table rendered");
  await env.addUser("op2");
  eq(env.secrets().length, 1, "B after Add user: one temp-password panel");
  const code = env.secrets()[0];
  eq(code.textContent, TEMP_PW, "B it shows the server's temporary password");
  // a role change of ANOTHER user re-renders the tab: the panel must not come back
  const sel = env.dialog().querySelector('select[aria-label="Role of OP1"]');
  sel.value = "viewer";
  sel.dispatchEvent(new env.Event("change"));
  await env.settle();
  check(env.log.some((x) => x.url === "/api/admin/users/4f5031" && x.method === "PATCH"), "B the role change was sent");
  eq(env.secrets().length, 0, "B after the role change re-render: no temp-password panel");
  check(!env.text().includes(TEMP_PW), "B the password text is nowhere in the dialog");
  eq(code.textContent, "", "B the shown element was scrubbed (no copy of the password left in it)");
  // a plain refresh (the Users tab again) and a close/reopen never bring it back either
  env.dialog().querySelector("#admTab-users").click();
  await env.settle();
  eq(env.secrets().length, 0, "B a refresh of the tab shows no panel");
  env.ctx.TRAdmin.close(); env.ctx.TRAdmin.open("users");
  await env.settle();
  eq(env.secrets().length, 0, "B close + reopen shows no panel");
  check(!env.text().includes(TEMP_PW), "B ... and no password text");
});
test("B Done dismisses and scrubs; the next secret is shown once too", async () => {
  const env = adminEnv();
  env.ctx.TRAdmin.open("users");
  await env.settle();
  await env.addUser("op3");
  const code = env.secrets()[0];
  check(!!code, "B panel shown");
  const done = [...env.dialog().querySelectorAll(".tm-secret-panel button")].find((b) => b.textContent === "Done");
  done.click();
  eq(env.secrets().length, 0, "B Done removes the panel");
  eq(code.textContent, "", "B Done scrubs the password from the element");
  env.dialog().querySelector("#admTab-users").click();
  await env.settle();
  eq(env.secrets().length, 0, "B nothing after Done + refresh");
  // a password reset shows ITS temp password once
  const row = env.dialog().querySelector('tr[data-user="V1"]');
  [...row.querySelectorAll("button")].find((b) => b.textContent === "Reset password").click();
  const go = [...env.dialog().querySelectorAll(".tm-subrow button")].find((b) => b.textContent === "Reset password");
  go.click();
  await env.settle();
  eq(env.secrets().length, 1, "B reset: one panel");
  eq(env.secrets()[0].textContent, TEMP_PW + "R", "B reset: its own temp password");
  env.dialog().querySelector("#admTab-access").click();
  await env.settle();
  env.dialog().querySelector("#admTab-users").click();
  await env.settle();
  eq(env.secrets().length, 0, "B reset: gone after a tab switch and back");
});

// ---- C. app.js top-bar STOP on the shared TR.stopArm -------------------------------------------------
function appEnv() {
  const sockets = [];
  function FakeWS() { this.readyState = 1; this.sent = []; this.send = (d) => this.sent.push(JSON.parse(d)); this.close = () => {}; sockets.push(this); }
  const env = makeEnv({ WebSocket: FakeWS });
  env.ctx.Scope = function () { return new Proxy({ meta: {} }, { get: (t, k) => (k in t ? t[k] : () => []) }); };
  for (const id of ["onAir", "onAirStop", "onAirStopLbl", "onAirWho", "onAirTime", "pttBtn", "txStopFab"]) env.el("div", { id });
  env.doc.getElementById("onAirStopLbl").textContent = "STOP";
  env.load("tr.js");
  env.load("app.js");
  const ws = sockets[sockets.length - 1];
  env.frame = (f) => ws.onmessage({ data: JSON.stringify(f) });
  env.frame({ type: "hello", v: 1, conn: 3, user: "ME", role: "operator",
              perms: ["view", "listen", "menu_read", "operate", "tx", "connect", "menu"], status: {}, tx: { owner: null }, hb_interval: 2 });
  env.owner = (o) => env.frame({ type: "tx", owner: o });
  env.tap = () => env.doc.getElementById("onAirStop").click();
  env.stops = () => ws.sent.filter((f) => f.action === "tx_stop").length;
  env.sent = () => ws.sent.filter((f) => f.action !== "tx_hb").map((f) => f.action + (f.tx === false ? ":off" : ""));
  env.label = () => env.doc.getElementById("onAirStopLbl").textContent;
  env.armed = () => env.doc.getElementById("onAirStop").classList.contains("armed");
  return env;
}
const OP1 = { conn: 5, user: "OP1", claims: ["ptt"], elapsed_ms: 0 }, OP9 = { conn: 9, user: "OP9", claims: ["ptt"], elapsed_ms: 0 };
test("C top-bar STOP: two taps on the same owner -> one tx_stop", async () => {
  const e = appEnv();
  e.owner(OP1); e.tap();
  eq([e.stops(), e.armed(), e.label()], [0, true, "Tap again to stop OP1"], "C first tap arms, sends nothing");
  e.clock.advance(500); e.tap();
  eq([e.stops(), e.armed(), e.label()], [1, false, "STOP"], "C second tap: exactly one tx_stop, disarmed");
});
test("C top-bar STOP: an owner change between the taps disarms; the next tap only arms", async () => {
  const e = appEnv();
  e.owner(OP1); e.tap(); e.owner(OP9);
  eq([e.armed(), e.label()], [false, "STOP"], "C the tx frame for OP9 disarmed the arm for OP1");
  e.tap();
  eq([e.stops(), e.label()], [0, "Tap again to stop OP9"], "C the next tap arms for OP9, no tx_stop");
});
test("C top-bar STOP: the arm lapses after 3 s; a released owner disarms", async () => {
  const e = appEnv();
  e.owner(OP1); e.tap(); e.clock.advance(3001);
  eq([e.armed(), e.label()], [false, "STOP"], "C lapsed after 3 s");
  e.tap(); e.owner(null);
  eq([e.armed(), e.stops()], [false, 0], "C the owner released: disarmed, nothing sent");
  e.tap();
  eq(e.stops(), 0, "C a tap with nobody on air sends nothing");
});
test("C top-bar STOP: the owner's own STOP is one tap (unkeys its claims)", async () => {
  const e = appEnv();
  e.owner({ conn: 3, user: "ME", claims: ["ptt"], elapsed_ms: 0 }); e.tap();
  eq(e.sent(), ["ptt:off"], "C own PTT: one tap sends ptt false (no tx_stop)");
});
test("C top-bar STOP: a viewer can't arm or stop", async () => {
  const e = appEnv();
  e.frame({ type: "hello", v: 1, conn: 4, user: "V1", role: "viewer", perms: ["view", "listen", "menu_read"], status: {}, tx: { owner: OP1 } });
  e.tap(); e.tap();
  eq([e.stops(), e.armed()], [0, false], "C viewer: nothing");
});

// ---- D. waterfall.js: the scope edge frequencies are drawn once --------------------------------------
test("D drawOverlay draws the centre frequency but not the edge frequencies (HTML labels do that)", async () => {
  const env = makeEnv();
  env.load("waterfall.js");
  const texts = [];
  const ctx2d = new Proxy({ fillText: (t) => texts.push(String(t)), measureText: (t) => ({ width: String(t).length * 6 }) },
                          { get: (t, k) => (k in t ? t[k] : () => {}), set: (t, k, v) => { t[k] = v; return true; } });
  for (const meta of [{ mode: 0, center: 144200000, span: 50000, tuned: 144200000, filterBw: 2400 },          // CI-V centre mode
                      { mode: 1, lower: 144100000, upper: 144300000, tuned: 144200000, filterBw: 2400 },        // CI-V fixed mode
                      { mode: 0, center: 14075000, span: 3000, lower: 14074000, upper: 14077000, tuned: 0 }]) { // AF scope
    texts.length = 0;
    const sc = Object.create(env.ctx.Scope.prototype);
    Object.assign(sc, { octx: ctx2d, W: 600, scopeH: 300, specH: 150, wfH: 150, meta, opMode: "USB", showBandplan: false, bandplan: [] });
    sc.drawOverlay();
    const [lo, hi] = sc.visibleRange();
    for (const edge of [lo, hi]) {
      check(!texts.includes((edge / 1e6).toFixed(3)), "D mode " + meta.mode + ": edge " + (edge / 1e6).toFixed(3) + " not drawn on the canvas (texts " + JSON.stringify(texts) + ")");
    }
    if (meta.tuned) check(texts.includes((meta.tuned / 1e6).toFixed(4) + " MHz"), "D mode " + meta.mode + ": the centre label is still drawn");
  }
});

// ---- E. tr.js: role_changed -----------------------------------------------------------------------
test("E a 4401 role_changed close lands on /login?reason=role_changed", async () => {
  const env = makeEnv({ fetch: fetchStub({ "/api/auth_status": [200, { authed: false, setup: { kind: null } }] }, []) });
  env.load("tr.js");
  let rc = 0;
  env.ctx.TR.handleClose(4401, "role_changed", () => { rc++; });
  await env.flush(); await env.flush();
  eq(env.ctx.location.replaced, ["/login?reason=role_changed&next=%2F"], "E the sign-in page explains the role change");
  eq(rc, 0, "E no reconnect");
});
test("E role_changed inside the consecutive-4401 cap keeps its reason", async () => {
  const env = makeEnv({ fetch: fetchStub({ "/api/auth_status": [200, { authed: true, must_change: false }] }, []) });
  env.load("tr.js");
  let rc = 0;
  env.ctx.TR.handleClose(4401, "session_revoked", () => { rc++; });   // own password change: reconnect
  await env.flush(); await env.flush();
  eq(rc, 1, "E the first 4401 reconnected (session still valid)");
  env.clock.advance(5000);
  env.ctx.TR.handleClose(4401, "role_changed", () => { rc++; });
  await env.flush();
  eq(env.ctx.location.replaced, ["/login?reason=role_changed&next=%2F"], "E the cap goes to the sign-in page with role_changed");
});

(async () => {
  for (const [name, fn] of pending) {
    try { await fn(); } catch (e) { failures++; checks++; console.log("FAIL " + name + " threw: " + (e && e.stack || e)); }
  }
  console.log(failures ? failures + " of " + checks + " checks FAILED" : "ALL PASS (" + checks + " checks)");
  process.exit(failures ? 1 : 0);
})();
