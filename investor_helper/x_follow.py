"""Manage which X accounts @HSfrHSfr2 follows, based on learned source weights.

This is the ONLY module in the app allowed to click anything, and it only ever
clicks a profile's own Follow/Unfollow button (plus X's unfollow confirmation)
on x.com. It never touches Trading212. Rules, agreed with the user:

1. Identity first: display name must match the expected outlet (whole words),
   the account must be public, and the badge rule must pass:
   gold or grey badge OK; blue only if the profile website is the outlet's
   domain, it has 500k+ followers (100k for journalists) and joined before
   2015; no badge -> skip.
2. Only the Follow button carrying the profile's own handle, inside the
   profile's main column, is clicked.
3. Follows are 20-40 s apart, at most a few changes per day, and any X
   rate-limit page stops X for the rest of the day.
4. The browser tab must still be on the profile we opened, and the logged-in
   account must be the expected one, or nothing is clicked.
5. Every change is logged with its reason and shown in the report.
On replace, the new account is followed first; the old one is unfollowed only
after that succeeded.
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlparse

from .config import data_dir

log = logging.getLogger(__name__)

EXPECTED_ACCOUNT = "HSfrHSfr2"
MAX_CHANGES_PER_DAY = 3
BLUE_MIN_FOLLOWERS = 500_000
BLUE_MIN_FOLLOWERS_JOURNALIST = 100_000
BLUE_JOINED_BEFORE = 2015

# Official websites, used for the blue-badge rule.
DOMAINS = {
    "Reuters": "reuters.com", "ReutersBiz": "reuters.com", "business": "bloomberg.com",
    "markets": "bloomberg.com", "BloombergTV": "bloomberg.com", "WSJ": "wsj.com",
    "WSJmarkets": "wsj.com", "FT": "ft.com", "CNBC": "cnbc.com", "CNBCnow": "cnbc.com",
    "SquawkCNBC": "cnbc.com", "MarketWatch": "marketwatch.com", "YahooFinance": "finance.yahoo.com",
    "FoxBusiness": "foxbusiness.com", "BBCBusiness": "bbc.co.uk", "nytimesbusiness": "nytimes.com",
    "TheEconomist": "economist.com", "Investingcom": "investing.com", "Benzinga": "benzinga.com",
    "SeekingAlpha": "seekingalpha.com", "NickTimiraos": "wsj.com", "carlquintanilla": "cnbc.com",
    "SECGov": "sec.gov", "federalreserve": "federalreserve.gov", "ecb": "ecb.europa.eu",
    "bankofengland": "bankofengland.co.uk", "USTreasury": "treasury.gov", "BLS_gov": "bls.gov",
    "BEA_News": "bea.gov", "EIAgov": "eia.gov", "IMFNews": "imf.org", "CFTC": "cftc.gov",
    "FINRA": "finra.org", "TheFCA": "fca.org.uk", "NYSE": "nyse.com", "Nasdaq": "nasdaq.com",
    "CMEGroup": "cmegroup.com", "LSEGplc": "lseg.com", "euronext": "euronext.com",
    "OPECSecretariat": "opec.org", "IEA": "iea.org", "IPOtweet": "renaissancecapital.com",
    "eWhispers": "earningswhispers.com", "Trading212": "trading212.com",
}

FACTS_JS = r"""
(handle) => {
  const col = document.querySelector('[data-testid="primaryColumn"]') || document;
  const q = s => col.querySelector(s);
  const nameEl = q('[data-testid="UserName"]');
  const badge = nameEl ? nameEl.querySelector('svg[data-testid="icon-verified"], svg[aria-label*="erified"]') : null;
  let fill = '';
  if (badge) {
    const p = badge.querySelector('path[fill], g[fill]');
    fill = (p && p.getAttribute('fill')) || badge.getAttribute('fill') || getComputedStyle(badge).color || '';
    if (badge.querySelector('linearGradient')) fill = 'gradient';
  }
  const url = q('[data-testid="UserUrl"]');
  const joined = q('[data-testid="UserJoinDate"]');
  let followers = '';
  for (const a of col.querySelectorAll('a[href$="/verified_followers"], a[href$="/followers"]')) {
    if (/followers/i.test(a.innerText)) { followers = a.innerText; break; }
  }
  const h = handle.toLowerCase();
  const btn = sel => Array.from(col.querySelectorAll(sel)).find(b =>
      (b.getAttribute('aria-label') || '').toLowerCase().endsWith('@' + h));
  const me = document.querySelector('[data-testid="SideNav_AccountSwitcher_Button"]');
  const body = document.body ? document.body.innerText : '';
  return {
    exists: !!nameEl,
    name: nameEl ? nameEl.innerText.split('\n')[0] : '',
    badge_fill: fill,
    has_badge: !!badge,
    website: url ? (url.getAttribute('href') && !url.getAttribute('href').startsWith('https://t.co') ? url.getAttribute('href') : url.innerText) : '',
    joined: joined ? joined.innerText : '',
    followers: followers,
    protected: !!q('[data-testid="icon-lock"]') || /posts are protected/i.test(body),
    following: !!btn('[data-testid$="-unfollow"]'),
    can_follow: !!btn('[data-testid$="-follow"]'),
    me: me ? me.innerText : '',
  };
}
"""


# ----------------------------------------------------------------- checks
def badge_type(fill: str, has_badge: bool) -> str:
    if not has_badge:
        return "none"
    f = (fill or "").lower().replace(" ", "")
    if "gradient" in f or "url(" in f or "#d18800" in f or "rgb(209,136,0)" in f or "#e2b719" in f:
        return "gold"
    if "#829aab" in f or "rgb(130,154,171)" in f:
        return "grey"
    return "blue"


def parse_count(text: str) -> int:
    m = re.search(r"([\d.,]+)\s*([KkMmBb]?)", text or "")
    if not m:
        return 0
    n = float(m.group(1).replace(",", ""))
    return int(n * {"k": 1e3, "m": 1e6, "b": 1e9}.get(m.group(2).lower(), 1))


def joined_year(text: str) -> int | None:
    m = re.search(r"(19|20)\d\d", text or "")
    return int(m.group(0)) if m else None


def website_host(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    host = urlparse(url).hostname or ""
    return host.lower().removeprefix("www.")


def name_matches(display: str, expected: list[str]) -> bool:
    """Whole-word match: every word of one expected name appears in sequence as whole words."""
    display = re.sub(r"@\w+", " ", display)  # the @handle is not part of the name
    words = re.findall(r"[a-z0-9.&]+", display.lower())
    joined = " " + " ".join(words) + " "
    for e in expected:
        ew = re.findall(r"[a-z0-9.&]+", e.lower())
        if ew and (" " + " ".join(ew) + " ") in joined:
            return True
    return False


def identity_failures(source: dict[str, Any], facts: dict[str, Any]) -> list[str]:
    """Reasons this profile must NOT be followed. Empty list means it passes."""
    h = source.get("handle", "")
    out: list[str] = []
    if not facts.get("exists"):
        return [f"@{h} profile did not load"]
    if facts.get("protected"):
        out.append(f"@{h} is a private account")
    expected = source.get("expect") or [source.get("name", h)]
    if not name_matches(facts.get("name", ""), expected):
        out.append(f"@{h} is named '{facts.get('name', '')}', not {' / '.join(expected)}")
    badge = badge_type(facts.get("badge_fill", ""), facts.get("has_badge", False))
    domain = source.get("domain") or DOMAINS.get(h, "")
    host = website_host(facts.get("website", ""))
    domain_ok = bool(domain) and (host == domain or host.endswith("." + domain))
    if badge == "none":
        out.append(f"@{h} has no verified badge")
    elif badge == "blue":
        need = BLUE_MIN_FOLLOWERS_JOURNALIST if source.get("category") == "journalist" else BLUE_MIN_FOLLOWERS
        n = parse_count(facts.get("followers", ""))
        year = joined_year(facts.get("joined", ""))
        if not domain_ok:
            out.append(f"@{h} blue badge but website '{host or 'none'}' is not {domain or 'a known domain'}")
        if n < need:
            out.append(f"@{h} blue badge with only {n:,} followers (needs {need:,})")
        if not year or year >= BLUE_JOINED_BEFORE:
            out.append(f"@{h} blue badge but joined {year or 'unknown'} (needs before {BLUE_JOINED_BEFORE})")
    return out


# ------------------------------------------------------------------ state
def _state_path():
    return data_dir() / "x_follows.json"


def load_state() -> dict[str, Any]:
    p = _state_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"accounts": {}, "log": [], "pending": [], "day": "", "changes_today": 0, "stopped_until": ""}


def save_state(st: dict[str, Any]) -> None:
    st["log"] = st.get("log", [])[-300:]
    _state_path().write_text(json.dumps(st, indent=2), encoding="utf-8")


def seed_state_from_vetted(st: dict[str, Any], vetted: dict[str, Any]) -> None:
    """Record what the setup script already followed, so we don't redo it."""
    for a in vetted.get("accounts", []):
        st["accounts"].setdefault(a["handle"].lower(), {
            "status": "followed" if a.get("follow_status") == "followed" else (a.get("follow_status") or "unknown"),
            "by": "setup", "at": vetted.get("created", "")})


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------- planning
def plan_changes(sources: list[dict[str, Any]], weights: dict[str, dict[str, Any]],
                 st: dict[str, Any], exceptions: set[str] | None = None) -> list[dict[str, Any]]:
    """Decide follow/unfollow/replace actions. Pure: no browser access.

    * unfollow: a followed X source that the learner switched off for poor accuracy.
      If a trial replacement exists, it is a replace (follow new first).
    * follow: an enabled X source (e.g. a trialled replacement or one the user
      added) that we don't follow yet and that wasn't rejected by the badge rule.
    """
    exceptions = {e.lower() for e in (exceptions or set())}
    accts = st.get("accounts", {})
    xs = [s for s in sources if s["kind"] == "x" and s.get("handle")]
    followed = lambda h: accts.get(h.lower(), {}).get("status") == "followed"
    rejected = lambda h: (accts.get(h.lower(), {}).get("status", "").startswith("skipped")
                          and h.lower() not in exceptions)

    to_follow = [s for s in xs if s.get("enabled") and not followed(s["handle"]) and not rejected(s["handle"])
                 and accts.get(s["handle"].lower(), {}).get("status") != "unfollowed"]
    to_drop = [s for s in xs if not s.get("enabled") and followed(s["handle"])
               and str(s.get("disabled_reason", "")).startswith("auto: weight")]

    actions: list[dict[str, Any]] = []
    for old in to_drop:
        repl = next((n for n in to_follow if n.get("added_reason", "") == f"replacing {old['name']}"), None)
        w = weights.get(old["id"], {})
        why = f"right {w.get('hits', 0)} / wrong {w.get('misses', 0)}, weight {w.get('weight', 0):.2f}"
        if repl:
            to_follow.remove(repl)
            actions.append({"action": "replace", "handle": repl["handle"], "old_handle": old["handle"],
                            "reason": f"@{old['handle']} kept being wrong ({why}); trying @{repl['handle']}"})
        else:
            actions.append({"action": "unfollow", "handle": old["handle"], "reason": f"kept being wrong ({why})"})
    for s in to_follow:
        actions.append({"action": "follow", "handle": s["handle"],
                        "reason": s.get("added_reason") or "new source in your list"})
    return actions


# ----------------------------------------------------------------- acting
class XFollowStopped(RuntimeError):
    pass


class XFollower:
    """Performs follow/unfollow on one Playwright page. Only acts on x.com."""

    def __init__(self, page: Any, expected_account: str = EXPECTED_ACCOUNT,
                 sleep: Callable[[float], None] | None = None):
        self.page = page
        self.expected_account = expected_account
        self.sleep = sleep or (lambda s: page.wait_for_timeout(int(s * 1000)))

    def _on_profile(self, handle: str) -> None:
        url = self.page.url.lower()
        if not re.match(rf"https://(www\.)?x\.com/{re.escape(handle.lower())}/?(\?|$)", url):
            raise XFollowStopped(f"tab left the profile (now {self.page.url[:80]}); not clicking")

    def _limited(self) -> bool:
        try:
            text = self.page.inner_text("body", timeout=3000)[:3000]
        except Exception:
            return False
        return bool(re.search(r"rate limit|try again later|unable to follow more", text, re.I))

    def facts(self, handle: str) -> dict[str, Any]:
        self.page.goto(f"https://x.com/{handle}", wait_until="domcontentloaded")
        try:
            self.page.wait_for_selector('[data-testid="primaryColumn"] [data-testid="UserName"]', timeout=15000)
        except Exception:
            pass
        if self._limited():
            raise XFollowStopped("X rate limit page")
        self._on_profile(handle)
        f = self.page.evaluate(FACTS_JS, handle)
        if f.get("me") and f"@{self.expected_account.lower()}" not in f["me"].lower():
            raise XFollowStopped(f"logged in as {f['me'].splitlines()[-1][:40]}, not @{self.expected_account}")
        return f

    def _button(self, handle: str, kind: str) -> Any:
        sel = f'[data-testid="primaryColumn"] [data-testid$="-{kind}"][aria-label$="@{handle}" i]'
        loc = self.page.locator(sel)
        return loc.first if loc.count() == 1 else None

    def _wait_state(self, handle: str, kind: str, timeout_s: float = 15) -> bool:
        waited = 0.0
        while waited < timeout_s:
            if self._button(handle, kind) is not None:
                return True
            self.sleep(1)
            waited += 1
        return False

    def follow(self, source: dict[str, Any]) -> tuple[bool, str]:
        h = source["handle"]
        f = self.facts(h)
        if f.get("following"):
            return True, "already following"
        problems = identity_failures(source, f)
        if problems:
            return False, "; ".join(problems)
        self._on_profile(h)
        btn = self._button(h, "follow")
        if btn is None:
            return False, "own Follow button not found"
        btn.click()
        if self._wait_state(h, "unfollow"):
            return True, "followed"
        if self._limited():
            raise XFollowStopped("X rate limit after clicking Follow")
        return False, "Follow did not take effect"

    def unfollow(self, handle: str) -> tuple[bool, str]:
        f = self.facts(handle)
        if not f.get("following"):
            return True, "was not following"
        self._on_profile(handle)
        btn = self._button(handle, "unfollow")
        if btn is None:
            return False, "own Following button not found"
        btn.click()
        confirm = self.page.locator('[data-testid="confirmationSheetConfirm"]')
        try:
            confirm.wait_for(timeout=8000)
            confirm.click()
        except Exception:
            return False, "unfollow confirmation did not appear"
        if self._wait_state(handle, "follow"):
            return True, "unfollowed"
        return False, "Unfollow did not take effect"


def run(page: Any, sources: list[dict[str, Any]], weights: dict[str, dict[str, Any]], mode: str,
        notify: Callable[[str, str], None], exceptions: set[str] | None = None,
        sleep: Callable[[float], None] | None = None) -> list[str]:
    """Plan and (in auto mode, or for approved items) perform follow changes. Returns log lines."""
    st = load_state()
    try:
        from .config import bundled_path
        seed_state_from_vetted(st, json.loads(bundled_path("investor_helper", "data", "official_x_accounts.json")
                                              .read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        pass
    if st.get("day") != _today():
        st["day"], st["changes_today"] = _today(), 0
    lines: list[str] = []
    if mode == "off" or st.get("stopped_until", "") > _now():
        save_state(st)
        return lines
    plan = plan_changes(sources, weights, st, exceptions)
    if mode == "approve":
        approved = {(p["action"], p["handle"]) for p in st.get("pending", []) if p.get("approved")}
        st["pending"] = [dict(p, approved=False) for p in plan]
        plan = [p for p in plan if (p["action"], p["handle"]) in approved]
    by_handle = {s["handle"].lower(): s for s in sources if s["kind"] == "x" and s.get("handle")}
    follower = XFollower(page, sleep=sleep)
    pause = sleep or (lambda s: time.sleep(s))

    def record(handle: str, status: str, msg: str) -> None:
        st["accounts"][handle.lower()] = {"status": status, "by": "investor-helper", "at": _now(), "reason": msg}
        st["log"].append({"at": _now(), "handle": handle, "status": status, "reason": msg})
        lines.append(f"{status.capitalize()} @{handle}: {msg}")

    try:
        for i, act in enumerate(plan):
            if st["changes_today"] >= MAX_CHANGES_PER_DAY:
                lines.append(f"Daily limit of {MAX_CHANGES_PER_DAY} follow changes reached; the rest wait until tomorrow.")
                break
            if i:
                pause(random.uniform(20, 40))
            if act["action"] in ("follow", "replace"):
                src = by_handle.get(act["handle"].lower(), {"handle": act["handle"], "name": act["handle"]})
                ok, msg = follower.follow(src)
                if ok:
                    record(act["handle"], "followed", act["reason"])
                    st["changes_today"] += 1
                else:
                    record(act["handle"], "skipped", msg)
                    continue  # never unfollow the old one if the new one didn't work
                if act["action"] == "replace":
                    pause(random.uniform(20, 40))
                    ok, msg = follower.unfollow(act["old_handle"])
                    record(act["old_handle"], "unfollowed" if ok else "unfollow_failed",
                           act["reason"] if ok else msg)
                    st["changes_today"] += 1 if ok else 0
            else:
                ok, msg = follower.unfollow(act["handle"])
                record(act["handle"], "unfollowed" if ok else "unfollow_failed", act["reason"] if ok else msg)
                st["changes_today"] += 1 if ok else 0
    except XFollowStopped as exc:
        st["stopped_until"] = f"{_today()}T23:59:59+00:00"
        lines.append(f"Stopped X follow changes for today: {exc}")
        notify("X follow changes paused", str(exc)[:200])
    finally:
        save_state(st)
    if lines:
        notify("X follows updated", "; ".join(lines)[:250])
    return lines


def pending_summary() -> list[dict[str, Any]]:
    return load_state().get("pending", [])


def approve_all_pending() -> int:
    st = load_state()
    for p in st.get("pending", []):
        p["approved"] = True
    save_state(st)
    return len(st.get("pending", []))
