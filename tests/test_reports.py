from datetime import datetime, timedelta, timezone
from decimal import Decimal

from agent.models import Proposal, ProposalState, ProposedOrder, Side
from agent.reports import format_proposal, report_text, status_text
from agent.runtime import AccountRuntime
from agent.store import Store
from tests.fakes import FakeBroker, make_account

T0 = datetime(2026, 10, 7, 14, 0, tzinfo=timezone.utc)


def proposal(pid="a" * 32, reasoning="Uptrend; momentum positive.", state=ProposalState.PENDING):
    o = ProposedOrder("luno", "XBTZAR", Side.BUY, Decimal("0.002"), Decimal("1200000"),
                      Decimal("1199000"), 0.72, reasoning)
    return Proposal(pid, o, state, T0, T0 + timedelta(minutes=60), 4.0)


def test_format_proposal():
    text = format_proposal(proposal(), "ZAR", "paper")
    assert "PAPER" in text and "BUY 0.002 XBTZAR" in text
    assert "2,400.00 ZAR" in text and "4.0% of account" in text
    assert "0.72" in text and "Uptrend" in text and "15:00 UTC" in text


def test_format_proposal_shows_estimated_fee():
    assert "fee" not in format_proposal(proposal(), "ZAR", "paper")
    text = format_proposal(proposal(), "ZAR", "paper", Decimal("0.6"))
    assert "4.0% of account, est. fee 14.40 ZAR" in text


def test_format_proposal_live_tag_and_truncation():
    text = format_proposal(proposal(reasoning="x" * 5000), "ZAR", "live")
    assert "LIVE" in text and len(text) < 1500


def test_report_text(tmp_path):
    store = Store(str(tmp_path / "db.sqlite"))
    store.add_proposal(proposal())
    store.add_proposal(proposal("b" * 32, state=ProposalState.FILLED))
    store.transition("b" * 32, [ProposalState.FILLED], ProposalState.FILLED, T0,
                     filled_qty=Decimal("0.002"), avg_price=Decimal("1199500"))
    store.mark_equity("luno", T0 - timedelta(days=1), Decimal("10000"))
    store.mark_equity("luno", T0, Decimal("10250"))
    text = report_text(store, ["luno"], T0 + timedelta(minutes=1), 7)
    assert "filled 1" in text and "pending 1" in text
    assert "10,000.00 → 10,250.00 (+2.50%)" in text
    assert "1199500" in text


def test_report_text_empty(tmp_path):
    store = Store(str(tmp_path / "db.sqlite"))
    assert "No proposals" in report_text(store, ["luno"], T0, 7)


def test_status_text_survives_broker_error(tmp_path):
    store = Store(str(tmp_path / "db.sqlite"))
    ok = FakeBroker(prices={"SPY": 100}, positions={"SPY": 2})
    bad = FakeBroker(prices={"SPY": 100})
    bad.fail_reads = RuntimeError("down")
    runtimes = {"good": AccountRuntime(make_account("good"), ok, "paper"),
                "bad": AccountRuntime(make_account("bad"), bad, "paper")}
    store.set_flag("paused", "1")
    text = status_text(store, runtimes)
    assert "PAUSED" in text and "SPY: 2" in text and "could not fetch" in text
