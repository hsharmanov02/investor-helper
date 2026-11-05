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
    if info.get("suspended"):
        return False, f"@{handle} does not exist or is suspended"
    if not info.get("exists"):
        # Page did not render (rate limit, slow load, logged out): undecided, try later.
        raise XProfileUnreadable(f"@{handle} profile did not load")
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


class XRateLimited(RuntimeError):
    pass


class XProfileUnreadable(RuntimeError):
    pass


class TabHijacked(ChromeUnavailable):
    """Our tab ended up somewhere we didn't navigate (e.g. the user clicked in it)."""


def _expect_url(page: Any, needle: str) -> None:
    if needle.lower() not in page.url.lower():
        raise TabHijacked(f"tab is on {page.url[:80]}, expected {needle}")


_X_LIMIT = re.compile(r"rate limit|try again later|something went wrong\. try reloading", re.I)


def _check_x_limit(page: Any) -> None:
    try:
        text = page.inner_text("body", timeout=3000)[:3000]
    except Exception:
        return
    if _X_LIMIT.search(text):
        raise XRateLimited("X says to slow down (rate limit)")


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
        self._page = _open_background_tab(browser, ctx)
        self._page.set_default_navigation_timeout(self.nav_timeout_ms)
        # Only Trading212 traffic goes through the guard, so X pages load at full speed.
        self._page.route(re.compile(r"https?://([^/]*\.)?trading212\.com/"), _guard_route)
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
        expected = url.split("://", 1)[1].rstrip("/")
        try:
            page.wait_for_selector('article[data-testid="tweet"]', timeout=15000)
        except Exception:
            _check_x_limit(page)
            log.warning("No posts rendered at %s (logged out?)", url)
            return []
        posts: dict[str, dict[str, Any]] = {}
        for _ in range(4):
            _expect_url(page, expected)
            for p in page.evaluate(X_EXTRACT_JS, max_posts * 2):
                if p.get("url") and not p.get("pinned"):
                    posts.setdefault(p["url"], p)
            if len(posts) >= max_posts:
                break
            page.mouse.wheel(0, 2500)  # scrolling reads more; it does not interact with content
            page.wait_for_timeout(1200)  # (time.sleep would freeze Playwright's event loop)
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
            _check_x_limit(page)
        _expect_url(page, f"x.com/{handle}")
        return page.evaluate(PROFILE_JS)

    def read_x_following(self, max_posts: int = 40) -> list[dict[str, Any]]:
        """The 'Following' home timeline: posts from every account you follow."""
        return self._read_x("https://x.com/home", max_posts)

    # ------------------------------------------------------------ Trading212
    def read_trading212(self, wait_s: float = 25.0) -> dict[str, Any]:
        """Open the Trading212 web app and read the Invest account. Never clicks anything.

        The web app fetches its own account summaries on load; we only read
        those responses (GET /rest/v1/customers/accounts/summary and the
        app-initiated multi-accounts summary). We never send requests ourselves.
        """
        page = self._page
        responses: list[Any] = []
        wanted = ("/customers/accounts/summary", "/multi-accounts/summary")

        def on_response(resp: Any) -> None:
            if "trading212.com" in resp.url and any(w in resp.url for w in wanted):
                responses.append(resp)

        page.on("response", on_response)
        try:
            page.goto("https://app.trading212.com/", wait_until="domcontentloaded")
            waited = 0
            # Must use wait_for_timeout: it keeps Playwright's event loop running.
            while waited < wait_s * 1000:
                page.wait_for_timeout(1000)
                waited += 1000
                urls = " ".join(r.url for r in responses)
                if all(w in urls for w in wanted) and waited >= 4000:
                    break
            captured = []
            for r in responses:
                try:
                    captured.append({"url": r.url, "json": r.json()})
                except Exception:
                    pass
            _expect_url(page, "trading212.com")
            text = page.inner_text("body")
        finally:
            page.remove_listener("response", on_response)
        snap = parse_trading212(captured, text)
        if snap.get("total") is None:
            low = text.lower()[:3000]
            if "log in" in low or "sign in" in low:
                raise ChromeUnavailable("Trading212 is not logged in in this Chrome profile.")
            raise ChromeUnavailable(
                f"Trading212 loaded but no account summary was seen ({len(captured)} summary responses).")
        return snap


def _open_background_tab(browser: Any, ctx: Any) -> Any:
    """Open a tab without bringing it to the front, so the user's work isn't interrupted."""
    try:
        cdp = browser.new_browser_cdp_session()
        with ctx.expect_page(timeout=10000) as info:
            cdp.send("Target.createTarget", {"url": "about:blank", "background": True})
        cdp.detach()
        return info.value
    except Exception as exc:  # fall back to a normal new tab
        log.info("Background tab not available (%s); opening a normal tab", exc)
        return ctx.new_page()


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
    """Pull the Invest (EQUITY) account figures from the web app's own JSON.

    `captured` holds {"url", "json"} entries. Shapes (live.services.trading212.com):
    * /rest/v1/equity/multi-accounts/summary:
      accountsByType.EQUITY.{accountCurrency, cash.{total, freeForStocks, stockInvestment, ppl, result}}
    * /rest/v1/customers/accounts/summary:
      LIVE.currentAccounts[] {tradingType: EQUITY|ISA|CFD, summaryValue.{value, currencyCode}}
    Only the EQUITY (Invest) account is used; ISA and CFD are ignored.
    """
    snap: dict[str, Any] = {"source": "chrome", "positions": []}

    def num(v: Any) -> float | None:
        try:
            return None if v is None else float(v)
        except (TypeError, ValueError):
            return None

    for entry in captured:
        url = entry.get("url", "") if isinstance(entry, dict) else ""
        data = entry.get("json") if isinstance(entry, dict) and "json" in entry else entry
        if not isinstance(data, dict):
            continue
        if "multi-accounts/summary" in url or "accountsByType" in data:
            eq = (data.get("accountsByType") or {}).get("EQUITY") or {}
            cash = eq.get("cash") or {}
            if num(cash.get("total")) is not None:
                snap["total"] = num(cash.get("total"))
                snap["cash"] = num(cash.get("freeForStocks"))
                snap["invested"] = num(cash.get("stockInvestment"))
                snap["ppl"] = num(cash.get("ppl"))
                snap["result"] = num(cash.get("result"))
                snap["currency"] = eq.get("accountCurrency") or data.get("activeAccountCurrency", "")
        elif "accounts/summary" in url or "LIVE" in data:
            for acc in ((data.get("LIVE") or {}).get("currentAccounts") or []):
                sv = acc.get("summaryValue") or {}
                if acc.get("tradingType") == "EQUITY" and num(sv.get("value")) is not None:
                    snap.setdefault("total_from_overview", num(sv.get("value")))
                    snap.setdefault("currency_from_overview", sv.get("currencyCode", ""))

    if snap.get("total") is None and snap.get("total_from_overview") is not None:
        snap["total"] = snap["total_from_overview"]
        snap["currency"] = snap.get("currency_from_overview", "")
    snap.pop("total_from_overview", None)
    snap.pop("currency_from_overview", None)
    return snap
