"""Recent headlines per symbol from Alpaca's news API. Best effort: failures mean no news."""
from __future__ import annotations

import logging
from datetime import datetime

import httpx

from agent.store import ts

log = logging.getLogger(__name__)
MAX_PER_SYMBOL = 5


class NoNews:
    def headlines(self, symbol_map: dict[str, str], since: datetime, limit: int = 50) -> dict[str, list[str]]:
        return {}


class AlpacaNews:
    URL = "https://data.alpaca.markets/v1beta1/news"

    def __init__(self, key: str, secret: str, http: httpx.Client | None = None) -> None:
        self._headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
        self._http = http or httpx.Client(timeout=15.0)

    def headlines(self, symbol_map: dict[str, str], since: datetime, limit: int = 50) -> dict[str, list[str]]:
        """symbol_map: our symbol -> news symbol (e.g. XBTZAR -> BTCUSD)."""
        try:
            resp = self._http.get(self.URL, headers=self._headers, params={
                "symbols": ",".join(sorted(set(symbol_map.values()))),
                "start": ts(since), "limit": limit, "sort": "desc",
            })
            resp.raise_for_status()
            items = resp.json().get("news", [])
            ours_by_news: dict[str, list[str]] = {}
            for ours, news_sym in symbol_map.items():
                ours_by_news.setdefault(news_sym, []).append(ours)
            out: dict[str, list[str]] = {s: [] for s in symbol_map}
            for item in items:
                for news_sym in item.get("symbols") or []:
                    for ours in ours_by_news.get(news_sym, []):
                        if len(out[ours]) < MAX_PER_SYMBOL and item.get("headline"):
                            out[ours].append(item["headline"])
            return out
        except Exception as e:
            log.warning("news fetch failed: %s", e)
            return {}
