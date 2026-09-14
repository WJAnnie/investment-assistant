"""Bar status contract (R02, R08).

A bar that is not CLOSED cannot be used as confirmation input for signals
or decisions. Missing/stale/invalid data must lead to WAIT (R08).
"""

from enum import Enum


class BarStatus(Enum):
    CLOSED = "closed"
    FORMING = "forming"
    MISSING = "missing"
    STALE = "stale"
    INVALID = "invalid"
