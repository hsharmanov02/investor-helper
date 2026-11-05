"""SQLite storage for items, signals, predictions, outcomes, weights, events."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import data_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,            -- stable id, e.g. url or source:guid
    source_id TEXT NOT NULL,
    kind TEXT NOT NULL,             -- news | filing | ipo | event
    title TEXT NOT NULL,
    summary TEXT DEFAULT '',
    url TEXT DEFAULT '',
    published_at TEXT NOT NULL,     -- ISO 8601 UTC
    fetched_at TEXT NOT NULL,
    tickers TEXT DEFAULT '[]',      -- JSON list
    sentiment REAL DEFAULT 0,
    meta TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_items_pub ON items(published_at);

CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    made_at TEXT NOT NULL,
    ticker TEXT NOT NULL,
    horizon_days INTEGER NOT NULL,
    price_at TEXT,                  -- price when the prediction was made
    predicted_move REAL NOT NULL,   -- fraction, e.g. 0.021 = +2.1%
    confidence REAL NOT NULL,
    score REAL NOT NULL,
    contributing TEXT DEFAULT '{}', -- JSON {source_id: contribution}
    resolved_at TEXT,
    actual_move REAL,
    correct INTEGER
);
CREATE INDEX IF NOT EXISTS idx_pred_ticker ON predictions(ticker, made_at);

CREATE TABLE IF NOT EXISTS source_weights (
    source_id TEXT PRIMARY KEY,
    weight REAL NOT NULL DEFAULT 1.0,
    hits INTEGER NOT NULL DEFAULT 0,
    misses INTEGER NOT NULL DEFAULT 0,
    last_updated TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    starts_at TEXT,                 -- ISO or NULL when unknown
    url TEXT DEFAULT '',
    source_id TEXT DEFAULT '',
    tickers TEXT DEFAULT '[]',
    notified INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS page_snapshots (
    source_id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    excerpt TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS account_snapshots (
    taken_at TEXT PRIMARY KEY,
    total REAL,
    cash REAL,
    invested REAL,
    ppl REAL,                       -- unrealised profit/loss
    currency TEXT,
    raw TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS prices (
    ticker TEXT NOT NULL,
    day TEXT NOT NULL,              -- YYYY-MM-DD
    close REAL NOT NULL,
    PRIMARY KEY (ticker, day)
);

CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    markdown TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Database:
    def __init__(self, path: Path | None = None):
        self.path = path or data_dir() / "investor_helper.sqlite3"
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def close(self) -> None:
        self._conn.close()

    # ----------------------------------------------------------------- items
    def upsert_item(self, item: dict[str, Any]) -> bool:
        """Insert an item; return True when it was new."""
        with self.tx() as c:
            cur = c.execute(
                """INSERT OR IGNORE INTO items
                   (id, source_id, kind, title, summary, url, published_at, fetched_at,
                    tickers, sentiment, meta)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (item["id"], item["source_id"], item["kind"], item["title"],
                 item.get("summary", ""), item.get("url", ""), item["published_at"],
                 now_iso(), json.dumps(item.get("tickers", [])),
                 float(item.get("sentiment", 0.0)), json.dumps(item.get("meta", {}))),
            )
            return cur.rowcount > 0

    def recent_items(self, since_iso: str, kind: str | None = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM items WHERE published_at >= ?"
        args: list[Any] = [since_iso]
        if kind:
            q += " AND kind = ?"
            args.append(kind)
        q += " ORDER BY published_at DESC"
        rows = self._conn.execute(q, args).fetchall()
        return [_row_item(r) for r in rows]

    # ------------------------------------------------------------ predictions
    def add_prediction(self, p: dict[str, Any]) -> int:
        with self.tx() as c:
            cur = c.execute(
                """INSERT INTO predictions
                   (made_at, ticker, horizon_days, price_at, predicted_move, confidence,
                    score, contributing)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (p.get("made_at") or now_iso(), p["ticker"], p["horizon_days"],
                 p.get("price_at"), p["predicted_move"], p["confidence"], p["score"],
                 json.dumps(p.get("contributing", {}))),
            )
            return int(cur.lastrowid)

    def unresolved_predictions(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM predictions WHERE resolved_at IS NULL ORDER BY made_at"
        ).fetchall()
        return [dict(r) for r in rows]

    def resolve_prediction(self, pid: int, actual_move: float, correct: bool) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE predictions SET resolved_at=?, actual_move=?, correct=? WHERE id=?",
                (now_iso(), actual_move, int(correct), pid),
            )

    def prediction_stats(self) -> dict[str, Any]:
        row = self._conn.execute(
            """SELECT COUNT(*) AS n, SUM(correct) AS hits
               FROM predictions WHERE resolved_at IS NOT NULL"""
        ).fetchone()
        n = row["n"] or 0
        hits = row["hits"] or 0
        return {"resolved": n, "hits": hits, "hit_rate": (hits / n) if n else None}

    def latest_predictions(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM predictions ORDER BY made_at DESC, score DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- weights
    def get_weights(self) -> dict[str, dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM source_weights").fetchall()
        return {r["source_id"]: dict(r) for r in rows}

    def set_weight(self, source_id: str, weight: float, hits: int, misses: int) -> None:
        with self.tx() as c:
            c.execute(
                """INSERT INTO source_weights (source_id, weight, hits, misses, last_updated)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(source_id) DO UPDATE SET weight=excluded.weight,
                   hits=excluded.hits, misses=excluded.misses, last_updated=excluded.last_updated""",
                (source_id, weight, hits, misses, now_iso()),
            )

    # ----------------------------------------------------------------- events
    def upsert_event(self, ev: dict[str, Any]) -> bool:
        with self.tx() as c:
            cur = c.execute(
                """INSERT OR IGNORE INTO events (id, title, starts_at, url, source_id, tickers, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (ev["id"], ev["title"], ev.get("starts_at"), ev.get("url", ""),
                 ev.get("source_id", ""), json.dumps(ev.get("tickers", [])), now_iso()),
            )
            return cur.rowcount > 0

    def upcoming_events(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """SELECT * FROM events WHERE starts_at IS NULL OR starts_at >= ?
               ORDER BY starts_at IS NULL, starts_at LIMIT ?""",
            (now_iso()[:10], limit),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["tickers"] = json.loads(d["tickers"] or "[]")
            out.append(d)
        return out

    def mark_event_notified(self, event_id: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE events SET notified=1 WHERE id=?", (event_id,))

    # ---------------------------------------------------------- page snapshots
    def get_snapshot(self, source_id: str) -> dict[str, Any] | None:
        r = self._conn.execute(
            "SELECT * FROM page_snapshots WHERE source_id=?", (source_id,)
        ).fetchone()
        return dict(r) if r else None

    def set_snapshot(self, source_id: str, content_hash: str, excerpt: str) -> None:
        with self.tx() as c:
            c.execute(
                """INSERT INTO page_snapshots (source_id, content_hash, checked_at, excerpt)
                   VALUES (?,?,?,?)
                   ON CONFLICT(source_id) DO UPDATE SET content_hash=excluded.content_hash,
                   checked_at=excluded.checked_at, excerpt=excluded.excerpt""",
                (source_id, content_hash, now_iso(), excerpt[:500]),
            )

    # ---------------------------------------------------------------- account
    def add_account_snapshot(self, snap: dict[str, Any]) -> None:
        with self.tx() as c:
            c.execute(
                """INSERT OR REPLACE INTO account_snapshots
                   (taken_at, total, cash, invested, ppl, currency, raw) VALUES (?,?,?,?,?,?,?)""",
                (now_iso(), snap.get("total"), snap.get("cash"), snap.get("invested"),
                 snap.get("ppl"), snap.get("currency"), json.dumps(snap.get("raw", {}))),
            )

    def account_history(self, limit: int = 500) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM account_snapshots ORDER BY taken_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    # ----------------------------------------------------------------- prices
    def store_prices(self, ticker: str, series: list[tuple[str, float]]) -> None:
        with self.tx() as c:
            c.executemany(
                "INSERT OR REPLACE INTO prices (ticker, day, close) VALUES (?,?,?)",
                [(ticker, d, float(px)) for d, px in series],
            )

    def prices_for(self, ticker: str, limit: int = 60) -> list[tuple[str, float]]:
        rows = self._conn.execute(
            "SELECT day, close FROM prices WHERE ticker=? ORDER BY day DESC LIMIT ?",
            (ticker, limit),
        ).fetchall()
        return [(r["day"], r["close"]) for r in reversed(rows)]

    # ---------------------------------------------------------------- reports
    def add_report(self, markdown: str) -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO reports (created_at, markdown) VALUES (?,?)", (now_iso(), markdown)
            )
            return int(cur.lastrowid)

    def latest_report(self) -> dict[str, Any] | None:
        r = self._conn.execute(
            "SELECT * FROM reports ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(r) if r else None


def _row_item(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d["tickers"] = json.loads(d.get("tickers") or "[]")
    d["meta"] = json.loads(d.get("meta") or "{}")
    return d
