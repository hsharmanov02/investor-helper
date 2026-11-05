"""Regular trading hours for the main exchanges Trading212 Invest offers."""
from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

# (timezone, open, close). Holidays are not modelled; the report says so.
EXCHANGES = {
    "NYSE/NASDAQ": ("America/New_York", time(9, 30), time(16, 0)),
    "London (LSE)": ("Europe/London", time(8, 0), time(16, 30)),
    "Xetra (Frankfurt)": ("Europe/Berlin", time(9, 0), time(17, 30)),
    "Euronext Paris/Amsterdam": ("Europe/Paris", time(9, 0), time(17, 30)),
    "SIX Swiss": ("Europe/Zurich", time(9, 0), time(17, 30)),
    "Toronto (TSX)": ("America/Toronto", time(9, 30), time(16, 0)),
}


def is_open(exchange: str, now: datetime | None = None) -> bool:
    tz, o, c = EXCHANGES[exchange]
    local = (now or datetime.now(ZoneInfo("UTC"))).astimezone(ZoneInfo(tz))
    return local.weekday() < 5 and o <= local.time() < c


def status(now: datetime | None = None) -> dict[str, bool]:
    return {ex: is_open(ex, now) for ex in EXCHANGES}


def exchange_for(ticker: str) -> str:
    if ticker.endswith(".L"):
        return "London (LSE)"
    if ticker.endswith(".DE"):
        return "Xetra (Frankfurt)"
    if ticker.endswith(".PA") or ticker.endswith(".AS"):
        return "Euronext Paris/Amsterdam"
    if ticker.endswith(".SW"):
        return "SIX Swiss"
    if ticker.endswith(".TO"):
        return "Toronto (TSX)"
    return "NYSE/NASDAQ"
