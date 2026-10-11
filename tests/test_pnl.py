from datetime import datetime, timedelta, timezone
from decimal import Decimal

from agent.models import Proposal, ProposalState, ProposedOrder, Side
from agent.pnl import build_book

D = Decimal
T0 = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)


def fill(i, side, qty, price, symbol="SPY", state=ProposalState.FILLED):
    o = ProposedOrder("test", symbol, side, D(qty), D(price), D(price), 0.8, "r")
    return Proposal(f"{i:032x}", o, state, T0 + timedelta(days=i), T0 + timedelta(days=i, hours=1), 1.0,
                    filled_qty=D(qty), avg_price=D(price))


def test_average_cost_and_realised_pnl():
    book = build_book([
        fill(0, Side.BUY, "10", "100"),
        fill(1, Side.BUY, "10", "120"),            # avg cost 110
        fill(2, Side.SELL, "5", "130"),            # +20 x 5 = +100
        fill(3, Side.SELL, "5", "100", state=ProposalState.PARTIALLY_FILLED),  # -10 x 5 = -50
    ])
    lot = book.lots["SPY"]
    assert lot.qty == D("10") and lot.avg_cost == D("110")
    assert [r.pnl for r in book.realised] == [D("100"), D("-50")]
    assert book.realised_since(T0 + timedelta(days=3)) == D("-50")


def test_sells_beyond_tracked_units_only_match_what_was_bought():
    # 3 units were bought outside the tool; the agent only knows about 2.
    book = build_book([fill(0, Side.BUY, "2", "50"), fill(1, Side.SELL, "5", "60")])
    assert [(r.qty, r.pnl) for r in book.realised] == [(D("2"), D("20"))]
    assert book.lots["SPY"].qty == 0 and book.lots["SPY"].avg_cost is None


def test_unfilled_proposals_are_ignored():
    p = fill(0, Side.BUY, "2", "50", state=ProposalState.EXPIRED)
    assert build_book([p]).lots == {}
