"""Update TeleRig from GitHub from the web page (Administration > Update).

A copy installed with git (install.bat clones Dreikor17/TeleRig) follows ONE branch: `master`, the
releases (also accepted under the name `main`), or `dev`. The updater fetches THAT branch from
`origin` and moves the install forward to it, so a dev install updates from dev and a master install
from master. It never switches branches, never merges and never touches local changes:

  1. fetch     git fetch origin <branch>
  2. check     nothing new: done. Commits here that GitHub's branch does not have (diverged), or
               changes to tracked files: refused, update by hand.
  3. update    git merge --ff-only <the fetched commit>
  4. packages  pip install -r requirements.txt, only when requirements.txt changed
  5. self-test the new code must import (python -c "import backend, backend.server"), the same check
               install.bat makes. If it does not, the install goes back to the commit it had
               (git reset --hard <old>; the tree was clean, so nothing of anyone's is lost) and
               nothing restarts.
  6. restart   once nobody transmits (waits up to RESTART_WAIT s, then restarts anyway: the shutdown
               releases a transmission, as every shutdown does), the server starts a new copy of
               itself with the same command line, which waits until this one has exited, and shuts
               down the normal way (server.py _restart_self, run.py).

The steps run on a thread (git and pip block); status() is what the page shows. Not offered: a copy
without git (a zip download), another branch, no git on the PC.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

BRANCHES = ("master", "main", "dev")
FETCH_TIMEOUT, GIT_TIMEOUT, PIP_TIMEOUT, TEST_TIMEOUT = 120, 30, 900, 120
RESTART_WAIT = 60.0                   # seconds to wait for a transmission to end before restarting
RESTARTING_STALE = 120.0              # a restart that has not happened after this long blocks nothing
COMMITS_MAX = 30                      # commits listed by a check
OUT_MAX = 2000                        # characters of a failed step's output kept for the page


def _noop(*a, **k) -> None:
    return None


def _short(text: str, n: int = OUT_MAX) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else "…" + text[-n:]


def version_label(version: str, info: Optional[dict]) -> str:
    """The version the pages show, from Updater.info() of the copy that runs. A dev install (a git
    checkout on the dev branch) is not the release it will become, so it shows "-dev" and its commit
    after the number: "0.3.0-dev+0205284" (semver build metadata). A master install, another branch,
    a zip copy or no git: the release number alone. Version checks keep using the number alone."""
    info = info or {}
    if info.get("branch") != "dev":
        return version
    short = str(info.get("short") or "").strip()
    return f"{version}-dev+{short}" if short else f"{version}-dev"


class Updater:
    """`runner(args, timeout) -> (exit code, output)` runs a command in the repo (tests replace it);
    `git` is the git executable (None: not installed); `python` the interpreter that runs TeleRig."""

    def __init__(self, repo: Any, *, python: Optional[str] = None, git: Any = "auto",
                 runner: Optional[Callable] = None, log: Callable[[str], None] = _noop,
                 audit: Callable[..., None] = _noop):
        self.repo = Path(repo)
        self.python = python or sys.executable
        if git == "auto":
            from shutil import which
            git = which("git")
        self.git_exe = git
        self.run = runner or self._subprocess
        self.log = log
        self.audit = audit
        self._mu = threading.Lock()
        self.job: dict = {"state": "idle"}
        self.last_check: Optional[dict] = None
        self.thread: Optional[threading.Thread] = None

    # -- commands ------------------------------------------------------------------------------------
    def _subprocess(self, args: list, timeout: float) -> tuple:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
        try:
            r = subprocess.run(args, cwd=str(self.repo), capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=timeout, env=env, stdin=subprocess.DEVNULL,
                               creationflags=flags)
            return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()
        except subprocess.TimeoutExpired:
            return -1, f"no answer within {int(timeout)} s"
        except OSError as e:
            return -2, str(e)

    def git(self, *args: str, timeout: float = GIT_TIMEOUT) -> tuple:
        return self.run([self.git_exe, "-C", str(self.repo), *args], timeout)

    # -- what this install is ---------------------------------------------------------------------------
    def info(self) -> dict:
        out = {"git": False, "branch": None, "head": None, "short": None, "subject": None, "date": None,
               "origin": None, "updatable": False, "reason": None}
        if not self.git_exe:
            out["reason"] = ("git is not installed on this PC, so TeleRig cannot update itself. Install Git "
                             "(winget install --id Git.Git -e), or update by downloading the new version.")
            return out
        if not (self.repo / ".git").exists():
            out["reason"] = ("This copy of TeleRig was not installed with git (a zip download), so it cannot "
                             "update itself. Download the new version from GitHub, or install with git.")
            return out
        code, br = self.git("rev-parse", "--abbrev-ref", "HEAD")
        if code != 0:
            out["reason"] = "git could not read this install: " + _short(br, 300)
            return out
        out["git"] = True
        out["branch"] = br.strip()
        code, line = self.git("log", "-1", "--format=%H%x09%cI%x09%s")
        if code == 0 and line.count("\t") >= 2:
            h, d, s = line.split("\t", 2)
            out.update(head=h.strip(), short=h.strip()[:7], date=d.strip(), subject=s.strip())
        code, url = self.git("remote", "get-url", "origin")
        out["origin"] = url.strip() if code == 0 else None
        if out["branch"] == "HEAD":
            out["reason"] = "This install is not on a branch (a detached checkout). Update it by hand."
        elif out["branch"] not in BRANCHES:
            out["reason"] = (f"This install is on the branch {out['branch']!r}. The update button follows master "
                             "or dev only; update this one by hand.")
        elif not out["origin"]:
            out["reason"] = "This install has no 'origin' remote to update from."
        else:
            out["updatable"] = True
        return out

    # -- a check (fetch + compare) -----------------------------------------------------------------------
    def check(self) -> dict:
        """Fetch this install's branch and compare. Never changes the install."""
        inf = self.info()
        if not inf["updatable"]:
            return {"ok": False, "error": inf["reason"]}
        b = inf["branch"]
        code, out = self.git("fetch", "--quiet", "origin", b, timeout=FETCH_TIMEOUT)
        if code != 0:
            return {"ok": False, "error": "Could not reach GitHub (git fetch origin " + b + "): " + _short(out, 400)}
        code, target = self.git("rev-parse", "FETCH_HEAD")
        if code != 0:
            return {"ok": False, "error": "git did not report what it fetched: " + _short(target, 300)}
        target = target.strip()
        behind = self._count(f"HEAD..{target}")
        ahead = self._count(f"{target}..HEAD")
        commits = []
        if behind:
            code, log = self.git("log", f"--max-count={COMMITS_MAX}", "--format=%h%x09%cI%x09%s", f"HEAD..{target}")
            for ln in (log.splitlines() if code == 0 else []):
                parts = ln.split("\t", 2)
                if len(parts) == 3:
                    commits.append({"short": parts[0], "date": parts[1], "subject": parts[2]})
        res = {"ok": True, "at": time.time(), "branch": b, "target": target, "target_short": target[:7],
               "behind": behind, "ahead": ahead, "commits": commits}
        self.last_check = res
        return res

    def _count(self, rng: str) -> int:
        code, n = self.git("rev-list", "--count", rng)
        try:
            return int(n.strip()) if code == 0 else 0
        except ValueError:
            return 0

    # -- the update ----------------------------------------------------------------------------------------
    def status(self) -> dict:
        with self._mu:
            return {"job": _copy(self.job), "check": _copy(self.last_check)}

    def start(self, by: str, *, tx_busy: Callable[[], bool], restart: Callable[[], Optional[str]]) -> Optional[str]:
        """Begin an update on a thread. Returns None, or why not: "running", "busy" (someone
        transmits) or the reason the install cannot update itself."""
        with self._mu:
            st = self.job.get("state")
            since = time.time() - float(self.job.get("ended") or self.job.get("started") or 0)
            if st == "running" or (st == "restarting" and since < RESTARTING_STALE):
                return "running"
            inf = self.info()
            if not inf["updatable"]:
                return inf["reason"]
            try:
                if tx_busy():
                    return "busy"
            except Exception:
                return "busy"                        # cannot tell: fail safe
            self.job = {"state": "running", "by": by, "branch": inf["branch"], "from": inf["head"],
                        "from_short": inf["short"], "to": None, "to_short": None, "steps": [], "error": None,
                        "started": time.time(), "ended": None, "restart": None}
            self.thread = threading.Thread(target=self._job, args=(by, tx_busy, restart), daemon=True,
                                           name="telerig-update")
            self.thread.start()
        return None

    def _step(self, name: str, ok: bool, text: str = "") -> None:
        with self._mu:
            self.job["steps"].append({"step": name, "ok": bool(ok), "text": _short(text, 600)})

    def _end(self, state: str, error: Optional[str] = None, **extra) -> None:
        with self._mu:
            self.job.update(state=state, error=error, ended=time.time(), **extra)

    def _job(self, by: str, tx_busy: Callable[[], bool], restart: Callable[[], Optional[str]]) -> None:
        try:
            self._job_steps(by, tx_busy, restart)
        except Exception as e:                       # never leave the job "running"
            self._step("error", False, repr(e))
            self._end("failed", "The update stopped with an error: " + repr(e))
            self.audit("update_failed", by=by, step="error", error=repr(e)[:200])

    def _job_steps(self, by: str, tx_busy: Callable[[], bool], restart: Callable[[], Optional[str]]) -> None:
        b, old = self.job["branch"], self.job["from"]
        self.audit("update_start", by=by, branch=b, commit=old)
        self.log(f"  TeleRig: update from GitHub ({b}) started by {by}.")
        chk = self.check()
        if not chk.get("ok"):
            self._step("fetch", False, chk.get("error") or "")
            return self._fail(by, "fetch", chk.get("error") or "The check failed.")
        self._step("fetch", True, f"origin/{b} is at {chk['target_short']}")
        if chk["ahead"]:
            msg = (f"This install has {chk['ahead']} commit(s) that GitHub's {b} does not have, so it cannot "
                   "simply move forward. Update it by hand.")
            self._step("check", False, msg)
            return self._fail(by, "check", msg)
        if not chk["behind"]:
            self._step("check", True, "Already up to date.")
            self._end("current", to=old, to_short=(old or "")[:7])
            self.audit("update_done", by=by, branch=b, commit=old, changed=False)
            return None
        code, dirty = self.git("status", "--porcelain", "--untracked-files=no")
        if code != 0 or dirty.strip():
            msg = ("This install has local changes to TeleRig's own files, so it is not updated automatically: "
                   + _short(dirty, 300)) if code == 0 else "git could not check for local changes: " + _short(dirty, 300)
            self._step("check", False, msg)
            return self._fail(by, "check", msg)
        self._step("check", True, f"{chk['behind']} new commit(s) on GitHub's {b}.")
        target = chk["target"]
        code, out = self.git("merge", "--ff-only", target, timeout=GIT_TIMEOUT * 2)
        if code != 0:
            self._step("update", False, out)
            return self._fail(by, "update", "git could not move the install forward: " + _short(out, 400))
        self._step("update", True, f"{(old or '')[:7]} -> {target[:7]}")
        code, changed = self.git("diff", "--name-only", old, target, "--", "requirements.txt")
        if code == 0 and changed.strip():
            code, out = self.run([self.python, "-m", "pip", "install", "-r", "requirements.txt",
                                  "--disable-pip-version-check"], PIP_TIMEOUT)
            self._step("packages", code == 0, out if code != 0 else "requirements.txt changed: packages installed")
            if code != 0:
                return self._rollback(by, old, "packages", "Installing the new packages failed: " + _short(out, 400))
        else:
            self._step("packages", True, "requirements.txt unchanged: nothing to install")
        code, out = self.run([self.python, "-c", "import backend, backend.server; print(backend.__version__)"],
                             TEST_TIMEOUT)
        if code != 0:
            self._step("self-test", False, out)
            return self._rollback(by, old, "self-test", "The new version failed its self-test (it does not start): "
                                  + _short(out, 400))
        self._step("self-test", True, "the new version imports: " + _short(out, 60))
        with self._mu:
            self.job.update(to=target, to_short=target[:7], state="restarting")
            self.last_check = dict(chk, behind=0, ahead=0, commits=[], at=time.time())   # the install is there now
        self.audit("update_done", by=by, branch=b, commit=target, previous=old, changed=True)
        self.log(f"  TeleRig: updated {(old or '')[:7]} -> {target[:7]} ({b}); restarting.")
        waited = 0.0
        while waited < RESTART_WAIT:
            try:
                if not tx_busy():
                    break
            except Exception:
                break
            time.sleep(0.5)
            waited += 0.5
        self._step("restart", True, f"restarting (waited {int(waited)} s for a transmission to end)" if waited
                   else "restarting now")
        try:
            how = restart()
        except Exception as e:
            self._step("restart", False, repr(e))
            return self._end("updated", "Updated, but the restart failed: " + repr(e)
                             + ". Restart TeleRig on its PC to run the new version.", restart="failed")
        if how == "skipped":
            return self._end("updated", None, restart="skipped")
        return self._end("restarting", None, restart="started")

    def _fail(self, by: str, step: str, msg: str) -> None:
        self._end("failed", msg)
        self.audit("update_failed", by=by, step=step, error=msg[:200])
        self.log(f"  TeleRig: update failed at {step}: {msg}")

    def _rollback(self, by: str, old: Optional[str], step: str, msg: str) -> None:
        code, out = self.git("reset", "--hard", old) if old else (1, "no previous commit")
        ok = code == 0
        self._step("rollback", ok, f"back to {(old or '')[:7]}" if ok else out)
        self._fail(by, step, msg + (f" The install is back on {(old or '')[:7]}; nothing restarted." if ok else
                                    " Going back to the previous version failed too: run install.bat on the PC."))


def _copy(d: Any) -> Any:
    if d is None:
        return None
    out = dict(d)
    if isinstance(out.get("steps"), list):
        out["steps"] = [dict(s) for s in out["steps"]]
    if isinstance(out.get("commits"), list):
        out["commits"] = [dict(c) for c in out["commits"]]
    return out
