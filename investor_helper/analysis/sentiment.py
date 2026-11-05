"""Finance-tuned lexicon sentiment.

A compact word list in the spirit of Loughran-McDonald, plus headline phrases
that move prices (beats, guidance cuts, downgrades). Returns a score in [-1, 1].
It is fast, offline and explainable; the learning loop decides how much each
source's sentiment should count.
"""
from __future__ import annotations

import math
import re

POSITIVE = {
    "beat": 1.5, "beats": 1.5, "tops": 1.2, "surpass": 1.2, "surpasses": 1.2, "exceed": 1.2,
    "exceeds": 1.2, "record": 0.8, "growth": 0.6, "grows": 0.6, "surge": 1.2, "surges": 1.2,
    "soar": 1.3, "soars": 1.3, "jump": 1.0, "jumps": 1.0, "rally": 1.0, "rallies": 1.0,
    "gain": 0.7, "gains": 0.7, "rise": 0.6, "rises": 0.6, "climb": 0.7, "climbs": 0.7,
    "upgrade": 1.4, "upgraded": 1.4, "upgrades": 1.4, "outperform": 1.1, "overweight": 0.9,
    "buyback": 1.0, "repurchase": 0.8, "dividend": 0.5, "raises": 0.8, "raised": 0.6,
    "approval": 1.1, "approved": 1.1, "approves": 1.1, "wins": 0.9, "win": 0.7,
    "partnership": 0.6, "contract": 0.5, "acquire": 0.4, "profit": 0.6, "profitable": 0.8,
    "strong": 0.7, "robust": 0.7, "bullish": 1.2, "optimistic": 0.7, "boost": 0.8,
    "boosts": 0.8, "expands": 0.5, "launch": 0.3, "launches": 0.3, "breakthrough": 1.0,
    "rebound": 0.8, "recovers": 0.7, "upbeat": 0.9, "accelerates": 0.7, "higher": 0.4,
}

NEGATIVE = {
    "miss": -1.5, "misses": -1.5, "missed": -1.4, "falls": -0.8, "fall": -0.7, "drop": -0.9,
    "drops": -0.9, "plunge": -1.4, "plunges": -1.4, "tumble": -1.2, "tumbles": -1.2,
    "slump": -1.1, "slumps": -1.1, "sink": -1.0, "sinks": -1.0, "slide": -0.8, "slides": -0.8,
    "downgrade": -1.4, "downgraded": -1.4, "downgrades": -1.4, "underperform": -1.1,
    "underweight": -0.9, "cut": -0.8, "cuts": -0.8, "lowers": -0.9, "lowered": -0.9,
    "weak": -0.8, "weaker": -0.8, "loss": -0.8, "losses": -0.8, "lawsuit": -0.9, "sued": -0.9,
    "probe": -1.0, "investigation": -1.0, "fraud": -1.6, "recall": -1.0, "bankruptcy": -1.8,
    "default": -1.3, "layoffs": -0.7, "warns": -1.1, "warning": -1.0, "bearish": -1.2,
    "fine": -0.6, "fined": -1.0, "penalty": -0.9, "delay": -0.7, "delays": -0.7,
    "halt": -1.0, "halts": -1.0, "resigns": -0.8, "concern": -0.6, "concerns": -0.6,
    "fears": -0.8, "selloff": -1.1, "sell-off": -1.1, "decline": -0.8, "declines": -0.8,
    "lower": -0.4, "dilution": -1.0, "restatement": -1.4, "subpoena": -1.2, "short": -0.3,
    "tariff": -0.5, "tariffs": -0.5, "sanctions": -0.6, "crash": -1.5,
}

PHRASES = {
    "raises guidance": 1.6, "raised guidance": 1.6, "raises outlook": 1.4, "raises forecast": 1.4,
    "cuts guidance": -1.6, "lowers guidance": -1.6, "cuts forecast": -1.5, "lowers forecast": -1.5,
    "price target raised": 1.2, "raises price target": 1.2, "price target cut": -1.2,
    "cuts price target": -1.2, "better than expected": 1.3, "worse than expected": -1.3,
    "above estimates": 1.2, "below estimates": -1.2, "going concern": -1.8,
    "share buyback": 1.0, "stock split": 0.6, "fda approval": 1.4, "chapter 11": -1.9,
    "sec charges": -1.4, "record revenue": 1.2, "profit warning": -1.5,
}

NEGATORS = {"not", "no", "never", "without", "fails", "failed", "fail", "isn't", "wasn't", "didn't"}
_WORD = re.compile(r"[a-z][a-z'\-]*")


def score(text: str) -> float:
    """Return sentiment in [-1, 1]."""
    if not text:
        return 0.0
    low = text.lower()
    total = 0.0
    hits = 0
    for phrase, w in PHRASES.items():
        if phrase in low:
            total += w
            hits += 1
            low = low.replace(phrase, " ")
    words = _WORD.findall(low)
    for i, w in enumerate(words):
        v = POSITIVE.get(w) or NEGATIVE.get(w)
        if v is None:
            continue
        window = words[max(0, i - 3):i]
        if any(n in NEGATORS for n in window):
            v = -0.6 * v
        total += v
        hits += 1
    if hits == 0:
        return 0.0
    return math.tanh(total / (1.5 + 0.25 * hits))
