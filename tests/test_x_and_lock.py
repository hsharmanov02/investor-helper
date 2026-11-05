import json
import os

import pytest

from investor_helper import engine as engine_mod
from investor_helper.analysis.tickers import TickerFinder
from investor_helper.chrome_lock import ChromeBusy, ChromeLock
from investor_helper.config import Settings
from investor_helper.db import Database
from investor_helper.sources.chrome_live import XRateLimited


def test_lock_exclusive_and_stale(tmp_path):
    p = tmp_path / "chrome.lock"
    a = ChromeLock(p, owner="a")
    a.acquire()
    info = json.loads(p.read_text())
    assert info["owner"] == "a" and info["pid"] == os.getpid() and "started_at" in info
    with pytest.raises(ChromeBusy):
        ChromeLock(p, owner="b").acquire()
    a.release()
    assert not p.exists()
    p.write_text(json.dumps({"owner": "x_follow", "pid": 1, "started_at": "2020-01-01T00:00:00+00:00"}))
    with ChromeLock(p, owner="c"):  # stale lock is taken over
        assert json.loads(p.read_text())["owner"] == "c"
    assert not p.exists()


class FakeChrome:
    def __init__(self, limit_on_profile=False):
        self.calls = []
        self.limit_on_profile = limit_on_profile

    def read_x_following(self, max_posts=40):
        self.calls.append("following")
        return [{"text": "$NVDA beats", "url": "https://x.com/Reuters/status/1", "time": "2026-11-05T20:00:00Z"},
                {"text": "hello", "url": "https://x.com/randomguy/status/2", "time": "2026-11-05T20:00:00Z"}]

    def x_profile_info(self, h):
        self.calls.append(f"info:{h}")
        return {"exists": True, "name": h}

    def read_x_profile(self, h, max_posts=15):
        self.calls.append(f"profile:{h}")
        if self.limit_on_profile:
            raise XRateLimited("slow down")
        return []


def _engine(tmp_path, monkeypatch):
    monkeypatch.setattr(engine_mod.time, "sleep", lambda s: None)
    s = Settings(x_profile_reads_per_cycle=2, x_verifications_per_cycle=1)
    return engine_mod.Engine(db=Database(tmp_path / "x.sqlite3"), settings=s, finder=TickerFinder(),
                             notify=lambda *a: None)


def test_x_reading_is_rationed_and_attributed(tmp_path, monkeypatch):
    eng = _engine(tmp_path, monkeypatch)
    sources = [{"id": "x:Reuters", "kind": "x", "name": "Reuters", "handle": "Reuters", "enabled": True,
                "verified_at": "2026-11-05"},
               {"id": "x:CNBC", "kind": "x", "name": "CNBC", "handle": "CNBC", "enabled": True,
                "verified_at": "2026-11-05"},
               {"id": "x:FT", "kind": "x", "name": "Financial Times", "handle": "FT", "enabled": True,
                "verified_at": "2026-11-05"},
               {"id": "x:New1", "kind": "x", "name": "New1", "handle": "New1", "enabled": True},
               {"id": "x:New2", "kind": "x", "name": "New2", "handle": "New2", "enabled": True}]
    c = FakeChrome()
    eng._collect_x_chrome(c, sources, sources)
    assert c.calls[0] == "following"
    assert len([x for x in c.calls if x.startswith("info:")]) == 1
    assert len([x for x in c.calls if x.startswith("profile:")]) == 2
    ids = {i["source_id"] for i in eng.db.recent_items("2000-01-01")}
    assert ids == {"x:Reuters", "x:following"}
    # Next cycle reads the profiles that were not read last time first.
    c2 = FakeChrome()
    eng._collect_x_chrome(c2, sources, sources)
    first_profiles = [x for x in c.calls if x.startswith("profile:")]
    second_profiles = [x for x in c2.calls if x.startswith("profile:")]
    assert second_profiles[0] not in first_profiles


def test_x_rate_limit_backs_off(tmp_path, monkeypatch):
    eng = _engine(tmp_path, monkeypatch)
    src = [{"id": "x:Reuters", "kind": "x", "name": "Reuters", "handle": "Reuters", "enabled": True,
            "verified_at": "2026-11-05"}]
    eng._collect_x_chrome(FakeChrome(limit_on_profile=True), src, src)
    assert "pausing X" in eng.status["x"]
    c = FakeChrome()
    eng._collect_x_chrome(c, src, src)
    assert c.calls == [] and "paused until" in eng.status["x"]


def test_retag_fixes_old_tags(tmp_path, monkeypatch):
    eng = _engine(tmp_path, monkeypatch)
    eng.db.upsert_item({"id": "a", "source_id": "rss:x", "kind": "news", "title": "Jefferies raises AppLovin price target",
                        "published_at": "2099-01-01T00:00:00+00:00", "tickers": ["TGT"]})
    assert eng.retag_if_rules_changed() == 1
    assert eng.db.recent_items("2000-01-01")[0]["tickers"] == []
    assert eng.retag_if_rules_changed() == 0
