"""X (Twitter) API v2 reader for official news accounts.

Needs a bearer token from developer.x.com (a paid tier is needed for reading
timelines). Without a token this module is skipped and RSS feeds cover news.
Read-only: it only calls GET user lookup and GET user timeline.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from ..config import data_dir
from ..net import get

log = logging.getLogger(__name__)
API = "https://api.x.com/2"
_MENTION = re.compile(r"(?<![\w@])@([A-Za-z0-9_]{2,15})")


def _id_cache() -> dict[str, str]:
    p = data_dir() / "x_user_ids.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _save_id_cache(c: dict[str, str]) -> None:
    (data_dir() / "x_user_ids.json").write_text(json.dumps(c, indent=2), encoding="utf-8")


def user_id(handle: str, token: str) -> str | None:
    cache = _id_cache()
    if handle.lower() in cache:
        return cache[handle.lower()]
    r = get(f"{API}/users/by/username/{handle}", headers={"Authorization": f"Bearer {token}"})
    if not r.ok:
        log.warning("X user lookup %s failed: HTTP %s", handle, r.status_code)
        return None
    uid = r.json().get("data", {}).get("id")
    if uid:
        cache[handle.lower()] = uid
        _save_id_cache(cache)
    return uid


def parse_tweets(source: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
    items = []
    handle = source.get("handle", "")
    for t in payload.get("data", []) or []:
        text = t.get("text", "")
        urls = [u.get("expanded_url") for u in (t.get("entities", {}) or {}).get("urls", []) if u.get("expanded_url")]
        items.append({
            "id": f"{source['id']}:{t['id']}",
            "source_id": source["id"],
            "kind": "news",
            "title": text[:280],
            "summary": text,
            "url": f"https://x.com/{handle}/status/{t['id']}",
            "published_at": t.get("created_at", ""),
            "meta": {"links": urls, "mentions": _MENTION.findall(text)},
        })
    return items


def fetch(source: dict[str, Any], token: str, max_results: int = 20) -> list[dict[str, Any]]:
    if not token:
        return []
    uid = user_id(source["handle"], token)
    if not uid:
        return []
    r = get(
        f"{API}/users/{uid}/tweets",
        headers={"Authorization": f"Bearer {token}"},
        params={"max_results": max_results, "tweet.fields": "created_at,entities",
                "exclude": "retweets,replies"},
        min_interval=1.0,
    )
    if not r.ok:
        log.warning("X timeline %s failed: HTTP %s", source["handle"], r.status_code)
        return []
    return parse_tweets(source, r.json())


def profile_info(handle: str, token: str) -> dict[str, Any]:
    """Same shape as chrome_live.x_profile_info, from the API's user lookup."""
    r = get(f"{API}/users/by/username/{handle}", headers={"Authorization": f"Bearer {token}"},
            params={"user.fields": "name,description,protected,verified"})
    data = r.json().get("data") if r.ok else None
    if not data:
        return {"exists": False}
    return {"exists": True, "name": data.get("name", ""), "bio": data.get("description", ""),
            "protected": bool(data.get("protected")), "verified": bool(data.get("verified")),
            "suspended": False}
