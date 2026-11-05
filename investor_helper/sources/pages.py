"""Tracked web pages and event extraction.

* Watched pages (FOMC calendar, BLS schedule, investor-relations pages...) are
  fetched, reduced to text and hashed; a change raises a notification.
* Any news text (RSS item or X post) is scanned for scheduled happenings such
  as "earnings call on November 12" or "Fed meeting tomorrow", which become
  events the app reminds you about.
"""
from __future__ import annotations

import difflib
import hashlib
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from ..net import get
from .rss import clean_html

_SCRIPT = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.S | re.I)

EVENT_WORDS = re.compile(
    r"\b(earnings call|earnings|conference call|investor day|shareholder meeting|annual meeting|"
    r"fomc|fed meeting|rate decision|hearing|testimony|webcast|vote|summit|keynote|"
    r"product event|launch event|ipo|pricing|cpi report|jobs report|payrolls|gdp report)\b",
    re.I,
)
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_DATE = re.compile(
    r"\b(?:on\s+)?(?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?\b",
    re.I,
)
_REL = re.compile(r"\b(today|tonight|tomorrow|next week|this week|(?:on |next |this )?(?:monday|tuesday|wednesday|thursday|friday))\b", re.I)


def page_text(html: str) -> str:
    return clean_html(_SCRIPT.sub(" ", html))


def check_page(source: dict[str, Any], previous_hash: str | None) -> dict[str, Any]:
    """Fetch a page; return {'hash', 'text', 'changed', 'excerpt'}."""
    r = get(source["url"], timeout=25)
    text = page_text(r.text) if r.ok else ""
    return compare(text, previous_hash, source.get("_previous_text", ""))


def compare(text: str, previous_hash: str | None, previous_excerpt: str = "") -> dict[str, Any]:
    h = hashlib.sha256(text.encode("utf-8")).hexdigest()
    changed = previous_hash is not None and previous_hash != h and bool(text)
    excerpt = ""
    if changed and previous_excerpt:
        diff = difflib.ndiff(previous_excerpt.split(". "), text[:500].split(". "))
        excerpt = " ".join(d[2:] for d in diff if d.startswith("+ "))[:300]
    return {"hash": h, "text": text, "changed": changed, "excerpt": excerpt or text[:300]}


def resolve_date(phrase: str, today: date | None = None) -> date | None:
    today = today or datetime.now(timezone.utc).date()
    p = phrase.lower().strip()
    m = _DATE.fullmatch(p) or _DATE.search(p)
    if m:
        mon = MONTHS[m.group("mon")[:3].lower()]
        day = int(m.group("day"))
        try:
            d = date(today.year, mon, day)
        except ValueError:
            return None
        if d < today - timedelta(days=30):  # "Jan 10" said in December means next year
            d = date(today.year + 1, mon, day)
        return d
    if "today" in p or "tonight" in p:
        return today
    if "tomorrow" in p:
        return today + timedelta(days=1)
    if "next week" in p:
        return today + timedelta(days=7 - today.weekday())
    if "this week" in p:
        return today
    for i, wd in enumerate(WEEKDAYS):
        if wd in p:
            ahead = (i - today.weekday()) % 7
            if ahead == 0 or p.startswith("next"):
                ahead = ahead or 7
            return today + timedelta(days=ahead)
    return None


def extract_events(item: dict[str, Any], today: date | None = None) -> list[dict[str, Any]]:
    """Find scheduled happenings in a news item's text."""
    text = f"{item.get('title', '')}. {item.get('summary', '')}"
    out = []
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        kw = EVENT_WORDS.search(sentence)
        if not kw:
            continue
        dm = _DATE.search(sentence) or _REL.search(sentence)
        if not dm:
            continue
        d = resolve_date(dm.group(0), today)
        if not d or d < (today or datetime.now(timezone.utc).date()):
            continue
        title = sentence.strip()[:200]
        key = hashlib.sha1(f"{title}{d}".encode()).hexdigest()[:12]
        links = (item.get("meta") or {}).get("links") or []
        out.append({
            "id": f"news-event:{key}",
            "title": title,
            "starts_at": d.isoformat(),
            "url": links[0] if links else item.get("url", ""),
            "source_id": item.get("source_id", ""),
            "tickers": item.get("tickers", []),
        })
    return out
