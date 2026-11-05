from datetime import date, datetime, timedelta, timezone

from investor_helper.analysis import learner, market_hours, sentiment
from investor_helper.analysis.scoring import price_features, rank, score_ticker
from investor_helper.analysis.tickers import TickerFinder
from investor_helper.db import Database


def test_sentiment_direction():
    assert sentiment.score("Apple beats estimates and raises guidance") > 0.4
    assert sentiment.score("Boeing misses estimates, cuts guidance amid probe") < -0.4
    assert sentiment.score("Company did not miss estimates") > 0
    assert sentiment.score("The weather is nice") == 0


def test_ticker_finder():
    f = TickerFinder()
    f.load_sec_list({"0": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"},
                     "1": {"cik_str": 1234, "ticker": "ACME", "title": "Acme Rocket Works Inc"}})
    found = f.find("Nvidia and $TSLA rally; Acme Rocket Works (NYSE: ACME) jumps. CEO says AI wins")
    assert found[:2] == ["TSLA", "ACME"]
    assert "NVDA" in found and "CEO" not in found and "AI" not in found
    assert f.cik_to_ticker["0001045810"] == "NVDA"


def _series(start, n, step):
    d0 = date(2026, 9, 1)
    return [((d0 + timedelta(days=i)).isoformat(), start * (1 + step) ** i) for i in range(n)]


def test_price_features_uptrend():
    pf = price_features(_series(100, 30, 0.01))
    assert pf["momentum"] > 0.5 and pf["last"] > 100


def test_score_and_rank():
    now = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
    good = [{"source_id": "rss:a", "kind": "news", "title": "X beats", "sentiment": 0.8,
             "published_at": (now - timedelta(hours=2)).isoformat()},
            {"source_id": "rss:b", "kind": "news", "title": "X upgrade", "sentiment": 0.6,
             "published_at": (now - timedelta(hours=5)).isoformat()}]
    bad = [{"source_id": "rss:a", "kind": "news", "title": "Y fraud", "sentiment": -0.9,
            "published_at": (now - timedelta(hours=1)).isoformat()}]
    a = score_ticker("XXX", good, {}, _series(50, 30, 0.004), now=now)
    b = score_ticker("YYY", bad, {}, _series(50, 30, -0.004), now=now)
    assert a.predicted_move > 0 > b.predicted_move
    assert 0.05 <= a.confidence <= 0.95
    assert a.predicted_price > a.last_price
    assert [s.ticker for s in rank([b, a])] == ["XXX", "YYY"]
    assert "rss:a" in a.contributing and "factor:news" in a.contributing


def test_learner_rewards_right_sources(tmp_path):
    db = Database(tmp_path / "t.sqlite3")
    made = "2026-10-01T15:00:00+00:00"
    for _ in range(6):
        db.add_prediction({"made_at": made, "ticker": "XXX", "horizon_days": 3, "price_at": 100.0,
                           "predicted_move": 0.02, "confidence": 0.5, "score": 0.4,
                           "contributing": {"rss:right": 0.5, "rss:wrong": -0.3}})
    prices = {"XXX": [("2026-10-03", 101.0), ("2026-10-04", 104.0)]}
    graded = learner.resolve_predictions(db, lambda t: prices[t], today=date(2026, 10, 10))
    assert len(graded) == 6 and all(g["correct"] for g in graded)
    w = db.get_weights()
    assert w["rss:right"]["weight"] > 1.5 and w["rss:wrong"]["weight"] < 0.5
    assert db.prediction_stats()["hit_rate"] == 1.0


def test_rotation_replaces_bad_source():
    sources = [{"id": "rss:bad", "kind": "rss", "name": "Bad", "enabled": True}]
    weights = {"rss:bad": {"weight": 0.4, "hits": 4, "misses": 20}}
    changes = learner.rotate_sources(sources, weights)
    assert sources[0]["enabled"] is False
    assert len(sources) == 2 and sources[1]["trial"] is True
    assert "Disabled Bad" in changes[0]


def test_market_hours():
    wed_ny_noon = datetime(2026, 11, 4, 17, 0, tzinfo=timezone.utc)
    assert market_hours.is_open("NYSE/NASDAQ", wed_ny_noon)
    sat = datetime(2026, 11, 7, 17, 0, tzinfo=timezone.utc)
    assert not any(market_hours.status(sat).values())
    assert market_hours.exchange_for("VUSA.L") == "London (LSE)"


def test_penny_single_filing_ranks_last():
    now = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
    one = [{"source_id": "edgar:recent", "kind": "filing", "title": "8-K", "sentiment": 0.9,
            "published_at": (now - timedelta(hours=1)).isoformat(), "meta": {"importance": 0.6}}]
    penny = score_ticker("KBSR", one, {}, _series(0.5, 30, 0.05), now=now)
    solid = score_ticker("XXX", [one[0], dict(one[0], source_id="rss:a")], {}, _series(50, 30, 0.002), now=now)
    assert rank([penny, solid])[0].ticker == "XXX"
    assert abs(penny.predicted_move) <= 3 * penny.daily_vol * (3 ** 0.5) + 1e-9
