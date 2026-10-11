"""Alpaca adapter (stocks/ETFs). paper=True uses Alpaca's own paper endpoint."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import LimitOrderRequest

from agent.models import Candle, OrderConstraints, OrderResult, OrderStatus, Portfolio, Side

_CLOSED = {"canceled", "expired", "rejected", "suspended", "stopped", "done_for_day"}


def _dec(x) -> Decimal:
    return Decimal(str(x))


def map_order(order) -> OrderResult:
    status = getattr(order.status, "value", order.status)
    filled = _dec(order.filled_qty or 0)
    avg = _dec(order.filled_avg_price) if order.filled_avg_price else None
    if status == "filled":
        st = OrderStatus.FILLED
    elif status in _CLOSED:
        st = OrderStatus.PARTIALLY_FILLED if filled > 0 else OrderStatus.REJECTED
    else:
        st = OrderStatus.SUBMITTED  # includes open partially_filled
    return OrderResult(str(order.client_order_id), st, filled, avg, reason=str(status))


class AlpacaBroker:
    name = "alpaca"

    def __init__(self, trading, data) -> None:
        self._trading, self._data = trading, data

    @classmethod
    def from_keys(cls, key: str, secret: str, paper: bool) -> "AlpacaBroker":
        return cls(TradingClient(key, secret, paper=paper), StockHistoricalDataClient(key, secret))

    def is_market_open(self) -> bool:
        return bool(self._trading.get_clock().is_open)

    def get_portfolio(self, symbols: list[str]) -> Portfolio:
        cash = _dec(self._trading.get_account().cash)
        positions = {p.symbol: _dec(p.qty) for p in self._trading.get_all_positions()
                     if p.symbol in symbols}
        return Portfolio(cash, positions, {s: self.get_price(s) for s in symbols})

    def get_price(self, symbol: str) -> Decimal:
        req = StockLatestTradeRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX)
        return _dec(self._data.get_stock_latest_trade(req)[symbol].price)

    def get_candles(self, symbol: str, days: int) -> list[Candle]:
        req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
                               start=datetime.now(timezone.utc) - timedelta(days=days),
                               feed=DataFeed.IEX)
        bars = self._data.get_stock_bars(req).data.get(symbol, [])
        return [Candle(b.timestamp, _dec(b.open), _dec(b.high), _dec(b.low), _dec(b.close),
                       _dec(b.volume)) for b in bars]

    def constraints(self, symbol: str) -> OrderConstraints:
        return OrderConstraints(Decimal("1"), Decimal("1"), Decimal("0.01"))

    def place_limit_order(self, symbol, side, qty, limit_price, client_order_id) -> OrderResult:
        req = LimitOrderRequest(
            symbol=symbol, qty=float(qty),
            side=OrderSide.BUY if side is Side.BUY else OrderSide.SELL,
            time_in_force=TimeInForce.DAY, limit_price=float(limit_price),
            client_order_id=client_order_id,
        )
        return map_order(self._trading.submit_order(req))

    def get_order(self, client_order_id: str) -> OrderResult | None:
        try:
            return map_order(self._trading.get_order_by_client_id(client_order_id))
        except Exception as e:
            if getattr(e, "status_code", None) == 404:
                return None
            raise

    def cancel_order(self, client_order_id: str) -> None:
        try:
            order = self._trading.get_order_by_client_id(client_order_id)
        except Exception as e:
            if getattr(e, "status_code", None) == 404:
                return
            raise
        if getattr(order.status, "value", order.status) in _CLOSED | {"filled"}:
            return
        self._trading.cancel_order_by_id(order.id)
