from decimal import Decimal
from urllib.parse import parse_qs

import httpx
import pytest

from agent.brokers.luno import LunoBroker, LunoError, map_luno_order
from agent.models import OrderStatus, Side

D = Decimal
CID = "c" * 32
BALANCES = {"balance": [
    {"asset": "XBT", "balance": "0.1", "reserved": "0.02", "unconfirmed": "0"},
    {"asset": "ETH", "balance": "0", "reserved": "0", "unconfirmed": "0"},
    {"asset": "ZAR", "balance": "5000", "reserved": "1000", "unconfirmed": "0"},
]}
TICKER = {"pair": "XBTZAR", "bid": "1000000", "ask": "1000200", "last_trade": "1000100"}
MARKETS = {"markets": [{"market_id": "XBTZAR", "min_volume": "0.0005",
                        "volume_scale": 6, "price_scale": 0}]}


def broker(routes):
    def handler(request):
        resp = routes.get((request.method, request.url.path))
        if resp is None:
            return httpx.Response(404, json={"error": "not found", "error_code": "ErrNotFound"})
        return resp(request) if callable(resp) else httpx.Response(200, json=resp)

    http = httpx.Client(base_url="https://api.luno.com", transport=httpx.MockTransport(handler))
    return LunoBroker("id", "secret", "ZAR", http=http)


def test_portfolio_uses_available_not_reserved():
    b = broker({("GET", "/api/1/balance"): BALANCES, ("GET", "/api/1/ticker"): TICKER})
    pf = b.get_portfolio(["XBTZAR", "ETHZAR"])
    assert pf.cash == D("4000")
    assert pf.positions == {"XBTZAR": D("0.08")}
    assert pf.prices["XBTZAR"] == D("1000100")


def test_price_falls_back_to_last_trade_when_book_empty():
    b = broker({("GET", "/api/1/ticker"): {**TICKER, "bid": "0", "ask": "0"}})
    assert b.get_price("XBTZAR") == D("1000100")


def test_candles():
    b = broker({("GET", "/api/exchange/1/candles"): {"candles": [
        {"timestamp": 1759795200000, "open": "1", "high": "2", "low": "0.5",
         "close": "1.5", "volume": "10"}]}})
    [c] = b.get_candles("XBTZAR", 90)
    assert c.close == D("1.5") and c.ts.year == 2025


def test_constraints_from_market_info():
    c = broker({("GET", "/api/exchange/1/markets"): MARKETS}).constraints("XBTZAR")
    assert (c.min_qty, c.qty_step, c.price_tick) == (D("0.0005"), D("0.000001"), D("1"))


def test_place_order_sends_plain_decimals():
    seen = {}

    def post(request):
        seen.update({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
        return httpx.Response(200, json={"order_id": "BXMC2CJ7HNB88U4"})

    b = broker({("POST", "/api/1/postorder"): post})
    r = b.place_limit_order("XBTZAR", Side.BUY, D("0.00001"), D("1000200"), CID)
    assert seen == {"pair": "XBTZAR", "type": "BID", "volume": "0.00001",
                    "price": "1000200", "client_order_id": CID}
    assert r.status is OrderStatus.SUBMITTED


def test_sell_is_ask():
    seen = {}

    def post(request):
        seen.update({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
        return httpx.Response(200, json={"order_id": "x"})

    broker({("POST", "/api/1/postorder"): post}).place_limit_order(
        "XBTZAR", Side.SELL, D("0.001"), D("999000"), CID)
    assert seen["type"] == "ASK"


def test_get_order_not_found_is_none():
    assert broker({}).get_order(CID) is None


@pytest.mark.parametrize("payload,expected", [
    ({"status": "COMPLETE", "base": "0.01", "counter": "10000", "limit_volume": "0.01"}, OrderStatus.FILLED),
    ({"status": "COMPLETE", "base": "0.004", "counter": "4000", "limit_volume": "0.01"}, OrderStatus.PARTIALLY_FILLED),
    ({"status": "COMPLETE", "base": "0", "counter": "0", "limit_volume": "0.01"}, OrderStatus.REJECTED),
    ({"status": "AWAITING", "base": "0", "counter": "0", "limit_volume": "0.01"}, OrderStatus.SUBMITTED),
    ({"status": "PENDING", "base": "0.002", "counter": "2000", "limit_volume": "0.01"}, OrderStatus.SUBMITTED),
])
def test_map_luno_order(payload, expected):
    r = map_luno_order(CID, payload)
    assert r.status is expected
    if r.filled_qty > 0:
        assert r.avg_price == D(payload["counter"]) / D(payload["base"])


def test_http_error_raises():
    b = broker({("GET", "/api/1/balance"): lambda r: httpx.Response(500, json={"error": "boom"})})
    with pytest.raises(LunoError):
        b.get_portfolio(["XBTZAR"])


def test_pair_must_end_with_quote():
    with pytest.raises(ValueError):
        broker({("GET", "/api/1/balance"): BALANCES,
                ("GET", "/api/1/ticker"): TICKER}).get_portfolio(["XBTNGN"])


def test_transport_error_raises_luno_error():
    def boom(request):
        raise httpx.ConnectTimeout("timed out")

    with pytest.raises(LunoError):
        broker({("GET", "/api/1/balance"): boom}).get_portfolio(["XBTZAR"])


def test_non_json_success_body_raises_luno_error():
    b = broker({("GET", "/api/1/balance"): lambda r: httpx.Response(200, text="<html>oops</html>")})
    with pytest.raises(LunoError):
        b.get_portfolio(["XBTZAR"])


def test_all_zero_ticker_raises():
    zero = {**TICKER, "bid": "0", "ask": "0", "last_trade": "0"}
    with pytest.raises(LunoError):
        broker({("GET", "/api/1/ticker"): zero}).get_price("XBTZAR")
