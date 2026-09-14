"""Decision-action contract (R01, R07).

DecisionAction is the *only* vocabulary in which the final action of the
system may be expressed. Signals (SignalType) are structural facts; every
BUY/ADD requires all seven gates to pass (R07) and auto_execute is always
false (R18).
"""

from enum import Enum


class DecisionAction(Enum):
    BUY = "BUY"
    ADD = "ADD"
    HOLD = "HOLD"
    WAIT = "WAIT"
    REDUCE = "REDUCE"
    EXIT = "EXIT"
