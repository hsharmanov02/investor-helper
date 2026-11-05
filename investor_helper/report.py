"""Builds the "thread" report: a numbered series of short posts in Markdown."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .analysis import market_hours
from .analysis.scoring import TickerScore

DISCLAIMER = ("Informational only, not financial advice. Investor Helper never places trades; "
              "every decision is yours. Predictions are statistical guesses and are often wrong.")


def _pct(x: float) -> str:
    return f"{x * 100:+.1f}%"


def _money(x: float | None, cur: str = "") -> str:
    if x is None:
        return "n/a"
    sym = {"GBP": "£", "USD": "$", "EUR": "€"}.get(cur, "")
    return f"{sym}{x:,.2f}" + ("" if sym or not cur else f" {cur}")


def build_report(*, ranked: list[TickerScore], horizon_days: int, max_items: int,
                 events: list[dict[str, Any]], ipos: list[dict[str, Any]],
                 filings: list[dict[str, Any]], account: dict[str, Any] | None,
                 account_prev: dict[str, Any] | None, weights: dict[str, dict[str, Any]],
                 source_names: dict[str, str], stats: dict[str, Any],
                 rotation: list[str], suggestions: list[tuple[str, int]],
                 now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    posts: list[str] = []

    open_now = [ex for ex, o in market_hours.status(now).items() if o]
    hr = stats.get("hit_rate")
    track = (f"Track record so far: {stats['hits']}/{stats['resolved']} calls right "
             f"({hr * 100:.0f}%)." if hr is not None else "No graded predictions yet; accuracy appears after the first horizon passes.")
    posts.append(
        f"**Investor Helper report, {now.strftime('%a %d %b %Y %H:%M')} UTC**\n\n"
        f"Markets open now: {', '.join(open_now) if open_now else 'none (holidays not modelled)'}.\n"
        f"{track}"
    )

    buys = [s for s in ranked if s.predicted_move > 0.003][:max_items]
    if buys:
        lines = [f"**Worth a look to buy (next {horizon_days} days)**", ""]
        for s in buys:
            price = (f"{s.last_price:,.2f} → {s.predicted_price:,.2f}" if s.last_price else "price n/a")
            lines.append(f"- **{s.ticker}** {_pct(s.predicted_move)} ({price}), confidence {s.confidence * 100:.0f}%. "
                         f"News {s.news_score:+.2f}, momentum {s.momentum_score:+.2f}, fundamentals {s.fundamental_score:+.2f}; "
                         f"{s.n_items} items from {s.n_sources} sources.")
            for h in s.headlines[:2]:
                lines.append(f"  - [{h['title'][:110]}]({h['url']}) ({source_names.get(h['source_id'], h['source_id'])})")
        posts.append("\n".join(lines))
    else:
        posts.append("**Worth a look to buy**\n\nNothing clears the bar right now.")

    avoid = [s for s in reversed(ranked) if s.predicted_move < -0.003][:5]
    if avoid:
        posts.append("**Leaning down (caution / hold off)**\n\n" + "\n".join(
            f"- **{s.ticker}** {_pct(s.predicted_move)}, confidence {s.confidence * 100:.0f}%" for s in avoid))

    if account:
        cur = account.get("currency", "")
        line = f"**Your Trading212 account**\n\nValue {_money(account.get('total'), cur)}"
        if account_prev and account_prev.get("total") and account.get("total"):
            d = account["total"] - account_prev["total"]
            line += f" ({'+' if d >= 0 else ''}{_money(d, cur)} since last check)"
        line += f". Cash {_money(account.get('cash'), cur)}, invested {_money(account.get('invested'), cur)}, unrealised P/L {_money(account.get('ppl'), cur)}."
        held = {s.ticker: s for s in ranked}
        pos_lines = []
        for p in account.get("positions", [])[:15]:
            s = held.get(p["ticker"])
            view = f", outlook {_pct(s.predicted_move)} ({s.confidence * 100:.0f}% conf.)" if s else ""
            pos_lines.append(f"- {p['ticker']}: {p['quantity']:g} @ {p['current_price']:,.2f}, P/L {p['ppl']:+,.2f}{view}")
        if pos_lines:
            line += "\n\n" + "\n".join(pos_lines)
        posts.append(line)

    if events:
        posts.append("**Coming up**\n\n" + "\n".join(
            f"- {(e.get('starts_at') or 'date tbc')[:16].replace('T', ' ')}: "
            + (f"[{e['title'][:120]}]({e['url']})" if e.get("url") else e["title"][:120])
            for e in events[:12]))

    if ipos:
        posts.append("**IPO pipeline**\n\n" + "\n".join(
            f"- {i.get('date') or 'tbc'}: {i.get('company')} ({i.get('ticker') or 'no ticker yet'}) "
            f"{i.get('exchange', '')} {i.get('price_range', '')} [{i.get('status')}]" for i in ipos[:10]))

    if filings:
        posts.append("**Notable SEC filings**\n\n" + "\n".join(
            f"- [{f['title'][:110]}]({f['url']})" + (f" {', '.join(f['tickers'])}" if f.get("tickers") else "")
            for f in filings[:10]))

    graded = [(sid, w) for sid, w in weights.items() if not sid.startswith("factor:") and w["hits"] + w["misses"] >= 3]
    if graded:
        graded.sort(key=lambda kv: kv[1]["weight"], reverse=True)
        top = ", ".join(f"{source_names.get(s, s)} {w['weight']:.2f}" for s, w in graded[:5])
        bottom = ", ".join(f"{source_names.get(s, s)} {w['weight']:.2f}" for s, w in graded[-3:])
        posts.append(f"**Source scoreboard** (1.0 = neutral)\n\nMost reliable: {top}\n\nLeast reliable: {bottom}")
    if rotation:
        posts.append("**Source changes**\n\n" + "\n".join(f"- {c}" for c in rotation))
    if suggestions:
        posts.append("**Accounts your best sources keep citing** (add them in Sources if you trust them)\n\n"
                     + ", ".join(f"@{h} ({n})" for h, n in suggestions))

    posts.append(f"_{DISCLAIMER}_")
    n = len(posts)
    return "\n\n---\n\n".join(f"{i}/{n} {p}" for i, p in enumerate(posts, start=1))
