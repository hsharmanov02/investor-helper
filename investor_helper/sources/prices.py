"""Daily closing prices from free sources (Yahoo chart API, Stooq fallback)."""
from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timezone
from typing import Any

from ..net import get

log = logging.getLogger(__name__)


def parse_yahoo(payload: dict[str, Any]) -> list[tuple[str, float]]:
    try:
        res = payload["chart"]["result"][0]
        ts = res["timestamp"]
        closes = res["indicators"]["quote"][0]["close"]
    except (KeyError, IndexError, TypeError):
        return []
    out = []
    for t, c in zip(ts, closes):
        if c is None:
            continue
        out.append((datetime.fromtimestamp(t, tz=timezone.utc).date().isoformat(), float(c)))
    return out


def parse_stooq(text: str) -> list[tuple[str, float]]:
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        try:
            out.append((row["Date"], float(row["Close"])))
        except (KeyError, ValueError):
            continue
    return out


def fetch_daily(ticker: str, range_: str = "6mo") -> list[tuple[str, float]]:
    try:
        r = get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
                params={"range": range_, "interval": "1d"}, min_interval=0.3)
        if r.ok:
            s = parse_yahoo(r.json())
            if s:
                return s
    except Exception as exc:
        log.info("Yahoo price fetch failed for %s: %s", ticker, exc)
    try:
        sym = ticker.lower().replace("-", ".")
        sym = sym if "." in sym else f"{sym}.us"
        r = get("https://stooq.com/q/d/l/", params={"s": sym, "i": "d"}, min_interval=0.3)
        if r.ok:
            return parse_stooq(r.text)[-130:]
    except Exception as exc:
        log.info("Stooq price fetch failed for %s: %s", ticker, exc)
    return []
