#!/usr/bin/env python3
"""Id audit for frontend/index.html (spec §3.3 step 10): moving the console into tiles must not lose
or duplicate any element id the scripts look up.

    .venv\\Scripts\\python.exe tests\\check_ids.py --allow-missing rxDev,micDev
          [--base-ref dev] [--base FILE] [--file frontend/index.html]

Every id of the BASE page (default: `git show dev:frontend/index.html`) must exist exactly once in
--file, except the ids named in --allow-missing (the per-step list: B1/B3 `rxDev,micDev`; B4 adds
`remoteBtn,remoteModal,remoteClose,remoteBody`). No id may appear twice in --file at all.
Standard library only. Exit 0 = pass, 1 = a problem (each is printed), 3 = usage / git error.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


class _Ids(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ids: list = []

    def handle_starttag(self, tag, attrs):
        for k, v in attrs:
            if k == "id" and v:
                self.ids.append(v)

    handle_startendtag = handle_starttag


def ids_of(html: str) -> list:
    p = _Ids()
    p.feed(html)
    p.close()
    return p.ids


def audit(base_html: str, html: str, allow_missing=()) -> list:
    """Problems (empty = pass)."""
    allow = {a for a in allow_missing if a}
    base = ids_of(base_html)
    now = Counter(ids_of(html))
    problems = [f"duplicate id: {i} (x{n})" for i, n in sorted(now.items()) if n > 1]
    for i in sorted(set(base)):
        if i in allow:
            continue
        if now.get(i, 0) == 0:
            problems.append(f"missing id: {i}")
    for i in sorted(allow):                     # a typo in the list must not silently allow nothing
        if i not in base:
            problems.append(f"--allow-missing names an id the base page never had: {i}")
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--allow-missing", default="", help="comma-separated ids that may be gone")
    ap.add_argument("--base-ref", default="dev", help="git ref of the base page (default: dev)")
    ap.add_argument("--base", default=None, help="read the base page from this file instead of git")
    ap.add_argument("--file", default=str(REPO / "frontend" / "index.html"))
    a = ap.parse_args(argv)
    if a.base:
        base_html = Path(a.base).read_text(encoding="utf-8")
    else:
        try:
            r = subprocess.run(["git", "show", f"{a.base_ref}:frontend/index.html"], cwd=str(REPO),
                               capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as e:
            print(f"check_ids: cannot run git: {e}", file=sys.stderr)
            return 3
        if r.returncode != 0:
            print(f"check_ids: git show {a.base_ref}:frontend/index.html failed: {r.stderr.decode('utf-8', 'replace')}",
                  file=sys.stderr)
            return 3
        base_html = r.stdout.decode("utf-8")
    html = Path(a.file).read_text(encoding="utf-8")
    allow = [x.strip() for x in a.allow_missing.split(",") if x.strip()]
    problems = audit(base_html, html, allow)
    for p in problems:
        print("check_ids: " + p)
    if problems:
        return 1
    n = len(set(ids_of(base_html)))
    print(f"check_ids: ok ({n} base ids; allowed missing: {', '.join(allow) or 'none'}; "
          f"{len(ids_of(html))} ids in {Path(a.file).name}, none duplicated)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
