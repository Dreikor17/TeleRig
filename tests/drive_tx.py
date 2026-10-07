#!/usr/bin/env python3
"""Sim-only TX driver: the ONLY way TX state is produced for manual UI observation (spec §7.0).

    .venv\\Scripts\\python.exe tests\\drive_tx.py --port 8790 --user OP1 [--seconds 10]
                                                [--cw TEXT [--wpm 20] [--cw-mode]]
                                                [--password-stdin] [--host 127.0.0.1]

Signs in as --user, opens the WebSocket and keys PTT (or, with --cw, sends one cw_tx message) for
--seconds with heartbeats, then releases. It never touches a real radio:

  * ports 8700 and 8722 (the user's real TeleRig instances) are refused outright;
  * the password is read with getpass (never echoed), or with --password-stdin as ONE line from
    stdin (for callers without a terminal). It is never printed, logged or put on a command line;
  * GET /api/status must report sim_only:true and transport_kind "sim" before the first TX command
    and AGAIN before every TX command (harness.assert_sim; the WS wrapper also re-checks before each
    TX-capable send). Anything else aborts with "REFUSING: non-simulator transport" (exit 2);
  * heartbeats ({"action":"tx_hb"}, which never touches the radio) start the moment the claim is
    sent, every 2 s, as the §2.13 contract requires;
  * Ctrl-C, an error or a STOP from another user ends the run; the release is always attempted, and
    closing the socket unkeys server-side anyway (the owner's drop releases its claims).

Exit codes: 0 done, 1 failure (login, busy, denied, not connected, ...), 2 REFUSING (interlock).
"""
from __future__ import annotations

import argparse
import getpass
import json
import sys
import time
from pathlib import Path

TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

import harness  # noqa: E402  (stdlib + websockets only; never imports the backend)

CW_MODES = {"civ17": "CW", "line": "CW-USB"}   # profile cw_send -> the radio's CW mode name


def log(msg: str) -> None:
    print(f"drive_tx: {msg}", flush=True)


def read_password(from_stdin: bool) -> str:
    if from_stdin:
        line = sys.stdin.readline()
        pw = line[:-1] if line.endswith("\n") else line
        if pw.endswith("\r"):
            pw = pw[:-1]
        return pw
    return getpass.getpass("Password (not echoed): ")


class Run:
    def __init__(self, args):
        self.args = args
        self.client = harness.Client(args.port, host=args.host)
        self.ws = None
        self.hb = None
        self.conn_id = None
        self.claim = None              # "ptt" | "cw" once claimed

    # -- helpers ---------------------------------------------------------------------------------
    def sim_check(self) -> dict:
        """The interlock, before EVERY TX command (harness.WS re-checks on each TX send as well)."""
        return harness.assert_sim(self.client)

    def owner(self, frame) -> dict | None:
        return frame.get("owner") if isinstance(frame, dict) else None

    def wait_answer(self, seq: int, timeout: float = 5.0) -> dict:
        return self.ws.wait_for(lambda m: m.get("type") in ("tx", "busy", "denied", "error")
                                and ((m.get("req") or {}).get("seq") == seq or m.get("type") == "error"),
                                timeout=timeout)

    # -- steps -------------------------------------------------------------------------------------
    def login(self, password: str) -> None:
        r = harness.login(self.client, self.args.user, password)
        if r.status != 200:
            j = r.json() or {}
            raise SystemExit(self.fail(f"login as {self.args.user} failed: HTTP {r.status} {j.get('error')}"))
        if (r.json() or {}).get("must_change"):
            raise SystemExit(self.fail(f"{self.args.user} must change the password first (sign in once in a browser)"))
        log(f"signed in as {self.args.user.upper()}")

    def open(self) -> None:
        st = self.sim_check()                       # sim_only + a connected simulator, or REFUSING
        if not st.get("connected"):
            raise SystemExit(self.fail("the simulator is not connected; connect it first"))
        self.ws = self.client.ws()
        hello = self.ws.recv_json(type="hello", timeout=5)
        self.conn_id = hello.get("conn")
        if hello.get("sim_only") is not True:
            raise harness.Refusing("REFUSING: non-simulator transport (hello.sim_only is not true)")
        owner = (hello.get("tx") or {}).get("owner")
        if owner:
            raise SystemExit(self.fail(f"TX is busy: {owner.get('user')} owns it"))
        log(f"websocket open (conn {self.conn_id}, role {hello.get('role')}, radio {st.get('radio')})")

    def set_cw_mode(self) -> None:
        """Not a TX command: switches the mode so the cw_tx checks (mode must be CW) can pass."""
        radio = (self.client.get("/api/status").json() or {}).get("radio")
        prof = next((p for p in (self.client.get("/api/radios").json() or {}).get("radios", [])
                     if p.get("id") == radio), None)
        if not prof or not prof.get("has_cw_tx"):
            raise SystemExit(self.fail(f"{radio} cannot send a CW message"))
        mode = CW_MODES[_cw_send(prof)]
        self.ws.send({"action": "set_mode", "mode": mode})
        self.ws.wait_for(lambda m: m.get("type") == "state" and m.get("mode_name") == mode, timeout=5)
        log(f"mode set to {mode}")

    def key(self) -> None:
        a = self.args
        seq = 1
        if a.cw is not None:
            cmd = {"action": "cw_tx", "text": a.cw, "wpm": a.wpm, "seq": seq}
            kind = "cw"
        else:
            cmd = {"action": "ptt", "tx": True, "seq": seq}
            kind = "ptt"
        self.sim_check()                            # before every TX command
        self.ws.send(cmd)                           # harness.WS: assert_sim again, then send
        self.hb = harness.Heartbeat(self.ws, interval=2.0)   # from the moment the claim is sent
        ans = self.wait_answer(seq)
        own = self.owner(ans)
        if ans.get("type") != "tx" or not own or own.get("conn") != self.conn_id:
            raise SystemExit(self.fail(f"{kind} claim refused: {json.dumps(ans)}"))
        self.claim = kind
        log(f"{kind} claimed: owner {own.get('user')} claims {own.get('claims')}")

    def hold(self) -> str:
        """Drain frames for --seconds; stop early when the claim ends (STOP, timeout, CW done)."""
        end = time.monotonic() + self.args.seconds
        last = 0.0
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return "time"
            try:
                m = self.ws.recv_json(timeout=min(left, 0.5))
            except TimeoutError:
                m = None
            if m and m.get("type") == "tx_revoked":
                log(f"TX revoked: reason {m.get('reason')} by {m.get('by')}")
                self.claim = None
                return "revoked"
            if m and m.get("type") == "tx":
                own = m.get("owner")
                if not own or own.get("conn") != self.conn_id:
                    log("the claim ended (" + ("CW message done" if self.claim == "cw" else "owner changed") + ")")
                    self.claim = None
                    return "ended"
            now = time.monotonic()
            if now - last >= 1.0:
                last = now
                log(f"{self.claim} keyed, {self.args.seconds - left:4.1f} s")

    def release(self) -> None:
        if not self.claim or self.ws is None:
            return
        self.sim_check()                            # before every TX command
        if self.claim == "cw":
            self.ws.send({"action": "cw_stop"})
        else:
            self.ws.send({"action": "ptt", "tx": False})
        try:
            self.ws.wait_for(lambda m: m.get("type") == "tx" and not m.get("owner"), timeout=5)
            log("released: TX free")
        except TimeoutError:
            log("no idle tx frame within 5 s after the release")
        self.claim = None

    def emergency_release(self) -> None:
        """On an error path: send the unkey on the raw socket (it can only unkey), then close."""
        if self.ws is None:
            return
        if self.claim:
            frame = {"action": "cw_stop"} if self.claim == "cw" else {"action": "ptt", "tx": False}
            try:
                self.ws._c.send(json.dumps(frame))
            except Exception:
                pass

    def close(self) -> None:
        if self.hb:
            self.hb.stop()
            self.hb = None
        if self.ws is not None:
            self.ws.close()

    @staticmethod
    def fail(msg: str) -> int:
        log(msg)
        return 1


def _cw_send(prof: dict) -> str:
    """has_cw_tx says yes/no; the CW mode name depends on the protocol (Icom CI-V vs Yaesu CAT)."""
    return "line" if prof.get("protocol") == "yaesu" else "civ17"


def main() -> int:
    ap = argparse.ArgumentParser(description="Sim-only TX driver for manual UI observation (TeleRig Phase 1).")
    ap.add_argument("--port", type=int, required=True, help="the preview server's port (never 8700/8722)")
    ap.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1", "localhost"))
    ap.add_argument("--user", required=True, help="an operator or admin account on that server")
    ap.add_argument("--seconds", type=float, default=10.0, help="how long to hold the claim (1-110 s)")
    ap.add_argument("--cw", default=None, metavar="TEXT", help="send this CW message instead of keying PTT")
    ap.add_argument("--wpm", type=int, default=20)
    ap.add_argument("--cw-mode", action="store_true", help="with --cw: switch the radio to its CW mode first")
    ap.add_argument("--password-stdin", action="store_true",
                    help="read the password as one line from stdin (no terminal needed); never echoed or logged")
    a = ap.parse_args()
    try:
        harness.check_port(a.port)                  # 8700 / 8722 -> Refusing
    except harness.Refusing as e:
        print(str(e), flush=True)
        return 2
    if not 1.0 <= a.seconds <= 110.0:
        log("--seconds must be between 1 and 110 (the 120 s TX failsafe is never approached)")
        return 1
    if a.cw is not None and not a.cw.strip():
        log("--cw needs a non-empty message")
        return 1
    password = read_password(a.password_stdin)
    if not password:
        log("no password given")
        return 1
    run = Run(a)
    try:
        run.login(password)
        password = None                             # not kept around
        run.open()
        if a.cw is not None and a.cw_mode:
            run.set_cw_mode()
        run.key()
        why = run.hold()
        if why == "time":
            run.release()
        return 0
    except harness.Refusing as e:
        print(str(e), flush=True)
        run.emergency_release()
        return 2
    except SystemExit as e:
        run.emergency_release()
        return int(e.code or 0)
    except KeyboardInterrupt:
        log("interrupted: releasing")
        run.emergency_release()
        return 1
    except Exception as e:
        log(f"error: {e!r}")
        run.emergency_release()
        return 1
    finally:
        run.close()


if __name__ == "__main__":
    sys.exit(main())
