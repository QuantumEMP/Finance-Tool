from datetime import datetime, timedelta, timezone
from decimal import Decimal

from agent.models import Candle, Proposal, ProposalState, ProposedOrder, Side
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


def spy(pid, side, qty, price, conf=0.8, state=ProposalState.FILLED, created=T0):
    o = ProposedOrder("test", "SPY", side, Decimal(qty), Decimal(price), Decimal(price), conf, "r")
    return Proposal(pid, o, state, created, created + timedelta(minutes=60), 1.0,
                    filled_qty=Decimal(qty) if state is ProposalState.FILLED else Decimal(0),
                    avg_price=Decimal(price) if state is ProposalState.FILLED else None)


def test_status_shows_cost_basis_and_unrealised(tmp_path):
    store = Store(str(tmp_path / "db.sqlite"))
    store.add_proposal(spy("a" * 32, Side.BUY, "2", "90", created=T0 - timedelta(days=2)))
    broker = FakeBroker(prices={"SPY": 100}, positions={"SPY": 2})
    text = status_text(store, {"test": AccountRuntime(make_account(), broker, "paper")})
    assert "avg cost 90.00, unrealised +20.00" in text


def test_report_excludes_deposits_and_shows_realised(tmp_path):
    store = Store(str(tmp_path / "db.sqlite"))
    store.mark_equity("test", T0 - timedelta(days=2), Decimal("10000"))
    store.add_cash_flow("test", T0 - timedelta(days=1), Decimal("1000"))
    store.mark_equity("test", T0, Decimal("11200"))
    store.add_proposal(spy("a" * 32, Side.BUY, "2", "90", created=T0 - timedelta(days=10)))
    store.add_proposal(spy("b" * 32, Side.SELL, "2", "100", created=T0 - timedelta(days=1)))
    text = report_text(store, ["test"], T0 + timedelta(minutes=1), 7)
    assert "10,000.00 → 11,200.00 (+2.00% excluding net deposits of 1,000.00)" in text
    assert "realised P&L +20.00" in text


def test_report_compares_with_buy_and_hold_and_scores_signals(tmp_path):
    store = Store(str(tmp_path / "db.sqlite"))
    broker = FakeBroker(prices={"SPY": 100, "QQQ": 200})
    # Prices at the start of the period: SPY 80, QQQ 200.
    start = T0 - timedelta(days=7)
    broker.get_candles = lambda s, days: [
        Candle(start - timedelta(days=1), *([Decimal(80 if s == "SPY" else 200)] * 4), Decimal(1))]
    store.add_proposal(spy("a" * 32, Side.BUY, "1", "90", conf=0.85, state=ProposalState.REJECTED,
                           created=T0 - timedelta(days=1)))
    store.add_proposal(spy("b" * 32, Side.SELL, "1", "95", conf=0.65, state=ProposalState.EXPIRED,
                           created=T0 - timedelta(days=1)))
    text = report_text(store, ["test"], T0, 7,
                       {"test": AccountRuntime(make_account(), broker, "paper")})
    assert "buy-and-hold of the allowlist: +12.50%" in text   # (+25% + 0%) / 2
    assert "0.8–0.9: +11.11% avg over 1" in text                # BUY at 90, now 100
    assert "<0.7: -5.26% avg over 1" in text                    # SELL at 95, now 100


def test_report_survives_market_data_errors(tmp_path):
    store = Store(str(tmp_path / "db.sqlite"))
    store.add_proposal(spy("a" * 32, Side.BUY, "1", "90", state=ProposalState.PENDING))
    broker = FakeBroker(prices={"SPY": 100})
    broker.fail_reads = RuntimeError("down")
    text = report_text(store, ["test"], T0, 7, {"test": AccountRuntime(make_account(), broker, "paper")})
    assert "could not fetch prices" in text and "pending 1" in text
