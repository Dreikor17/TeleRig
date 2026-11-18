#!/usr/bin/env python3
"""TeleRig Phase 1 test runner (stdlib only).

    .venv\\Scripts\\python.exe tests\\run_phase1.py [--only GROUP[,GROUP...]] [-k TEXT] [--list]
                                                  [--tmp DIR] [--keep] [-v]

Groups: unit, smoke, store, auth, ws, tx, admin, layout, bridge. Tests live in tests/t_<anything>.py and
register with @harness.test("<group>"); `unit` also runs every backend/*_test.py as a module
(python -m backend.<name>) with a scratch environment.

Scratch folders go under --tmp, else $TELERIG_TEST_TMP, else the system temp folder; they are
deleted after a fully green run unless --keep. Servers always run --sim-only --no-autoconnect on
free ports (never 8700/8722).

Exit codes: 0 all passed, 1 failures, 2 REFUSING (a safety interlock aborted the run),
3 usage error or nothing selected, 130 interrupted.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

import harness  # noqa: E402


class Ctx:
    """Per-test context: scratch folders, servers and cleanups, all released after the test."""

    def __init__(self, base: Path, name: str, verbose: bool):
        self.base = base
        self.name = name
        self.verbose = verbose
        self._n = 0
        self._servers: list = []
        self._cleanups: list = []

    def tmp(self, label: str = "") -> Path:
        self._n += 1
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in f"{self.name[:28]}-{self._n}{'-' + label if label else ''}")
        d = self.base / safe
        harness.check_config_path(d / "config.json")
        d.mkdir(parents=True, exist_ok=False)
        return d

    def server(self, tmp: Path | None = None, **kw) -> harness.Server:
        srv = harness.Server(tmp if tmp is not None else self.tmp("srv"), **kw)
        self._servers.append(srv)
        srv.start()
        self.log(f"server up on :{srv.port} (/login -> {srv.ready_status})")
        return srv

    def cleanup(self, fn) -> None:
        self._cleanups.append(fn)

    def log(self, msg: str) -> None:
        if self.verbose:
            print(f"      {msg}")

    def close(self) -> list:
        logs = []
        for fn in reversed(self._cleanups):
            try:
                fn()
            except Exception:
                pass
        for srv in self._servers:
            try:
                if srv.log_path:
                    logs.append((srv.port, srv.log_tail()))
                srv.stop()
            except Exception:
                pass
        return logs


def _discover() -> None:
    for path in sorted(TESTS.glob("t_*.py")):
        spec = importlib.util.spec_from_file_location(f"phase1_{path.stem}", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)


def main() -> int:
    ap = argparse.ArgumentParser(description="TeleRig Phase 1 tests")
    ap.add_argument("--only", default="", help="comma-separated groups: " + ", ".join(harness.KNOWN_GROUPS))
    ap.add_argument("-k", default="", help="run only tests whose name contains this text")
    ap.add_argument("--list", action="store_true", help="list the selected tests and exit")
    ap.add_argument("--tmp", default=os.environ.get("TELERIG_TEST_TMP") or tempfile.gettempdir(),
                    help="base folder for scratch configs (default: $TELERIG_TEST_TMP or the system temp)")
    ap.add_argument("--keep", action="store_true", help="keep scratch folders even when everything passes")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    groups = [g.strip() for g in args.only.split(",") if g.strip()]
    bad = [g for g in groups if g not in harness.KNOWN_GROUPS]
    if bad:
        print(f"unknown group(s): {', '.join(bad)} (known: {', '.join(harness.KNOWN_GROUPS)})")
        return 3

    _discover()
    selected = [(g, n, fn) for (g, n, fn) in harness.REGISTRY
                if (not groups or g in groups) and (not args.k or args.k in n)]
    selected.sort(key=lambda t: harness.KNOWN_GROUPS.index(t[0]))   # stable: unit first, then smoke, ...
    if args.list:
        for g, n, _ in selected:
            print(f"{g:7} {n}")
        return 0
    if not selected:
        have = sorted({g for g, _, _ in harness.REGISTRY})
        print(f"no tests selected (groups with tests so far: {', '.join(have) or 'none'})")
        return 3

    try:
        base_root = Path(args.tmp)
        harness.check_config_path(base_root / "x" / "config.json")
        base_root.mkdir(parents=True, exist_ok=True)
        base = Path(tempfile.mkdtemp(prefix="tr-", dir=str(base_root)))
    except harness.Refusing as e:
        print(str(e))
        return 2

    print(f"TeleRig Phase 1 tests | python {sys.executable} | scratch {base}")
    passed, failed = [], []
    t_run = time.monotonic()
    current = None
    try:
        for i, (group, name, fn) in enumerate(selected):
            label = f"{group}/{name}"
            ctx = current = Ctx(base, f"{i:03d}-{name}", args.verbose)
            t0 = time.monotonic()
            ok, err = False, None
            try:
                fn(ctx)                              # Refusing / KeyboardInterrupt propagate
                ok = True
            except Exception:
                err = traceback.format_exc()
            logs = ctx.close()
            current = None
            dt = time.monotonic() - t0
            if ok:
                passed.append(label)
                print(f"PASS  {label}  ({dt:.1f}s)")
            else:
                failed.append(label)
                print(f"FAIL  {label}  ({dt:.1f}s)")
                print("      " + err.rstrip().replace("\n", "\n      "))
                for port, tail in logs:
                    if tail:
                        print(f"      --- server :{port} log (tail) ---")
                        print("      " + tail.replace("\n", "\n      "))
    except harness.Refusing as e:
        if current is not None:
            current.close()
        print(f"\n{e}\nRun aborted by a safety interlock. Scratch kept at {base}")
        return 2
    except KeyboardInterrupt:
        if current is not None:
            current.close()
        print(f"\ninterrupted. Scratch kept at {base}")
        return 130

    total = time.monotonic() - t_run
    print(f"\n{len(passed)} passed, {len(failed)} failed in {total:.1f}s")
    if failed:
        print("failed: " + ", ".join(failed))
        print(f"scratch kept at {base}")
        return 1
    if args.keep:
        print(f"scratch kept at {base}")
    else:
        shutil.rmtree(base, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
