from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from agent.models import (
    OrderResult, OrderStatus, Proposal, ProposalState, ProposedOrder, Side,
)
from agent.store import Store, ts

T0 = datetime(2026, 10, 7, 14, 0, tzinfo=timezone.utc)
PS = ProposalState


def _proposal(pid="a" * 32, symbol="SPY", account="test", state=PS.PENDING, created=T0):
    order = ProposedOrder(account, symbol, Side.BUY, Decimal("5"), Decimal("100.20"),
                          Decimal("100"), 0.8, "because")
    return Proposal(pid, order, state, created, created + timedelta(minutes=60), 5.01)


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "db.sqlite"))


def test_proposal_roundtrip(store):
    p = _proposal()
    store.add_proposal(p)
    assert store.get_proposal(p.id) == p
    assert store.get_proposal("f" * 32) is None


def test_transition_is_guarded(store):
    store.add_proposal(_proposal())
    assert store.transition("a" * 32, [PS.PENDING], PS.APPROVED, T0) is True
    assert store.transition("a" * 32, [PS.PENDING], PS.APPROVED, T0) is False
    assert store.get_proposal("a" * 32).state is PS.APPROVED


def test_transition_sets_fill_fields(store):
    store.add_proposal(_proposal(state=PS.SUBMITTED))
    ok = store.transition("a" * 32, [PS.SUBMITTED], PS.FILLED, T0, reason="filled",
                          filled_qty=Decimal("5"), avg_price=Decimal("100.10"))
    p = store.get_proposal("a" * 32)
    assert ok and p.state is PS.FILLED and p.reason == "filled"
    assert p.filled_qty == Decimal("5") and p.avg_price == Decimal("100.10")


def test_data_survives_reopen(tmp_path):
    path = str(tmp_path / "db.sqlite")
    Store(path).add_proposal(_proposal())
    assert Store(path).get_proposal("a" * 32).state is PS.PENDING


def test_counts_pending_symbols_and_listing(store):
    store.add_proposal(_proposal("a" * 32, "SPY"))
    store.add_proposal(_proposal("b" * 32, "QQQ", state=PS.FILLED))
    store.add_proposal(_proposal("c" * 32, "VTI", created=T0 - timedelta(days=2)))
    assert store.count_proposals("test", T0 - timedelta(hours=1), [PS.PENDING, PS.FILLED]) == 2
    assert store.pending_symbols("test") == {"SPY", "VTI"}
    assert [p.id for p in store.proposals_in_state(PS.PENDING)] == ["c" * 32, "a" * 32]
    assert len(store.proposals_since(T0 - timedelta(days=1))) == 2


def test_message_id(store):
    store.add_proposal(_proposal())
    store.set_message_id("a" * 32, 42)
    assert store.get_proposal("a" * 32).message_id == 42


def test_equity_marks(store):
    store.mark_equity("test", T0 - timedelta(days=2), Decimal("100"))
    store.mark_equity("test", T0 - timedelta(days=1), Decimal("120"))
    store.mark_equity("test", T0, Decimal("110"))
    assert store.peak_equity("test") == Decimal("120")
    assert store.peak_equity("other") is None
    assert store.first_equity_since("test", T0 - timedelta(hours=1)) == Decimal("110")
    assert store.first_equity_since("test", T0 + timedelta(hours=1)) is None
    assert [e for _, e in store.equity_marks_since("test", T0 - timedelta(days=1, hours=1))] == [
        Decimal("120"), Decimal("110")]


def test_flags(store):
    assert store.get_flag("paused") is None
    store.set_flag("paused", "1")
    store.set_flag("paused:luno", "1")
    assert store.get_flag("paused") == "1"
    assert store.flags_with_prefix("paused:") == ["paused:luno"]
    store.set_flag("paused", None)
    assert store.get_flag("paused") is None


def test_events(store):
    store.record_event(T0, "test", "veto", {"symbol": "SPY", "qty": Decimal("1")})
    assert store.events("test", "veto") == [{"symbol": "SPY", "qty": "1"}]


def test_paper_holdings_and_orders(store):
    store.set_paper_holding("luno", "__CASH__", Decimal("10000"))
    store.set_paper_holding("luno", "__CASH__", Decimal("9000"))
    assert store.paper_holdings("luno") == {"__CASH__": Decimal("9000")}
    r = OrderResult("c" * 32, OrderStatus.FILLED, Decimal("2"), Decimal("101"), "paper fill")
    store.add_paper_order("luno", r)
    assert store.get_paper_order("c" * 32) == r
    assert store.get_paper_order("d" * 32) is None


def test_naive_datetime_rejected():
    with pytest.raises(ValueError):
        ts(datetime(2026, 1, 1))


def test_last_equity_before_and_open_symbols(store):
    store.mark_equity("test", T0 - timedelta(days=2), Decimal("90"))
    store.mark_equity("test", T0 - timedelta(days=1), Decimal("100"))
    store.mark_equity("test", T0, Decimal("110"))
    assert store.last_equity_before("test", T0) == Decimal("100")
    assert store.last_equity_before("test", T0 - timedelta(days=3)) is None
    store.add_proposal(_proposal("a" * 32, "SPY", state=PS.SUBMITTED))
    store.add_proposal(_proposal("b" * 32, "QQQ", state=PS.APPROVED))
    store.add_proposal(_proposal("c" * 32, "VTI"))
    store.add_proposal(_proposal("d" * 32, "IWM", state=PS.FILLED))
    assert store.open_symbols("test") == {"SPY", "QQQ", "VTI"}


def test_reset_peak_ignores_earlier_marks(store):
    store.mark_equity("test", T0 - timedelta(days=1), Decimal("120"))
    store.mark_equity("other", T0 - timedelta(days=1), Decimal("500"))
    store.reset_peak("test", T0)
    assert store.peak_equity("test") is None
    store.mark_equity("test", T0, Decimal("90"))
    store.mark_equity("test", T0 + timedelta(hours=1), Decimal("95"))
    assert store.peak_equity("test") == Decimal("95")
    assert store.peak_equity("other") == Decimal("500")
