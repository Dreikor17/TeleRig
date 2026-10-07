"""User accounts (auth.json). Step A2 stub: only `ensure_initialized()` exists yet.

Step A3 adds the user store (§2.2), credentials (§2.5), the v1 migration and `reconcile()`
(§2.6), tokens and revocation (§2.7) and `reset_admin()`.

Standard library only (the `--reset-admin` CLI path imports this before uvicorn). No file I/O at
import time (I12): everything happens in ensure_initialized(), which run.py, the server lifespan
and `--reset-admin` call.
"""
from __future__ import annotations

from pathlib import Path

from . import auth, settings
from .store import StoreCorrupt

__all__ = ["StoreCorrupt", "ensure_initialized"]


def ensure_initialized() -> Path:
    """Resolve (and cache) the config folder — doing the one-time ~/.radio-webop -> ~/.telerig
    copy if needed — then check the stores. Idempotent. Raises StoreCorrupt when config.json
    cannot be parsed (the caller starts in locked mode; nothing is defaulted or overwritten)."""
    d = auth.config_dir(migrate=True)
    if settings.status() == "corrupt":
        st = settings.store()
        raise StoreCorrupt(st.path, st.detail())
    settings.view()                         # print any lenient-validation warnings once, at startup
    return d
