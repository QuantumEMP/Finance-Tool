from datetime import datetime, timezone

import httpx

from agent.news import AlpacaNews, NoNews

T0 = datetime(2026, 10, 7, tzinfo=timezone.utc)


def test_maps_news_symbols_to_ours():
    def handler(request):
        assert request.headers["APCA-API-KEY-ID"] == "k"
        assert request.url.params["symbols"] == "BTCUSD,SPY"
        return httpx.Response(200, json={"news": [
            {"headline": "Bitcoin rallies", "symbols": ["BTCUSD"]},
            {"headline": "Stocks flat", "symbols": ["SPY", "QQQ"]},
        ]})

    news = AlpacaNews("k", "s", http=httpx.Client(transport=httpx.MockTransport(handler)))
    got = news.headlines({"XBTZAR": "BTCUSD", "SPY": "SPY"}, T0)
    assert got == {"XBTZAR": ["Bitcoin rallies"], "SPY": ["Stocks flat"]}


def test_caps_headlines_per_symbol():
    items = [{"headline": f"h{i}", "symbols": ["SPY"]} for i in range(9)]
    news = AlpacaNews("k", "s", http=httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"news": items}))))
    assert len(news.headlines({"SPY": "SPY"}, T0)["SPY"]) == 5


def test_failure_returns_empty():
    news = AlpacaNews("k", "s", http=httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(500))))
    assert news.headlines({"SPY": "SPY"}, T0) == {}


def test_no_news():
    assert NoNews().headlines({"SPY": "SPY"}, T0) == {}


def test_non_dict_body_returns_empty():
    news = AlpacaNews("k", "s", http=httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=[1, 2]))))
    assert news.headlines({"SPY": "SPY"}, T0) == {}


def test_null_symbols_item_is_tolerated():
    items = [{"headline": "bad", "symbols": None}, {"headline": "good", "symbols": ["SPY"]}]
    news = AlpacaNews("k", "s", http=httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"news": items}))))
    assert news.headlines({"SPY": "SPY"}, T0) == {"SPY": ["good"]}
