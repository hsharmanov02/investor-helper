"""Settings and source lists, stored under the user's app-data folder.

API keys live only in `settings.json` on the user's machine and are never
checked into the repository or sent anywhere except the API they belong to.
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

APP_DIR_NAME = "InvestorHelper"


def data_dir() -> Path:
    """Per-user writable folder (``%APPDATA%\\InvestorHelper`` on Windows)."""
    override = os.environ.get("INVESTOR_HELPER_HOME")
    if override:
        base = Path(override)
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home())) / APP_DIR_NAME
    else:
        base = Path.home() / ".investor_helper"
    base.mkdir(parents=True, exist_ok=True)
    return base


def bundled_path(*parts: str) -> Path:
    """Path to a file shipped with the app (works inside a PyInstaller bundle)."""
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return root.joinpath(*parts)


@dataclass
class Settings:
    # Trading212: read-only use. Only GET endpoints are ever called.
    trading212_api_key: str = ""
    # Newer Trading212 keys come as key + secret (HTTP Basic). Leave blank for old single keys.
    trading212_api_secret: str = ""
    trading212_environment: str = "live"  # "live" or "demo"
    # Live access through the user's own logged-in Chrome (started with
    # --remote-debugging-port=9222). Used for X and Trading212; read-only.
    use_chrome_cdp: bool = True
    chrome_cdp_url: str = "http://localhost:9222"
    # Shared lock file; blank = %USERPROFILE%\\InvestorHelper\\chrome.lock
    chrome_lock_path: str = ""
    # X is read mainly from your Following timeline (one page). Individual profile
    # reads and identity checks are rationed to stay well clear of X rate limits.
    x_enabled: bool = True
    x_profile_reads_per_cycle: int = 4
    x_verifications_per_cycle: int = 4
    x_seconds_between_pages: int = 12
    # X (Twitter) API v2 bearer token. Optional; RSS fallback is used without it.
    x_bearer_token: str = ""
    # SEC requires a descriptive User-Agent with contact info.
    sec_user_agent: str = "InvestorHelper/0.1 (personal research; contact: you@example.com)"
    # Scheduling
    refresh_minutes: int = 30
    report_hour_local: int = 7  # daily thread report time
    # Prediction
    horizon_days: int = 3
    max_report_items: int = 15
    # Watchlist tickers to always score (besides those mentioned in news)
    watchlist: list[str] = field(default_factory=lambda: ["AAPL", "MSFT", "NVDA", "SPY"])
    # Notifications
    notify_events: bool = True
    notify_reports: bool = True
    notify_min_score: float = 0.6
    # Source rotation: disable sources whose hit rate stays poor, trial new ones.
    auto_rotate_sources: bool = True
    # User-defined events: [{"title": ..., "starts_at": "2026-11-12T14:00:00Z", "url": ...}]
    custom_events: list[dict] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "Settings":
        known = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
        return cls(**known)


def settings_path() -> Path:
    return data_dir() / "settings.json"


def load_settings() -> Settings:
    p = settings_path()
    if p.exists():
        try:
            return Settings.from_json(json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, TypeError):
            pass
    return Settings()


def save_settings(s: Settings) -> None:
    settings_path().write_text(json.dumps(s.to_json(), indent=2), encoding="utf-8")


# ---------------------------------------------------------------- sources.json
# Each source: {"id", "kind", "name", "url"|"handle", "enabled", "tags"}
#   kind: "rss" | "x" | "edgar" | "ipo" | "page"
DEFAULT_SOURCES: list[dict[str, Any]] = [
    {"id": "rss:cnbc-top", "kind": "rss", "name": "CNBC Top News",
     "url": "https://www.cnbc.com/id/100003114/device/rss/rss.html", "enabled": True},
    {"id": "rss:cnbc-earnings", "kind": "rss", "name": "CNBC Earnings",
     "url": "https://www.cnbc.com/id/15839135/device/rss/rss.html", "enabled": True},
    {"id": "rss:marketwatch-top", "kind": "rss", "name": "MarketWatch Top Stories",
     "url": "https://feeds.content.dowjones.io/public/rss/mw_topstories", "enabled": True},
    {"id": "rss:wsj-markets", "kind": "rss", "name": "WSJ Markets",
     "url": "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain", "enabled": True},
    {"id": "rss:ft-companies", "kind": "rss", "name": "Financial Times Companies",
     "url": "https://www.ft.com/companies?format=rss", "enabled": True},
    {"id": "rss:sec-press", "kind": "rss", "name": "SEC Press Releases",
     "url": "https://www.sec.gov/news/pressreleases.rss", "enabled": True},
    {"id": "rss:fed-press", "kind": "rss", "name": "Federal Reserve Press Releases",
     "url": "https://www.federalreserve.gov/feeds/press_all.xml", "enabled": True},
    {"id": "rss:bls", "kind": "rss", "name": "US Bureau of Labor Statistics",
     "url": "https://www.bls.gov/feed/bls_latest.rss", "enabled": True},
    {"id": "rss:bbc-business", "kind": "rss", "name": "BBC Business",
     "url": "https://feeds.bbci.co.uk/news/business/rss.xml", "enabled": True},
    {"id": "rss:investing-com-news", "kind": "rss", "name": "Investing.com Stock Market News",
     "url": "https://www.investing.com/rss/news_25.rss", "enabled": True},
    # X accounts (official outlets). Read live through Chrome, or the X API if a token is set.
    {"id": "x:Reuters", "kind": "x", "expect": ["Reuters"], "name": "Reuters", "handle": "Reuters", "enabled": True},
    {"id": "x:CNBC", "kind": "x", "expect": ["CNBC"], "name": "CNBC", "handle": "CNBC", "enabled": True},
    {"id": "x:business", "kind": "x", "expect": ["Bloomberg"], "name": "Bloomberg", "handle": "business", "enabled": True},
    {"id": "x:WSJmarkets", "kind": "x", "expect": ["WSJ", "Wall Street Journal"], "name": "WSJ Markets", "handle": "WSJmarkets", "enabled": True},
    {"id": "x:FT", "kind": "x", "expect": ["Financial Times"], "name": "Financial Times", "handle": "FT", "enabled": True},
    {"id": "x:SECGov", "kind": "x", "expect": ["Securities and Exchange", "SEC"], "name": "U.S. SEC", "handle": "SECGov", "enabled": True},
    {"id": "x:federalreserve", "kind": "x", "expect": ["Federal Reserve"], "name": "Federal Reserve", "handle": "federalreserve", "enabled": True},
    {"id": "x:MarketWatch", "kind": "x", "expect": ["MarketWatch"], "name": "MarketWatch", "handle": "MarketWatch", "enabled": True},
    # EDGAR and IPO feeds are built in; these rows let the user toggle them.
    {"id": "edgar:recent", "kind": "edgar", "name": "SEC EDGAR recent filings (8-K, 10-Q, 10-K, Form 4)",
     "forms": ["8-K", "10-Q", "10-K", "4", "S-1", "424B4", "SC 13D", "SC 13G"], "enabled": True},
    {"id": "ipo:edgar", "kind": "ipo", "name": "IPO calendar (Nasdaq) + EDGAR S-1/424B4 filings", "enabled": True},
    # Tracked pages / events. "watch" pages notify when the content changes.
    {"id": "page:fomc-calendar", "kind": "page", "name": "FOMC meeting calendar",
     "url": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm", "enabled": True},
    {"id": "page:bls-schedule", "kind": "page", "name": "BLS release schedule",
     "url": "https://www.bls.gov/schedule/news_release/", "enabled": True},
]


# Vetted official outlets held in reserve. When auto-rotation drops a poorly
# performing source of the same kind, the next unused one here is trialled.
CANDIDATE_SOURCES: list[dict[str, Any]] = [
    {"id": "rss:cnbc-markets", "kind": "rss", "name": "CNBC Markets",
     "url": "https://www.cnbc.com/id/20910258/device/rss/rss.html", "enabled": True},
    {"id": "rss:marketwatch-pulse", "kind": "rss", "name": "MarketWatch Market Pulse",
     "url": "https://feeds.content.dowjones.io/public/rss/mw_marketpulse", "enabled": True},
    {"id": "rss:yahoo-finance", "kind": "rss", "name": "Yahoo Finance News",
     "url": "https://finance.yahoo.com/news/rssindex", "enabled": True},
    {"id": "rss:nasdaq-markets", "kind": "rss", "name": "Nasdaq Markets",
     "url": "https://www.nasdaq.com/feed/rssoutbound?category=Markets", "enabled": True},
    {"id": "rss:seekingalpha-currents", "kind": "rss", "name": "Seeking Alpha Market Currents",
     "url": "https://seekingalpha.com/market_currents.xml", "enabled": True},
    {"id": "rss:ecb-press", "kind": "rss", "name": "European Central Bank Press",
     "url": "https://www.ecb.europa.eu/rss/press.html", "enabled": True},
    {"id": "rss:boe-news", "kind": "rss", "name": "Bank of England News",
     "url": "https://www.bankofengland.co.uk/rss/news", "enabled": True},
    {"id": "x:YahooFinance", "kind": "x", "expect": ["Yahoo Finance"], "name": "Yahoo Finance", "handle": "YahooFinance", "enabled": True},
    {"id": "x:Benzinga", "kind": "x", "expect": ["Benzinga"], "name": "Benzinga", "handle": "Benzinga", "enabled": True},
    {"id": "x:BloombergTV", "kind": "x", "expect": ["Bloomberg"], "name": "Bloomberg TV", "handle": "BloombergTV", "enabled": True},
    {"id": "x:ReutersBiz", "kind": "x", "expect": ["Reuters"], "name": "Reuters Business", "handle": "ReutersBiz", "enabled": True},
    {"id": "x:NasdaqExchange", "kind": "x", "expect": ["Nasdaq"], "name": "Nasdaq", "handle": "Nasdaq", "enabled": True},
    {"id": "x:NYSE", "kind": "x", "expect": ["NYSE", "New York Stock Exchange"], "name": "NYSE", "handle": "NYSE", "enabled": True},
]


def sources_path() -> Path:
    return data_dir() / "sources.json"


# Extra names to look for in a profile's display name (the vetted list's names
# carry descriptions in brackets, and some outlets use short brand names on X).
X_ALIASES: dict[str, list[str]] = {
    "markets": ["Bloomberg"], "WSJ": ["Wall Street Journal", "WSJ"], "CNBCnow": ["CNBC"],
    "SquawkCNBC": ["Squawk"], "nytimesbusiness": ["New York Times", "NYT"], "BBCBusiness": ["BBC"],
    "Investingcom": ["Investing.com"], "TheFCA": ["Financial Conduct Authority", "FCA"],
    "USTreasury": ["Treasury"], "BLS_gov": ["Labor Statistics", "BLS"],
    "BEA_News": ["Economic Analysis", "BEA"], "EIAgov": ["Energy Information", "EIA"],
    "IMFNews": ["IMF", "International Monetary Fund"], "LSEGplc": ["LSEG", "London Stock Exchange"],
    "OPECSecretariat": ["OPEC"], "IEA": ["IEA", "International Energy Agency"],
    "IPOtweet": ["Renaissance"], "eWhispers": ["Earnings Whispers"], "CMEGroup": ["CME"],
    "Trading212": ["Trading 212", "Trading212"], "SECGov": ["Securities and Exchange", "SEC"],
    "CFTC": ["CFTC", "Commodity Futures"], "NYSE": ["NYSE", "New York Stock Exchange"],
    "FINRA": ["FINRA"], "ecb": ["European Central Bank", "ECB"], "bankofengland": ["Bank of England"],
}

# Feeds known to be dead or blocking; disabled on load.
DEAD_SOURCES = {"rss:reuters-business": "feeds.reuters.com no longer exists",
                "rss:bls": "bls.gov blocks automated feed readers (HTTP 403)"}


def _expect_for(handle: str, name: str) -> list[str]:
    clean = re.sub(r"\s*\(.*?\)", "", name).strip()
    out = [clean, re.sub(r"^(The|U\.S\.|UK)\s+", "", clean)]
    out += X_ALIASES.get(handle, [])
    return list(dict.fromkeys(e for e in out if e))


def merge_vetted_x_accounts(sources: list[dict[str, Any]]) -> bool:
    """Add accounts from the bundled vetted list once per list version.

    Every one is still checked on X (public, right display name) before it is read.
    Accounts the user removed are not re-added until the list version changes.
    """
    try:
        vetted = json.loads(bundled_path("investor_helper", "data", "official_x_accounts.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    marker = data_dir() / "vetted_x_version.txt"
    version = f"{vetted.get('version')}-{vetted.get('created')}-{len(vetted.get('accounts', []))}"
    if marker.exists() and marker.read_text(encoding="utf-8").strip() == version:
        return False
    have = {str(s.get("handle", "")).lower() for s in sources if s["kind"] == "x"}
    for a in vetted.get("accounts", []):
        h = a["handle"]
        if h.lower() in have:
            continue
        sources.append({"id": f"x:{h}", "kind": "x", "name": re.sub(r"\s*\(.*?\)", "", a["name"]).strip(),
                        "handle": h, "expect": _expect_for(h, a["name"]), "enabled": True,
                        "prior_weight": float(a.get("weight", 0.6)), "category": a.get("category", ""),
                        "seeded_from": "official_x_accounts"})
    marker.write_text(version, encoding="utf-8")
    return True


def load_sources() -> list[dict[str, Any]]:
    p = sources_path()
    sources = None
    if p.exists():
        try:
            sources = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            sources = None
    if sources is None:
        sources = [dict(s) for s in DEFAULT_SOURCES]
        save_sources(sources)
    changed = merge_vetted_x_accounts(sources)
    changed = migrate_sources(sources) or changed
    if changed:
        save_sources(sources)
    return sources


def migrate_sources(sources: list[dict[str, Any]]) -> bool:
    """Apply fixes to an existing sources.json: dead feeds, name-check aliases, false rejections."""
    changed = False
    for src in sources:
        reason = src.get("disabled_reason", "")
        if src["id"] in DEAD_SOURCES and src.get("enabled"):
            src["enabled"] = False
            src["disabled_reason"] = DEAD_SOURCES[src["id"]]
            changed = True
        if src["kind"] != "x":
            continue
        h = src.get("handle", "")
        extra = [a for a in X_ALIASES.get(h, []) if a not in src.get("expect", [])]
        if extra:
            src["expect"] = list(src.get("expect", [])) + extra
            changed = True
        # Re-check accounts rejected by the old, stricter name rule or while X was rate limiting.
        if not src.get("enabled") and reason.startswith("failed check") and (
                "does not exist or is suspended" in reason or ("does not match" in reason and extra)):
            src["enabled"] = True
            src.pop("disabled_reason", None)
            src.pop("verified_at", None)
            changed = True
    return changed


def prior_weight(source: dict[str, Any]) -> float:
    """Starting weight before any predictions are graded (vetted list 0-1 -> 0.6-1.2)."""
    pw = source.get("prior_weight")
    return 1.0 if pw is None else 0.6 + 0.6 * float(pw)


def save_sources(sources: list[dict[str, Any]]) -> None:
    sources_path().write_text(json.dumps(sources, indent=2), encoding="utf-8")
