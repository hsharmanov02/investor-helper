"""Find stock tickers mentioned in text.

Uses three signals: cashtags ($AAPL), exchange tags "(NASDAQ: AAPL)", and
company names from the SEC ticker list (cached locally, refreshed weekly).
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

from ..config import data_dir

log = logging.getLogger(__name__)

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

# Common names that appear in headlines without the legal suffix.
BUILTIN_NAMES = {
    "apple": "AAPL", "microsoft": "MSFT", "nvidia": "NVDA", "amazon": "AMZN",
    "alphabet": "GOOGL", "google": "GOOGL", "meta platforms": "META", "facebook": "META",
    "tesla": "TSLA", "berkshire hathaway": "BRK-B", "broadcom": "AVGO", "jpmorgan": "JPM",
    "visa": "V", "mastercard": "MA", "walmart": "WMT", "exxon": "XOM", "exxonmobil": "XOM",
    "chevron": "CVX", "unitedhealth": "UNH", "johnson & johnson": "JNJ", "procter & gamble": "PG",
    "home depot": "HD", "costco": "COST", "netflix": "NFLX", "oracle": "ORCL", "salesforce": "CRM",
    "adobe": "ADBE", "amd": "AMD", "advanced micro devices": "AMD", "intel": "INTC",
    "qualcomm": "QCOM", "boeing": "BA", "coca-cola": "KO", "pepsico": "PEP", "mcdonald's": "MCD",
    "disney": "DIS", "nike": "NKE", "starbucks": "SBUX", "pfizer": "PFE", "moderna": "MRNA",
    "eli lilly": "LLY", "merck": "MRK", "abbvie": "ABBV", "goldman sachs": "GS",
    "morgan stanley": "MS", "bank of america": "BAC", "wells fargo": "WFC", "citigroup": "C",
    "palantir": "PLTR", "uber": "UBER", "airbnb": "ABNB", "paypal": "PYPL", "shopify": "SHOP",
    "snowflake": "SNOW", "coinbase": "COIN", "micron": "MU", "arm holdings": "ARM",
    "taiwan semiconductor": "TSM", "tsmc": "TSM", "asml": "ASML", "alibaba": "BABA",
    "caterpillar": "CAT", "lockheed martin": "LMT", "general motors": "GM", "ford": "F",
    "ibm": "IBM", "cisco": "CSCO", "at&t": "T", "verizon": "VZ", "comcast": "CMCSA",
    "target": "TGT", "spotify": "SPOT", "dell": "DELL", "super micro": "SMCI",
}

# Uppercase words that look like tickers but usually aren't.
STOP_TICKERS = {
    "A", "I", "CEO", "CFO", "IPO", "SEC", "FDA", "GDP", "CPI", "US", "USA", "UK", "EU", "AI",
    "ETF", "Q1", "Q2", "Q3", "Q4", "EPS", "FED", "FOMC", "PCE", "PPI", "NYSE", "LLC", "INC",
    "THE", "AND", "FOR", "NEW", "ON", "AT", "IT", "BE", "ALL", "ARE", "IS", "OR", "NOW", "BY",
}

_CASHTAG = re.compile(r"\$([A-Z]{1,5}(?:\.[A-Z])?)\b")
_EXCHANGE = re.compile(
    r"\((?:NASDAQ|NYSE|NYSE American|AMEX|Nasdaq|NasdaqGS|NasdaqGM|LSE|TSX|OTC)\s*:\s*([A-Z]{1,5}(?:\.[A-Z])?)\)"
)

_SUFFIX = re.compile(r"[,.]?\s+(inc|corp|corporation|co|company|ltd|plc|holdings|group|sa|nv|ag|lp)\.?$", re.I)


class TickerFinder:
    def __init__(self, names: dict[str, str] | None = None, known: set[str] | None = None):
        self.names = dict(BUILTIN_NAMES)
        if names:
            self.names.update(names)
        self.known = set(known or set()) | set(self.names.values())
        self.cik_to_ticker: dict[str, str] = {}
        self._name_re = self._compile()

    def _compile(self) -> re.Pattern[str] | None:
        if not self.names:
            return None
        # Longest names first so "bank of america" wins over "america".
        parts = sorted((re.escape(n) for n in self.names if len(n) >= 3), key=len, reverse=True)
        return re.compile(r"\b(" + "|".join(parts) + r")\b", re.I)

    def load_sec_list(self, raw: dict) -> None:
        """Load SEC company_tickers.json content (dict of {idx: {cik_str, ticker, title}})."""
        added = {}
        for row in raw.values():
            t = str(row.get("ticker", "")).upper().replace(".", "-")
            title = str(row.get("title", ""))
            cik = str(row.get("cik_str", "")).zfill(10)
            if not t:
                continue
            self.known.add(t)
            self.cik_to_ticker.setdefault(cik, t)
            clean = _SUFFIX.sub("", title).strip().lower()
            # Only multi-word or distinctive names, to avoid matching common words.
            if len(clean) >= 6 and clean not in added and " " in clean:
                added[clean] = t
        for k, v in added.items():
            self.names.setdefault(k, v)
        self._name_re = self._compile()

    def find(self, text: str) -> list[str]:
        if not text:
            return []
        found: list[str] = []

        def add(t: str) -> None:
            t = t.upper().replace(".", "-")
            if t not in STOP_TICKERS and t not in found:
                found.append(t)

        for m in _CASHTAG.finditer(text):
            add(m.group(1))
        for m in _EXCHANGE.finditer(text):
            add(m.group(1))
        if self._name_re:
            for m in self._name_re.finditer(text):
                t = self.names.get(m.group(1).lower())
                if t:
                    add(t)
        return found


def load_finder(sec_user_agent: str, fetch: bool = True) -> TickerFinder:
    """Build a finder, using a cached SEC ticker list refreshed weekly."""
    cache: Path = data_dir() / "company_tickers.json"
    finder = TickerFinder()
    raw = None
    if cache.exists() and time.time() - cache.stat().st_mtime < 7 * 86400:
        raw = json.loads(cache.read_text(encoding="utf-8"))
    elif fetch:
        try:
            from ..net import get
            r = get(SEC_TICKERS_URL, headers={"User-Agent": sec_user_agent}, timeout=30)
            if r.ok:
                raw = r.json()
                cache.write_text(json.dumps(raw), encoding="utf-8")
        except Exception as exc:  # offline is fine; builtin names still work
            log.warning("SEC ticker list unavailable: %s", exc)
            if cache.exists():
                raw = json.loads(cache.read_text(encoding="utf-8"))
    if raw:
        finder.load_sec_list(raw)
    return finder
