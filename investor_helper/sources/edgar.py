"""SEC EDGAR: recent filings feed, IPO pipeline and company fundamentals.

The SEC asks for a User-Agent with contact details and at most 10 requests per
second; we stay well under that.
"""
from __future__ import annotations

import logging
import re
from typing import Any

import feedparser

from ..net import get
from .rss import clean_html, _entry_time

log = logging.getLogger(__name__)

CURRENT_FEED = "https://www.sec.gov/cgi-bin/browse-edgar"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

# How strongly each form type tends to matter for price, and its usual direction.
FORM_INFO = {
    "8-K": ("Material event", 0.6),
    "10-Q": ("Quarterly report", 0.5),
    "10-K": ("Annual report", 0.5),
    "4": ("Insider transaction", 0.4),
    "S-1": ("IPO registration", 0.3),
    "424B4": ("IPO prospectus (pricing)", 0.4),
    "SC 13D": ("Activist stake (>5%)", 0.7),
    "SC 13G": ("Passive stake (>5%)", 0.3),
}

_TITLE = re.compile(r"^(?P<form>[\w\- /]+?) - (?P<name>.+?) \((?P<cik>\d{10})\)")
# 8-K item codes and the typical sentiment of each.
ITEM_SENTIMENT = {
    "1.01": 0.3,   # material agreement
    "1.02": -0.3,  # termination of agreement
    "1.03": -0.9,  # bankruptcy
    "2.02": 0.0,   # results (direction from text)
    "2.05": -0.3,  # exit / restructuring costs
    "2.06": -0.5,  # impairments
    "3.01": -0.7,  # delisting notice
    "4.02": -0.8,  # non-reliance on financials
    "5.02": -0.1,  # officer departure/appointment
    "7.01": 0.0,
    "8.01": 0.0,
}


def ua_headers(user_agent: str) -> dict[str, str]:
    return {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}


def parse_current_feed(source_id: str, content: bytes | str, cik_to_ticker: dict[str, str]) -> list[dict[str, Any]]:
    feed = feedparser.parse(content)
    out = []
    for e in feed.entries:
        title = clean_html(e.get("title", ""))
        m = _TITLE.match(title)
        if not m:
            continue
        form = m.group("form").strip()
        cik = m.group("cik")
        ticker = cik_to_ticker.get(cik)
        summary = clean_html(e.get("summary", ""))
        items = re.findall(r"Item (\d\.\d\d)", summary)
        sent = sum(ITEM_SENTIMENT.get(i, 0.0) for i in items)
        sent = max(-1.0, min(1.0, sent))
        label, importance = FORM_INFO.get(form, (form, 0.2))
        out.append({
            "id": f"edgar:{e.get('id') or e.get('link')}",
            "source_id": source_id,
            "kind": "ipo" if form in ("S-1", "424B4", "F-1") else "filing",
            "title": f"{label}: {m.group('name')} ({form})",
            "summary": summary[:800],
            "url": e.get("link", ""),
            "published_at": _entry_time(e),
            "tickers": [ticker] if ticker else [],
            "sentiment": sent,
            "meta": {"form": form, "cik": cik, "company": m.group("name"),
                     "items": items, "importance": importance},
        })
    return out


def fetch_current(source: dict[str, Any], user_agent: str, cik_to_ticker: dict[str, str]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for form in source.get("forms", ["8-K"]):
        r = get(CURRENT_FEED, headers=ua_headers(user_agent), min_interval=0.25,
                params={"action": "getcurrent", "type": form, "count": 40, "output": "atom"})
        if r.ok:
            items.extend(parse_current_feed(source["id"], r.content, cik_to_ticker))
        else:
            raise RuntimeError(f"EDGAR {form} feed HTTP {r.status_code}")
    return items


# ------------------------------------------------------------ fundamentals
def _quarterly(facts: dict[str, Any], concept: str) -> list[tuple[str, float]]:
    try:
        units = facts["facts"]["us-gaap"][concept]["units"]
    except KeyError:
        return []
    vals = units.get("USD") or units.get("USD/shares") or []
    q = {}
    for v in vals:
        if v.get("form") in ("10-Q", "10-K") and v.get("fp") and v.get("end") and "frame" in v:
            # Frames like CY2025Q3 are single-quarter values.
            if re.fullmatch(r"CY\d{4}Q\d", v["frame"]):
                q[v["frame"]] = float(v["val"])
    return sorted(q.items())


def fundamentals_from_facts(facts: dict[str, Any]) -> dict[str, Any]:
    """Year-over-year revenue and net income growth from the latest quarter."""
    out: dict[str, Any] = {}
    for key, concepts in {
        "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"],
        "net_income": ["NetIncomeLoss"],
    }.items():
        series: list[tuple[str, float]] = []
        for c in concepts:
            series = _quarterly(facts, c)
            if len(series) >= 5:
                break
        if len(series) >= 5:
            latest_frame, latest = series[-1]
            year, qn = latest_frame[2:6], latest_frame[-1]
            prior = dict(series).get(f"CY{int(year) - 1}Q{qn}")
            if prior:
                out[f"{key}_yoy"] = (latest - prior) / abs(prior)
            out[f"{key}_latest"] = latest
            out[f"{key}_frame"] = latest_frame
    return out


def fundamental_score(f: dict[str, Any]) -> float:
    """Map growth figures to a small score in [-1, 1]."""
    s = 0.0
    if "revenue_yoy" in f:
        s += max(-1.0, min(1.0, f["revenue_yoy"] * 3))
    if "net_income_yoy" in f:
        s += 0.5 * max(-1.0, min(1.0, f["net_income_yoy"]))
    if f.get("net_income_latest", 0) < 0:
        s -= 0.2
    return max(-1.0, min(1.0, s / 1.5))


def fetch_fundamentals(cik: str, user_agent: str) -> dict[str, Any]:
    r = get(FACTS_URL.format(cik=cik.zfill(10)), headers=ua_headers(user_agent), min_interval=0.25)
    if not r.ok:
        return {}
    return fundamentals_from_facts(r.json())
