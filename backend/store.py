"""Fail-closed JSON stores (spec §2.4): one JSON object per file, shared safely between the
server, a `--reload` child and the CLI.

- Reads are cached. A read re-stats the file at most every 0.5 s and reloads when the file id,
  mtime_ns or size changed; every 2 s it also re-reads the bytes and compares a SHA-256, which
  catches same-size rewrites on filesystems with coarse timestamps (exFAT, SMB).
- A missing file reads as {} (status "missing"); nothing is created on read.
- A parse or validation error sets status "corrupt" and raises StoreCorrupt. The last good cache
  is kept and the file is NEVER written while it is corrupt: the caller goes into locked mode.
- Writes go through mutate(): an OS lock on a persistent `<name>.lock` file (never deleted; the OS
  drops it when the holder dies, so there is no stale-lock breaking), a reload under the lock,
  then a temp file + flush + fsync + os.replace. Any failure leaves the file and the cache as
  they were and the exception propagates.

Standard library only (the `--reset-admin` CLI path imports this before uvicorn). No file I/O at
import time.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

if sys.platform == "win32":
    import msvcrt
    fcntl = None
else:  # pragma: no cover - exercised on POSIX only
    import fcntl
    msvcrt = None

STAT_INTERVAL = 0.5      # s between re-stats of the file
HASH_INTERVAL = 2.0      # s between full re-reads + SHA-256 comparisons
LOCK_TIMEOUT = 5.0       # s to wait for the OS lock before StoreBusy
LOCK_RETRY = 0.05        # s between lock attempts (the first retries are quicker, see _acquire)
REPLACE_TRIES = 10       # os.replace retries on a Windows PermissionError (a reader has it open)
REPLACE_WAIT = 0.05


class StoreError(Exception):
    """Base class: any store failure (HTTP 500 store_error unless a subclass says otherwise)."""


class StoreCorrupt(StoreError):
    """The file exists but is not a valid store. Locked mode; the file is never written."""

    def __init__(self, path: Any, detail: str = ""):
        self.path = Path(path)
        self.detail = detail
        super().__init__(f"{self.path}: {detail}" if detail else str(self.path))


class StoreBusy(StoreError):
    """Another process holds the store lock for longer than LOCK_TIMEOUT (HTTP 503 store_busy)."""


class StoreMissing(StoreError):
    """mutate(create=False) on a store whose file does not exist."""


def console(msg: str) -> None:
    """A console line on stderr that never raises (a console that cannot encode a path must not
    turn a store problem into a 500)."""
    try:
        print(msg, file=sys.stderr, flush=True)
    except Exception:
        try:
            print(msg.encode("ascii", "replace").decode("ascii"), file=sys.stderr, flush=True)
        except Exception:
            pass


def _file_sig(st: os.stat_result) -> tuple:
    # st_ino is the NTFS file index on Windows and the inode on POSIX.
    return (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)


class JsonStore:
    """One JSON-object file. Thread-safe within a process; cross-process safe through the OS lock.

    validate(data) raises (ValueError/TypeError/...) for content that must not be accepted; it runs
    on every (re)load and on every mutate() result. on_change(old, new) is called after the cached
    content changes (reload or mutate), outside the internal lock; it must not block."""

    def __init__(self, path: Any, *, validate: Optional[Callable[[dict], None]] = None,
                 on_change: Optional[Callable[[dict, dict], None]] = None,
                 stat_interval: float = STAT_INTERVAL, hash_interval: float = HASH_INTERVAL,
                 lock_timeout: float = LOCK_TIMEOUT, clock: Callable[[], float] = time.monotonic,
                 file_mode: int = 0o600, dir_mode: int = 0o700, indent: Optional[int] = 2):
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.validate = validate
        self.on_change = on_change
        self.stat_interval = stat_interval
        self.hash_interval = hash_interval
        self.lock_timeout = lock_timeout
        self.file_mode = file_mode
        self.dir_mode = dir_mode
        self.indent = indent
        self._clock = clock
        self._mu = threading.RLock()        # cache state (held briefly)
        self._wmu = threading.Lock()        # serialises this process's writers
        self._loaded = False
        self._status = "missing"            # ok | missing | corrupt
        self._detail = ""
        self._data: dict = {}
        self._sig: Optional[tuple] = None
        self._digest: Optional[str] = None
        self._last_stat = float("-inf")
        self._last_hash = float("-inf")
        self.version = 0                    # bumps on every cache change (cheap change detection)

    # -- reading ----------------------------------------------------------------------------
    def status(self) -> str:
        """ok | missing | corrupt (refreshes first, subject to the stat/hash intervals)."""
        self._refresh()
        return self._status

    def detail(self) -> str:
        return self._detail

    def load(self) -> dict:
        """A deep copy of the current content ({} when the file is missing).
        Raises StoreCorrupt while the file is corrupt (the cache is kept, the file is not touched)."""
        self._refresh()
        with self._mu:
            if self._status == "corrupt":
                raise StoreCorrupt(self.path, self._detail)
            return copy.deepcopy(self._data)

    def check(self) -> str:
        """Re-stat and re-hash now, ignoring the intervals. Returns the status."""
        self._refresh(force=True)
        return self._status

    def _read_bytes(self) -> Optional[bytes]:
        """File bytes, or None when missing. Transient sharing errors are retried; a persistent
        OSError propagates (callers keep the previous state: an unreadable moment is not corrupt)."""
        for i in range(5):
            try:
                with open(self.path, "rb") as f:
                    return f.read()
            except FileNotFoundError:
                return None
            except PermissionError:
                if i == 4:
                    raise
                time.sleep(0.01)
        return None  # pragma: no cover

    def _parse(self, raw: bytes) -> dict:
        try:
            data = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError, RecursionError) as e:
            raise StoreCorrupt(self.path, f"not valid JSON ({e})") from None
        if not isinstance(data, dict):
            raise StoreCorrupt(self.path, f"top level is {type(data).__name__}, not an object")
        if self.validate is not None:
            try:
                self.validate(data)
            except StoreCorrupt:
                raise
            except Exception as e:
                raise StoreCorrupt(self.path, f"invalid content ({e})") from None
        return data

    def _unreadable(self, e: OSError) -> None:
        """The file exists but cannot be read (not a FileNotFoundError). Before any good load this
        is fail-closed (corrupt: never treat an unreadable store as empty); after one, a moment of
        unreadability (a writer replacing the file on Windows) keeps the last good state."""
        if not self._loaded:
            self._loaded = True
            self._sig = self._digest = None
            self._status, self._detail = "corrupt", f"cannot be read ({e.strerror or e})"
            self.version += 1

    def _refresh(self, force: bool = False) -> None:
        fire = None
        with self._mu:
            now = self._clock()
            if not force and self._loaded and now - self._last_stat < self.stat_interval:
                return
            self._last_stat = now
            try:
                sig = _file_sig(os.stat(self.path))
            except FileNotFoundError:
                sig = None
            except OSError as e:
                self._unreadable(e)
                return
            hash_due = force or not self._loaded or now - self._last_hash >= self.hash_interval
            if self._loaded and sig == self._sig and not hash_due:
                return
            if sig is None:
                raw = None
            else:
                try:
                    raw = self._read_bytes()
                except OSError as e:
                    self._unreadable(e)
                    return
            self._last_hash = now
            if raw is None:
                old_status = self._status
                old = self._data
                self._status, self._detail = "missing", ""
                self._data, self._sig, self._digest = {}, None, None
                self._loaded = True
                if old_status != "missing" or old:
                    self.version += 1
                if old:
                    fire = (old, {})
            else:
                digest = hashlib.sha256(raw).hexdigest()
                if self._loaded and digest == self._digest:
                    self._sig = sig                          # same bytes (touch / same rewrite)
                    return
                self._digest, self._sig = digest, sig
                self._loaded = True
                try:
                    data = self._parse(raw)
                except StoreCorrupt as e:
                    if self._status != "corrupt":
                        self.version += 1
                    self._status, self._detail = "corrupt", e.detail
                    return                                   # cache kept, file never written
                old = self._data
                was = self._status
                self._data = data
                self._status, self._detail = "ok", ""
                if data != old or was != "ok":
                    self.version += 1
                if data != old:
                    fire = (old, data)
        if fire is not None:
            self._fire(*fire)

    def _fire(self, old: dict, new: dict) -> None:
        cb = self.on_change
        if cb is None:
            return
        try:
            cb(copy.deepcopy(old), copy.deepcopy(new))
        except Exception as e:                               # never let a hook break a read/write
            console(f"TeleRig: store change hook failed for {self.path.name}: {e!r}")

    # -- writing ----------------------------------------------------------------------------
    def mutate(self, fn: Callable[[dict], Optional[dict]], create: bool = False, *,
               timeout: Optional[float] = None, move_corrupt: bool = False) -> dict:
        """Apply fn to a deep copy of the current content and write the result atomically under
        the OS lock. fn may return the new dict or modify its argument and return None.
        create=False: a missing file raises StoreMissing (nothing is written). Returns a copy of
        the new content. Raises StoreCorrupt / StoreBusy / StoreMissing / the validator's or
        fn's exception / OSError; in every failure case the file and the cache are unchanged.

        timeout: seconds to wait for the OS lock (default: lock_timeout), e.g. a short wait for a
        best-effort write. move_corrupt=True (the `--reset-admin` repair, an explicit operator
        choice): a corrupt file is renamed to `<name>.corrupt-<UTC>` under the same lock and fn
        then starts from {} (create is implied); `last_backup` names the moved file."""
        with self._wmu:
            fd = self._acquire(create or move_corrupt, timeout)
            fire = None
            try:
                self._refresh(force=True)                    # reload under the lock
                if move_corrupt and self._status == "corrupt":
                    self._move_aside()
                    create = True
                with self._mu:
                    if self._status == "corrupt":
                        raise StoreCorrupt(self.path, self._detail)
                    if self._status == "missing" and not create:
                        raise StoreMissing(str(self.path))
                    work = copy.deepcopy(self._data)
                res = fn(work)
                new = work if res is None else res
                if not isinstance(new, dict):
                    raise TypeError(f"store content must be a dict, not {type(new).__name__}")
                if self.validate is not None:
                    self.validate(new)
                payload = json.dumps(new, indent=self.indent).encode("utf-8")
                self._write_atomic(payload)
                with self._mu:
                    old = self._data
                    self._data = json.loads(payload.decode("utf-8"))   # exactly what is on disk
                    self._status, self._detail = "ok", ""
                    self._digest = hashlib.sha256(payload).hexdigest()
                    try:
                        self._sig = _file_sig(os.stat(self.path))
                    except OSError:
                        self._sig = None
                    now = self._clock()
                    self._last_stat = self._last_hash = now
                    self._loaded = True
                    self.version += 1
                    fire = (old, self._data)
                    result = copy.deepcopy(self._data)
            finally:
                self._release(fd)
        if fire is not None and fire[0] != fire[1]:
            self._fire(*fire)
        return result

    last_backup: Optional[Path] = None

    def _move_aside(self) -> None:
        """Rename the (corrupt) file to <name>.corrupt-<UTC>; caller holds the OS lock. The cache
        becomes "missing" so the caller's fn starts from {}."""
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        dst = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
        n = 1
        while dst.exists():
            n += 1
            dst = self.path.with_name(f"{self.path.name}.corrupt-{stamp}-{n}")
        for i in range(REPLACE_TRIES):
            try:
                os.replace(self.path, dst)
                break
            except PermissionError:
                if i == REPLACE_TRIES - 1:
                    raise
                time.sleep(REPLACE_WAIT)
        self.last_backup = dst
        with self._mu:
            self._status, self._detail = "missing", ""
            self._data, self._sig, self._digest = {}, None, None
            self._loaded = True
            self.version += 1

    def _write_atomic(self, payload: bytes) -> None:
        d = self.path.parent
        tmp = d / f".{self.path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        fd = os.open(tmp, flags, self.file_mode)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            if os.name != "nt":
                os.chmod(tmp, self.file_mode)
            for i in range(REPLACE_TRIES):
                try:
                    os.replace(tmp, self.path)
                    break
                except PermissionError:
                    if i == REPLACE_TRIES - 1:
                        raise
                    time.sleep(REPLACE_WAIT)
            if os.name != "nt":                              # best effort: persist the rename
                try:
                    dfd = os.open(d, os.O_RDONLY)
                    try:
                        os.fsync(dfd)
                    finally:
                        os.close(dfd)
                except OSError:
                    pass
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # -- the OS lock --------------------------------------------------------------------------
    def _ensure_dir(self, create: bool) -> None:
        d = self.path.parent
        if d.is_dir():
            return
        if not create:
            raise StoreMissing(str(d))
        d.mkdir(parents=True, exist_ok=True, mode=self.dir_mode)
        if os.name != "nt":
            try:
                os.chmod(d, self.dir_mode)
            except OSError:
                pass

    def _acquire(self, create: bool, timeout: Optional[float] = None) -> int:
        self._ensure_dir(create)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), self.file_mode)
        deadline = time.monotonic() + (self.lock_timeout if timeout is None else max(0.0, timeout))
        wait = 0.005
        while True:
            try:
                _lock_fd(fd)
                return fd
            except OSError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise StoreBusy(f"{self.path.name} is locked by another TeleRig process") from None
                time.sleep(min(wait, LOCK_RETRY))
                wait *= 2

    def _release(self, fd: int) -> None:
        try:
            _unlock_fd(fd)
        except OSError:
            pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    def hold_lock(self, create: bool = True) -> "HeldLock":
        """Context manager that holds the OS lock (tests, and the CLI around multi-step repairs)."""
        return HeldLock(self, create)


class HeldLock:
    def __init__(self, store: JsonStore, create: bool):
        self.store, self.create, self.fd = store, create, None

    def __enter__(self) -> "HeldLock":
        self.fd = self.store._acquire(self.create)
        return self

    def __exit__(self, *exc) -> None:
        if self.fd is not None:
            self.store._release(self.fd)
            self.fd = None


def _lock_fd(fd: int) -> None:
    if msvcrt is not None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)       # byte 0; works on an empty file
    else:  # pragma: no cover - POSIX
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_fd(fd: int) -> None:
    if msvcrt is not None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:  # pragma: no cover - POSIX
        fcntl.flock(fd, fcntl.LOCK_UN)
