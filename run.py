#!/usr/bin/env python3
"""Launch the TeleRig server.

    python run.py [--host H] [--port P] [--sim-only] [--no-autoconnect] [--no-browser]
    python run.py --reset-admin [--password-stdin] [--real-config] [--demote-other-admins]

`--reset-admin` never starts the server. It runs on a standard-library-only path (no uvicorn or
Starlette import), so it works even when the web dependencies cannot be imported.
"""
import argparse
import os
import sys
import webbrowser

# make the app importable and paths stable regardless of where we're launched
_HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(_HERE)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def recovery_cmd() -> str:
    return "run.bat --reset-admin" if sys.platform == "win32" else "python3 run.py --reset-admin"


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
    ap.add_argument("--reset-admin", action="store_true",
                    help="set a new password for the ADMIN account (and repair a corrupt account store), "
                         "then exit; does not start the server. Works while the server runs.")
    ap.add_argument("--password-stdin", action="store_true",
                    help="with --reset-admin: read the new password as one line from stdin (refused "
                         "unless TELERIG_CONFIG is set or --real-config is given)")
    ap.add_argument("--real-config", action="store_true",
                    help="with --reset-admin --password-stdin: allow the default ~/.telerig config")
    ap.add_argument("--demote-other-admins", action="store_true",
                    help="with --reset-admin: demote every other admin account to operator")
    args = ap.parse_args()

    if (args.password_stdin or args.real_config or args.demote_other_admins) and not args.reset_admin:
        ap.error("--password-stdin, --real-config and --demote-other-admins only go with --reset-admin")
    if args.reset_admin:
        sys.exit(reset_admin_cli(args))          # before uvicorn is ever imported (AR-35)

    # Dev guards travel in the environment and are set BEFORE uvicorn.run, so the server (and any
    # --reload child process) inherits them. backend/server.py reads them per request.
    if args.sim_only:
        os.environ["TELERIG_SIM_ONLY"] = "1"
    if args.no_autoconnect:
        os.environ["TELERIG_NO_AUTOCONNECT"] = "1"
    os.environ["TELERIG_PORT"] = str(args.port)  # internal: the bound port for a --reload child

    # Serve HTTPS directly only if BOTH cert and key are given; otherwise plain HTTP.
    ssl_kw = {}
    if args.ssl_certfile and args.ssl_keyfile:
        ssl_kw = {"ssl_certfile": args.ssl_certfile, "ssl_keyfile": args.ssl_keyfile}
    elif args.ssl_certfile or args.ssl_keyfile:
        print("  NOTE: --ssl-certfile and --ssl-keyfile must be given together; serving plain HTTP.")

    # Resolve the config folder (one-time ~/.radio-webop -> ~/.telerig copy), seed or migrate the
    # account store and check both stores before serving. A corrupt store never stops the start:
    # the server comes up LOCKED (503 with the fix on every page) instead of a traceback.
    from backend import accounts, settings
    from backend.store import StoreCorrupt
    config_dir, locked, info = None, None, None
    try:
        config_dir = accounts.ensure_initialized(port=args.port)
        info = accounts.banner_info()
    except StoreCorrupt as e:
        locked = e
    except Exception as e:                      # e.g. the config folder is unreadable
        locked = e

    scheme = "https" if ssl_kw else "http"
    local = f"{scheme}://localhost:{args.port}"
    code = accounts.format_setup(info["setup_code"]) if info and info["default_pw"] and info["setup_code"] else None
    open_url = f"{local}/login#setup={code}" if code else local
    if not args.no_browser and not args.reload:
        try:
            webbrowser.open(open_url)
        except Exception:
            pass
    _say(f"TeleRig -> {local}")
    if config_dir is not None:
        _say(f"  Config: {config_dir}")
    _say("  Login required for everyone.")
    cmd = recovery_cmd()
    if locked is not None:
        where = getattr(locked, "path", None) or "the config folder"
        _say(f"  LOCKED: {where} cannot be read ({getattr(locked, 'detail', '') or locked}).")
        _say(f"  Nothing was changed. Fix that file or move it aside (or run {cmd}); the server")
        _say("  starts locked (every page shows this) and unlocks by itself once the file is readable.")
    elif info is not None:
        try:
            behind = settings.behind_proxy()
        except Exception:
            behind = False
        if info["default_pw"]:
            _say(f"  First run: sign in on THIS PC as admin / admin with setup code {code or '(none: run ' + cmd + ')'}"
                 f" — the browser opens {local}/login#setup={code} for you. You will choose a new password."
                 " (The default password never works from other devices.)")
            if behind:
                _say("  Remote access (Tailscale Serve) is ON, so the default password cannot be used — "
                     f"run: {cmd}")
        if info["migrated_v1"]:
            _say("  Upgraded install: sign in as admin with your previous remote-access password. "
                 "Admin functions work only on this PC until you change it.")
            if behind:
                _say(f"  Remote access is ON, so this PC has no direct sign-in: run {cmd} once to get admin access.")
        if not info["admins"]:
            _say(f"  WARNING: there is no admin account. Run {cmd} to create one.")
    if args.host in ("0.0.0.0", "::"):
        _say(f"  Also reachable on all interfaces (LAN / VPN / port-forward) at port {args.port} over {scheme}.")
        if not ssl_kw:
            _say("  Note: passwords cross the LAN unencrypted over http:// — prefer Tailscale (https).")
        _say("  Secure remote tip: bind --host 127.0.0.1 and front it with 'tailscale serve --bg "
             f"{args.port}' for tailnet-only HTTPS (mic/TX needs HTTPS). See docs/REMOTE-ACCESS.md.")
    if _env_flag("TELERIG_SIM_ONLY"):
        _say("  SIM-ONLY: serial (COM) and LAN radios are refused; only the simulator can connect.")
    if _env_flag("TELERIG_NO_AUTOCONNECT"):
        _say("  Auto-connect is OFF: no radio is connected automatically when a page loads.")

    import uvicorn                               # only now: --reset-admin never needs it
    # proxy_headers=False: scope["client"] is always the real TCP peer. Forwarding headers are
    # interpreted by the app itself, and only when that peer is loopback (never by uvicorn).
    uvicorn.run("backend.server:app", host=args.host, port=args.port,
                reload=args.reload, log_level="info",
                ws_ping_interval=10, ws_ping_timeout=10, ws_max_size=1048576,
                proxy_headers=False, **ssl_kw)


# -- --reset-admin (stdlib only) -------------------------------------------------------------------
def _ask(prompt: str) -> bool:
    try:
        return input(prompt).strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def _read_password(args) -> str:
    if args.password_stdin:
        line = sys.stdin.readline()
        return line.rstrip("\r\n")
    import getpass
    a = getpass.getpass("New ADMIN password (8-72 bytes): ")
    b = getpass.getpass("Repeat it: ")
    if a != b:
        raise ValueError("The two passwords do not match.")
    return a


def reset_admin_cli(args) -> int:
    """§2.20: exit 0 on success, 1 on refusal or failure. Host-only by construction."""
    env_set = bool(os.environ.get("TELERIG_CONFIG") or os.environ.get("RADIO_WEBOP_CONFIG"))
    if args.password_stdin and not env_set and not args.real_config:
        _say("TeleRig --reset-admin: refusing --password-stdin without TELERIG_CONFIG or --real-config "
             "(it would change the real ~/.telerig).")
        return 1
    from backend import accounts, audit, auth, settings
    from backend.store import StoreBusy, StoreCorrupt
    interactive = not args.password_stdin
    try:
        d = auth.config_dir(migrate=True)
    except Exception as e:
        _say(f"TeleRig --reset-admin: cannot use the config folder: {e}")
        return 1
    _say(f"Using {d}")
    if interactive and not _ask(f"Reset ADMIN in {d}? [y/N] "):
        _say("Nothing changed.")
        return 1

    # 1. repairs
    move_corrupt = False
    if accounts.status() == "corrupt":
        st = accounts.store()
        _say(f"  {st.path} is corrupt ({st.detail()}). Everyone is locked out until it is repaired.")
        if args.password_stdin or _ask("  Move it aside and create a fresh store? [y/N] "):
            move_corrupt = True
        else:
            _say("Nothing changed.")
            return 1
    if settings.status() == "corrupt":
        st = settings.store()
        _say(f"  {st.path} cannot be parsed ({st.detail()}); TeleRig stays locked until it is fixed.")
        if interactive and _ask("  Move it aside as config.json.corrupt-<UTC> (TeleRig then starts with "
                                "default settings; config.v1.bak.json keeps the v1 copy)? [y/N] "):
            try:
                settings.store().mutate(lambda x: x, create=True, move_corrupt=True)
                _say(f"  Moved it to {settings.store().last_backup}.")
            except Exception as e:
                _say(f"  Could not move it: {e}")
        elif not interactive:
            _say("  (--password-stdin never moves config.json; fix or move it by hand.)")

    # 2. the password
    try:
        pw = _read_password(args)
    except ValueError as e:
        _say(f"TeleRig --reset-admin: {e}")
        return 1
    prob = accounts.password_problem(pw, user=accounts.ADMIN)
    if prob:
        _say(f"TeleRig --reset-admin: {prob}")
        return 1

    # 3. other admins
    demote = ()
    if not move_corrupt:
        try:
            others = accounts.other_admins()
        except StoreCorrupt:
            others = []
        if others:
            _say(f"  Other admin accounts: {', '.join(others)}")
            if args.demote_other_admins:
                demote = tuple(others)
            elif interactive:
                demote = tuple(n for n in others if _ask(f"  Demote {n} to operator? [y/N] "))
    try:
        res = accounts.reset_admin(pw, demote=demote, move_corrupt=move_corrupt)
    except StoreBusy:
        _say("TeleRig --reset-admin: the account store is locked by another process; try again.")
        return 1
    except Exception as e:
        _say(f"TeleRig --reset-admin: failed: {e}")
        return 1
    if res.get("backup"):
        _say(f"  The corrupt store was moved to {res['backup']}; a fresh one was created.")
    for n in res.get("demoted", []):
        _say(f"  Demoted {n} to operator.")
        audit.log("role_changed", user=n, **{"from": "admin", "to": "operator", "by": "reset-admin"})
    audit.log("reset_admin", user=accounts.ADMIN, demoted=res.get("demoted", []),
              fresh_store=bool(res.get("created")))
    audit.flush(2.0)
    _say("ADMIN password reset. A running server picks this up within ~1 s.")
    return 0


def _say(msg: str) -> None:
    """A console line that never crashes the start (a console that cannot encode a path)."""
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
