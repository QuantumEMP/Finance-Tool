"""The one interface every venue implements."""
from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from agent.models import Candle, OrderConstraints, OrderResult, Portfolio, Side


class BrokerError(Exception):
    """A venue rejected a request or returned something unusable."""


class Broker(Protocol):
    name: str

    def is_market_open(self) -> bool: ...

    def get_portfolio(self, symbols: list[str]) -> Portfolio: ...

    def get_price(self, symbol: str) -> Decimal: ...

    def get_candles(self, symbol: str, days: int) -> list[Candle]: ...

    def constraints(self, symbol: str) -> OrderConstraints: ...

    def place_limit_order(self, symbol: str, side: Side, qty: Decimal,
                          limit_price: Decimal, client_order_id: str) -> OrderResult: ...

    def get_order(self, client_order_id: str) -> OrderResult | None:
        """None means the venue confirms no such order exists."""
        ...
