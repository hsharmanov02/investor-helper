"""Per-ticker scoring and short-horizon movement prediction.

score = w_news * news + w_mom * momentum + w_fund * fundamentals, each in [-1, 1].

* news: sum of item sentiment x source weight x recency decay x item importance.
* momentum: 5- and 20-day returns scaled by volatility.
* fundamentals: YoY revenue / net income growth from EDGAR XBRL data.

The factor weights (``factor:news`` etc.) and every source weight are learned
by ``learner.py`` from how past predictions turned out.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

DEFAULT_FACTORS = {"factor:news": 1.0, "factor:momentum": 0.6, "factor:fundamentals": 0.3}
DECAY_HOURS = 36.0
FALLBACK_DAILY_VOL = 0.02


@dataclass
class TickerScore:
    ticker: str
    score: float
    predicted_move: float
    confidence: float
    last_price: float | None
    predicted_price: float | None
    daily_vol: float
    news_score: float
    momentum_score: float
    fundamental_score: float
    n_items: int
    n_sources: int
    contributing: dict[str, float] = field(default_factory=dict)
    headlines: list[dict[str, Any]] = field(default_factory=list)

    @property
    def direction(self) -> str:
        if self.predicted_move > 0.005:
            return "up"
        if self.predicted_move < -0.005:
            return "down"
        return "flat"


def _age_hours(iso: str, now: datetime) -> float:
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return 48.0
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return max(0.0, (now - t).total_seconds() / 3600)


def price_features(series: list[tuple[str, float]]) -> dict[str, float | None]:
    closes = [c for _, c in series if c and c > 0]
    if len(closes) < 6:
        return {"last": closes[-1] if closes else None, "vol": FALLBACK_DAILY_VOL, "momentum": 0.0}
    rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    window = rets[-20:]
    vol = statistics.pstdev(window) if len(window) > 2 else FALLBACK_DAILY_VOL
    vol = max(vol, 0.003)
    r5 = closes[-1] / closes[-6] - 1
    r20 = closes[-1] / closes[-21] - 1 if len(closes) >= 21 else r5
    z = (0.6 * r5 / (vol * math.sqrt(5))) + (0.4 * r20 / (vol * math.sqrt(20)))
    return {"last": closes[-1], "vol": vol, "momentum": math.tanh(z / 2)}


def score_ticker(
    ticker: str,
    items: list[dict[str, Any]],
    weights: dict[str, float],
    prices: list[tuple[str, float]],
    fundamentals: float = 0.0,
    horizon_days: int = 3,
    model_hit_rate: float | None = None,
    now: datetime | None = None,
) -> TickerScore:
    now = now or datetime.now(timezone.utc)
    contrib: dict[str, float] = {}
    headlines = []
    for it in items:
        w = weights.get(it["source_id"], 1.0)
        rec = math.exp(-_age_hours(it["published_at"], now) / DECAY_HOURS)
        imp = float((it.get("meta") or {}).get("importance", 1.0 if it["kind"] == "news" else 0.5))
        c = float(it.get("sentiment", 0.0)) * w * rec * imp
        contrib[it["source_id"]] = contrib.get(it["source_id"], 0.0) + c
        headlines.append({"title": it["title"], "url": it.get("url", ""), "source_id": it["source_id"],
                          "sentiment": it.get("sentiment", 0.0), "published_at": it["published_at"]})
    news_raw = sum(contrib.values())
    news = math.tanh(news_raw / 1.5)

    pf = price_features(prices)
    mom = float(pf["momentum"] or 0.0)
    fund = max(-1.0, min(1.0, fundamentals))

    fw = {k: weights.get(k, v) for k, v in DEFAULT_FACTORS.items()}
    total_w = sum(fw.values()) or 1.0
    combined = (fw["factor:news"] * news + fw["factor:momentum"] * mom
                + fw["factor:fundamentals"] * fund) / total_w

    vol = float(pf["vol"] or FALLBACK_DAILY_VOL)
    predicted_move = combined * vol * math.sqrt(horizon_days) * 1.2
    last = pf["last"]

    # Factor contributions are tracked like sources so the learner can tune them.
    factor_contrib = {"factor:news": news, "factor:momentum": mom, "factor:fundamentals": fund}
    all_contrib = {**contrib, **{k: v for k, v in factor_contrib.items() if abs(v) > 1e-6}}

    abs_sum = sum(abs(v) for v in contrib.values())
    agreement = abs(news_raw) / abs_sum if abs_sum else 0.0
    n_sources = len([v for v in contrib.values() if abs(v) > 1e-3])
    signs = [s for s in (news, mom, fund) if abs(s) > 0.05]
    factor_agree = abs(sum(math.copysign(1, s) for s in signs)) / len(signs) if signs else 0.0
    conf = 0.15 + 0.3 * agreement * min(1.0, n_sources / 3) + 0.2 * factor_agree
    conf += 0.1 if last else 0.0
    if model_hit_rate is not None:
        conf += 0.25 * (model_hit_rate - 0.5)
    conf = max(0.05, min(0.95, conf))

    return TickerScore(
        ticker=ticker, score=combined, predicted_move=predicted_move, confidence=conf,
        last_price=last, predicted_price=(last * (1 + predicted_move)) if last else None,
        daily_vol=vol, news_score=news, momentum_score=mom, fundamental_score=fund,
        n_items=len(items), n_sources=n_sources, contributing=all_contrib,
        headlines=sorted(headlines, key=lambda h: abs(h["sentiment"]), reverse=True)[:5],
    )


def rank(scores: list[TickerScore]) -> list[TickerScore]:
    """Best buy candidates first: expected move weighted by confidence."""
    return sorted(scores, key=lambda s: s.predicted_move * s.confidence, reverse=True)
