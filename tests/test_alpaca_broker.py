from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from alpaca.trading.enums import OrderSide, TimeInForce

from agent.brokers.alpaca import AlpacaBroker, map_order
from agent.models import OrderStatus, Side

D = Decimal
CID = "c" * 32


class NotFound(Exception):
    status_code = 404


class FakeTrading:
    def __init__(self):
        self.submitted = []
        self.orders = {}
        self.cancelled = []

    def get_clock(self):
        return SimpleNamespace(is_open=True)

    def get_account(self):
        return SimpleNamespace(cash="1500.50")

    def get_all_positions(self):
        return [SimpleNamespace(symbol="SPY", qty="3"), SimpleNamespace(symbol="TSLA", qty="1")]

    def submit_order(self, req):
        self.submitted.append(req)
        return SimpleNamespace(client_order_id=req.client_order_id,
                               status=SimpleNamespace(value="new"),
                               filled_qty="0", filled_avg_price=None)

    def get_order_by_client_id(self, cid):
        if cid in self.orders:
            return self.orders[cid]
        raise NotFound()

    def cancel_order_by_id(self, order_id):
        self.cancelled.append(order_id)


class FakeData:
    def get_stock_latest_trade(self, req):
        return {req.symbol_or_symbols: SimpleNamespace(price=101.5)}

    def get_stock_bars(self, req):
        bar = SimpleNamespace(timestamp=datetime(2026, 10, 1, tzinfo=timezone.utc),
                              open=1.0, high=2.0, low=0.5, close=1.5, volume=100.0)
        return SimpleNamespace(data={req.symbol_or_symbols: [bar]})


@pytest.fixture
def broker():
    return AlpacaBroker(FakeTrading(), FakeData())


def test_portfolio_only_allowlisted(broker):
    pf = broker.get_portfolio(["SPY", "QQQ"])
    assert pf.cash == D("1500.50")
    assert pf.positions == {"SPY": D("3")}
    assert pf.prices == {"SPY": D("101.5"), "QQQ": D("101.5")}


def test_candles(broker):
    [c] = broker.get_candles("SPY", 90)
    assert c.close == D("1.5") and c.ts.year == 2026


def test_whole_share_constraints(broker):
    c = broker.constraints("SPY")
    assert (c.min_qty, c.qty_step, c.price_tick) == (D("1"), D("1"), D("0.01"))


def test_place_limit_order_request(broker):
    r = broker.place_limit_order("SPY", Side.BUY, D("7"), D("100.20"), CID)
    req = broker._trading.submitted[0]
    assert req.client_order_id == CID and req.symbol == "SPY"
    assert req.side == OrderSide.BUY and req.time_in_force == TimeInForce.DAY
    assert float(req.limit_price) == 100.2 and float(req.qty) == 7
    assert r.status is OrderStatus.SUBMITTED


def test_get_order_not_found_is_none(broker):
    assert broker.get_order(CID) is None


def test_cancel_open_order_by_its_alpaca_id(broker):
    broker._trading.orders[CID] = SimpleNamespace(id="uuid-1", status=SimpleNamespace(value="new"))
    broker.cancel_order(CID)
    assert broker._trading.cancelled == ["uuid-1"]


def test_cancel_skips_closed_and_missing_orders(broker):
    broker._trading.orders[CID] = SimpleNamespace(id="uuid-1", status=SimpleNamespace(value="filled"))
    broker.cancel_order(CID)
    broker.cancel_order("d" * 32)
    assert broker._trading.cancelled == []


@pytest.mark.parametrize("status,filled,expected", [
    ("filled", "7", OrderStatus.FILLED),
    ("partially_filled", "3", OrderStatus.SUBMITTED),  # still open
    ("new", "0", OrderStatus.SUBMITTED),
    ("canceled", "0", OrderStatus.REJECTED),
    ("expired", "3", OrderStatus.PARTIALLY_FILLED),
    ("rejected", "0", OrderStatus.REJECTED),
])
def test_map_order(status, filled, expected):
    o = SimpleNamespace(client_order_id=CID, status=SimpleNamespace(value=status),
                        filled_qty=filled, filled_avg_price="100.1" if filled != "0" else None)
    r = map_order(o)
    assert r.status is expected and r.filled_qty == D(filled)
