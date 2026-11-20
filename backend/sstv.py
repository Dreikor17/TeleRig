"""SSTV pictures sent from the page (frontend/sstv-tx.js): how long each one is on the air, for its TX limit.

One picture is one operator-triggered transmission: a ptt claim that names its mode
({"action": "ptt", "tx": true, "sstv": <mode id>}). It is held as long as the picture takes, not the
120 s of a voice over: the TX arbiter's session and the radio handler's failsafe both end it at its
time on the air + CAP_EXTRA, and never later than the radio's longest keying (radio.ptt_hold_max:
3 s inside the 3-minute hardware time-out set on connect, the backstop if the control link fails).
Only a mode that fits is accepted (fits()); the page offers no other.

ON_AIR_S is the page's own count, rounded up to 10 ms: the silence after the key, the VIS header, the
picture and the silent tail, from the mode tables the encoder uses. tests/t_c1.py checks that the two
agree, mode for mode.
"""
from __future__ import annotations

from typing import Optional

ON_AIR_S = {
    "r36": 37.81, "r72": 73.81, "m1": 116.11, "m2": 59.88, "s1": 111.45, "s2": 72.91, "sdx": 270.70,
    "pd50": 51.50, "pd90": 91.80, "pd120": 127.92, "pd160": 162.70, "pd180": 188.87, "pd240": 249.81,
    "pd290": 290.50,
}
CAP_EXTRA = 5.0          # a picture's TX limit: its time on the air + this, at most the radio's longest keying
FIT_MARGIN = 3.0         # a mode fits a radio when its picture ends this far inside the radio's longest keying


def fits(mode: object, hold_max: float) -> bool:
    """True when `mode` is a known SSTV mode whose transmission ends FIT_MARGIN inside `hold_max` (s)."""
    s = ON_AIR_S.get(mode) if isinstance(mode, str) else None
    return s is not None and s <= float(hold_max) - FIT_MARGIN


def cap_s(mode: object, hold_max: float) -> Optional[float]:
    """The TX limit of one picture in `mode`, in seconds: its time on the air + CAP_EXTRA, at most
    `hold_max`. None when the mode is unknown or doesn't fit (fits())."""
    if not fits(mode, hold_max):
        return None
    return min(ON_AIR_S[mode] + CAP_EXTRA, float(hold_max))
