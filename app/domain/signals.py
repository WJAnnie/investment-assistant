"""Structure-signal contract (R01, R04).

A SignalType value describes a *structural fact* detected by the Chan
engine. It is NOT a trade decision. Decisions are produced by the
seven-gate decision engine (see app/domain/decisions.py).

Values are kept identical to app.chan.signal.ChanSignal so that existing
serialization and tests remain compatible.
"""

from enum import Enum


class SignalType(Enum):
    NONE = "WAIT"
    FIRST_BUY = "FIRST_BUY"
    CLASS_SECOND_BUY = "CLASS_SECOND_BUY"
    SECOND_BUY = "SECOND_BUY"
    THIRD_BUY = "THIRD_BUY"
    SELL_RISK = "SELL_RISK"
