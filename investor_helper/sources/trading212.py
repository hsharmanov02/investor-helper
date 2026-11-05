"""Trading212 public API, read-only.

Only these GET endpoints are used:
  /equity/account/info   currency and account id
  /equity/account/cash   free cash, invested, unrealised P/L, total
  /equity/portfolio      open positions
There is intentionally no code here that places, modifies or cancels orders,
and `ReadOnlyTrading212` refuses any path outside the allow-list.

Create the key in Trading212 (Invest account) -> Settings -> API (Beta) and
give it only the "Account data" and "Portfolio" read permissions.
"""
from __future__ import annotations

import logging
from typing import Any

from ..net import get

log = logging.getLogger(__name__)

BASES = {"live": "https://live.trading212.com/api/v0", "demo": "https://demo.trading212.com/api/v0"}
ALLOWED_PATHS = frozenset({"/equity/account/info", "/equity/account/cash", "/equity/portfolio"})


class Trading212Error(RuntimeError):
    pass


class ReadOnlyTrading212:
    def __init__(self, api_key: str, api_secret: str = "", environment: str = "live"):
        if not api_key:
            raise Trading212Error("No Trading212 API key set (Settings tab).")
        self.base = BASES.get(environment, BASES["live"])
        self.api_key = api_key
        self.api_secret = api_secret

    def _get(self, path: str) -> Any:
        if path not in ALLOWED_PATHS:
            raise Trading212Error(f"Blocked: {path} is not a read-only endpoint this app may call.")
        if self.api_secret:
            r = get(self.base + path, auth=(self.api_key, self.api_secret), min_interval=2.0)
        else:
            r = get(self.base + path, headers={"Authorization": self.api_key}, min_interval=2.0)
        if r.status_code == 401:
            raise Trading212Error("Trading212 rejected the API key (401). Check it in Settings.")
        if r.status_code == 403:
            raise Trading212Error("The API key lacks read permission for this data (403).")
        if not r.ok:
            raise Trading212Error(f"Trading212 HTTP {r.status_code}: {r.text[:200]}")
        return r.json()

    def account_info(self) -> dict[str, Any]:
        return self._get("/equity/account/info")

    def cash(self) -> dict[str, Any]:
        return self._get("/equity/account/cash")

    def portfolio(self) -> list[dict[str, Any]]:
        return self._get("/equity/portfolio")

    def snapshot(self) -> dict[str, Any]:
        info = self.account_info()
        cash = self.cash()
        positions = self.portfolio()
        return summarize(info, cash, positions)


def plain_ticker(t212_ticker: str) -> str:
    """'AAPL_US_EQ' -> 'AAPL'; 'VUSAl_EQ' -> 'VUSA.L' (LSE lines carry a lowercase 'l')."""
    base = t212_ticker.split("_")[0]
    if base.endswith("l") and base[:-1].isupper():
        return base[:-1] + ".L"
    return base.upper()


def summarize(info: dict[str, Any], cash: dict[str, Any], positions: list[dict[str, Any]]) -> dict[str, Any]:
    pos = []
    for p in positions or []:
        qty = float(p.get("quantity", 0))
        cur = float(p.get("currentPrice", 0))
        pos.append({
            "ticker": plain_ticker(p.get("ticker", "")),
            "t212_ticker": p.get("ticker", ""),
            "quantity": qty,
            "average_price": float(p.get("averagePrice", 0)),
            "current_price": cur,
            "value": qty * cur,
            "ppl": float(p.get("ppl", 0)),
        })
    return {
        "currency": info.get("currencyCode", ""),
        "total": cash.get("total"),
        "cash": cash.get("free"),
        "invested": cash.get("invested"),
        "ppl": cash.get("ppl"),
        "result": cash.get("result"),
        "positions": pos,
        "raw": {"cash": cash},
    }
