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
    # A second refresh within 20 minutes skips X entirely.
    c_skip = FakeChrome()
    eng._collect_x_chrome(c_skip, sources, sources)
    assert c_skip.calls == []
    # Later, it reads the profiles that were not read last time first.
    st = eng._x_state(); st["last_cycle"] = "2000-01-01T00:00:00+00:00"; eng._save_x_state(st)
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


def test_lock_from_dead_process_is_taken_over(tmp_path):
    import subprocess, sys, datetime
    p = tmp_path / "chrome.lock"
    proc = subprocess.Popen([sys.executable, "-c", "pass"]); proc.wait()
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    p.write_text(json.dumps({"owner": "investor-helper", "pid": proc.pid, "started_at": now}))
    with ChromeLock(p, owner="me"):
        assert json.loads(p.read_text())["owner"] == "me"


# ------------------------------------------------------------ follow manager
from investor_helper import x_follow as xf


def test_badge_and_identity_rules():
    assert xf.badge_type("gradient", True) == "gold"
    assert xf.badge_type("#829AAB", True) == "grey"
    assert xf.badge_type("#1D9BF0", True) == "blue"
    assert xf.badge_type("", False) == "none"
    assert xf.parse_count("1.2M Followers") == 1_200_000 and xf.parse_count("845K Followers") == 845_000
    reuters = {"handle": "Reuters", "expect": ["Reuters"]}
    gold = {"exists": True, "name": "Reuters", "has_badge": True, "badge_fill": "gradient"}
    assert xf.identity_failures(reuters, gold) == []
    # Substring is not enough: "Reutersfan" must not pass as Reuters.
    assert xf.identity_failures(reuters, dict(gold, name="Reutersfan Daily"))
    assert xf.identity_failures(reuters, dict(gold, name="Random Person @Reuters"))
    assert xf.identity_failures(reuters, dict(gold, protected=True))
    assert xf.identity_failures(reuters, dict(gold, has_badge=False, badge_fill=""))
    blue_ok = {"exists": True, "name": "Reuters", "has_badge": True, "badge_fill": "#1d9bf0",
               "website": "reuters.com", "followers": "25.1M Followers", "joined": "Joined March 2007"}
    assert xf.identity_failures(reuters, blue_ok) == []
    assert xf.identity_failures(reuters, dict(blue_ok, website="reuters-news.biz"))
    assert xf.identity_failures(reuters, dict(blue_ok, followers="12K Followers"))
    assert xf.identity_failures(reuters, dict(blue_ok, joined="Joined May 2021"))
    journo = {"handle": "NickTimiraos", "expect": ["Nick Timiraos"], "category": "journalist"}
    assert xf.identity_failures(journo, dict(blue_ok, name="Nick Timiraos", website="wsj.com",
                                             followers="200K Followers", joined="Joined 2009")) == []


def _src(h, **kw):
    return dict({"id": f"x:{h}", "kind": "x", "name": h, "handle": h, "enabled": True}, **kw)


def test_plan_changes():
    st = {"accounts": {"reuters": {"status": "followed"}, "bad": {"status": "followed"},
                       "finra": {"status": "skipped_badge_rule"}}}
    sources = [_src("Reuters"),
               _src("Bad", enabled=False, disabled_reason="auto: weight 0.40 after 24 graded calls"),
               _src("NewOne", added_reason="replacing Bad", trial=True),
               _src("FINRA"),
               _src("UserAdded")]
    weights = {"x:Bad": {"weight": 0.4, "hits": 4, "misses": 20}}
    plan = xf.plan_changes(sources, weights, st)
    assert {"action": "replace", "handle": "NewOne", "old_handle": "Bad"} == {k: plan[0][k] for k in ("action", "handle", "old_handle")}
    assert [p["handle"] for p in plan if p["action"] == "follow"] == ["UserAdded"]  # FINRA skipped by badge rule
    assert "FINRA" in [p["handle"] for p in xf.plan_changes(sources, weights, st, exceptions={"FINRA"})]


class FakeFollower:
    def __init__(self, ok_follow=True, limit=False):
        self.done = []
        self.ok_follow, self.limit = ok_follow, limit

    def follow(self, src):
        if self.limit:
            raise xf.XFollowStopped("X rate limit page")
        self.done.append(("follow", src["handle"]))
        return (self.ok_follow, "followed" if self.ok_follow else "name mismatch")

    def unfollow(self, h):
        self.done.append(("unfollow", h))
        return True, "unfollowed"


def _run(monkeypatch, fake, sources, weights, mode="auto"):
    monkeypatch.setattr(xf, "XFollower", lambda page, sleep=None: fake)
    return xf.run(None, sources, weights, mode, lambda *a: None, sleep=lambda s: None)


def test_run_replace_unfollows_only_after_successful_follow(monkeypatch):
    sources = [_src("Bad", enabled=False, disabled_reason="auto: weight 0.40"),
               _src("NewOne", added_reason="replacing Bad")]
    st = xf.load_state(); st["accounts"]["bad"] = {"status": "followed"}; xf.save_state(st)
    fail = FakeFollower(ok_follow=False)
    _run(monkeypatch, fail, sources, {})
    assert fail.done == [("follow", "NewOne")]  # old account kept
    st = xf.load_state(); st["accounts"].pop("newone"); xf.save_state(st)
    ok = FakeFollower()
    lines = _run(monkeypatch, ok, sources, {})
    assert ok.done == [("follow", "NewOne"), ("unfollow", "Bad")]
    assert any("Unfollowed @Bad" in l for l in lines)


def test_run_daily_cap_and_stop(monkeypatch):
    sources = [_src(f"Acc{i}") for i in range(6)]
    fake = FakeFollower()
    lines = _run(monkeypatch, fake, sources, {})
    assert len(fake.done) == xf.MAX_CHANGES_PER_DAY and any("Daily limit" in l for l in lines)
    st = xf.load_state(); st["changes_today"] = 0; xf.save_state(st)
    limited = FakeFollower(limit=True)
    lines = _run(monkeypatch, limited, sources, {})
    assert any("Stopped" in l for l in lines)
    again = FakeFollower()
    assert _run(monkeypatch, again, sources, {}) == [] and again.done == []


def test_approve_mode_waits_for_approval(monkeypatch):
    sources = [_src("UserAdded")]
    fake = FakeFollower()
    _run(monkeypatch, fake, sources, {}, mode="approve")
    assert fake.done == [] and xf.pending_summary()[0]["handle"] == "UserAdded"
    xf.approve_all_pending()
    _run(monkeypatch, fake, sources, {}, mode="approve")
    assert fake.done == [("follow", "UserAdded")]
