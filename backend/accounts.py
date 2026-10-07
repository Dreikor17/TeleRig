"""User accounts: the auth.json store (spec §2.2), credentials (§2.5), the v1 migration and
`reconcile()` (§2.6), tokens and revocation (§2.7) and `reset_admin()` (§2.20).

auth.json (version 2):
    {"version":2, "secret":"<64 hex>", "created":"...",
     "meta":{"limiter_reset":0, "gen_seq":1, "setup_code":"K7QM4XPA9TRB"|null, "revoked":{fp: exp}},
     "users":{"ADMIN":{"hash":"scrypt$N$r$p$<b64 salt>$<b64 dk>", "role":"admin", "gen":1,
                       "must_change":true, "default_pw":true, "migrated_v1":false,
                       "created":"...", "created_by":"system", "pw_changed":null, "last_login":null}}}
Every load is validated; a violation makes the store corrupt (locked mode, never written).

Generations (I19): every gen assignment is `gen = meta.gen_seq = meta.gen_seq + 1`; deleting a
user never lowers gen_seq, so a token is never valid again for a re-created account.

Revocation (§2.7, see revoke()): a logout lists the token's fingerprint until it expires. When
the list is full, the signing-out user's gen is bumped instead; an overflow never signs out,
kicks or unlocks any other user.

Standard library only (the `--reset-admin` CLI path imports this before uvicorn). No file I/O at
import time (I12): the store opens lazily; ensure_initialized() (run.py, the server lifespan) seeds
or migrates it.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import shutil
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from . import auth, settings
from .store import JsonStore, StoreCorrupt, StoreMissing, console

__all__ = ["StoreCorrupt", "ensure_initialized", "get", "parse_token", "make_token", "token_fp",
           "is_revoked", "verify", "hash_password", "canonical"]

VERSION = 2
ROLES = ("viewer", "operator", "admin")
ADMIN = "ADMIN"
SETUP_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"            # no I, O, 0, 1 (~60 bits for 12)
TEMP_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
USER_RE = re.compile(r"[A-Za-z0-9/_-]{2,32}")
SETUP_RE = re.compile(r"[A-HJ-NP-Z2-9]{12}")
FP_RE = re.compile(r"[0-9a-f]{16}")
SECRET_RE = re.compile(r"[0-9a-f]{64}")
B64U_RE = re.compile(r"[A-Za-z0-9_-]*")
DENYLIST = frozenset({"admin", "password", "password1", "12345678", "123456789", "1234567890",
                      "qwertyuiop", "changeme", "letmein1", "telerig", "radiowebop"})
PW_MIN, PW_MAX = 8, 72                    # bytes of UTF-8 (no normalization)
SCRYPT_N, SCRYPT_R, SCRYPT_P = 32768, 8, 1
DKLEN, SALT_LEN = 32, 16
MAXMEM = 64 * 1024 * 1024
N_MAX, R_MAX, P_MAX = 1 << 17, 16, 4      # parse bounds for stored hashes
TOKEN_TTL = 30 * 86400                    # fixed, not sliding
TOKEN_MAX = 512
REVOKED_MAX = 2000
REVOKED_PER_USER = 100                    # entries one user may hold (of those this process listed)
USER_KEYS_BOOL = ("must_change", "default_pw", "migrated_v1")
USER_KEYS_TS = ("created", "pw_changed", "last_login")


class AccountError(ValueError):
    """A request the store refuses (unknown user, duplicate, bad input). `code` is the API error."""

    def __init__(self, code: str, message: str = "", field: Optional[str] = None):
        super().__init__(message or code)
        self.code, self.message, self.field = code, message or code, field


class _AlreadyInitialized(Exception):
    pass


# -- small helpers ------------------------------------------------------------------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64u_dec(s: str) -> bytes:
    """Strict unpadded base64url: only canonical encodings decode (a re-encoding must give the
    same string), so one token has exactly one spelling and one fingerprint (revocation)."""
    if not isinstance(s, str) or not B64U_RE.fullmatch(s) or len(s) % 4 == 1:
        raise ValueError("bad base64url")
    raw = base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    if _b64u(raw) != s:
        raise ValueError("non-canonical base64url")
    return raw


def canonical(name) -> Optional[str]:
    """`^[A-Za-z0-9/_-]{2,32}$` after strip(), at least one letter or digit; canonical = upper().
    Anything else -> None (treated as an unknown user at login)."""
    if not isinstance(name, str):
        return None
    n = name.strip()
    if not USER_RE.fullmatch(n) or not any(c.isalnum() for c in n):
        return None
    return n.upper()


def uid(name: str) -> str:
    """Path-safe id of a canonical name: lowercase hex of its UTF-8 (ADMIN -> 41444d494e)."""
    return name.encode("utf-8").hex()


def password_problem(new, *, user: Optional[str] = None, current: Optional[str] = None) -> Optional[str]:
    """None if `new` is acceptable as a NEW password (§2.5), else the message for the user."""
    if not isinstance(new, str):
        return "The password must be text."
    try:
        n = len(new.encode("utf-8"))
    except UnicodeEncodeError:
        return "The password contains characters that can't be used."
    if n < PW_MIN:
        return f"Password must be 8–72 bytes (this one is {n})."
    if n > PW_MAX:
        return f"Password must be 8–72 bytes (this one is {n})."
    if current is not None and new == current:
        return "Choose a password that is different from the current one."
    if user and new.strip().casefold() == user.casefold():
        return "The password can't be the same as the callsign."
    if new.casefold() in DENYLIST:
        return "That password is too common — choose another."
    return None


def temp_password() -> str:
    return "".join(secrets.choice(TEMP_ALPHABET) for _ in range(12))


def new_setup_code() -> str:
    return "".join(secrets.choice(SETUP_ALPHABET) for _ in range(12))


def normalize_setup(value) -> str:
    """User input -> the stored form: upper-case, dashes and whitespace removed."""
    if not isinstance(value, str):
        return ""
    return "".join(ch for ch in value.upper() if ch != "-" and not ch.isspace())[:64]


def format_setup(code: Optional[str]) -> str:
    """K7QM4XPA9TRB -> K7QM-4XPA-9TRB."""
    c = code or ""
    return "-".join(c[i:i + 4] for i in range(0, len(c), 4))


# -- password hashing (scrypt) ------------------------------------------------------------------
def _maxmem(n: int, r: int, p: int) -> int:
    return max(MAXMEM, 128 * r * (n + p) + (1 << 20))


def hash_password(pw: str, *, n: int = SCRYPT_N, r: int = SCRYPT_R, p: int = SCRYPT_P) -> str:
    salt = secrets.token_bytes(SALT_LEN)
    dk = hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=DKLEN, maxmem=_maxmem(n, r, p))
    return f"scrypt${n}${r}${p}${_b64(salt)}${_b64(dk)}"


def parse_hash(stored) -> tuple:
    """(n, r, p, salt, dk) of a stored hash; ValueError when malformed or out of bounds."""
    if not isinstance(stored, str):
        raise ValueError("hash is not a string")
    parts = stored.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        raise ValueError("not a scrypt$N$r$p$salt$dk hash")
    for x in parts[1:4]:
        if not (x.isascii() and x.isdigit()) or len(x) > 7:
            raise ValueError("bad scrypt parameter")
    n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
    if n < 2 or n > N_MAX or n & (n - 1):
        raise ValueError("scrypt N out of bounds")
    if not 1 <= r <= R_MAX or not 1 <= p <= P_MAX:
        raise ValueError("scrypt r/p out of bounds")
    salt = base64.b64decode(parts[4], validate=True)
    dk = base64.b64decode(parts[5], validate=True)
    if len(salt) < 8 or not 16 <= len(dk) <= 64:
        raise ValueError("bad salt/key length")
    return n, r, p, salt, dk


def verify(stored, pw) -> tuple:
    """(ok, needs_rehash). Never raises: anything malformed -> (False, False).
    needs_rehash = the stored N is below the current SCRYPT_N."""
    try:
        n, r, p, salt, dk = parse_hash(stored)
        test = hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(dk),
                              maxmem=_maxmem(n, r, p))
        return hmac.compare_digest(test, dk), n < SCRYPT_N
    except Exception:
        return False, False


_dummy: Optional[str] = None
_dummy_mu = threading.Lock()


def dummy_hash() -> str:
    """A hash with the current parameters for unknown users (same work as a real check)."""
    global _dummy
    with _dummy_mu:
        if _dummy is None:
            _dummy = hash_password(secrets.token_hex(16))
        return _dummy


def v1_to_v2(password_hash) -> str:
    """The v1 config.json password_hash = base64(salt16 + scrypt(n=16384,r=8,p=1,dklen=32))."""
    if not isinstance(password_hash, str):
        raise ValueError("v1 password_hash is not a string")
    raw = base64.b64decode(password_hash, validate=True)
    if len(raw) != 48:
        raise ValueError(f"v1 password_hash decodes to {len(raw)} bytes, not 48")
    return f"scrypt$16384$8$1${_b64(raw[:16])}${_b64(raw[16:])}"


# -- validation (every load and every write) -----------------------------------------------------
def validate(data: dict) -> None:
    """Raise ValueError for anything §2.2 does not allow (-> StoreCorrupt / locked mode)."""
    if data.get("version") != VERSION:
        raise ValueError(f"version must be {VERSION}")
    s = data.get("secret")
    if not isinstance(s, str) or not SECRET_RE.fullmatch(s):
        raise ValueError("secret must be 64 lowercase hex characters")
    if not isinstance(data.get("created"), (str, type(None))):
        raise ValueError("created must be a string")
    meta = data.get("meta")
    if not isinstance(meta, dict):
        raise ValueError("meta must be an object")
    lr = meta.get("limiter_reset")
    if type(lr) is not int or lr < 0:
        raise ValueError("meta.limiter_reset must be an int >= 0")
    gs = meta.get("gen_seq")
    if type(gs) is not int or gs < 0:
        raise ValueError("meta.gen_seq must be an int >= 0")
    sc = meta.get("setup_code")
    if sc is not None and (not isinstance(sc, str) or not SETUP_RE.fullmatch(sc)):
        raise ValueError("meta.setup_code must be 12 characters from the setup alphabet, or null")
    rv = meta.get("revoked")
    if not isinstance(rv, dict) or len(rv) > REVOKED_MAX:
        raise ValueError(f"meta.revoked must be an object with at most {REVOKED_MAX} entries")
    for k, v in rv.items():
        if not FP_RE.fullmatch(k) or type(v) is not int:
            raise ValueError("meta.revoked entries must be 16-hex fingerprint -> int expiry")
    users = data.get("users")
    if not isinstance(users, dict):
        raise ValueError("users must be an object")
    for name, u in users.items():
        if canonical(name) != name:
            raise ValueError(f"user key {name!r} is not a canonical user name")
        if not isinstance(u, dict):
            raise ValueError(f"user {name}: not an object")
        if u.get("role") not in ROLES:
            raise ValueError(f"user {name}: unknown role {u.get('role')!r}")
        g = u.get("gen")
        if type(g) is not int or g < 1:
            raise ValueError(f"user {name}: gen must be an int >= 1")
        if g > gs:
            raise ValueError(f"user {name}: gen {g} is above meta.gen_seq {gs}")
        for k in USER_KEYS_BOOL:
            if type(u.get(k)) is not bool:
                raise ValueError(f"user {name}: {k} must be true or false")
        for k in USER_KEYS_TS:
            if not isinstance(u.get(k), (str, type(None))):
                raise ValueError(f"user {name}: {k} must be a string or null")
        if not isinstance(u.get("created_by"), (str, type(None))):
            raise ValueError(f"user {name}: created_by must be a string or null")
        parse_hash(u.get("hash"))


# -- the store (re-resolved when the config folder changes) --------------------------------------
_mu = threading.Lock()
_store: Optional[JsonStore] = None
_listeners: list = []
_state: dict = {"port": None, "reconciled": False}


def path() -> Path:
    return auth.config_dir() / "auth.json"


def store() -> JsonStore:
    global _store
    p = path()
    with _mu:
        if _store is None or _store.path != p:
            _store = JsonStore(p, validate=validate, on_change=_on_change)
        return _store


def _reset_for_tests() -> None:
    global _store, _vcache
    with _mu:
        _store = None
        _vcache = (None, None)
    _state.update(port=None, reconciled=False)
    with _rv_mu:
        _rv_owner.clear()


def status() -> str:
    """ok | missing | corrupt."""
    return store().status()


def load() -> dict:
    """Raw content (deep copy; {} when missing). Raises StoreCorrupt when corrupt."""
    return store().load()


def add_listener(fn: Callable[[dict, dict], None]) -> None:
    """fn(old, new) after every content change (a reload, a mutate here or a CLI write)."""
    if fn not in _listeners:
        _listeners.append(fn)


def _on_change(old: dict, new: dict) -> None:
    for fn in list(_listeners):
        try:
            fn(old, new)
        except Exception as e:                                # never break a read/write
            console(f"TeleRig: accounts change listener failed: {e!r}")


@dataclass(frozen=True)
class Change:
    users: frozenset            # users whose gen changed or who were removed
    revoked: frozenset          # newly revoked token fingerprints
    limiter_reset: bool
    secret: bool                # a different store (fresh secret): every token is invalid


def diff(old: dict, new: dict) -> Change:
    def gens(d):
        return {k: (v or {}).get("gen") for k, v in ((d or {}).get("users") or {}).items()}

    def meta(d):
        return (d or {}).get("meta") or {}
    og, ng = gens(old), gens(new)
    changed = frozenset(u for u, g in og.items() if ng.get(u) != g)
    orv, nrv = set((meta(old).get("revoked") or {})), set((meta(new).get("revoked") or {}))
    return Change(changed, frozenset(nrv - orv),
                  meta(old).get("limiter_reset") != meta(new).get("limiter_reset"),
                  bool(old) and (old or {}).get("secret") != (new or {}).get("secret"))


# -- cached, immutable view --------------------------------------------------------------------
@dataclass(frozen=True)
class User:
    name: str
    hash: str
    role: str
    gen: int
    must_change: bool
    default_pw: bool
    migrated_v1: bool
    created: Optional[str]
    created_by: Optional[str]
    pw_changed: Optional[str]
    last_login: Optional[str]


@dataclass(frozen=True)
class View:
    users: dict
    meta: dict
    secret: Optional[bytes]


_vcache: tuple = (None, None)


def view() -> View:
    """Users + meta (cached per store version). Raises StoreCorrupt when auth.json is corrupt."""
    global _vcache
    st = store()
    if st.status() == "corrupt":
        raise StoreCorrupt(st.path, st.detail())
    key = (str(st.path), st.version)
    ck, cv = _vcache
    if ck == key and cv is not None:
        return cv
    data = st.load()
    users = {}
    for name, u in (data.get("users") or {}).items():
        users[name] = User(name, u.get("hash"), u.get("role"), u.get("gen"), u.get("must_change"),
                           u.get("default_pw"), u.get("migrated_v1"), u.get("created"),
                           u.get("created_by"), u.get("pw_changed"), u.get("last_login"))
    secret = bytes.fromhex(data["secret"]) if data.get("secret") else None
    v = View(users, dict(data.get("meta") or {}), secret)
    _vcache = (key, v)
    return v


def get(name) -> Optional[User]:
    """The user with this (case-insensitive) name, or None. Raises StoreCorrupt."""
    c = canonical(name)
    if c is None:
        return None
    return view().users.get(c)


def users() -> dict:
    return dict(view().users)


def meta() -> dict:
    return dict(view().meta)


def other_admins() -> list:
    return sorted(n for n, u in view().users.items() if u.role == "admin" and n != ADMIN)


def admin_count() -> int:
    return sum(1 for u in view().users.values() if u.role == "admin")


def setup_code() -> Optional[str]:
    return view().meta.get("setup_code")


def setup_matches(value) -> bool:
    code = view().meta.get("setup_code")
    if not code:
        return False
    return hmac.compare_digest(normalize_setup(value).encode("ascii", "replace"), code.encode("ascii"))


# -- tokens (§2.7) ------------------------------------------------------------------------------
@dataclass(frozen=True)
class Token:
    user: str
    gen: int
    exp: int


def _sig(secret: bytes, user: str, gen: int, exp: int) -> bytes:
    msg = b"tr2|" + user.encode("utf-8") + b"|" + str(gen).encode() + b"|" + str(exp).encode()
    return hmac.new(secret, msg, hashlib.sha256).digest()


_last_exp: dict = {}                  # user -> the last exp issued by this process
_exp_mu = threading.Lock()


def make_token(user: str, gen: int, *, now: Optional[float] = None, ttl: int = TOKEN_TTL) -> tuple:
    """(token, exp) for a canonical user at generation gen.

    Every issued token is unique: exp is kept strictly increasing per user, so two sign-ins of the
    same user in the same second never share a token, and so never share a fingerprint (logging out
    one session must not revoke the other; sudo windows are per session). The drift is one second
    per extra sign-in within the same second, i.e. negligible against the 30-day lifetime."""
    sec = view().secret
    if not sec:
        raise StoreMissing("auth.json has no secret yet")
    exp = int(now if now is not None else time.time()) + int(ttl)
    if now is None:
        with _exp_mu:
            exp = max(exp, _last_exp.get(user, 0) + 1)
            _last_exp[user] = exp
    tok = f"v2.{_b64u(user.encode('utf-8'))}.{gen}.{exp}.{_b64u(_sig(sec, user, gen, exp))}"
    return tok, exp


def parse_token(tok) -> Optional[Token]:
    """Token or None. Any malformed, tampered, non-canonical or expired token -> None; never
    raises (except StoreCorrupt for a corrupt auth.json, so the caller can lock)."""
    if not isinstance(tok, str) or not tok or len(tok) > TOKEN_MAX:
        return None
    sec = view().secret                                       # StoreCorrupt propagates
    if not sec:
        return None
    try:
        if not tok.isascii():
            return None
        parts = tok.split(".")
        if len(parts) != 5 or parts[0] != "v2":
            return None
        user = _b64u_dec(parts[1]).decode("ascii")
        if canonical(user) != user:
            return None
        if not parts[2].isdigit() or not parts[3].isdigit():
            return None
        gen, exp = int(parts[2]), int(parts[3])
        if str(gen) != parts[2] or str(exp) != parts[3] or gen < 1:
            return None                                       # one spelling per token
        if not hmac.compare_digest(_b64u_dec(parts[4]), _sig(sec, user, gen, exp)):
            return None
        if exp <= time.time():
            return None
        return Token(user, gen, exp)
    except Exception:
        return None


def token_fp(tok: str) -> str:
    """16-hex fingerprint of a token (the revocation key; the token itself is never stored)."""
    return hashlib.sha256((tok or "").encode("utf-8", "replace")).hexdigest()[:16]


def is_revoked(fp: str) -> bool:
    exp = (view().meta.get("revoked") or {}).get(fp)
    return exp is not None and exp > time.time()


# -- writes -------------------------------------------------------------------------------------
def _next_gen(d: dict) -> int:
    d["meta"]["gen_seq"] = int(d["meta"]["gen_seq"]) + 1
    return d["meta"]["gen_seq"]


def _fresh_store() -> dict:
    return {"version": VERSION, "secret": secrets.token_hex(32), "created": now_iso(),
            "meta": {"limiter_reset": 0, "gen_seq": 0, "setup_code": None, "revoked": {}},
            "users": {}}


def _user_record(h: str, role: str, gen: int, *, must_change: bool, default_pw: bool = False,
                 migrated_v1: bool = False, created_by: str = "system") -> dict:
    return {"hash": h, "role": role, "gen": gen, "must_change": bool(must_change),
            "default_pw": bool(default_pw), "migrated_v1": bool(migrated_v1),
            "created": now_iso(), "created_by": created_by, "pw_changed": None, "last_login": None}


def _clear_setup_if_unused(d: dict) -> None:
    if not any(u.get("default_pw") for u in d["users"].values()):
        d["meta"]["setup_code"] = None


def set_password(name: str, new_pw: str) -> User:
    """The user's own password change (§2.16): new hash, must_change/default_pw/migrated_v1
    cleared, gen = ++gen_seq (signs out every other session), pw_changed = now. The setup code is
    cleared once no user has the default password. Returns the updated user."""
    c = canonical(name)
    h = hash_password(new_pw)

    def fn(d):
        u = d["users"].get(c)
        if u is None:
            raise AccountError("not_found", "No such user.")
        u.update(hash=h, must_change=False, default_pw=False, migrated_v1=False,
                 gen=_next_gen(d), pw_changed=now_iso())
        _clear_setup_if_unused(d)
    store().mutate(fn)
    return get(c)


def bump_gen(name: str) -> User:
    """Sign out everywhere: gen = ++gen_seq."""
    c = canonical(name)

    def fn(d):
        u = d["users"].get(c)
        if u is None:
            raise AccountError("not_found", "No such user.")
        u["gen"] = _next_gen(d)
    store().mutate(fn)
    return get(c)


_rv_mu = threading.Lock()             # serialises revoke() in this process (and guards _rv_owner)
_rv_owner: dict = {}                  # fp -> (user, gen) of the entries THIS process listed
_tl = threading.local()               # .bump = (user, gen) while revoke()'s own write fires the hook


def revocation_bump(user: str, gen) -> bool:
    """True only inside the reload hook fired by a revoke() that bumped `user` to `gen` instead of
    listing the token. That bump is the user's own sign-out, not an account change: the hook closes
    the user's sockets as `signed_out` and leaves the rate-limit keys alone."""
    return gen is not None and getattr(_tl, "bump", None) == (user, gen)


def revoke(fp: str, exp: int, user: str, gen: int) -> str:
    """Logout (§2.7): list this token's fingerprint (fp -> exp) until it expires. `user` and `gen`
    are the token's (the signed-in principal). Returns "listed", "bumped" or "dead".

    - Every write prunes expired entries, and entries this process listed whose user is gone or
      has a newer gen (those tokens are dead anyway; gens are never reused, I19).
    - A token that is already dead (user gone, gen moved on, expired) is not listed: "dead".
    - When the list is full (REVOKED_MAX), or this user already holds REVOKED_PER_USER of the
      entries this process listed, the token is not listed. The SIGNING-OUT user's gen is bumped
      instead (gen = ++gen_seq), which also ends that user's other sessions (fail safe), and that
      user's listed entries are dropped (all dead now): "bumped".

    So an overflow only ever costs the user who signs out. No other user's gen, sessions, sockets or
    rate-limit keys are touched, and the list never grows past REVOKED_MAX. The owner index is in
    memory only: after a restart, older entries have no known owner and simply stay until they
    expire (they are never a reason to bump anyone)."""
    if not FP_RE.fullmatch(fp or ""):
        raise AccountError("invalid", "bad fingerprint")
    c = canonical(user)
    if c is None or c != user or type(gen) is not int or type(exp) is not int:
        raise AccountError("invalid", "bad user, generation or expiry")
    out = {"result": "dead"}

    with _rv_mu:
        owners = dict(_rv_owner)

        def owner(k):
            return (owners.get(k) or (None, None))[0]

        def fn(d):
            out["result"] = "dead"
            _tl.bump = None
            rv, users = d["meta"]["revoked"], d["users"]
            now = time.time()
            for k, v in list(rv.items()):
                o = owners.get(k)
                if v <= now or (o is not None and (users.get(o[0]) or {}).get("gen") != o[1]):
                    del rv[k]
            u = users.get(c)
            if u is None or u.get("gen") != gen or exp <= now:
                return
            if fp in rv:
                rv[fp] = max(rv[fp], exp)
                out["result"] = "listed"
                return
            mine = [k for k in rv if owner(k) == c]
            if len(rv) >= REVOKED_MAX or len(mine) >= REVOKED_PER_USER:
                for k in mine:
                    del rv[k]
                u["gen"] = _next_gen(d)
                _tl.bump = (c, u["gen"])
                out["result"] = "bumped"
                return
            rv[fp] = exp
            out["result"] = "listed"
        try:
            new = store().mutate(fn)
        finally:
            _tl.bump = None
        if out["result"] == "listed":
            _rv_owner[fp] = (c, gen)
        live = (new.get("meta") or {}).get("revoked") or {}
        for k in [k for k in _rv_owner if k not in live]:
            del _rv_owner[k]
    return out["result"]


def create_user(name, role: str = "viewer", password: Optional[str] = None, *, must_change: bool = True,
                created_by: str = "admin") -> tuple:
    """(canonical name, temp_password or None). gen = ++gen_seq (I19). A5's admin API and the
    test helpers use this. AccountError: invalid name/role/password, or exists."""
    c = canonical(name)
    if c is None:
        raise AccountError("invalid", "Use 2–32 letters, digits, / _ -", "user")
    if role not in ROLES:
        raise AccountError("invalid", "Unknown role.", "role")
    temp = None
    if password is None:
        temp = password = temp_password()
    else:
        prob = password_problem(password, user=c)
        if prob:
            raise AccountError("invalid", prob, "password")
    h = hash_password(password)

    def fn(d):
        if c in d["users"]:
            raise AccountError("exists", "That callsign already exists.", "user")
        d["users"][c] = _user_record(h, role, _next_gen(d), must_change=must_change, created_by=created_by)
    store().mutate(fn)
    return c, temp


def delete_user(name) -> None:
    """Remove the record. gen_seq is never lowered (I19)."""
    c = canonical(name)

    def fn(d):
        if c not in d["users"]:
            raise AccountError("not_found", "No such user.")
        del d["users"][c]
        _clear_setup_if_unused(d)
    store().mutate(fn)


def record_login(name: str, *, old_hash: Optional[str] = None, new_hash: Optional[str] = None,
                 timeout: float = 0.5) -> None:
    """Best-effort after a successful login (AR-53): last_login = now, and the re-hash when the
    stored hash is still the one that was verified. Raises on failure; the caller logs it and
    never fails the login. A short lock wait so a held lock cannot tie up a worker."""
    c = canonical(name)

    def fn(d):
        u = d["users"].get(c)
        if u is None:
            return
        u["last_login"] = now_iso()
        if new_hash and old_hash and u.get("hash") == old_hash:
            u["hash"] = new_hash
    store().mutate(fn, timeout=timeout)


def reset_admin(password: str, *, demote: tuple = (), move_corrupt: bool = False) -> dict:
    """`--reset-admin` (§2.20 step 3), under the OS lock: create or replace ADMIN (role admin, new
    hash, no must_change/default_pw/migrated_v1, gen = ++gen_seq), setup_code = null,
    limiter_reset += 1, and demote the named other admins to operator (each gen = ++gen_seq).
    move_corrupt=True moves a corrupt auth.json aside (auth.json.corrupt-<UTC>) and starts a fresh
    store. Returns {"demoted": [...], "backup": path|None, "created": bool}."""
    h = hash_password(password)
    out = {"demoted": [], "backup": None, "created": False}
    want = {canonical(x) for x in demote if canonical(x)}

    def fn(d):
        if not d:
            d.update(_fresh_store())
            out["created"] = True
        gen = _next_gen(d)
        a = d["users"].get(ADMIN)
        if a is None:
            a = _user_record(h, "admin", gen, must_change=False, created_by="reset-admin")
        a.update(hash=h, role="admin", gen=gen, must_change=False, default_pw=False,
                 migrated_v1=False, pw_changed=now_iso())
        d["users"][ADMIN] = a
        out["demoted"] = []
        for n in sorted(want):
            u = d["users"].get(n)
            if n != ADMIN and u is not None and u.get("role") == "admin":
                u["role"] = "operator"
                u["gen"] = _next_gen(d)
                out["demoted"].append(n)
        d["meta"]["setup_code"] = None
        d["meta"]["limiter_reset"] = int(d["meta"]["limiter_reset"]) + 1
    st = store()
    st.last_backup = None
    st.mutate(fn, create=True, move_corrupt=move_corrupt)
    out["backup"] = str(st.last_backup) if st.last_backup else None
    return out


# -- first start / upgrade (§2.6) ---------------------------------------------------------------
def _config_corrupt() -> Optional[StoreCorrupt]:
    if settings.status() == "corrupt":
        st = settings.store()
        return StoreCorrupt(st.path, st.detail())
    return None


def _seed() -> Optional[str]:
    """auth.json is missing: migrate the v1 shared password (M1/M3a) or seed the default ADMIN
    with a setup code (M2/M3b). Returns "migrated" | "default" | None (another process won)."""
    v1 = settings.raw() or {}
    cfg = settings.path()
    bak = cfg.with_name("config.v1.bak.json")
    ph = v1.get("password_hash")
    if ph:
        try:
            h = v1_to_v2(ph)
        except Exception as e:
            raise StoreCorrupt(cfg, f"the v1 password_hash is malformed ({e}); run --reset-admin") from None
        rec = _user_record(h, "admin", 1, must_change=False, migrated_v1=True, created_by="migration")
        code, result = None, "migrated"
    else:
        rec = _user_record(hash_password("admin"), "admin", 1, must_change=True, default_pw=True,
                           created_by="system")
        code, result = new_setup_code(), "default"

    def fn(d):
        if d:
            raise _AlreadyInitialized()
        new = _fresh_store()
        new["meta"].update(gen_seq=1, setup_code=code)
        new["users"][ADMIN] = rec
        return new
    try:
        if cfg.exists() and not bak.exists():
            shutil.copy2(cfg, bak)                            # one-time copy of the v1 settings
        store().mutate(fn, create=True)
    except _AlreadyInitialized:
        return None
    _audit("store_init", result=result)
    return result


def ensure_initialized(port: Optional[int] = None, *, reconcile_mode: str = "sync") -> Path:
    """Resolve (and cache) the config folder — the one-time ~/.radio-webop -> ~/.telerig copy —
    then check the stores, seed/migrate a missing auth.json, and reconcile a stale behind_proxy
    once per process. Idempotent. Raises StoreCorrupt when auth.json is corrupt or config.json
    is unparsable (the caller starts in locked mode; nothing is defaulted or overwritten).
    reconcile_mode: "sync" | "thread" (from the event loop) | "skip"."""
    d = auth.config_dir(migrate=True)
    if port:
        _state["port"] = int(port)
    st = store()
    s = st.status()
    if s == "corrupt":
        raise StoreCorrupt(st.path, st.detail())
    bad = _config_corrupt()
    if bad is not None:
        raise bad
    if s == "missing":
        _seed()
        if store().status() == "corrupt":
            raise StoreCorrupt(st.path, st.detail())
    settings.view()                         # print any lenient-validation warnings once
    if not _state["reconciled"] and reconcile_mode != "skip":
        if reconcile_mode == "thread":
            threading.Thread(target=_reconcile_safely, daemon=True, name="tr-reconcile").start()
        else:
            _reconcile_safely()
    return d


def _port() -> Optional[int]:
    if _state["port"]:
        return _state["port"]
    v = os.environ.get("TELERIG_PORT", "")
    return int(v) if v.isdigit() else None


def _reconcile_safely() -> None:
    try:
        reconcile()
    except Exception as e:
        console(f"TeleRig: behind_proxy reconcile skipped: {e!r}")


def reconcile(port: Optional[int] = None, *, ts_status: Optional[Callable[[int], dict]] = None) -> None:
    """Every start, once auth.json is ok (AR-34): clear a stale Tailscale-set behind_proxy when
    Tailscale reports that Serve is not running for this port, then (if still behind a proxy)
    add https://N for every bare *.ts.net allowlist name without an origin entry (I15)."""
    _state["reconciled"] = True
    port = port or _port()
    v = settings.view()
    if v.behind_proxy and v.behind_proxy_source in ("tailscale", None) and port:
        try:
            if ts_status is None:
                from . import tailscale
                ts_status = tailscale.status
            st = ts_status(port) or {}
        except Exception:
            st = None                                         # any CLI error -> leave the flag alone
        if st is not None:
            installed = bool(st.get("installed"))
            if (installed and st.get("serve_status_ok") and not st.get("serve_running")) \
                    or (not installed and v.behind_proxy_source == "tailscale"):
                settings.set_behind_proxy(False)
                console("  TeleRig: behind_proxy was set but Tailscale Serve is not running for "
                        f"port {port}; cleared it.")
                _audit("behind_proxy_cleared", reason="serve_not_running")
    v = settings.view()
    if v.behind_proxy:
        names = [e for e in v.allowed_hosts if "://" not in e and e.endswith(".ts.net")]
        add = [f"https://{n}" for n in names if f"https://{n}:443" not in v.allowed_origins]
        if add:
            def fn(d):
                hosts = d.get("allowed_hosts")
                if not isinstance(hosts, list):
                    hosts = []
                for o in add:
                    if o not in hosts:
                        hosts.append(o)
                d["allowed_hosts"] = hosts
            settings.mutate(fn, create=True)
            console(f"  TeleRig: added the Serve origin(s) {', '.join(add)} to the allowlist.")


def banner_info() -> dict:
    """What run.py prints at startup (no secrets except the one-time setup code, which the console
    shows on purpose: F1)."""
    v = view()
    a = v.users.get(ADMIN)
    return {"admin": a is not None, "default_pw": bool(a and a.default_pw),
            "migrated_v1": bool(a and a.migrated_v1), "setup_code": v.meta.get("setup_code"),
            "admins": sum(1 for u in v.users.values() if u.role == "admin")}


def _audit(ev: str, **fields) -> None:
    try:
        from . import audit
        audit.log(ev, **fields)
    except Exception:
        pass
