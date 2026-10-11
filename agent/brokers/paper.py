"""Simulated trading on real market data. Balances and fills persist in SQLite."""
from __future__ import annotations

from decimal import Decimal

from agent.brokers.base import Broker
from agent.models import ZERO, OrderResult, OrderStatus, Portfolio, Side, pct
from agent.store import Store

CASH = "__CASH__"


class PaperBroker:
    def __init__(self, market: Broker, store: Store, account: str,
                 starting_cash: Decimal, fee_pct: Decimal = ZERO) -> None:
        self.name = f"paper:{market.name}"
        self._market, self._store, self._account = market, store, account
        self._fee = pct(fee_pct)
        if CASH not in store.paper_holdings(account):
            store.set_paper_holding(account, CASH, Decimal(starting_cash))

    def is_market_open(self) -> bool:
        return self._market.is_market_open()

    def get_price(self, symbol):
        return self._market.get_price(symbol)

    def get_candles(self, symbol, days):
        return self._market.get_candles(symbol, days)

    def constraints(self, symbol):
        return self._market.constraints(symbol)

    def get_portfolio(self, symbols: list[str]) -> Portfolio:
        h = self._store.paper_holdings(self._account)
        return Portfolio(
            cash=h.get(CASH, ZERO),
            positions={s: h[s] for s in symbols if h.get(s, ZERO) > 0},
            prices={s: self._market.get_price(s) for s in symbols},
        )

    def place_limit_order(self, symbol, side, qty, limit_price, client_order_id) -> OrderResult:
        existing = self._store.get_paper_order(client_order_id)
        if existing is not None:
            return existing
        result = self._simulate(symbol, side, qty, limit_price, client_order_id)
        self._store.add_paper_order(self._account, result)
        return result

    def get_order(self, client_order_id):
        return self._store.get_paper_order(client_order_id)

    def cancel_order(self, client_order_id):
        pass  # paper orders fill or reject immediately; nothing is ever left open

    def _simulate(self, symbol, side, qty, limit_price, cid) -> OrderResult:
        def reject(reason: str) -> OrderResult:
            return OrderResult(cid, OrderStatus.REJECTED, reason=f"paper: {reason}")

        if qty <= 0 or limit_price <= 0:
            return reject("invalid order")
        price = self._market.get_price(symbol)
        h = self._store.paper_holdings(self._account)
        cash, held = h.get(CASH, ZERO), h.get(symbol, ZERO)
        if side is Side.BUY:
            if limit_price < price:
                return reject("limit below market, not filled")
            cost = qty * limit_price * (1 + self._fee)
            if cost > cash:
                return reject("insufficient cash")
            self._store.set_paper_holding(self._account, CASH, cash - cost)
            self._store.set_paper_holding(self._account, symbol, held + qty)
        else:
            if limit_price > price:
                return reject("limit above market, not filled")
            if qty > held:
                return reject("insufficient holdings")
            self._store.set_paper_holding(self._account, symbol, held - qty)
            self._store.set_paper_holding(self._account, CASH,
                                          cash + qty * limit_price * (1 - self._fee))
        return OrderResult(cid, OrderStatus.FILLED, qty, limit_price, "paper fill")
