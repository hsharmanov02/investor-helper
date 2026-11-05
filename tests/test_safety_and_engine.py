"""The app must never trade. These tests guard that, then run a full offline cycle."""
import re
from pathlib import Path

from investor_helper import engine as engine_mod
from investor_helper.config import Settings
from investor_helper.db import Database

PKG = Path(__file__).resolve().parent.parent / "investor_helper"
# Anywhere in the app: no write-style HTTP calls and no order endpoints.
FORBIDDEN_ANYWHERE = [
    r"requests\.(post|put|patch|delete)\(", r"_session\.(post|put|patch|delete)\(",
    r"\.(post|put|patch|delete)\(\s*f?['\"]http", r"equity/orders", r"/orders/(market|limit|stop)",
    r"method\s*=\s*['\"](POST|PUT|DELETE)",
]
# In the data sources (where the browser is driven): no clicking or typing at all.
FORBIDDEN_IN_SOURCES = [r"\.click\(", r"\.fill\(", r"\.type\(", r"keyboard\.", r"\.press\(",
                        r"\.check\(", r"\.select_option\(", r"\.dispatch_event\(", r"\.tap\(",
                        r"-follow", r"/follow", r"friendships"]


def _hits(files, patterns):
    for f in files:
        text = f.read_text(encoding="utf-8")
        for pat in patterns:
            for m in re.finditer(pat, text):
                yield f"{f.name}:{text[:m.start()].count(chr(10)) + 1} matches {pat}"


FOLLOW_MODULE = PKG / "x_follow.py"


def test_no_order_or_interaction_code():
    assert list(_hits(PKG.rglob("*.py"), FORBIDDEN_ANYWHERE)) == []
    assert list(_hits((PKG / "sources").rglob("*.py"), FORBIDDEN_IN_SOURCES)) == []
    # Clicking exists in exactly one module: the X follow manager.
    others = [f for f in PKG.rglob("*.py") if f != FOLLOW_MODULE and f.parent.name != "gui"]
    assert list(_hits(others, [r"\.click\("])) == []


def test_follow_module_only_acts_on_x():
    text = FOLLOW_MODULE.read_text(encoding="utf-8")
    for url in re.findall(r"goto\(f?[\"']([^\"']+)", text):
        assert url.startswith("https://x.com/"), url
    assert not re.search(r"app\.trading212|live\.trading212|/equity/|\border", text, re.I)
    # Every click is on a locator scoped to the profile's own handle or X's confirm sheet.
    clicks = re.findall(r"(\w+)\.click\(", text)
    assert set(clicks) <= {"btn", "confirm"}


def test_full_cycle_offline(tmp_path, monkeypatch):
    db = Database(tmp_path / "e.sqlite3")
    notes = []
    s = Settings(use_chrome_cdp=False, watchlist=["AAPL"], notify_min_score=0.0)
    from investor_helper.analysis.tickers import TickerFinder
    eng = engine_mod.Engine(db=db, settings=s, notify=lambda t, b: notes.append(t), finder=TickerFinder())

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    monkeypatch.setattr(engine_mod.rss, "fetch", lambda src: [{
        "id": f"{src['id']}:1", "source_id": src["id"], "kind": "news", "url": "https://ex.com",
        "title": "Apple beats estimates and raises guidance; earnings call tomorrow",
        "summary": "Record revenue", "published_at": now}])
    monkeypatch.setattr(engine_mod.edgar, "fetch_current", lambda *a, **k: [])
    monkeypatch.setattr(engine_mod.calendars, "fetch_ipo_calendar", lambda: [])
    monkeypatch.setattr(engine_mod.calendars, "fetch_earnings", lambda n: [])
    monkeypatch.setattr(engine_mod.pages, "check_page", lambda src, h: {"hash": "h", "text": "t", "changed": False, "excerpt": ""})
    from datetime import date, timedelta
    series = [((date.today() - timedelta(days=30 - i)).isoformat(), 100 + i) for i in range(31)]
    monkeypatch.setattr(engine_mod.prices, "fetch_daily", lambda t: series)
    monkeypatch.setattr(engine_mod.Engine, "_fundamentals", lambda self, t: 0.2)

    md = eng.run_cycle()
    assert "AAPL" in md and "never places trades" in md
    assert "1/" in md.splitlines()[0]
    preds = db.latest_predictions()
    assert preds and preds[0]["ticker"] == "AAPL" and preds[0]["predicted_move"] > 0
    assert any(e["title"].startswith("Apple beats") for e in db.upcoming_events())
    assert any(n.startswith("Buy idea") for n in notes)
    # A second cycle on the same day must not duplicate predictions.
    eng.run_cycle()
    assert len([p for p in db.latest_predictions() if p["ticker"] == "AAPL"]) == 1
