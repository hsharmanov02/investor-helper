"""RSS/Atom news feeds from official outlets."""
from __future__ import annotations

import calendar
import hashlib
import html
import logging
import re
from datetime import datetime, timezone
from typing import Any

import feedparser

from ..net import get

log = logging.getLogger(__name__)
_TAG = re.compile(r"<[^>]+>")


def clean_html(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG.sub(" ", s or ""))).strip()


def _entry_time(e: Any) -> str:
    for key in ("published_parsed", "updated_parsed"):
        t = e.get(key)
        if t:
            return datetime.fromtimestamp(calendar.timegm(t), tz=timezone.utc).isoformat()
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_feed(source: dict[str, Any], content: bytes | str) -> list[dict[str, Any]]:
    feed = feedparser.parse(content)
    items = []
    for e in feed.entries:
        link = e.get("link", "")
        guid = e.get("id") or link or e.get("title", "")
        items.append({
            "id": f"{source['id']}:{hashlib.sha1(guid.encode('utf-8')).hexdigest()[:16]}",
            "source_id": source["id"],
            "kind": "news",
            "title": clean_html(e.get("title", ""))[:400],
            "summary": clean_html(e.get("summary", ""))[:1500],
            "url": link,
            "published_at": _entry_time(e),
        })
    return items


def fetch(source: dict[str, Any]) -> list[dict[str, Any]]:
    r = get(source["url"], timeout=20)
    if not r.ok:
        log.warning("RSS %s returned HTTP %s", source["id"], r.status_code)
        return []
    return parse_feed(source, r.content)
