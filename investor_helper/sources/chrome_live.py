"""Live, read-only access to X and Trading212 through the user's own Chrome.

Chrome must be running with ``--remote-debugging-port=9222`` (see README).
We attach with Playwright over CDP, open ONE extra background tab, read pages,
and close that tab again. Your own tabs are never touched.

Safety rules enforced here:
* No click(), fill(), type() or keyboard calls exist in this module.
* Any non-GET request from our tab whose URL mentions order/trade/instrument
  execution is aborted by a route guard, so even a page script cannot place
  an order from the tab we control.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

_ORDER_URL = re.compile(r"order|trade|execut|/pies", re.I)
_MONEY = re.compile(r"([£$€])\s?([\d,]+(?:\.\d{1,2})?)")

X_EXTRACT_JS = """
(max) => {
  const out = [];
  for (const a of document.querySelectorAll('article[data-testid="tweet"]')) {
    const ctx = a.querySelector('[data-testid="socialContext"]');
    const pinned = ctx && /pinned/i.test(ctx.innerText || '');
    const t = a.querySelector('time');
    const link = t && t.closest('a') ? t.closest('a').href : '';
    const textEl = a.querySelector('[data-testid="tweetText"]');
    const user = a.querySelector('[data-testid="User-Name"]');
    const links = Array.from(a.querySelectorAll('[data-testid="tweetText"] a'))
      .map(x => x.href).filter(h => h && !h.includes('x.com/hashtag'));
    out.push({
      text: textEl ? textEl.innerText : '',
      time: t ? t.getAttribute('datetime') : '',
      url: link, pinned: !!pinned,
      author: user ? user.innerText.split('\\n')[0] : '',
      links: links,
    });
    if (out.length >= max) break;
  }
  return out;
}
"""


PROFILE_JS = """
() => {
  const q = s => document.querySelector(s);
  const name = q('[data-testid="UserName"]');
  const bio = q('[data-testid="UserDescription"]');
  const body = document.body ? document.body.innerText : '';
  return {
    exists: !!name,
    name: name ? name.innerText : '',
    bio: bio ? bio.innerText : '',
    verified: !!(name && name.querySelector('[data-testid="icon-verified"], svg[aria-label*="erified"]')),
    protected: !!q('[data-testid="icon-lock"]') || /posts are protected/i.test(body),
    suspended: /account suspended|doesn.t exist/i.test(body),
  };
}
"""


def verify_x_profile(source: dict[str, Any], info: dict[str, Any]) -> tuple[bool, str]:
    """Is this profile really the outlet we expect? Returns (ok, reason).

    Rules: the account must exist, be public and not suspended, and its display
    name must contain one of the source's expected names. Anything else
    is skipped and reported, never read or followed.
    """
    handle = source.get("handle", "")
    if not info.get("exists") or info.get("suspended"):
        return False, f"@{handle} does not exist or is suspended"
    if info.get("protected"):
        return False, f"@{handle} is a private account"
    expect = source.get("expect") or [source.get("name", handle)]
    # Display name only, with the @handle stripped: a bio that merely mentions
    # an outlet ("ex-Reuters reporter") is not enough.
    shown = " ".join(w for w in re.split(r"\s+", info.get("name", "")) if not w.startswith("@")).strip()
    if not any(e.lower() in shown.lower() for e in expect if e):
        return False, f"@{handle} is '{shown}', which does not match {', '.join(expect)}"
    return True, "verified" + (" (blue check)" if info.get("verified") else "")


class ChromeUnavailable(RuntimeError):
    pass


class ChromeLive:
    """Use as a context manager: ``with ChromeLive(url) as c: c.read_x_profile('Reuters')``."""

    def __init__(self, cdp_url: str = "http://localhost:9222", nav_timeout_ms: int = 30000):
        self.cdp_url = cdp_url
        self.nav_timeout_ms = nav_timeout_ms
        self._pw = None
        self._page = None

    def __enter__(self) -> "ChromeLive":
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover
            raise ChromeUnavailable("Playwright is not installed") from exc
        self._pw = sync_playwright().start()
        try:
            browser = self._pw.chromium.connect_over_cdp(self.cdp_url, timeout=10000)
        except Exception as exc:
            self._pw.stop()
            self._pw = None
            raise ChromeUnavailable(
                f"Could not reach Chrome at {self.cdp_url}. Start Chrome with "
                "--remote-debugging-port=9222 (see README)."
            ) from exc
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        self._page = ctx.new_page()
        self._page.set_default_navigation_timeout(self.nav_timeout_ms)
        self._page.route("**/*", _guard_route)
        return self

    def __exit__(self, *exc: Any) -> None:
        try:
            if self._page is not None:
                release_page(self._page)
        except Exception:
            pass
        # Stopping Playwright only drops our connection; Chrome keeps running.
        if self._pw is not None:
            self._pw.stop()
        self._page = None
        self._pw = None

    # ------------------------------------------------------------------- X
    def _read_x(self, url: str, max_posts: int) -> list[dict[str, Any]]:
        page = self._page
        page.goto(url, wait_until="domcontentloaded")
        try:
            page.wait_for_selector('article[data-testid="tweet"]', timeout=15000)
        except Exception:
            log.warning("No posts rendered at %s (logged out or rate-limited?)", url)
            return []
        posts: dict[str, dict[str, Any]] = {}
        for _ in range(4):
            for p in page.evaluate(X_EXTRACT_JS, max_posts * 2):
                if p.get("url") and not p.get("pinned"):
                    posts.setdefault(p["url"], p)
            if len(posts) >= max_posts:
                break
            page.mouse.wheel(0, 2500)  # scrolling reads more; it does not interact with content
            time.sleep(1.2)
        return list(posts.values())[:max_posts]

    def read_x_profile(self, handle: str, max_posts: int = 15) -> list[dict[str, Any]]:
        """Posts written by this account only (reposts of other accounts are dropped)."""
        posts = self._read_x(f"https://x.com/{handle}", max_posts)
        own = f"x.com/{handle.lower()}/status/"
        return [p for p in posts if own in p.get("url", "").lower()]

    def x_profile_info(self, handle: str) -> dict[str, Any]:
        """Read the profile header (name, bio, verified, protected). Never follows."""
        page = self._page
        page.goto(f"https://x.com/{handle}", wait_until="domcontentloaded")
        try:
            page.wait_for_selector('[data-testid="UserName"], [data-testid="emptyState"]', timeout=15000)
        except Exception:
            pass
        return page.evaluate(PROFILE_JS)

    def read_x_following(self, max_posts: int = 40) -> list[dict[str, Any]]:
        """The 'Following' home timeline: posts from every account you follow."""
        return self._read_x("https://x.com/home", max_posts)

    # ------------------------------------------------------------ Trading212
    def read_trading212(self, wait_s: float = 12.0) -> dict[str, Any]:
        """Open the Trading212 web app and read account value. Never clicks anything."""
        page = self._page
        captured: list[Any] = []

        def on_response(resp: Any) -> None:
            try:
                if "trading212.com" in resp.url and "json" in (resp.headers.get("content-type") or ""):
                    captured.append(resp.json())
            except Exception:
                pass

        page.on("response", on_response)
        page.goto("https://app.trading212.com/", wait_until="domcontentloaded")
        deadline = time.time() + wait_s
        while time.time() < deadline:
            time.sleep(1)
        text = page.inner_text("body")
        page.remove_listener("response", on_response)
        if "log in" in text.lower()[:2000] and "account value" not in text.lower():
            raise ChromeUnavailable("Trading212 is not logged in in this Chrome profile.")
        return parse_trading212(captured, text)


def release_page(page: Any) -> None:
    """Close our tab, unless it is the last normal tab (closing that would quit Chrome)."""
    others = [p for p in page.context.pages
              if p is not page and not p.url.startswith(("chrome-extension://", "devtools://"))]
    if others:
        page.close()
    else:
        page.goto("about:blank")


def _guard_route(route: Any) -> None:
    req = route.request
    if req.method != "GET" and "trading212" in req.url and _ORDER_URL.search(req.url):
        log.error("Blocked a %s to %s from the Investor Helper tab", req.method, req.url)
        route.abort()
        return
    route.continue_()


# --------------------------------------------------------------- parsing
def parse_x_posts(source: dict[str, Any], posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for p in posts:
        m = re.search(r"/status/(\d+)", p.get("url", ""))
        if not m or not p.get("text"):
            continue
        text = p["text"]
        out.append({
            "id": f"{source['id']}:{m.group(1)}",
            "source_id": source["id"],
            "kind": "news",
            "title": text[:280],
            "summary": text,
            "url": p["url"],
            "published_at": p.get("time") or datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "meta": {"links": p.get("links", []), "author": p.get("author", ""),
                     "mentions": re.findall(r"(?<![\w@])@([A-Za-z0-9_]{2,15})", text)},
        })
    return out


def _walk(o: Any):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)


def parse_trading212(captured: list[Any], body_text: str) -> dict[str, Any]:
    """Pull account figures from the web app's own JSON, falling back to page text."""
    snap: dict[str, Any] = {"source": "chrome", "positions": []}
    for blob in captured:
        for d in _walk(blob):
            keys = set(d)
            if "total" in keys and keys & {"free", "invested", "ppl", "result", "freeForInvest"}:
                try:
                    snap.setdefault("total", float(d["total"]))
                    if "free" in d or "freeForInvest" in d:
                        snap.setdefault("cash", float(d.get("free", d.get("freeForInvest"))))
                    if "invested" in d:
                        snap.setdefault("invested", float(d["invested"]))
                    if "ppl" in d:
                        snap.setdefault("ppl", float(d["ppl"]))
                except (TypeError, ValueError):
                    pass
            if "currencyCode" in keys and isinstance(d.get("currencyCode"), str):
                snap.setdefault("currency", d["currencyCode"])
            if {"ticker", "quantity"} <= keys and ("currentPrice" in keys or "averagePrice" in keys):
                try:
                    from .trading212 import plain_ticker
                    qty = float(d["quantity"])
                    cur = float(d.get("currentPrice") or 0)
                    snap["positions"].append({
                        "ticker": plain_ticker(str(d["ticker"])), "t212_ticker": d["ticker"],
                        "quantity": qty, "average_price": float(d.get("averagePrice") or 0),
                        "current_price": cur, "value": qty * cur, "ppl": float(d.get("ppl") or 0),
                    })
                except (TypeError, ValueError):
                    pass
    if "total" not in snap:
        # Fallback: the number shown next to "Account value" / "Total".
        m = re.search(r"(?:account value|total value|total)\s*[\n:]*\s*([£$€])\s?([\d,]+(?:\.\d{1,2})?)",
                      body_text, re.I)
        if m:
            snap["total"] = float(m.group(2).replace(",", ""))
            snap.setdefault("currency", {"£": "GBP", "$": "USD", "€": "EUR"}[m.group(1)])
    # De-duplicate positions captured from several responses.
    seen = {}
    for p in snap["positions"]:
        seen[p["t212_ticker"]] = p
    snap["positions"] = list(seen.values())
    return snap


def check_connection(cdp_url: str = "http://localhost:9222") -> str:
    """Connect, report what we see, disconnect. Opens no pages."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.connect_over_cdp(cdp_url, timeout=8000)
        except Exception as exc:
            raise ChromeUnavailable(
                f"Could not reach Chrome at {cdp_url}. Start Chrome with --remote-debugging-port=9222."
            ) from exc
        pages = [p for c in browser.contexts for p in c.pages]
        urls = " ".join(p.url for p in pages)
        return (f"Connected to Chrome {browser.version} with {len(pages)} tabs open. "
                f"X tab open: {'yes' if 'x.com' in urls else 'no'}; "
                f"Trading212 tab open: {'yes' if 'trading212.com' in urls else 'no'}.")
