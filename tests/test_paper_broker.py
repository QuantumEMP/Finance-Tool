from decimal import Decimal

import pytest

from agent.brokers.paper import CASH, PaperBroker
from agent.models import OrderStatus, Side
from agent.store import Store
from tests.fakes import FakeBroker

D = Decimal
CID = "c" * 32


@pytest.fixture
def setup(tmp_path):
    store = Store(str(tmp_path / "db.sqlite"))
    market = FakeBroker(name="luno", prices={"XBTZAR": 100})
    return store, market, PaperBroker(market, store, "luno", D("10000"), fee_pct=D("0.5"))


def test_starts_with_cash(setup):
    _, _, paper = setup
    pf = paper.get_portfolio(["XBTZAR"])
    assert pf.cash == D("10000") and pf.positions == {} and pf.prices == {"XBTZAR": D("100")}
    assert paper.name == "paper:luno"


def test_buy_fills_at_limit_with_fee(setup):
    _, _, paper = setup
    r = paper.place_limit_order("XBTZAR", Side.BUY, D("2"), D("101"), CID)
    assert r.status is OrderStatus.FILLED and r.filled_qty == D("2") and r.avg_price == D("101")
    pf = paper.get_portfolio(["XBTZAR"])
    assert pf.cash == D("9796.99") and pf.positions == {"XBTZAR": D("2")}


def test_sell_credits_cash_less_fee(setup):
    _, _, paper = setup
    paper.place_limit_order("XBTZAR", Side.BUY, D("2"), D("100"), "a" * 32)
    paper.place_limit_order("XBTZAR", Side.SELL, D("2"), D("100"), "b" * 32)
    pf = paper.get_portfolio(["XBTZAR"])
    assert pf.positions == {}
    assert pf.cash == D("10000") - D("201") + D("199")


def test_rejections(setup):
    _, _, paper = setup
    assert paper.place_limit_order("XBTZAR", Side.BUY, D("1"), D("99"), "a" * 32).status is OrderStatus.REJECTED
    assert paper.place_limit_order("XBTZAR", Side.BUY, D("1000"), D("100"), "b" * 32).status is OrderStatus.REJECTED
    assert paper.place_limit_order("XBTZAR", Side.SELL, D("1"), D("100"), "d" * 32).status is OrderStatus.REJECTED
    assert paper.get_portfolio(["XBTZAR"]).cash == D("10000")


def test_same_client_order_id_is_idempotent(setup):
    _, _, paper = setup
    first = paper.place_limit_order("XBTZAR", Side.BUY, D("1"), D("100"), CID)
    again = paper.place_limit_order("XBTZAR", Side.BUY, D("1"), D("100"), CID)
    assert first == again
    assert paper.get_portfolio(["XBTZAR"]).positions == {"XBTZAR": D("1")}
    assert paper.get_order(CID) == first
    assert paper.get_order("e" * 32) is None


def test_state_survives_new_instance(setup):
    store, market, paper = setup
    paper.place_limit_order("XBTZAR", Side.BUY, D("1"), D("100"), CID)
    again = PaperBroker(market, store, "luno", D("10000"), fee_pct=D("0.5"))
    assert again.get_portfolio(["XBTZAR"]).positions == {"XBTZAR": D("1")}
    assert store.paper_holdings("luno")[CASH] < D("10000")
