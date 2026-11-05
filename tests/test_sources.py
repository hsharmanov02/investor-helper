from datetime import date

import pytest

from investor_helper.sources import calendars, edgar, pages, prices, rss, x_api
from investor_helper.sources.chrome_live import parse_trading212, parse_x_posts, verify_x_profile
from investor_helper.sources.trading212 import (ReadOnlyTrading212, Trading212Error, plain_ticker,
                                                summarize)

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>
<item><title>Nvidia beats estimates</title><link>https://ex.com/a</link><guid>a1</guid>
<description>&lt;p&gt;Record &lt;b&gt;revenue&lt;/b&gt;&lt;/p&gt;</description>
<pubDate>Wed, 05 Nov 2026 14:00:00 GMT</pubDate></item></channel></rss>"""

EDGAR_ATOM = b"""<?xml version="1.0" encoding="ISO-8859-1" ?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Latest Filings</title>
<entry><title>8-K - NVIDIA CORP (0001045810) (Filer)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/x.htm"/>
<summary type="html"> &lt;b&gt;Filed:&lt;/b&gt; 2026-11-05 Item 2.02: Results Item 1.01: Agreement</summary>
<updated>2026-11-05T16:01:02-05:00</updated><id>urn:tag:sec.gov,2008:accession-number=0001</id></entry>
</feed>"""


def test_rss_parse():
    items = rss.parse_feed({"id": "rss:t"}, RSS)
    assert items[0]["title"] == "Nvidia beats estimates"
    assert items[0]["summary"] == "Record revenue"
    assert items[0]["published_at"].startswith("2026-11-05T14:00")


def test_edgar_parse():
    items = edgar.parse_current_feed("edgar:recent", EDGAR_ATOM, {"0001045810": "NVDA"})
    assert items[0]["tickers"] == ["NVDA"]
    assert items[0]["meta"]["form"] == "8-K" and items[0]["sentiment"] == pytest.approx(0.3)


def test_fundamentals():
    def q(frame, v):
        return {"form": "10-Q", "fp": "Q3", "end": "x", "frame": frame, "val": v}
    facts = {"facts": {"us-gaap": {
        "Revenues": {"units": {"USD": [q("CY2025Q1", 90), q("CY2025Q2", 95), q("CY2025Q3", 100),
                                       q("CY2025Q4", 110), q("CY2026Q1", 115), q("CY2026Q2", 120),
                                       q("CY2026Q3", 130)]}},
        "NetIncomeLoss": {"units": {"USD": [q("CY2025Q3", 10), q("CY2025Q4", 11), q("CY2026Q1", 12),
                                            q("CY2026Q2", 13), q("CY2026Q3", 15)]}}}}}
    f = edgar.fundamentals_from_facts(facts)
    assert f["revenue_yoy"] == pytest.approx(0.3)
    assert f["net_income_yoy"] == pytest.approx(0.5)
    assert edgar.fundamental_score(f) > 0.5


def test_trading212_is_read_only():
    c = ReadOnlyTrading212("key")
    with pytest.raises(Trading212Error):
        c._get("/equity/orders/market")
    with pytest.raises(Trading212Error):
        ReadOnlyTrading212("")
    assert plain_ticker("AAPL_US_EQ") == "AAPL" and plain_ticker("VUSAl_EQ") == "VUSA.L"
    s = summarize({"currencyCode": "GBP"}, {"total": 1000, "free": 100, "invested": 900, "ppl": 50},
                  [{"ticker": "AAPL_US_EQ", "quantity": 2, "averagePrice": 150, "currentPrice": 200, "ppl": 100}])
    assert s["total"] == 1000 and s["positions"][0]["value"] == 400


def test_chrome_parsers():
    snap = parse_trading212([{"cash": {"total": 1234.5, "free": 34.5, "invested": 1200, "ppl": 12}},
                             {"currencyCode": "GBP"},
                             [{"ticker": "MSFT_US_EQ", "quantity": 1, "averagePrice": 300, "currentPrice": 400}]], "")
    assert snap["total"] == 1234.5 and snap["currency"] == "GBP" and snap["positions"][0]["ticker"] == "MSFT"
    fallback = parse_trading212([], "Home\nAccount value\n£2,500.75\nInvest")
    assert fallback["total"] == 2500.75 and fallback["currency"] == "GBP"
    posts = parse_x_posts({"id": "x:Reuters"}, [
        {"text": "$AAPL beats. via @ReutersBiz", "time": "2026-11-05T10:00:00.000Z",
         "url": "https://x.com/Reuters/status/123", "links": []},
        {"text": "", "url": "https://x.com/Reuters/status/124"}])
    assert len(posts) == 1 and posts[0]["id"] == "x:Reuters:123" and posts[0]["meta"]["mentions"] == ["ReutersBiz"]


def test_x_api_parse():
    out = x_api.parse_tweets({"id": "x:CNBC", "handle": "CNBC"},
                             {"data": [{"id": "9", "text": "Fed meeting tomorrow", "created_at": "2026-11-05T01:00:00Z"}]})
    assert out[0]["url"] == "https://x.com/CNBC/status/9"


def test_event_extraction():
    today = date(2026, 11, 5)  # Thursday
    assert pages.resolve_date("Nov. 12", today) == date(2026, 11, 12)
    assert pages.resolve_date("Jan 10", today) == date(2027, 1, 10)
    assert pages.resolve_date("tomorrow", today) == date(2026, 11, 6)
    assert pages.resolve_date("on Tuesday", today) == date(2026, 11, 10)
    evs = pages.extract_events({"title": "Nvidia to hold earnings call on November 19",
                                "summary": "Shares rose. Nothing else.", "source_id": "rss:x",
                                "tickers": ["NVDA"]}, today)
    assert len(evs) == 1 and evs[0]["starts_at"] == "2026-11-19" and evs[0]["tickers"] == ["NVDA"]


def test_page_compare():
    first = pages.compare("FOMC meetings: Dec 9-10", None)
    assert not first["changed"]
    second = pages.compare("FOMC meetings: Dec 9-10. Added Jan 27-28", first["hash"], "FOMC meetings: Dec 9-10")
    assert second["changed"] and "Jan 27-28" in second["excerpt"]


def test_calendars():
    ipos = calendars.parse_ipo_calendar({"data": {"upcoming": {"upcomingTable": {"rows": [
        {"proposedTickerSymbol": "NEWC", "companyName": "NewCo", "proposedExchange": "NASDAQ",
         "proposedSharePrice": "14.00-16.00", "expectedPriceDate": "11/12/2026"}]}}}})
    assert ipos[0]["ticker"] == "NEWC" and ipos[0]["date"] == "2026-11-12"
    ev = calendars.parse_earnings({"data": {"rows": [{"symbol": "nvda", "name": "NVIDIA", "time": "time-after-hours",
                                                      "epsForecast": "$0.85"}]}}, "2026-11-19")
    assert ev[0]["starts_at"] == "2026-11-19T21:00:00+00:00"


def test_price_parsers():
    y = prices.parse_yahoo({"chart": {"result": [{"timestamp": [1762300800, 1762387200],
                                                  "indicators": {"quote": [{"close": [100.0, None]}]}}]}})
    assert y == [("2025-11-05", 100.0)]
    assert prices.parse_stooq("Date,Open,High,Low,Close,Volume\n2026-11-04,1,2,0.5,1.5,100\n") == [("2026-11-04", 1.5)]


def test_x_profile_verification():
    src = {"handle": "Reuters", "name": "Reuters", "expect": ["Reuters"]}
    ok, why = verify_x_profile(src, {"exists": True, "name": "Reuters\n@Reuters", "bio": "Top news", "verified": True})
    assert ok and "blue check" in why
    ok, why = verify_x_profile(src, {"exists": True, "name": "Joe\n@Reuters", "bio": "my page", "protected": True})
    assert not ok and "private" in why
    ok, why = verify_x_profile(src, {"exists": True, "name": "Random Person\n@Reuters", "bio": "ex-Reuters reporter"})
    assert not ok and "does not match" in why
    ok, _ = verify_x_profile(src, {"exists": False})
    assert not ok
