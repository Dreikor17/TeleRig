/* Administration > Update (admin.js renderUpdate), tested on the REAL frontend code (tr.js, account.js,
 * admin.js) in a Node vm on tests/fakedom.js against a FAKE server (a fetch stub). No browser, no server,
 * no git, no radio. Run by tests/t_af_scope.py (unit group):
 *
 *     node tests/update_ui_test.js    -> exit 0 and "ALL PASS", or exit 1 naming each failure
 *
 *  A. a copy that cannot update itself: the reason, no buttons
 *  B. up to date: "up to date", Update now stays disabled
 *  C. new commits: listed; Update now asks first (No sends nothing); Yes posts once and the tab follows
 *     the steps, "restarting", the server being away, then the NEW server (a new boot id) -> reload
 *  D. a failed update shows why and offers the check again; an update already running is followed
 */
"use strict";
const { makeEnv } = require("./fakedom");

let failures = 0, checks = 0;
const pending = [];
function check(cond, msg) { checks++; if (!cond) { failures++; console.log("FAIL " + msg); } }
function eq(a, b, msg) { const ja = JSON.stringify(a), jb = JSON.stringify(b); check(ja === jb, msg + ": got " + ja + ", want " + jb); }
function test(name, fn) { pending.push([name, fn]); }

function json(status, body) { return { ok: status >= 200 && status < 300, status, json: async () => body }; }

const INFO = { git: true, branch: "master", head: "a".repeat(40), short: "aaaaaaa", subject: "the current one",
               date: "2026-11-17T10:00:00+00:00", origin: "https://github.com/Dreikor17/TeleRig.git", updatable: true, reason: null };
const CHECK2 = { ok: true, at: 1794960000, branch: "master", target: "b".repeat(40), target_short: "bbbbbbb", behind: 2, ahead: 0,
                 commits: [{ short: "bbbbbbb", date: "2026-11-17T11:00:00+00:00", subject: "Band plan strip" },
                           { short: "ccccccc", date: "2026-11-17T10:30:00+00:00", subject: "Update now button" }] };

function env(state) {
  const log = [];
  const S = Object.assign({ info: INFO, job: { state: "idle" }, check: null, boot: "boot-1", pid: 11, version: "0.3.0",
                            down: false, checkAnswer: [200, { ok: true, check: CHECK2 }], startAnswer: [200, { ok: true }] }, state || {});
  const fetch = (url, init) => {
    const u = String(url).split("?")[0], method = (init && init.method) || "GET";
    log.push({ url: u, method, body: init && init.body ? JSON.parse(init.body) : null });
    if (S.down && u === "/api/admin/update") return Promise.reject(new TypeError("Failed to fetch"));
    if (u === "/api/admin/update" && method === "GET")
      return Promise.resolve(json(200, { info: S.info, job: S.job, check: S.check, boot: S.boot, pid: S.pid, version: S.version }));
    if (u === "/api/admin/update/check") return Promise.resolve(json(S.checkAnswer[0], S.checkAnswer[1]));
    if (u === "/api/admin/update" && method === "POST") return Promise.resolve(json(S.startAnswer[0], S.startAnswer[1]));
    if (u === "/api/admin/users") return Promise.resolve(json(200, { me: "ADMIN", users: [] }));
    return Promise.resolve(json(404, { ok: false, error: "not_found" }));
  };
  const e = makeEnv({ fetch });
  for (const id of ["userBtn", "adminBtn", "umAdmin", "umChangePw", "umSignoutAll", "umSignout", "modalRoot"]) e.el("div", { id });
  e.load("tr.js");
  e.ctx.TR.user = "ADMIN";
  e.ctx.TR.setRole("admin", ["view", "listen", "menu_read", "operate", "tx", "connect", "menu", "admin"]);
  e.confirms = [];
  e.confirmAnswer = true;
  e.ctx.TR.confirm = async (text, o) => { e.confirms.push([String(text), o && o.ok]); return e.confirmAnswer; };
  e.load("account.js");
  e.load("admin.js");
  e.S = S;
  e.log = log;
  e.settle = async () => { for (let i = 0; i < 8; i++) await e.flush(); };
  e.byId = (id) => e.doc.documentElement.querySelectorAll("#" + id)[0] || null;
  e.text = () => e.doc.querySelector(".tm-admin").textContent;
  e.posts = (u) => log.filter((c) => c.method === "POST" && c.url === u);
  e.tick = async (ms) => { e.clock.advance(ms || 1000); await e.settle(); };
  return e;
}
async function openTab(e) { e.ctx.TRAdmin.open("update"); await e.settle(); }

test("A a copy that cannot update itself says why and offers no button", async () => {
  const e = env({ info: { git: false, updatable: false, reason: "This copy of TeleRig was not installed with git (a zip download)." } });
  await openTab(e);
  check(e.text().includes("not installed with git"), "A the reason is shown: " + e.text().slice(0, 300));
  eq([e.byId("admUpGo"), e.byId("admUpCheck")], [null, null], "A no Update now, no Check");
});

test("B up to date: says so, Update now stays disabled", async () => {
  const e = env({ checkAnswer: [200, { ok: true, check: Object.assign({}, CHECK2, { behind: 0, commits: [] }) }] });
  await openTab(e);
  check(e.text().includes("Version") && e.text().includes("v0.3.0") && e.text().includes("master (releases)") && e.text().includes("aaaaaaa"),
        "B this install: version, branch, commit");
  check(e.text().includes("Not checked yet"), "B not checked yet");
  eq(e.byId("admUpGo").disabled, true, "B Update now is disabled before a check");
  e.byId("admUpCheck").click(); await e.settle();
  eq(e.posts("/api/admin/update/check").length, 1, "B Check posts once");
  check(!!e.byId("admUpCurrent") && e.text().includes("up to date"), "B up to date");
  eq(e.byId("admUpGo").disabled, true, "B ... and Update now stays disabled");
});

test("C new commits: listed; asks first; Yes posts once and follows to the restarted server, then reloads", async () => {
  const e = env();
  await openTab(e);
  e.byId("admUpCheck").click(); await e.settle();
  const rows = e.byId("admUpCommits").querySelectorAll("tbody tr");
  eq(rows.length, 2, "C two new commits listed");
  check(e.text().includes("Band plan strip") && e.text().includes("2 new commits"), "C with what changed");
  eq(e.byId("admUpGo").disabled, false, "C Update now is enabled");
  e.confirmAnswer = false;
  e.byId("admUpGo").click(); await e.settle();
  eq([e.confirms.length, e.posts("/api/admin/update").length], [1, 0], "C asked; No sends nothing");
  check(/bbbbbbb/.test(e.confirms[0][0]) && /disconnected/.test(e.confirms[0][0]), "C the question names the target and the disconnect: " + e.confirms[0][0]);
  e.confirmAnswer = true;
  e.S.job = { state: "running", steps: [{ step: "fetch", ok: true, text: "origin/master is at bbbbbbb" }] };
  e.byId("admUpGo").click(); await e.settle();
  eq(e.posts("/api/admin/update").length, 1, "C Yes posts once");
  eq(e.posts("/api/admin/update")[0].body, {}, "C ... with an empty body (the server decides the branch)");
  check(e.text().includes("Fetch from GitHub"), "C the steps are shown");
  eq([e.byId("admUpGo").disabled, e.byId("admUpCheck").disabled], [true, true], "C both buttons are off while it runs");
  e.S.job = { state: "restarting", steps: e.S.job.steps.concat([{ step: "update", ok: true, text: "aaaaaaa -> bbbbbbb" },
             { step: "packages", ok: true, text: "" }, { step: "self-test", ok: true, text: "" }, { step: "restart", ok: true, text: "restarting now" }]) };
  await e.tick();
  check(e.text().includes("Restarting TeleRig"), "C restarting");
  e.S.down = true;
  await e.tick();
  check(e.text().includes("TeleRig is restarting"), "C the server is away: still waiting");
  e.S.down = false;
  e.S.boot = "boot-2";
  e.S.info = Object.assign({}, INFO, { short: "bbbbbbb" });
  e.S.job = { state: "idle" };
  await e.tick();
  check(!!e.byId("admUpDone") && e.text().includes("bbbbbbb"), "C the new server answered: updated and restarted");
  eq(e.ctx.location.reloaded || 0, 0, "C not reloaded yet (the message shows first)");
  await e.tick(1600);
  eq(e.ctx.location.reloaded, 1, "C then the page reloads onto the new version");
});

test("D an update that could not restart (or a test server): the files are new, nothing more to do", async () => {
  const e = env();
  await openTab(e);
  e.byId("admUpCheck").click(); await e.settle();
  e.S.job = { state: "running", steps: [] };
  e.byId("admUpGo").click(); await e.settle();
  e.S.job = { state: "updated", restart: "skipped", steps: [{ step: "restart", ok: true, text: "" }] };
  e.S.check = Object.assign({}, CHECK2, { behind: 0, commits: [] });
  e.S.info = Object.assign({}, INFO, { short: "bbbbbbb", subject: "Update now button" });
  await e.tick();
  check(e.text().includes("Restart TeleRig on its PC"), "D says a restart is still needed");
  check(!!e.byId("admUpCurrent"), "D the list says up to date");
  eq(e.byId("admUpGo").disabled, true, "D Update now is off: nothing more to update");
  check(e.byId("admUpCommit").textContent.includes("bbbbbbb"), "D this install shows the new commit");
});
test("D a failed update shows why; an update already running is followed", async () => {
  const e = env();
  await openTab(e);
  e.byId("admUpCheck").click(); await e.settle();
  e.S.job = { state: "running", steps: [] };
  e.byId("admUpGo").click(); await e.settle();
  e.S.job = { state: "failed", error: "The new version failed its self-test (it does not start): boom. The install is back on aaaaaaa; nothing restarted.",
              steps: [{ step: "self-test", ok: false, text: "boom" }, { step: "rollback", ok: true, text: "back to aaaaaaa" }] };
  await e.tick();
  check(!!e.byId("admUpEnd") && e.text().includes("back on aaaaaaa"), "D the failure and the rollback are shown");
  eq(e.byId("admUpCheck").disabled, false, "D the check is offered again");
  const r = env({ job: { state: "running", steps: [{ step: "fetch", ok: true, text: "" }] } });
  await openTab(r);
  check(r.text().includes("Fetch from GitHub"), "D opening the tab during an update follows it");
  eq(r.byId("admUpGo").disabled, true, "D ... with Update now off");
});

(async () => {
  for (const [name, fn] of pending) {
    try { await fn(); } catch (err) { failures++; checks++; console.log("FAIL " + name + " threw: " + (err && err.stack || err)); }
  }
  console.log(failures ? failures + " of " + checks + " checks FAILED" : "ALL PASS (" + checks + " checks)");
  process.exit(failures ? 1 : 0);
})();
