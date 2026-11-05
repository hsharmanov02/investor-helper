"""Feedback loop: grade old predictions, re-weight sources, rotate bad ones out.

After a prediction's horizon passes, the realised move is compared with what
each contributing source (and each factor) pointed to. A source that keeps
pointing the right way gains weight (up to 2.0); one that keeps being wrong
loses weight (down to 0.2) and, with auto-rotation on, is disabled and
replaced by the next vetted candidate of the same kind.
"""
from __future__ import annotations

import json
import math
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from ..config import CANDIDATE_SOURCES, data_dir

MIN_W, MAX_W = 0.2, 2.0
ROTATE_MIN_SAMPLES = 20
ROTATE_BELOW = 0.7


def weight_from(hits: int, misses: int) -> float:
    """Beta(2,2) prior mean of hit rate, scaled so 50% maps to 1.0."""
    return max(MIN_W, min(MAX_W, 2.0 * (hits + 2) / (hits + misses + 4)))


def price_on_or_after(series: list[tuple[str, float]], day: date) -> float | None:
    for d, c in series:
        if d >= day.isoformat():
            return c
    return None


def resolve_predictions(db: Any, prices_for: Callable[[str], list[tuple[str, float]]],
                        today: date | None = None) -> list[dict[str, Any]]:
    """Grade predictions whose horizon has passed and update weights. Returns graded rows."""
    today = today or datetime.now(timezone.utc).date()
    weights = db.get_weights()
    graded = []
    for p in db.unresolved_predictions():
        made = datetime.fromisoformat(p["made_at"].replace("Z", "+00:00")).date()
        target = made + timedelta(days=int(p["horizon_days"]))
        if target > today or not p["price_at"]:
            continue
        series = prices_for(p["ticker"])
        end = price_on_or_after(series, target)
        if end is None:
            if (today - target).days > 10:  # data never arrived; drop it
                db.resolve_prediction(p["id"], 0.0, False)
            continue
        actual = end / float(p["price_at"]) - 1
        pred = float(p["predicted_move"])
        correct = (math.copysign(1, pred) == math.copysign(1, actual)) or (abs(pred) < 0.003 and abs(actual) < 0.01)
        db.resolve_prediction(p["id"], actual, correct)
        for src, c in json.loads(p["contributing"] or "{}").items():
            if abs(c) < 0.01 or abs(actual) < 0.002:
                continue
            row = weights.get(src, {"hits": 0, "misses": 0})
            hits, misses = int(row["hits"]), int(row["misses"])
            if math.copysign(1, c) == math.copysign(1, actual):
                hits += 1
            else:
                misses += 1
            w = weight_from(hits, misses)
            db.set_weight(src, w, hits, misses)
            weights[src] = {"weight": w, "hits": hits, "misses": misses}
        graded.append({**p, "actual_move": actual, "correct": correct})
    return graded


def rotate_sources(sources: list[dict[str, Any]], weights: dict[str, dict[str, Any]]) -> list[str]:
    """Disable chronically wrong news sources and trial replacements. Mutates `sources`."""
    changes: list[str] = []
    ids = {s["id"] for s in sources}
    for s in sources:
        if s["kind"] not in ("rss", "x") or not s.get("enabled"):
            continue
        w = weights.get(s["id"])
        if not w or (w["hits"] + w["misses"]) < ROTATE_MIN_SAMPLES or w["weight"] >= ROTATE_BELOW:
            continue
        s["enabled"] = False
        s["disabled_reason"] = f"auto: weight {w['weight']:.2f} after {w['hits'] + w['misses']} graded calls"
        msg = f"Disabled {s['name']} (weight {w['weight']:.2f}, {w['hits']} right / {w['misses']} wrong)"
        repl = next((c for c in CANDIDATE_SOURCES if c["kind"] == s["kind"] and c["id"] not in ids), None)
        if repl:
            new = dict(repl, trial=True, added_reason=f"replacing {s['name']}")
            sources.append(new)
            ids.add(new["id"])
            msg += f"; now trialling {new['name']}"
        changes.append(msg)
    if changes:
        log_path = data_dir() / "rotation_log.json"
        try:
            log = json.loads(log_path.read_text(encoding="utf-8")) if log_path.exists() else []
        except json.JSONDecodeError:
            log = []
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        log.extend({"at": stamp, "change": c} for c in changes)
        log_path.write_text(json.dumps(log[-500:], indent=2), encoding="utf-8")
    return changes


def suggest_handles(items: list[dict[str, Any]], sources: list[dict[str, Any]],
                    weights: dict[str, dict[str, Any]], min_mentions: int = 4) -> list[tuple[str, int]]:
    """X handles that well-scored sources keep citing. Shown for the user to approve."""
    known = {str(s.get("handle", "")).lower() for s in sources}
    c: Counter[str] = Counter()
    for it in items:
        w = weights.get(it["source_id"], {}).get("weight", 1.0)
        if w < 1.0:
            continue
        for h in (it.get("meta") or {}).get("mentions", []):
            if h.lower() not in known:
                c[h] += 1
    return [(h, n) for h, n in c.most_common(10) if n >= min_mentions]
