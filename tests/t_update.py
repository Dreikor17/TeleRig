"""`admin` group: Administration > Update (backend/updater.py) on a real server process.

SAFETY. Every server is harness.Server (run.py --sim-only --no-autoconnect, scratch config). The
updater NEVER touches this TeleRig checkout: each server gets TELERIG_TEST_UPDATE_REPO = a scratch git
repository built here (a bare repository plays GitHub, an install clone on master or dev is updated),
which only a --sim-only server honours. The restart is skipped (TELERIG_TEST_UPDATE_NORESTART) except in
U4, which restarts its scratch server for real and stops the new process at the end. TX is only ever
keyed on the simulator, through harness.WS (assert_sim first).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import stat
import subprocess
import time
from pathlib import Path

import harness
from harness import check, eq, test

REPO = Path(__file__).resolve().parent.parent


def _git(cwd, *args):
    r = subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "core.autocrlf=false",
                        "-c", "init.defaultBranch=master", *args], cwd=str(cwd), capture_output=True, text=True)
    if r.returncode != 0:
        raise harness.HarnessError(f"git {args}: {r.stdout}{r.stderr}")
    return r.stdout.strip()


class Repo:
    """A bare 'GitHub' + a seed clone that publishes; install(branch) makes the clone the server updates."""

    def __init__(self, base: Path):
        self.base = base
        self.origin, self.seed = base / "origin.git", base / "seed"
        _git(base, "init", "--bare", str(self.origin))
        _git(base, "clone", str(self.origin), str(self.seed))
        self.write({"backend/__init__.py": '__version__ = "9.9.9"\n', "backend/server.py": "OK = 1\n",
                    "requirements.txt": "# nothing\n"})
        _git(self.seed, "add", "-A")
        _git(self.seed, "commit", "-m", "v1")
        _git(self.seed, "push", "origin", "master")
        _git(self.seed, "checkout", "-b", "dev")
        _git(self.seed, "commit", "--allow-empty", "-m", "dev 1")
        _git(self.seed, "push", "origin", "dev")
        _git(self.seed, "checkout", "master")

    def write(self, files):
        for p, t in files.items():
            f = self.seed / p
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(t, encoding="utf-8")

    def publish(self, branch, files, msg):
        _git(self.seed, "checkout", branch)
        self.write(files)
        _git(self.seed, "add", "-A")
        _git(self.seed, "commit", "-m", msg)
        _git(self.seed, "push", "origin", branch)
        return _git(self.seed, "rev-parse", "HEAD")

    def install(self, name, branch="master") -> Path:
        d = self.base / name
        _git(self.base, "clone", "--branch", branch, str(self.origin), str(d))
        return d


def _rmtree(p: Path) -> None:
    def onerr(fn, path, _e):
        os.chmod(path, stat.S_IWRITE)
        fn(path)
    shutil.rmtree(p, onerror=onerr)


def _server(ctx, inst: Path, restart: bool = False):
    env = {"TELERIG_TEST_UPDATE_REPO": str(inst)}
    if not restart:
        env["TELERIG_TEST_UPDATE_NORESTART"] = "1"
    srv = ctx.server(extra_env=env)
    admin = harness.ready_admin(srv)
    return srv, admin


def _wait_job(admin, states=("updated", "failed", "current", "restarting"), timeout=60.0):
    end = time.monotonic() + timeout
    j = {}
    while time.monotonic() < end:
        r = admin.get("/api/admin/update")
        if r.status == 200:
            j = r.json()
            if (j.get("job") or {}).get("state") in states:
                return j
        time.sleep(0.25)
    return j


@test("unit")
def update_tab_in_node_vm(ctx):
    """admin.js Update tab on a fake server (tests/update_ui_test.js): the reason when a copy cannot
    update itself, the check, the question, the steps, the restart and the reload; the wiring."""
    repo_dir = harness.REPO
    r = subprocess.run(["node", str(repo_dir / "tests" / "update_ui_test.js")], capture_output=True, text=True,
                       timeout=120, cwd=str(repo_dir))
    eq(r.returncode, 0, f"update_ui_test.js: {r.stdout}{r.stderr}")
    check("ALL PASS" in r.stdout, f"update_ui_test.js output: {r.stdout}")
    adm = (repo_dir / "frontend" / "admin.js").read_text(encoding="utf-8")
    check('["update", "Update"]' in adm and "update: renderUpdate" in adm, "admin.js has the Update tab")
    api = (repo_dir / "backend" / "admin_api.py").read_text(encoding="utf-8")
    check('@requires("admin", sudo=True)\nasync def api_update_start' in api, "Update now needs a fresh sudo")
    check('@requires("admin")\nasync def api_update_get' in api and '@requires("admin")\nasync def api_update_check' in api,
          "reading and checking are for admins")
    srv = (repo_dir / "backend" / "server.py").read_text(encoding="utf-8")
    check('return Path(t) if (t and sim_only()) else _REPO_DIR' in srv,
          "only a --sim-only test server may point the updater at another repository")
    run = (repo_dir / "run.py").read_text(encoding="utf-8")
    check("_wait_for_old_server(args.host, args.port)" in run and '"_restart_requested"' in run,
          "run.py waits for the old server and treats the restart as a clean exit")


@test("admin")
def u1_update_tab_roles_check_branch(ctx):
    base = ctx.tmp("repo")
    repo = Repo(base)
    inst = repo.install("install")
    srv, admin = _server(ctx, inst)
    pws = harness.mk_users(srv.tmp, [("OP1", "operator"), ("V1", "viewer")])
    for name in ("OP1", "V1"):
        c = srv.client()
        eq(harness.login(c, name, pws[name]).status, 200, f"U1 login {name}")
        eq([c.get("/api/admin/update").status, c.post("/api/admin/update/check", {}).status, c.post("/api/admin/update", {}).status],
           [403, 403, 403], f"U1 {name} gets nothing of the Update tab")
    j = admin.get("/api/admin/update").json()
    eq((j["info"]["branch"], j["info"]["updatable"], j["info"]["head"] == _git(inst, "rev-parse", "HEAD"), j["job"]["state"]),
       ("master", True, True, "idle"), "U1 an admin sees this install: master, can update itself")
    check(isinstance(j["boot"], str) and len(j["boot"]) == 16 and isinstance(j["pid"], int), f"U1 boot id + pid: {j}")
    r = admin.post("/api/admin/update/check", {})
    eq((r.status, r.json()["check"]["behind"], r.json()["check"]["branch"]), (200, 0, "master"), "U1 up to date")
    new = repo.publish("master", {"backend/server.py": "OK = 2\n"}, "a new master commit")
    repo.publish("dev", {"dev.txt": "x\n"}, "a dev-only commit")
    r = admin.post("/api/admin/update/check", {})
    c = r.json()["check"]
    eq((c["behind"], [x["subject"] for x in c["commits"]], c["target"] == new), (1, ["a new master commit"], True),
       "U1 the check lists master's new commit (not dev's)")
    eq(_git(inst, "rev-parse", "HEAD") == new, False, "U1 ... and changed nothing")
    _rmtree(base)


@test("admin")
def u2_update_now_master_and_dev(ctx):
    base = ctx.tmp("repo")
    repo = Repo(base)
    for branch in ("master", "dev"):
        inst = repo.install("install-" + branch, branch)
        srv, admin = _server(ctx, inst)
        new = repo.publish(branch, {f"{branch}.txt": "new\n"}, f"new on {branch}")
        r = admin.post("/api/admin/update", {})
        eq(r.status, 200, f"U2 {branch}: Update now {r!r}")
        j = _wait_job(admin)
        eq((j["job"]["state"], j["job"]["branch"], j["job"]["restart"], _git(inst, "rev-parse", "HEAD") == new),
           ("updated", branch, "skipped", True), f"U2 a {branch} install updates from GitHub's {branch} (restart skipped in tests)")
        eq([s["step"] for s in j["job"]["steps"]], ["fetch", "check", "update", "packages", "self-test", "restart"], f"U2 {branch}: the steps")
        other = "dev" if branch == "master" else "master"
        eq((inst / f"{other}.txt").exists(), False, f"U2 {branch}: nothing of {other} came along")
        recs = [x for x in harness.audit_records(srv.tmp, "audit.log") if x.get("ev", "").startswith("update_")]
        eq([x["ev"] for x in recs][:3], ["update_requested", "update_start", "update_done"], f"U2 {branch}: logged")
        srv.stop()
    _rmtree(base)


@test("admin")
def u3_refused_while_transmitting_or_not_updatable(ctx):
    base = ctx.tmp("repo")
    repo = Repo(base)
    inst = repo.install("install")
    srv, admin = _server(ctx, inst)
    pws = harness.mk_users(srv.tmp, [("OP1", "operator")])
    op = srv.client()
    eq(harness.login(op, "OP1", pws["OP1"]).status, 200, "U3 login")
    eq(admin.post("/api/connect", {"transport": "sim", "radio": "ic7300mk2"}).status, 200, "U3 sim connect")
    harness.assert_sim(admin)
    repo.publish("master", {"a.txt": "a\n"}, "a commit")
    with op.ws() as ws:
        ws.recv_json(type="hello")
        ws.send({"action": "ptt", "tx": True, "seq": 1})
        ws.wait_for(lambda m: m.get("type") == "tx" and (m.get("owner") or {}).get("user") == "OP1", timeout=5)
        r = admin.post("/api/admin/update", {})
        eq((r.status, r.json().get("error")), (409, "busy"), "U3 refused while someone transmits")
        eq(admin.get("/api/admin/update").json()["job"]["state"], "idle", "U3 ... nothing started")
        ws.send({"action": "ptt", "tx": False, "seq": 2})
        ws.wait_for(lambda m: m.get("type") == "tx" and not m.get("owner"), timeout=5)
    r = admin.post("/api/admin/update", {})
    eq(r.status, 200, "U3 idle again: the update runs")
    eq(_wait_job(admin)["job"]["state"], "updated", "U3 updated")
    srv.stop()
    plain = base / "zip-copy"
    shutil.copytree(inst / "backend", plain / "backend")
    srv2, admin2 = _server(ctx, plain)
    j = admin2.get("/api/admin/update").json()
    check(j["info"]["updatable"] is False and "not installed with git" in (j["info"]["reason"] or ""), f"U3 a zip copy says why: {j['info']}")
    r = admin2.post("/api/admin/update", {})
    eq((r.status, r.json().get("error")), (409, "not_updatable"), "U3 ... and Update now is refused")
    srv2.stop()
    _rmtree(base)


@test("admin")
def u4_update_restarts_telerig_for_real(ctx):
    base = ctx.tmp("repo")
    repo = Repo(base)
    inst = repo.install("install")
    srv, admin = _server(ctx, inst, restart=True)
    j0 = admin.get("/api/admin/update").json()
    new = repo.publish("master", {"b.txt": "b\n"}, "restart me")
    old = srv.proc
    eq(admin.post("/api/admin/update", {}).status, 200, "U4 Update now")
    new_pid = None
    try:
        # the job runs, the server restarts: watch for the new server (a new boot id), or a failure
        end = time.monotonic() + 60
        j1, last = {}, {}
        while time.monotonic() < end:
            try:
                r = admin.get("/api/admin/update")
                if r.status == 200:
                    last = r.json()
                    if last.get("boot") != j0["boot"]:
                        j1 = last
                        break
                    if (last.get("job") or {}).get("state") == "failed":
                        break
            except Exception:
                pass                                       # down between the two servers
            time.sleep(0.25)
        check(bool(j1), f"U4 the update ran and TeleRig restarted (last status: {(last.get('job') or {})})")
        check(old is not None and old.poll() is not None, "U4 the old server process exited")
        eq(old.returncode, 0, "U4 ... cleanly (exit 0: run.bat does not stop with an error)")
        new_pid = j1.get("pid")
        check(bool(j1), "U4 a new server answers on the same port")
        eq((j1.get("pid") != j0["pid"], (j1.get("info") or {}).get("head") == new, (j1.get("job") or {}).get("state")),
           (True, True, "idle"), "U4 ... a new process, the install at the new commit, the admin still signed in")
        check("waiting for the previous server to stop" in srv.log_text(), "U4 the new copy waited for the old one")
    finally:
        if not new_pid:                                    # failed early: find the restarted copy anyway
            try:
                r = admin.get("/api/admin/update")
                if r.status == 200 and r.json().get("boot") != j0["boot"]:
                    new_pid = r.json().get("pid")
            except Exception:
                pass
        if new_pid:
            try:
                os.kill(int(new_pid), signal.SIGTERM)
            except OSError:
                pass
            end = time.monotonic() + 15
            while time.monotonic() < end:
                try:
                    admin.get("/login")
                except Exception:
                    break
                time.sleep(0.3)
            time.sleep(1.0)
    _rmtree(base)


@test("admin")
def u5_version_label_names_the_running_checkout(ctx):
    """The top bar's version (boot.version_label, /api/version label; tr.js / app.js show it): THIS
    checkout's branch and commit, read when the server starts, never the scratch update repository's.
    A dev checkout shows "<number>-dev+<commit>", any other the number alone; boot.version and
    /api/version current stay the number alone (the update check compares those)."""
    base = ctx.tmp("repo")
    repo = Repo(base)
    inst = repo.install("install", "dev")             # a scratch DEV install with commits of its own
    srv, admin = _server(ctx, inst)
    ver = re.search(r'__version__ = "([^"]+)"', (REPO / "backend" / "__init__.py").read_text(encoding="utf-8")).group(1)
    branch, sha = _git(REPO, "rev-parse", "--abbrev-ref", "HEAD"), _git(REPO, "rev-parse", "HEAD")[:7]
    want = f"{ver}-dev+{sha}" if branch == "dev" else ver
    html = admin.get("/").text
    boot = json.loads(re.search(r'<script id="tr-boot" type="application/json">(.*?)</script>', html, re.S).group(1))
    eq((boot["version"], boot["version_label"]), (ver, want), f"U5 the page (this checkout is on {branch} at {sha})")
    v = admin.get("/api/version").json()
    eq((v["current"], v["label"]), (ver, want), "U5 /api/version")
    scratch = _git(inst, "rev-parse", "HEAD")[:7]
    check(scratch == sha or scratch not in boot["version_label"], "U5 never the scratch update repository's commit")
    for f, use in (("tr.js", "boot.version_label || boot.version"), ("app.js", "v.label || v.current")):
        check(use in (REPO / "frontend" / f).read_text(encoding="utf-8"), f"U5 {f} shows the label ({use})")
    _rmtree(base)
