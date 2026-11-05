"""Orchestrates collection, analysis, learning and reporting. GUI-independent."""
from __future__ import annotations

import json
import logging
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .analysis import learner, sentiment
from .analysis.scoring import rank, score_ticker
from .analysis.tickers import TickerFinder, load_finder
from .config import Settings, data_dir, load_settings, load_sources, prior_weight, save_sources
from .db import Database, now_iso
from .report import build_report
from .sources import calendars, edgar, pages, prices, rss, x_api
from .chrome_lock import ChromeBusy, ChromeLock
from .sources.chrome_live import ChromeLive, ChromeUnavailable, XRateLimited, parse_x_posts, verify_x_profile
from .sources.trading212 import ReadOnlyTrading212, Trading212Error

log = logging.getLogger(__name__)
Notify = Callable[[str, str], None]


class Engine:
    def __init__(self, db: Database | None = None, settings: Settings | None = None,
                 notify: Notify | None = None, finder: TickerFinder | None = None):
        self.db = db or Database()
        self.settings = settings or load_settings()
        self.notify: Notify = notify or (lambda title, body: log.info("NOTIFY %s: %s", title, body))
        self._finder = finder
        self.status: dict[str, str] = {}
        self.last_account: dict[str, Any] | None = None
        self.ipos: list[dict[str, Any]] = []
        self.rotation: list[str] = []
        self.skip_x = False

    # ------------------------------------------------------------- helpers
    @property
    def finder(self) -> TickerFinder:
        if self._finder is None:
            self._finder = load_finder(self.settings.sec_user_agent)
        return self._finder

    def annotate(self, item: dict[str, Any]) -> dict[str, Any]:
        text = f"{item['title']} {item.get('summary', '')}"
        if not item.get("tickers"):
            item["tickers"] = self.finder.find(text)
        if item["kind"] == "news":
            item["sentiment"] = sentiment.score(text)
        return item

    def _ingest(self, items: list[dict[str, Any]]) -> int:
        new = 0
        for it in items:
            self.annotate(it)
            if self.db.upsert_item(it):
                new += 1
                if it["kind"] == "news":
                    for ev in pages.extract_events(it):
                        self.db.upsert_event(ev)
        return new

    # ------------------------------------------------------------- collect
    def collect(self) -> dict[str, int]:
        s = self.settings
        sources = load_sources()
        counts: Counter[str] = Counter()
        x_sources = [src for src in sources if src["kind"] == "x" and src.get("enabled")]

        for src in sources:
            if not src.get("enabled"):
                continue
            try:
                if src["kind"] == "rss":
                    counts["news"] += self._ingest(rss.fetch(src))
                elif src["kind"] == "edgar":
                    counts["filings"] += self._ingest(
                        edgar.fetch_current(src, s.sec_user_agent, self.finder.cik_to_ticker))
                elif src["kind"] == "ipo":
                    self.ipos = calendars.fetch_ipo_calendar()
                elif src["kind"] == "page":
                    self._check_page(src)
                self.status[src["id"]] = "ok"
            except Exception as exc:
                log.warning("Source %s failed: %s", src["id"], exc)
                self.status[src["id"]] = f"error: {exc}"[:200]

        # X and Trading212 live through Chrome; API keys as fallback.
        account = None
        chrome_ok = False
        if s.use_chrome_cdp:
            lock = ChromeLock(s.chrome_lock_path or None)
            try:
                lock.acquire()
                with ChromeLive(s.chrome_cdp_url) as chrome:
                    chrome_ok = True
                    if s.x_enabled and not self.skip_x:
                        counts["x"] += self._collect_x_chrome(chrome, sources, x_sources)
                    if not s.trading212_api_key:
                        try:
                            account = chrome.read_trading212()
                        except Exception as exc:
                            self.status["trading212"] = f"error: {exc}"[:200]
            except ChromeBusy as exc:
                self.status["chrome"] = f"skipped this refresh: {exc}"
                log.info("%s", exc)
                chrome_ok = True  # Chrome exists; just busy. Don't fall back to the X API.
            except ChromeUnavailable as exc:
                self.status["chrome"] = str(exc)
                log.warning("%s", exc)
            finally:
                lock.release()
        if not chrome_ok and s.x_bearer_token and s.x_enabled and not self.skip_x:
            x_sources = self._verify_x_sources(
                lambda h: x_api.profile_info(h, s.x_bearer_token), sources, x_sources)
            for src in x_sources:
                try:
                    counts["x"] += self._ingest(x_api.fetch(src, s.x_bearer_token))
                except Exception as exc:
                    self.status[src["id"]] = f"error: {exc}"[:200]
        if s.trading212_api_key:
            try:
                account = ReadOnlyTrading212(s.trading212_api_key, s.trading212_api_secret,
                                             s.trading212_environment).snapshot()
            except (Trading212Error, Exception) as exc:
                self.status["trading212"] = f"error: {exc}"[:200]
        if account and account.get("total") is not None:
            self.db.add_account_snapshot(account)
            self.last_account = account
            self.status["trading212"] = "ok"

        # Calendars and user events.
        try:
            for ev in calendars.fetch_earnings(5):
                self.db.upsert_event(ev)
        except Exception as exc:
            self.status["calendar:earnings"] = f"error: {exc}"[:200]
        for ev in calendars.custom_events(s.custom_events):
            self.db.upsert_event(ev)
        for i in self.ipos:
            if i.get("date") and i["status"] == "upcoming":
                self.db.upsert_event({"id": f"ipo-event:{i['id']}", "title": f"IPO: {i['company']} ({i['ticker']})",
                                      "starts_at": i["date"], "source_id": "ipo:calendar",
                                      "tickers": [i["ticker"]] if i["ticker"] else []})
        self._notify_events()
        return dict(counts)

    # ------------------------------------------------------------------ X
    def _x_state(self) -> dict[str, Any]:
        p = data_dir() / "x_state.json"
        try:
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        except json.JSONDecodeError:
            return {}

    def _save_x_state(self, st: dict[str, Any]) -> None:
        (data_dir() / "x_state.json").write_text(json.dumps(st, indent=2), encoding="utf-8")

    def _collect_x_chrome(self, chrome: ChromeLive, sources: list[dict[str, Any]],
                          x_sources: list[dict[str, Any]]) -> int:
        """Read X gently: the Following timeline, plus a few profile reads per refresh.

        Stops at the first sign of an X rate limit and backs off for an hour.
        """
        s = self.settings
        st = self._x_state()
        if st.get("backoff_until", "") > now_iso():
            self.status["x"] = f"paused until {st['backoff_until']} (X rate limit)"
            return 0
        gap = max(5, s.x_seconds_between_pages)
        new = 0
        try:
            # 1. Following timeline: one page covers every account you follow.
            by_handle = {str(x.get("handle", "")).lower(): x for x in x_sources if x.get("verified_at")}
            groups: dict[str, list[dict[str, Any]]] = {}
            for p in chrome.read_x_following():
                m = re.search(r"x\.com/([^/]+)/status/", p.get("url", ""))
                h = m.group(1).lower() if m else ""
                groups.setdefault(h if h in by_handle else "", []).append(p)
            for h, posts in groups.items():
                src = by_handle.get(h) or {"id": "x:following", "kind": "x", "name": "X Following timeline"}
                new += self._ingest(parse_x_posts(src, posts))
            self.status["x:following"] = "ok (chrome)"

            # 2. Identity checks for a few new accounts.
            todo = [x for x in x_sources if not x.get("verified_at")][: s.x_verifications_per_cycle]
            if todo:
                time.sleep(gap)
                self._verify_x_sources(chrome.x_profile_info, sources, todo, gap)

            # 3. A few direct profile reads, least recently read first.
            last = st.setdefault("last_read", {})
            ready = [x for x in x_sources if x.get("verified_at") and x.get("enabled")]
            ready.sort(key=lambda x: last.get(x["id"], ""))
            for src in ready[: s.x_profile_reads_per_cycle]:
                time.sleep(gap)
                new += self._ingest(parse_x_posts(src, chrome.read_x_profile(src["handle"])))
                last[src["id"]] = now_iso()
                self.status[src["id"]] = "ok (chrome)"
            self.status["x"] = "ok"
        except XRateLimited as exc:
            until = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(microsecond=0).isoformat()
            st["backoff_until"] = until
            self.status["x"] = f"{exc}; pausing X until {until}"
            self.notify("X reading paused", "X asked to slow down, so Investor Helper will skip X for an hour.")
        finally:
            self._save_x_state(st)
        return new

    def _verify_x_sources(self, profile_info: Callable[[str], dict[str, Any]], sources: list[dict[str, Any]],
                          x_sources: list[dict[str, Any]], gap: float = 1.5) -> list[dict[str, Any]]:
        """Check every not-yet-verified X account is the outlet it claims to be.

        Private, missing or mismatched accounts are disabled and reported. The app
        never follows anyone; it only reads public profiles.
        """
        ok_sources, changed = [], False
        for src in x_sources:
            if src.get("verified_at"):
                ok_sources.append(src)
                continue
            try:
                ok, reason = verify_x_profile(src, profile_info(src["handle"]))
            except XRateLimited:
                if changed:
                    save_sources(sources)
                raise
            except Exception as exc:
                self.status[src["id"]] = f"not verified yet: {exc}"[:200]
                continue
            changed = True
            if ok:
                src["verified_at"] = now_iso()
                ok_sources.append(src)
            else:
                src["enabled"] = False
                src["disabled_reason"] = f"failed check: {reason}"
                self.status[src["id"]] = src["disabled_reason"]
                self.notify("X source skipped", reason)
            time.sleep(gap)
        if changed:
            save_sources(sources)
        return ok_sources

    def _check_page(self, src: dict[str, Any]) -> None:
        prev = self.db.get_snapshot(src["id"])
        res = pages.check_page(dict(src, _previous_text=(prev or {}).get("excerpt", "")),
                               prev["content_hash"] if prev else None)
        if not res["text"]:
            return
        self.db.set_snapshot(src["id"], res["hash"], res["text"][:500])
        if res["changed"]:
            self.db.upsert_event({"id": f"page-change:{src['id']}:{res['hash'][:10]}",
                                  "title": f"{src['name']} page updated", "starts_at": None,
                                  "url": src["url"], "source_id": src["id"]})
            if self.settings.notify_events:
                self.notify(f"{src['name']} changed", res["excerpt"][:200])

    def _notify_events(self) -> None:
        if not self.settings.notify_events:
            return
        for ev in self.db.upcoming_events(200):
            if not ev["notified"] and calendars.is_soon(ev.get("starts_at"), 24):
                tick = f" [{', '.join(ev['tickers'])}]" if ev["tickers"] else ""
                self.notify("Coming up in the next 24h", f"{ev['title'][:150]}{tick}")
                self.db.mark_event_notified(ev["id"])

    # ------------------------------------------------------------- analyse
    def _prices(self, ticker: str) -> list[tuple[str, float]]:
        cached = self.db.prices_for(ticker, 130)
        today = datetime.now(timezone.utc).date()
        fresh = cached and cached[-1][0] >= (today - timedelta(days=1 if today.weekday() < 5 else 3)).isoformat()
        if not fresh:
            try:
                series = prices.fetch_daily(ticker)
                if series:
                    self.db.store_prices(ticker, series)
                    cached = self.db.prices_for(ticker, 130)
            except Exception as exc:
                log.info("prices %s: %s", ticker, exc)
        return cached

    def _fundamentals(self, ticker: str) -> float:
        cache_p = data_dir() / "fundamentals.json"
        try:
            cache = json.loads(cache_p.read_text(encoding="utf-8")) if cache_p.exists() else {}
        except json.JSONDecodeError:
            cache = {}
        hit = cache.get(ticker)
        if hit and time.time() - hit["at"] < 7 * 86400:
            return hit["score"]
        cik = next((c for c, t in self.finder.cik_to_ticker.items() if t == ticker), None)
        score = 0.0
        if cik:
            try:
                f = edgar.fetch_fundamentals(cik, self.settings.sec_user_agent)
                score = edgar.fundamental_score(f) if f else 0.0
            except Exception as exc:
                log.info("fundamentals %s: %s", ticker, exc)
        cache[ticker] = {"at": time.time(), "score": score}
        cache_p.write_text(json.dumps(cache), encoding="utf-8")
        return score

    def analyze(self, max_tickers: int = 40) -> str:
        s = self.settings
        graded = learner.resolve_predictions(self.db, self._prices)
        weights_full = self.db.get_weights()
        sources = load_sources()
        if s.auto_rotate_sources and graded:
            self.rotation = learner.rotate_sources(sources, weights_full)
            if self.rotation:
                save_sources(sources)
                self.notify("Sources updated", "; ".join(self.rotation)[:250])
        weights = {src["id"]: prior_weight(src) for src in sources}
        weights.update({k: v["weight"] for k, v in weights_full.items()})
        stats = self.db.prediction_stats()

        since = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
        items = self.db.recent_items(since)
        by_ticker: dict[str, list[dict[str, Any]]] = {}
        for it in items:
            for t in it["tickers"]:
                by_ticker.setdefault(t, []).append(it)
        held = [p["ticker"] for p in (self.last_account or {}).get("positions", [])]
        candidates = sorted(by_ticker, key=lambda t: len(by_ticker[t]), reverse=True)[:max_tickers]
        for t in [*s.watchlist, *held]:
            if t not in candidates:
                candidates.append(t)

        scores = []
        for t in candidates:
            sc = score_ticker(t, by_ticker.get(t, []), weights, self._prices(t),
                              fundamentals=self._fundamentals(t), horizon_days=s.horizon_days,
                              model_hit_rate=stats.get("hit_rate"))
            scores.append(sc)
        ranked = rank(scores)

        today = datetime.now(timezone.utc).date().isoformat()
        already = {p["ticker"] for p in self.db.latest_predictions(500) if p["made_at"][:10] == today}
        for sc in ranked:
            if sc.ticker in already or sc.last_price is None:
                continue
            self.db.add_prediction({"ticker": sc.ticker, "horizon_days": s.horizon_days,
                                    "price_at": sc.last_price, "predicted_move": sc.predicted_move,
                                    "confidence": sc.confidence, "score": sc.score,
                                    "contributing": sc.contributing})

        if s.notify_reports:
            for sc in ranked[:3]:
                if sc.predicted_move > 0 and sc.confidence >= s.notify_min_score:
                    self.notify(f"Buy idea: {sc.ticker}",
                                f"Predicted {sc.predicted_move * 100:+.1f}% over {s.horizon_days}d, "
                                f"confidence {sc.confidence * 100:.0f}%")

        hist = self.db.account_history(2)
        account_prev = hist[0] if len(hist) == 2 else None
        filings = [f for f in self.db.recent_items(since, "filing")
                   if (f.get("meta") or {}).get("form") in ("8-K", "SC 13D") and f.get("tickers")]
        names = {src["id"]: src.get("name", src["id"]) for src in sources}
        md = build_report(
            ranked=ranked, horizon_days=s.horizon_days, max_items=s.max_report_items,
            events=self.db.upcoming_events(20), ipos=self.ipos, filings=filings,
            account=self.last_account, account_prev=account_prev, weights=weights_full,
            source_names=names, stats=stats, rotation=self.rotation,
            suggestions=learner.suggest_handles(items, sources, weights_full),
        )
        self.db.add_report(md)
        out = data_dir() / "reports"
        out.mkdir(exist_ok=True)
        (out / f"report_{datetime.now().strftime('%Y%m%d_%H%M')}.md").write_text(md, encoding="utf-8")
        self.last_ranked = ranked
        return md

    def run_cycle(self) -> str:
        self.collect()
        return self.analyze()
