"""Luno adapter (crypto vs one fiat quote currency). Live API only: Luno has no sandbox,
so paper mode wraps this in PaperBroker and only its read methods are used."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx

from agent.brokers.base import BrokerError
from agent.models import ZERO, Candle, OrderConstraints, OrderResult, OrderStatus, Portfolio, Side

log = logging.getLogger(__name__)


class LunoError(BrokerError):
    pass


def map_luno_order(cid: str, o: dict) -> OrderResult:
    filled = Decimal(o.get("base") or "0")
    counter = Decimal(o.get("counter") or "0")
    limit_volume = Decimal(o.get("limit_volume") or "0")
    avg = counter / filled if filled > 0 else None
    status = o.get("status")
    if status == "COMPLETE":
        if limit_volume > 0 and filled >= limit_volume:
            st = OrderStatus.FILLED
        elif filled > 0:
            st = OrderStatus.PARTIALLY_FILLED
        else:
            st = OrderStatus.REJECTED
    else:
        st = OrderStatus.SUBMITTED
    return OrderResult(cid, st, filled, avg, reason=str(status))


class LunoBroker:
    BASE_URL = "https://api.luno.com"

    def __init__(self, key_id: str, secret: str, quote_currency: str,
                 http: httpx.Client | None = None) -> None:
        self.name = "luno"
        self._quote = quote_currency
        self._http = http or httpx.Client(base_url=self.BASE_URL, auth=(key_id, secret), timeout=15.0)
        self._markets: dict[str, dict] = {}

    def _json(self, r: httpx.Response) -> dict:
        if r.status_code >= 400:
            try:
                detail = r.json()
            except ValueError:
                detail = r.text
            raise LunoError(f"Luno HTTP {r.status_code}: {detail}")
        try:
            return r.json()
        except ValueError as e:
            raise LunoError(f"Luno returned non-JSON body: {r.text[:200]!r}") from e

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            return self._http.request(method, path, **kwargs)
        except httpx.HTTPError as e:
            raise LunoError(f"Luno transport error: {e!r}") from e

    def _get(self, path: str, **params) -> dict:
        return self._json(self._request("GET", path, params=params))

    def _base_asset(self, pair: str) -> str:
        if not pair.endswith(self._quote) or pair == self._quote:
            raise ValueError(f"{pair} is not quoted in {self._quote}")
        return pair[: -len(self._quote)]

    def is_market_open(self) -> bool:
        return True  # 24/7

    def get_portfolio(self, symbols: list[str]) -> Portfolio:
        total: dict[str, Decimal] = {}
        available: dict[str, Decimal] = {}
        for b in self._get("/api/1/balance").get("balance", []):
            bal = Decimal(b["balance"])
            free = bal - Decimal(b.get("reserved") or "0")
            total[b["asset"]] = total.get(b["asset"], ZERO) + bal
            available[b["asset"]] = available.get(b["asset"], ZERO) + free
        positions = {}
        for s in symbols:
            qty = available.get(self._base_asset(s), ZERO)
            if qty > 0:
                positions[s] = qty
        prices = {s: self.get_price(s) for s in symbols}
        # Equity covers the whole wallet: units reserved by open sells, and coins that
        # aren't on the allowlist. Assets with no market in the quote currency are skipped.
        traded = {self._base_asset(s): s for s in symbols}
        other = ZERO
        for asset, bal in total.items():
            if asset == self._quote:
                continue
            untradeable = bal - positions.get(traded[asset], ZERO) if asset in traded else bal
            if untradeable <= 0:
                continue
            try:
                price = prices[traded[asset]] if asset in traded else self.get_price(asset + self._quote)
            except LunoError as e:
                log.warning("cannot value %s %s in %s: %s", untradeable, asset, self._quote, e)
                continue
            other += untradeable * price
        return Portfolio(total.get(self._quote, ZERO), positions, prices,
                         spendable=available.get(self._quote, ZERO), other_value=other)

    def get_price(self, symbol: str) -> Decimal:
        t = self._get("/api/1/ticker", pair=symbol)
        bid, ask = Decimal(t.get("bid") or "0"), Decimal(t.get("ask") or "0")
        if bid > 0 and ask > 0:
            return (bid + ask) / 2
        last = Decimal(t.get("last_trade") or "0")
        if last > 0:
            return last
        raise LunoError(f"no usable price for {symbol}")

    def get_candles(self, symbol: str, days: int) -> list[Candle]:
        since = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
        data = self._get("/api/exchange/1/candles", pair=symbol, since=since, duration=86400)
        return [
            Candle(datetime.fromtimestamp(c["timestamp"] / 1000, timezone.utc),
                   Decimal(c["open"]), Decimal(c["high"]), Decimal(c["low"]),
                   Decimal(c["close"]), Decimal(c["volume"]))
            for c in data.get("candles", [])
        ]

    def constraints(self, symbol: str) -> OrderConstraints:
        if symbol not in self._markets:
            for m in self._get("/api/exchange/1/markets", pair=symbol).get("markets", []):
                self._markets[m["market_id"]] = m
        m = self._markets.get(symbol)
        if m is None:
            raise LunoError(f"no market info for {symbol}")
        return OrderConstraints(
            min_qty=Decimal(m["min_volume"]),
            qty_step=Decimal(1).scaleb(-int(m["volume_scale"])),
            price_tick=Decimal(1).scaleb(-int(m["price_scale"])),
        )

    def place_limit_order(self, symbol, side, qty, limit_price, client_order_id) -> OrderResult:
        # format(..., "f") avoids scientific notation like 1E-5, which Luno rejects.
        self._json(self._request("POST", "/api/1/postorder", data={
            "pair": symbol,
            "type": "BID" if side is Side.BUY else "ASK",
            "volume": format(qty, "f"),
            "price": format(limit_price, "f"),
            "client_order_id": client_order_id,
        }))
        return OrderResult(client_order_id, OrderStatus.SUBMITTED)

    def get_order(self, client_order_id: str) -> OrderResult | None:
        r = self._request("GET", "/api/exchange/3/order", params={"client_order_id": client_order_id})
        if r.status_code == 404 or (r.status_code >= 400 and "NotFound" in r.text):
            return None
        return map_luno_order(client_order_id, self._json(r))

    def cancel_order(self, client_order_id: str) -> None:
        # Luno cancels by its own order_id, so look that up first.
        r = self._request("GET", "/api/exchange/3/order", params={"client_order_id": client_order_id})
        if r.status_code == 404 or (r.status_code >= 400 and "NotFound" in r.text):
            return
        o = self._json(r)
        if o.get("status") == "COMPLETE":
            return
        self._json(self._request("POST", "/api/1/stoporder", data={"order_id": o["order_id"]}))
