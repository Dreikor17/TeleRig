#!/usr/bin/env python3
"""Launch the TeleRig server. Usage: python run.py [--host H] [--port P] [--sim-only] [--no-autoconnect]"""
import argparse
import os
import sys
import webbrowser

# make the app importable and paths stable regardless of where we're launched
_HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(_HERE)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import uvicorn


def main() -> None:
    ap = argparse.ArgumentParser(description="TeleRig server")
    ap.add_argument("--host", default="0.0.0.0",
                    help="bind address (default 0.0.0.0 = all interfaces: LAN, VPN, etc.)")
    ap.add_argument("--port", type=int, default=8700)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--reload", action="store_true")
    ap.add_argument("--ssl-certfile", default=os.environ.get("TELERIG_SSL_CERT") or os.environ.get("RADIO_WEBOP_SSL_CERT"),
                    help="TLS certificate (PEM) — serve HTTPS directly on --port "
                         "(also reads TELERIG_SSL_CERT, or the older RADIO_WEBOP_SSL_CERT). Pair with --ssl-keyfile.")
    ap.add_argument("--ssl-keyfile", default=os.environ.get("TELERIG_SSL_KEY") or os.environ.get("RADIO_WEBOP_SSL_KEY"),
                    help="TLS private key (PEM) for --ssl-certfile (also reads TELERIG_SSL_KEY, or the older "
                         "RADIO_WEBOP_SSL_KEY).")
    ap.add_argument("--sim-only", action="store_true",
                    help="development/test guard: /api/connect accepts only the built-in simulator and "
                         "refuses serial (COM) and LAN radios (also TELERIG_SIM_ONLY=1)")
    ap.add_argument("--no-autoconnect", action="store_true",
                    help="never connect a radio automatically when a page loads; the server refuses "
                         "auto connects (also TELERIG_NO_AUTOCONNECT=1)")
    args = ap.parse_args()

    # Dev guards travel in the environment and are set BEFORE uvicorn.run, so the server (and any
    # --reload child process) inherits them. backend/server.py reads them per request.
    if args.sim_only:
        os.environ["TELERIG_SIM_ONLY"] = "1"
    if args.no_autoconnect:
        os.environ["TELERIG_NO_AUTOCONNECT"] = "1"

    # Serve HTTPS directly only if BOTH cert and key are given; otherwise plain HTTP.
    ssl_kw = {}
    if args.ssl_certfile and args.ssl_keyfile:
        ssl_kw = {"ssl_certfile": args.ssl_certfile, "ssl_keyfile": args.ssl_keyfile}
    elif args.ssl_certfile or args.ssl_keyfile:
        print("  NOTE: --ssl-certfile and --ssl-keyfile must be given together; serving plain HTTP.")

    # Resolve the config folder (one-time ~/.radio-webop -> ~/.telerig copy) and check the stores
    # before serving. A corrupt store never stops the start: the server comes up LOCKED (503 with
    # the fix on every page), so the user sees what to do instead of a traceback.
    from backend import accounts
    from backend.store import StoreCorrupt
    config_dir, locked = None, None
    try:
        config_dir = accounts.ensure_initialized()
    except StoreCorrupt as e:
        locked = e
    except Exception as e:                      # e.g. the config folder is unreadable
        locked = e

    scheme = "https" if ssl_kw else "http"
    local = f"{scheme}://localhost:{args.port}"
    if not args.no_browser and not args.reload:
        try:
            webbrowser.open(local)
        except Exception:
            pass
    print(f"TeleRig -> {local}")
    if config_dir is not None:
        _say(f"  Config: {config_dir}")
    if locked is not None:
        cmd = "run.bat --reset-admin" if sys.platform == "win32" else "python3 run.py --reset-admin"
        where = getattr(locked, "path", None) or "the config folder"
        _say(f"  LOCKED: {where} cannot be read ({getattr(locked, 'detail', '') or locked}).")
        _say(f"  Nothing was changed. Fix that file or move it aside (or run {cmd}); the server")
        _say("  starts locked (every page shows this) and unlocks by itself once the file is readable.")
    if args.host in ("0.0.0.0", "::"):
        print(f"  Also reachable on all interfaces (LAN / VPN / port-forward) at port {args.port}"
              f" over {scheme}.")
        print("  WARNING: no login — anyone who can reach this port can control the radio (incl. TX).")
        print("  Secure remote tip: bind --host 127.0.0.1 and front it with 'tailscale serve --bg "
              f"{args.port}' for tailnet-only HTTPS (mic/TX needs HTTPS). See docs/REMOTE-ACCESS.md.")
    if _env_flag("TELERIG_SIM_ONLY"):
        print("  SIM-ONLY: serial (COM) and LAN radios are refused; only the simulator can connect.")
    if _env_flag("TELERIG_NO_AUTOCONNECT"):
        print("  Auto-connect is OFF: no radio is connected automatically when a page loads.")
    # proxy_headers=False: scope["client"] is always the real TCP peer. Forwarding headers are
    # interpreted by the app itself, and only when that peer is loopback (never by uvicorn).
    uvicorn.run("backend.server:app", host=args.host, port=args.port,
                reload=args.reload, log_level="info",
                ws_ping_interval=10, ws_ping_timeout=10, ws_max_size=1048576,
                proxy_headers=False, **ssl_kw)


def _say(msg: str) -> None:
    """A banner line that never crashes the start (a console that cannot encode a path)."""
    try:
        print(msg, flush=True)
    except Exception:
        print(msg.encode("ascii", "replace").decode("ascii"), flush=True)


def _env_flag(name: str) -> bool:
    """Same rule as backend/server.py: any value except empty/0/false/no/off counts as ON."""
    v = os.environ.get(name)
    return v is not None and v.strip().lower() not in ("", "0", "false", "no", "off")


if __name__ == "__main__":
    main()
