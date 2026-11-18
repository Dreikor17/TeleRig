"""The web updater (backend/updater.py) on REAL git repositories in a scratch folder.
run: python -m backend.updater_test

No network and no TeleRig install is touched: a bare repository plays GitHub, a "seed" clone publishes
commits to its master and dev, and "install" clones (master or dev) are what the Updater updates. pip is
faked (nothing is installed); the self-test really runs Python in the install (a tiny `backend` package).

  1. what an install is: branch, commit, updatable or why not (another branch, no git, a zip copy)
  2. a check sees ITS branch only: a master install master's new commits, a dev install dev's
  3. an update moves master to GitHub's master and dev to GitHub's dev, never across
  4. packages only when requirements.txt changed; the self-test runs; then the restart
  5. refused / failed without a change: local changes, local commits, someone transmitting, an
     unreachable origin; a failed self-test goes back to the old commit and does not restart
  6. the restart waits for a transmission to end (and then restarts anyway); one update at a time
"""
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from backend import updater as U

_ok = True


def C(label, got, want):
    global _ok
    good = got == want
    _ok = _ok and good
    print(("PASS" if good else "FAIL"), label, "->", repr(got)[:300], "" if good else f"(want {want!r})")


def git(cwd, *args):
    r = subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "core.autocrlf=false",
                        "-c", "init.defaultBranch=master", "-c", "advice.detachedHead=false", *args],
                       cwd=str(cwd), capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {args}: {r.stdout}{r.stderr}")
    return r.stdout.strip()


def write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


tmp = Path(tempfile.mkdtemp(prefix="telerig-updater-"))
origin, seed = tmp / "origin.git", tmp / "seed"
git(tmp, "init", "--bare", str(origin))
git(tmp, "clone", str(origin), str(seed))
write(seed / "backend" / "__init__.py", '__version__ = "1.0.0"\n')
write(seed / "backend" / "server.py", "OK = True\n")
write(seed / "requirements.txt", "# nothing\n")
git(seed, "add", "-A")
git(seed, "commit", "-m", "v1")
git(seed, "push", "origin", "master")
git(seed, "checkout", "-b", "dev")
write(seed / "dev.txt", "dev 1\n")
git(seed, "add", "-A")
git(seed, "commit", "-m", "dev 1")
git(seed, "push", "-u", "origin", "dev")
git(seed, "checkout", "master")


def publish(branch, files, msg):
    git(seed, "checkout", branch)
    for path, text in files.items():
        write(seed / path, text)
    git(seed, "add", "-A")
    git(seed, "commit", "-m", msg)
    git(seed, "push", "origin", branch)
    return git(seed, "rev-parse", "HEAD")


def install(name, branch):
    d = tmp / name
    git(tmp, "clone", "--branch", branch, str(origin), str(d))
    return d


audits = []


def mk(repo, **kw):
    up = U.Updater(repo, audit=lambda ev, **f: audits.append((ev, f)), **kw)
    real = up.run
    up.pip_calls = []

    def runner(args, timeout):
        if "pip" in args:
            up.pip_calls.append(list(args))
            return 0, "fake pip: installed"
        return real(args, timeout)
    up.run = runner
    return up


def wait(up, states=("current", "updated", "restarting", "failed"), t=30):
    end = time.time() + t
    while time.time() < end:
        th = up.thread
        if th is not None and not th.is_alive() and up.status()["job"]["state"] in states:
            return up.status()["job"]
        time.sleep(0.05)
    return up.status()["job"]


def head(repo):
    return git(repo, "rev-parse", "HEAD")


try:
    # -- 1. what an install is --------------------------------------------------------------------------
    im, idv = install("inst-master", "master"), install("inst-dev", "dev")
    um, ud = mk(im), mk(idv)
    inf = um.info()
    C("a master install: git, branch, updatable", (inf["git"], inf["branch"], inf["updatable"], inf["reason"]), (True, "master", True, None))
    C("... its commit and subject", (inf["head"] == head(im), inf["short"] == head(im)[:7], inf["subject"]), (True, True, "v1"))
    C("a dev install", (ud.info()["branch"], ud.info()["updatable"]), ("dev", True))
    # the version the pages show (server VERSION_LABEL): "-dev+<commit>" on a dev install only
    C("version label: a master install shows the release number alone", U.version_label("1.0.0", um.info()), "1.0.0")
    C("version label: a dev install adds -dev and the commit it is on", U.version_label("1.0.0", ud.info()),
      "1.0.0-dev+" + head(idv)[:7])
    labels = {}
    other = install("inst-other", "master")
    git(other, "checkout", "-b", "feature")
    r = mk(other).info()
    labels["another branch"] = U.version_label("1.0.0", r)
    C("another branch is not updated by the button (and says so)", (r["updatable"], "feature" in (r["reason"] or "")), (False, True))
    git(other, "checkout", "--detach")
    labels["a detached checkout"] = U.version_label("1.0.0", mk(other).info())
    C("a detached checkout neither", (mk(other).info()["updatable"], "detached" in (mk(other).info()["reason"] or "")), (False, True))
    plain = tmp / "zip-copy"
    shutil.copytree(im / "backend", plain / "backend")
    r = mk(plain).info()
    labels["a zip copy"] = U.version_label("1.0.0", r)
    C("a copy without git (a zip download): not updatable, says so", (r["git"], r["updatable"], "not installed with git" in r["reason"]), (False, False, True))
    r = mk(im, git=None).info()
    labels["no git"] = U.version_label("1.0.0", mk(idv, git=None).info())
    C("no git on the PC: not updatable, says so", (r["updatable"], "git is not installed" in r["reason"]), (False, True))
    C("version label: another branch, a detached checkout, a zip copy, no git: the number alone", labels,
      {"another branch": "1.0.0", "a detached checkout": "1.0.0", "a zip copy": "1.0.0", "no git": "1.0.0"})
    C("version label: on dev with no readable commit, just -dev; nothing known, the number",
      (U.version_label("1.0.0", {"branch": "dev", "short": None}), U.version_label("1.0.0", None)), ("1.0.0-dev", "1.0.0"))
    C("start refuses with the reason (no git)", "git is not installed" in (mk(im, git=None).start("A", tx_busy=lambda: False, restart=lambda: None) or ""), True)

    # -- 2. a check sees ITS branch only -----------------------------------------------------------------
    c = um.check()
    C("up to date: nothing behind, nothing ahead", (c["ok"], c["behind"], c["ahead"], c["commits"]), (True, 0, 0, []))
    m2 = publish("master", {"backend/server.py": "OK = 2\n"}, "master 2")
    d2 = publish("dev", {"dev.txt": "dev 2\n"}, "dev 2")
    c = um.check()
    C("a master install sees master's new commit, not dev's", (c["behind"], [x["subject"] for x in c["commits"]], c["target"] == m2), (1, ["master 2"], True))
    c = ud.check()
    C("a dev install sees dev's new commit, not master's", (c["behind"], [x["subject"] for x in c["commits"]], c["target"] == d2), (1, ["dev 2"], True))
    C("a check never changes the install", (head(im) == m2, head(idv) == d2), (False, False))

    # -- 3. + 4. the update -----------------------------------------------------------------------------
    restarts = []
    why = um.start("ADMIN", tx_busy=lambda: False, restart=lambda: restarts.append("master") or None)
    j = wait(um)
    C("master: started", why, None)
    C("master: moved to GitHub's master", (head(im) == m2, (im / "backend" / "server.py").read_text(), j["state"], j["to"] == m2),
      (True, "OK = 2\n", "restarting", True))
    C("master: every step ok, in order", [(s["step"], s["ok"]) for s in j["steps"]],
      [("fetch", True), ("check", True), ("update", True), ("packages", True), ("self-test", True), ("restart", True)])
    C("master: requirements.txt unchanged -> no pip; restarted once", (um.pip_calls, restarts), ([], ["master"]))
    C("master: the dev commit did not come along", (im / "dev.txt").exists(), False)
    C("master: after the update the last check says up to date", (um.status()["check"] or {}).get("behind"), 0)
    C("... right after it, another update waits for the restart", um.start("ADMIN", tx_busy=lambda: False, restart=lambda: None), "running")
    um = mk(im)                                          # the restarted server
    why = ud.start("ADMIN", tx_busy=lambda: False, restart=lambda: "skipped")
    j = wait(ud)
    C("dev: moved to GitHub's dev, not master", (head(idv) == d2, (idv / "dev.txt").read_text(), (idv / "backend" / "server.py").read_text()),
      (True, "dev 2\n", "OK = True\n"))
    C("dev: a skipped restart (test mode) ends as updated", (j["state"], j["restart"]), ("updated", "skipped"))
    m3 = publish("master", {"requirements.txt": "# still nothing, but changed\n"}, "master 3 (requirements)")
    restarts.clear()
    um.start("ADMIN", tx_busy=lambda: False, restart=lambda: restarts.append(1) or None)
    j = wait(um)
    C("requirements.txt changed -> pip install -r requirements.txt, then the restart",
      (head(im) == m3, len(um.pip_calls), um.pip_calls[0][-3:] if um.pip_calls else None, restarts),
      (True, 1, ["-r", "requirements.txt", "--disable-pip-version-check"], [1]))
    um = mk(im)
    restarts.clear()
    um.start("ADMIN", tx_busy=lambda: False, restart=lambda: restarts.append(1) or None)
    j = wait(um)
    C("already up to date: nothing changes, no restart", (j["state"], restarts, head(im) == m3), ("current", [], True))

    # -- 5. refused or failed without a change ------------------------------------------------------------
    m4 = publish("master", {"backend/server.py": "OK = 4\n"}, "master 4")
    C("someone transmitting: refused before anything", (um.start("ADMIN", tx_busy=lambda: True, restart=lambda: None), head(im) == m3), ("busy", True))
    write(im / "backend" / "server.py", "OK = 'edited here'\n")
    um.start("ADMIN", tx_busy=lambda: False, restart=lambda: restarts.append(1) or None)
    j = wait(um)
    C("local changes to tracked files: refused, left as they are",
      (j["state"], "local changes" in (j["error"] or ""), head(im) == m3, (im / "backend" / "server.py").read_text(), restarts),
      ("failed", True, True, "OK = 'edited here'\n", []))
    git(im, "checkout", "--", "backend/server.py")
    write(im / "backend" / "__init__.py", '__version__ = "edited here too"\n')    # a file the update does NOT touch
    um.start("ADMIN", tx_busy=lambda: False, restart=lambda: restarts.append(1) or None)
    j = wait(um)
    C("a local change to a file the update does not touch (git would carry it along): refused too",
      (j["state"], "local changes" in (j["error"] or ""), head(im) == m3, restarts), ("failed", True, True, []))
    git(im, "checkout", "--", "backend/__init__.py")
    git(im, "commit", "--allow-empty", "-m", "a local commit")
    local = head(im)
    um.start("ADMIN", tx_busy=lambda: False, restart=lambda: restarts.append(1) or None)
    j = wait(um)
    C("a local commit GitHub does not have (diverged): refused", (j["state"], "cannot" in (j["error"] or ""), head(im) == local, restarts),
      ("failed", True, True, []))
    git(im, "reset", "--hard", m3)
    m5 = publish("master", {"backend/server.py": "raise SystemExit(3)\n"}, "master 5 (broken)")
    um.start("ADMIN", tx_busy=lambda: False, restart=lambda: restarts.append(1) or None)
    j = wait(um)
    C("a new version that fails its self-test: back to the old commit, no restart",
      (j["state"], "self-test" in (j["error"] or ""), head(im) == m3, (im / "backend" / "server.py").read_text(), restarts),
      ("failed", True, True, "OK = 2\n", []))
    C("... the steps show the rollback", [(s["step"], s["ok"]) for s in j["steps"]][-2:], [("self-test", False), ("rollback", True)])
    publish("master", {"backend/server.py": "OK = 6\n"}, "master 6 (fixed)")
    lost = install("inst-lost", "master")
    git(lost, "remote", "set-url", "origin", str(tmp / "no-such-repo.git"))
    ul = mk(lost)
    c = ul.check()
    C("GitHub unreachable: the check says so", (c["ok"], "Could not reach GitHub" in c["error"]), (False, True))
    ul.start("ADMIN", tx_busy=lambda: False, restart=lambda: restarts.append(1) or None)
    j = wait(ul)
    C("... and an update fails at fetch without a change", (j["state"], j["steps"][0]["step"], j["steps"][0]["ok"], restarts),
      ("failed", "fetch", False, []))

    # -- 6. the restart waits for a transmission; one update at a time --------------------------------------
    old_wait = U.RESTART_WAIT
    try:
        U.RESTART_WAIT = 10.0
        on_air = {"until": 0.0}
        t_restart = []
        um.start("ADMIN", tx_busy=lambda: time.time() < on_air["until"], restart=lambda: t_restart.append(time.time()) or None)
        on_air["until"] = time.time() + 1.5               # someone keys right after the update began
        j = wait(um)
        C("someone keyed meanwhile: the restart waits until the transmission ended", (len(t_restart), t_restart and t_restart[0] >= on_air["until"]),
          (1, True))
        um = mk(im)
        publish("master", {"backend/server.py": "OK = 7\n"}, "master 7")
        U.RESTART_WAIT = 1.0
        t_restart.clear()
        stuck = {"on": False}
        um.start("ADMIN", tx_busy=lambda: stuck["on"], restart=lambda: t_restart.append(1) or None)
        stuck["on"] = True                                # and does not stop
        j = wait(um)
        C("a transmission that does not end: restarted anyway after the wait (the shutdown releases it)", (j["state"], t_restart), ("restarting", [1]))
        um.job["ended"] = time.time() - U.RESTARTING_STALE - 1
        C("a restart that never happened (stale) does not block the next update",
          um.start("ADMIN", tx_busy=lambda: False, restart=lambda: "skipped"), None)
        wait(um)
        um = mk(im)
    finally:
        U.RESTART_WAIT = old_wait
    publish("master", {"backend/server.py": "OK = 8\n"}, "master 8")
    gate = threading.Event()
    um.start("ADMIN", tx_busy=lambda: False, restart=lambda: gate.wait(10) and None)
    end = time.time() + 20
    while time.time() < end and um.status()["job"]["state"] != "restarting":
        time.sleep(0.05)
    C("while one update runs, a second is refused", um.start("ADMIN", tx_busy=lambda: False, restart=lambda: None), "running")
    gate.set()
    wait(um)
    evs = [e for e, _ in audits]
    C("the log has the starts, the ends and the failures", (evs.count("update_start") >= 8, "update_done" in evs, "update_failed" in evs),
      (True, True, True))
    C("... with who and which branch", {k: audits[0][1].get(k) for k in ("by", "branch")}, {"by": "ADMIN", "branch": "master"})
finally:
    def _rm(fn, p, _e):
        os.chmod(p, stat.S_IWRITE)
        fn(p)
    shutil.rmtree(tmp, onerror=_rm)

print("\nALL PASS" if _ok else "\nSOME FAILED")
raise SystemExit(0 if _ok else 1)
