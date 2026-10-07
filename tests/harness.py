"""TeleRig Phase 1 test harness: stdlib only, plus the `websockets` client the app already uses.

Run the tests with tests/run_phase1.py; this module only provides the building blocks.

Safety interlocks (do not weaken them):
  * Ports: free_port() / check_port() never hand out 8700 or 8722 (the user's real instances).
  * Server(): always runs run.py with --sim-only --no-autoconnect --no-browser on 127.0.0.1, with a
    scratch TELERIG_CONFIG, a scratch HOME/USERPROFILE and RADIO_WEBOP_CONFIG removed. It refuses a
    config that would resolve inside the real ~/.telerig or ~/.radio-webop (or the real home itself).
  * TX: every TX-capable WS send (ptt, cw_tx, tune_atu, rx_func vox, mic, binary audio) calls
    assert_sim() first, every time. GET /api/status must report sim_only:true and
    transport_kind=="sim"; otherwise Refusing is raised and the WHOLE run aborts with
    "REFUSING: non-simulator transport".

Importing this module does no file I/O and never imports the backend (I12): code that needs the
backend runs in a child process with scratch_env().
"""
from __future__ import annotations

import asyncio
import getpass
import http.client
import http.cookiejar
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

REPO = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent
FORBIDDEN_PORTS = frozenset({8700, 8722})          # the user's real TeleRig instances
REAL_CONFIG_DIRS = (".telerig", ".radio-webop")
TX_ACTIONS = frozenset({"ptt", "cw_tx", "tune_atu", "mic"})
DEFAULT_WAIT = 15.0                                # seconds to wait for /login after spawning


class Refusing(BaseException):
    """A safety interlock tripped. BaseException on purpose: no test's `except Exception` can
    swallow it, and the runner aborts the whole run when it escapes a test."""


class HarnessError(Exception):
    """The harness could not do what was asked (server did not start, bad arguments, ...)."""


class TestFailure(AssertionError):
    """A check failed (raised by check()/eq(); works under python -O, unlike assert)."""


def check(cond: Any, msg: str = "check failed") -> None:
    if not cond:
        raise TestFailure(msg)


def eq(got: Any, want: Any, msg: str = "") -> None:
    if got != want:
        raise TestFailure(f"{msg + ': ' if msg else ''}got {got!r}, want {want!r}")


# -- test registry (used by run_phase1.py) --------------------------------------------------
KNOWN_GROUPS = ("unit", "smoke", "store", "auth", "ws", "tx", "admin", "layout")
REGISTRY: list = []                                # [(group, name, fn)]


def test(group: str, name: Optional[str] = None) -> Callable:
    """Decorator: register fn(ctx) as a test in `group`."""
    if group not in KNOWN_GROUPS:
        raise HarnessError(f"unknown test group {group!r}; known: {', '.join(KNOWN_GROUPS)}")

    def deco(fn: Callable) -> Callable:
        REGISTRY.append((group, name or fn.__name__, fn))
        return fn
    return deco


# -- ports --------------------------------------------------------------------------------
def check_port(port: Any) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise Refusing(f"REFUSING: invalid port {port!r}")
    if port in FORBIDDEN_PORTS:
        raise Refusing(f"REFUSING: port {port} belongs to a real TeleRig instance (8700/8722 are never used by tests)")
    return port


def free_port() -> int:
    """A free loopback TCP port, never 8700 or 8722."""
    for _ in range(50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port not in FORBIDDEN_PORTS:
            return check_port(port)
    raise HarnessError("could not find a free port")


# -- the real home and its config folders (captured at import, before any test touches env) --
def _norm(p: Any) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(p))))


def _real_homes() -> tuple:
    cands = []
    for get in (lambda: str(Path.home()), lambda: os.path.expanduser("~"),
                lambda: os.environ.get("USERPROFILE"), lambda: os.environ.get("HOME"),
                lambda: (os.environ.get("HOMEDRIVE") or "") + (os.environ.get("HOMEPATH") or "")):
        try:
            v = get()
        except Exception:
            v = None
        if v:
            cands.append(v)
    if sys.platform == "win32":
        try:
            cands.append(os.path.join((os.environ.get("SystemDrive") or "C:") + os.sep, "Users", getpass.getuser()))
        except Exception:
            pass
    else:
        try:
            import pwd
            cands.append(pwd.getpwuid(os.getuid()).pw_dir)
        except Exception:
            pass
    out = []
    for c in cands:
        try:
            n = _norm(c)
        except Exception:
            continue
        if n not in out and os.path.isabs(n) and os.path.dirname(n) != n:   # never a bare drive/root
            out.append(n)
    return tuple(out)


REAL_HOMES = _real_homes()


def _inside(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


def check_config_path(config_path: Any, *, homes: Optional[tuple] = None) -> Path:
    """Refuse a TELERIG_CONFIG that resolves inside a real ~/.telerig or ~/.radio-webop, or whose
    folder is the real home itself. Returns the normalised absolute path. Never creates anything."""
    cfg = _norm(config_path)
    for home in (REAL_HOMES if homes is None else tuple(_norm(h) for h in homes)):
        for sub in REAL_CONFIG_DIRS:
            if _inside(cfg, _norm(os.path.join(home, sub))):
                raise Refusing(f"REFUSING: config {cfg} is inside the real {os.path.join(home, sub)}")
        if _norm(os.path.dirname(cfg)) == home:
            raise Refusing(f"REFUSING: config {cfg} would live directly in the real home {home}")
    return Path(cfg)


def check_scratch_home(home: Any, *, homes: Optional[tuple] = None) -> Path:
    """The child's HOME/USERPROFILE must not be (or sit inside the config folders of) a real home."""
    h = _norm(home)
    for real in (REAL_HOMES if homes is None else tuple(_norm(x) for x in homes)):
        if h == real:
            raise Refusing(f"REFUSING: scratch HOME {h} is the real home")
        for sub in REAL_CONFIG_DIRS:
            if _inside(h, _norm(os.path.join(real, sub))):
                raise Refusing(f"REFUSING: scratch HOME {h} is inside the real {os.path.join(real, sub)}")
    return Path(h)


_STRIP_ENV = ("RADIO_WEBOP_CONFIG", "TELERIG_CONFIG", "TELERIG_SIM_ONLY", "TELERIG_NO_AUTOCONNECT",
              "TELERIG_SSL_CERT", "TELERIG_SSL_KEY", "RADIO_WEBOP_SSL_CERT", "RADIO_WEBOP_SSL_KEY")


def scratch_env(tmp: Any, *, sim_only: bool = True, no_autoconnect: bool = True) -> dict:
    """Environment for any child that may import the backend: TELERIG_CONFIG=<tmp>/config.json,
    HOME/USERPROFILE=<tmp>/home, RADIO_WEBOP_CONFIG and TLS variables removed. Validates before
    creating anything; creates only <tmp>/home."""
    tmp = Path(tmp)
    cfg = check_config_path(tmp / "config.json")
    home = check_scratch_home(tmp / "home")
    home.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
    env["TELERIG_CONFIG"] = str(cfg)
    env["HOME"] = env["USERPROFILE"] = str(home)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    if sim_only:
        env["TELERIG_SIM_ONLY"] = "1"
    if no_autoconnect:
        env["TELERIG_NO_AUTOCONNECT"] = "1"
    return env


def run_child(args: list, tmp: Any, *, input: Optional[str] = None, timeout: float = 60.0,
              sim_only: bool = True, no_autoconnect: bool = True) -> subprocess.CompletedProcess:
    """Run [sys.executable, *args] from the repo root with scratch_env(tmp) (e.g. run.py CLI
    calls, `-m backend.x_test`, `-c <code>`). Never starts a server: use Server for that."""
    env = scratch_env(tmp, sim_only=sim_only, no_autoconnect=no_autoconnect)
    return subprocess.run([sys.executable, *[str(a) for a in args]], cwd=str(REPO), env=env,
                          input=input, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)


# -- HTTP ---------------------------------------------------------------------------------
class Resp:
    def __init__(self, status: int, headers: list, body: bytes):
        self.status = status
        self.header_list = [(k.lower(), v) for k, v in headers]
        self.headers = {}
        for k, v in self.header_list:
            self.headers.setdefault(k, v)
        self.body = body

    def header_all(self, name: str) -> list:
        n = name.lower()
        return [v for k, v in self.header_list if k == n]

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except Exception:
            return None

    def __repr__(self) -> str:
        return f"<Resp {self.status} {self.text[:160]!r}>"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):           # surface 3xx to the test instead of following
        return None


_DEFAULT = object()
_EMPTY = object()


class Client:
    """One browser-like session: its own cookie jar, no system proxy, Origin on every request,
    JSON bodies on non-GET. Redirects are returned, not followed."""

    def __init__(self, port: int, *, host: str = "127.0.0.1", origin: Any = _DEFAULT):
        self.port = check_port(port)
        self.host = host
        self.base = f"http://{host}:{port}"
        self.origin = f"http://{host}:{port}" if origin is _DEFAULT else origin
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                                  urllib.request.HTTPCookieProcessor(self.jar),
                                                  _NoRedirect())

    def request(self, method: str, path: str, body: Any = _EMPTY, *, headers: Optional[dict] = None,
                origin: Any = _DEFAULT, timeout: float = 10.0) -> Resp:
        method = method.upper()
        h = {}
        o = self.origin if origin is _DEFAULT else origin
        if o is not None:
            h["Origin"] = o
        data = None
        if method not in ("GET", "HEAD"):
            if body is _EMPTY:
                body = {}
            if body is not None:
                data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
                h["Content-Type"] = "application/json"
        h.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={k: v for k, v in h.items() if v is not None})
        try:
            with self.opener.open(req, timeout=timeout) as r:
                return Resp(r.status, list(r.headers.items()), r.read())
        except urllib.error.HTTPError as e:
            try:
                payload = e.read()
            except Exception:
                payload = b""
            return Resp(e.code, list(e.headers.items()) if e.headers else [], payload)

    def get(self, path: str, **kw) -> Resp:
        return self.request("GET", path, **kw)

    def post(self, path: str, body: Any = _EMPTY, **kw) -> Resp:
        return self.request("POST", path, body, **kw)

    def put(self, path: str, body: Any = _EMPTY, **kw) -> Resp:
        return self.request("PUT", path, body, **kw)

    def delete(self, path: str, body: Any = None, **kw) -> Resp:
        return self.request("DELETE", path, body, **kw)

    def cookie_header(self) -> str:
        return "; ".join(f"{c.name}={c.value}" for c in self.jar)

    def raw(self, method: str, path: str, *, body: Any = None, headers: Optional[dict] = None,
            cookies: bool = True, connect_host: str = "127.0.0.1", timeout: float = 10.0) -> Resp:
        """Exact request via http.client: Host defaults to <host>:<port> (override it in
        `headers`); no Origin unless given; response headers kept as a list (duplicates kept)."""
        h = {"Host": f"{self.host}:{self.port}"}
        if cookies and self.jar:
            h["Cookie"] = self.cookie_header()
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
            h["Content-Type"] = "application/json"
        for k, v in (headers or {}).items():
            for existing in [x for x in h if x.lower() == k.lower()]:
                del h[existing]
            if v is not None:
                h[k] = v
        if data is not None and not any(k.lower() == "content-length" for k in h):
            h["Content-Length"] = str(len(data))
        conn = http.client.HTTPConnection(connect_host, self.port, timeout=timeout)
        try:
            conn.putrequest(method.upper(), path, skip_host=True, skip_accept_encoding=True)
            for k, v in h.items():
                conn.putheader(k, v)
            conn.endheaders(data)
            r = conn.getresponse()
            return Resp(r.status, r.getheaders(), r.read())
        finally:
            conn.close()

    def ws(self, path: str = "/ws", *, origin: Any = _DEFAULT, cookies: bool = True,
           headers: Optional[dict] = None, open_timeout: float = 5.0) -> "WS":
        """websockets.sync.client.connect with this client's Origin and cookies. A handshake the
        server refuses with an HTTP status raises WSRejected(status)."""
        hdrs = dict(headers or {})
        if cookies and self.jar:
            hdrs["Cookie"] = self.cookie_header()
        o = self.origin if origin is _DEFAULT else origin
        conn = ws_connect(f"ws://{self.host}:{self.port}{path}", origin=o, headers=hdrs,
                          open_timeout=open_timeout)
        return WS(conn, self)


# -- WebSocket ----------------------------------------------------------------------------
class WSRejected(Exception):
    def __init__(self, status: int):
        super().__init__(f"WebSocket handshake rejected with HTTP {status}")
        self.status = status


class WSClosed(Exception):
    def __init__(self, code: Optional[int], reason: str):
        super().__init__(f"WebSocket closed {code} {reason!r}")
        self.code, self.reason = code, reason


def close_info(exc: BaseException) -> tuple:
    """(code, reason) of the close frame the SERVER sent. Pinned accessor for websockets 16:
    ConnectionClosed.rcvd.code / .rcvd.reason (the .code property is deprecated). No close frame
    received -> (1006, "") like the old property."""
    rcvd = getattr(exc, "rcvd", None)
    if rcvd is None:
        return 1006, ""
    return rcvd.code, rcvd.reason


def ws_connect(uri: str, *, origin: Optional[str], headers: Optional[dict] = None,
               open_timeout: float = 5.0):
    from websockets.exceptions import InvalidStatus
    from websockets.sync.client import connect
    try:
        return connect(uri, origin=origin, additional_headers=headers or None,
                       open_timeout=open_timeout, proxy=None)
    except InvalidStatus as e:
        raise WSRejected(e.response.status_code) from None


def is_tx_command(obj: Any) -> bool:
    """True for anything that can key the transmitter or feed it audio: ptt, cw_tx, tune_atu,
    mic and rx_func vox (any rx_func whose name is not a plain string counts too: fail closed)."""
    if not isinstance(obj, dict):
        return False
    a = obj.get("action")
    if not isinstance(a, str):
        return False
    a = a.strip().lower()
    if a in TX_ACTIONS:
        return True
    if a == "rx_func":
        n = obj.get("name")
        return not isinstance(n, str) or n.strip().lower() == "vox"
    return False


def assert_sim(client: Any) -> dict:
    """Interlock: run before EVERY TX command. GET /api/status must report sim_only:true and
    transport_kind=="sim", else Refusing (the whole run aborts)."""
    msg = "REFUSING: non-simulator transport"
    if client is None:
        raise Refusing(msg + " (no client to ask /api/status)")
    check_port(getattr(client, "port", None))
    try:
        r = client.get("/api/status")
    except Exception as e:
        raise Refusing(f"{msg} (/api/status failed: {e})") from None
    j = r.json() if getattr(r, "status", None) == 200 else None
    if not isinstance(j, dict):
        raise Refusing(f"{msg} (/api/status -> HTTP {getattr(r, 'status', '?')})")
    if j.get("sim_only") is not True or j.get("transport_kind") != "sim":
        raise Refusing(f"{msg} (sim_only={j.get('sim_only')!r}, transport_kind={j.get('transport_kind')!r})")
    return j


class WS:
    """A test WebSocket. TX-capable sends always pass through assert_sim(client) first."""

    def __init__(self, conn: Any, client: Any):
        self._c = conn
        self.client = client
        self.close_code: Optional[int] = None
        self.close_reason = ""

    def _guard(self, obj: Any) -> None:
        if is_tx_command(obj):
            assert_sim(self.client)

    def send(self, obj: dict) -> None:
        text = json.dumps(obj)
        self._guard(json.loads(text))              # exactly what the server will parse
        self._c.send(text)

    def send_text(self, text: str) -> None:
        try:
            obj = json.loads(text)
        except Exception:
            obj = None
        self._guard(obj)
        self._c.send(text)

    def send_bytes(self, data: bytes) -> None:
        assert_sim(self.client)                    # binary frames are mic audio -> the TX path
        self._c.send(bytes(data))

    def recv(self, timeout: float = 5.0) -> Any:
        from websockets.exceptions import ConnectionClosed
        try:
            return self._c.recv(timeout=timeout)
        except ConnectionClosed as e:
            self.close_code, self.close_reason = close_info(e)
            raise WSClosed(self.close_code, self.close_reason) from None

    def recv_json(self, timeout: float = 5.0, type: Optional[str] = None) -> dict:
        """Next text frame (optionally of the given "type"), skipping binary frames."""
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"no {type or 'text'} frame within {timeout}s")
            m = self.recv(timeout=left)
            if isinstance(m, str):
                obj = json.loads(m)
                if type is None or (isinstance(obj, dict) and obj.get("type") == type):
                    return obj

    def wait_for(self, pred: Callable[[dict], bool], timeout: float = 5.0, type: Optional[str] = None) -> dict:
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"condition not met within {timeout}s")
            obj = self.recv_json(timeout=left, type=type)
            if pred(obj):
                return obj

    def wait_closed(self, timeout: float = 5.0) -> tuple:
        """Drain until the server closes; returns (code, reason)."""
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"socket still open after {timeout}s")
            try:
                self.recv(timeout=left)
            except WSClosed as e:
                return e.code, e.reason

    def close(self) -> None:
        try:
            self._c.close()
        except Exception:
            pass

    def __enter__(self) -> "WS":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# -- the server under test ------------------------------------------------------------------
class Server:
    """run.py on 127.0.0.1:<free port> with --sim-only --no-autoconnect --no-browser and a
    scratch config/home under `tmp`. `env_flags=False` leaves TELERIG_SIM_ONLY/NO_AUTOCONNECT out
    of the environment so only the CLI flags (always passed) enable the guards."""

    def __init__(self, tmp: Any, *, port: Optional[int] = None, env_flags: bool = True,
                 wait: float = DEFAULT_WAIT):
        self.tmp = Path(tmp)
        check_config_path(self.tmp / "config.json")             # before anything is created
        check_scratch_home(self.tmp / "home")
        self._fixed_port = check_port(port) if port is not None else None
        self.env_flags = env_flags
        self.wait = min(float(wait), DEFAULT_WAIT)
        self.port: Optional[int] = None
        self.proc: Optional[subprocess.Popen] = None
        self.cmd: list = []
        self.log_path: Optional[Path] = None
        self.ready_status: Optional[int] = None
        self._log_fh = None

    @property
    def config_path(self) -> Path:
        return self.tmp / "config.json"

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "Server":
        if self.proc is not None:
            raise HarnessError("server already started")
        attempts = 1 if self._fixed_port else 3
        for attempt in range(attempts):
            port = self._fixed_port or free_port()
            if self._spawn(port):
                return self
            if attempt + 1 < attempts and "10048" not in self.log_text() \
                    and "address already in use" not in self.log_text().lower():
                break
        raise HarnessError(f"server did not become ready on port {self.port}:\n{self.log_tail()}")

    def _spawn(self, port: int) -> bool:
        self.port = check_port(port)
        env = scratch_env(self.tmp, sim_only=self.env_flags, no_autoconnect=self.env_flags)
        self.cmd = [sys.executable, str(REPO / "run.py"), "--host", "127.0.0.1", "--port", str(port),
                    "--sim-only", "--no-autoconnect", "--no-browser"]
        self.log_path = self.tmp / f"server-{port}.log"
        self._log_fh = open(self.log_path, "w", encoding="utf-8", errors="replace")
        self.proc = subprocess.Popen(self.cmd, cwd=str(REPO), env=env, stdin=subprocess.DEVNULL,
                                     stdout=self._log_fh, stderr=subprocess.STDOUT)
        end = time.monotonic() + self.wait
        while time.monotonic() < end:
            if self.proc.poll() is not None:
                self._reap()
                return False
            try:
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=1.0)
                c.request("GET", "/login", headers={"Host": f"127.0.0.1:{port}"})
                self.ready_status = c.getresponse().status
                c.close()
                return True
            except (OSError, http.client.HTTPException):
                time.sleep(0.1)
        self.stop()
        return False

    def _reap(self) -> None:
        if self._log_fh:
            try:
                self._log_fh.close()
            except Exception:
                pass
            self._log_fh = None
        self.proc = None

    def stop(self) -> None:
        p = self.proc
        if p is not None and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout=5)
        self._reap()

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def log_text(self) -> str:
        try:
            if self._log_fh:
                self._log_fh.flush()
            return self.log_path.read_text("utf-8", "replace") if self.log_path else ""
        except OSError:
            return ""

    def log_tail(self, n: int = 40) -> str:
        return "\n".join(self.log_text().splitlines()[-n:])

    def client(self, **kw) -> Client:
        if self.port is None:
            raise HarnessError("server not started")
        return Client(self.port, **kw)

    def __enter__(self) -> "Server":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


# -- in-process ASGI calls (for child processes that import the backend) ---------------------
def asgi_request(app: Any, method: str, path: str, *, body: Any = None, headers: Optional[dict] = None,
                 client: tuple = ("127.0.0.1", 50000), server: tuple = ("127.0.0.1", 8790),
                 scheme: str = "http", query: bytes = b"") -> Resp:
    """Call an ASGI app directly (no socket, no server): Host + same-origin Origin are filled in;
    `headers` overrides them (None removes one). Only for child processes run with scratch_env()."""
    method = method.upper()
    raw = b"" if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode("utf-8"))
    hosthdr = f"{server[0]}:{server[1]}"
    h = {"host": hosthdr}
    if method not in ("GET", "HEAD"):
        h["origin"] = f"{scheme}://{hosthdr}"
    if body is not None and not isinstance(body, bytes):
        h["content-type"] = "application/json"
    if raw:
        h["content-length"] = str(len(raw))
    for k, v in (headers or {}).items():
        if v is None:
            h.pop(k.lower(), None)
        else:
            h[k.lower()] = v
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
             "method": method, "scheme": scheme, "path": path, "raw_path": path.encode("utf-8"),
             "query_string": query, "root_path": "",
             "headers": [(k.encode("latin-1"), v.encode("latin-1")) for k, v in h.items()],
             "client": client, "server": server, "state": {}}

    async def run() -> Resp:
        out = {"status": 500, "headers": [], "body": b""}
        done = asyncio.Event()
        sent_body = False

        async def receive():
            nonlocal sent_body
            if not sent_body:
                sent_body = True
                return {"type": "http.request", "body": raw, "more_body": False}
            await done.wait()
            return {"type": "http.disconnect"}

        async def send(msg):
            if msg["type"] == "http.response.start":
                out["status"] = msg["status"]
                out["headers"] = [(k.decode("latin-1"), v.decode("latin-1")) for k, v in msg.get("headers", [])]
            elif msg["type"] == "http.response.body":
                out["body"] += msg.get("body", b"")
                if not msg.get("more_body"):
                    done.set()

        await app(scope, receive, send)
        done.set()
        return Resp(out["status"], out["headers"], out["body"])

    return asyncio.run(run())
