"""IPO and earnings calendars (Nasdaq public calendar API), plus user events."""
from __future__ import annotations

import hashlib
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from ..net import get

log = logging.getLogger(__name__)

NASDAQ_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}


def _to_float(v: Any) -> float | None:
    try:
        return float(str(v).replace("$", "").replace(",", "").split("-")[0])
    except (TypeError, ValueError):
        return None


def parse_ipo_calendar(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = (payload or {}).get("data") or {}
    out = []
    for section in ("upcoming", "priced", "filed"):
        block = data.get(section) or {}
        rows = block.get("upcomingTable", {}).get("rows") if section == "upcoming" else (block.get("rows"))
        for row in rows or []:
            sym = (row.get("proposedTickerSymbol") or "").strip().upper()
            name = row.get("companyName", "")
            when = row.get("expectedPriceDate") or row.get("pricedDate") or row.get("filedDate") or ""
            starts = None
            for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
                try:
                    starts = datetime.strptime(when, fmt).date().isoformat()
                    break
                except ValueError:
                    pass
            out.append({
                "id": f"ipo:{section}:{sym or name}",
                "status": section,
                "ticker": sym,
                "company": name,
                "exchange": row.get("proposedExchange", ""),
                "price_range": row.get("proposedSharePrice", "") or row.get("proposedSharePrice", ""),
                "shares": row.get("sharesOffered", ""),
                "value": row.get("dollarValueOfSharesOffered", ""),
                "date": starts,
            })
    return out


def fetch_ipo_calendar(month: date | None = None) -> list[dict[str, Any]]:
    m = (month or date.today()).strftime("%Y-%m")
    r = get("https://api.nasdaq.com/api/ipo/calendar", params={"date": m}, headers=NASDAQ_HEADERS)
    if not r.ok:
        log.warning("IPO calendar HTTP %s", r.status_code)
        return []
    return parse_ipo_calendar(r.json())


def parse_earnings(payload: dict[str, Any], day: str) -> list[dict[str, Any]]:
    rows = ((payload or {}).get("data") or {}).get("rows") or []
    out = []
    for row in rows:
        sym = (row.get("symbol") or "").upper()
        if not sym:
            continue
        timing = row.get("time", "")
        hour = "13:00" if "pre" in timing else "21:00" if "after" in timing else "16:00"
        out.append({
            "id": f"earnings:{sym}:{day}",
            "title": f"{sym} earnings ({row.get('name', '')}), EPS forecast {row.get('epsForecast', 'n/a')}",
            "starts_at": f"{day}T{hour}:00+00:00",
            "url": f"https://www.nasdaq.com/market-activity/stocks/{sym.lower()}/earnings",
            "source_id": "calendar:earnings",
            "tickers": [sym],
        })
    return out


def fetch_earnings(days_ahead: int = 7) -> list[dict[str, Any]]:
    out = []
    for i in range(days_ahead):
        d = (date.today() + timedelta(days=i)).isoformat()
        r = get("https://api.nasdaq.com/api/calendar/earnings", params={"date": d},
                headers=NASDAQ_HEADERS, min_interval=0.5)
        if r.ok:
            out.extend(parse_earnings(r.json(), d))
    return out


def custom_events(defs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for d in defs:
        title = d.get("title", "").strip()
        if not title:
            continue
        key = hashlib.sha1(f"{title}{d.get('starts_at')}".encode()).hexdigest()[:12]
        out.append({
            "id": f"custom:{key}", "title": title, "starts_at": d.get("starts_at"),
            "url": d.get("url", ""), "source_id": "user", "tickers": d.get("tickers", []),
        })
    return out


def is_soon(starts_at: str | None, within_hours: float = 24) -> bool:
    if not starts_at:
        return False
    try:
        t = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    delta = (t - datetime.now(timezone.utc)).total_seconds() / 3600
    return 0 <= delta <= within_hours
