from datetime import datetime, time, timedelta, timezone
from decimal import Decimal

from agent.config import Schedule
from agent.models import ProposalState
from agent.runtime import AccountRuntime, day_start, mark_all_equity, risk_context
from agent.store import Store
from tests.fakes import FakeBroker, make_account

T0 = datetime(2026, 10, 7, 14, 0, tzinfo=timezone.utc)  # 10:00 in New York


def ny_account():
    acct = make_account()
    return acct.model_copy(update={"schedule": Schedule(run_at=time(10, 0), timezone="America/New_York")})


def test_day_start_uses_the_given_time_zone():
    assert day_start(T0) == datetime(2026, 10, 7, tzinfo=timezone.utc)
    assert day_start(T0, "America/New_York") == datetime(2026, 10, 7, 4, 0, tzinfo=timezone.utc)
    # 01:00 UTC is still the previous evening in New York.
    early = datetime(2026, 10, 7, 1, 0, tzinfo=timezone.utc)
    assert day_start(early, "America/New_York") == datetime(2026, 10, 6, 4, 0, tzinfo=timezone.utc)


def test_daily_reference_is_last_mark_before_local_midnight(tmp_path):
    store = Store(str(tmp_path / "db"))
    store.mark_equity("test", T0 - timedelta(days=1), Decimal("9000"))      # yesterday's cycle
    store.mark_equity("test", datetime(2026, 10, 7, 3, 0, tzinfo=timezone.utc), Decimal("10000"))  # 23:00 NY
    store.mark_equity("test", datetime(2026, 10, 7, 5, 0, tzinfo=timezone.utc), Decimal("11000"))  # 01:00 NY
    ctx = risk_context(store, ny_account(), FakeBroker().get_portfolio([]), T0, [ProposalState.PENDING])
    assert ctx.day_start_equity == Decimal("10000")


def test_orders_today_counts_from_local_midnight(tmp_path):
    from tests.test_store import _proposal
    store = Store(str(tmp_path / "db"))
    # 02:00 UTC is the previous day in New York, so it doesn't count toward today.
    store.add_proposal(_proposal(pid="a" * 32, created=datetime(2026, 10, 7, 2, 0, tzinfo=timezone.utc)))
    store.add_proposal(_proposal(pid="b" * 32, created=datetime(2026, 10, 7, 5, 0, tzinfo=timezone.utc)))
    ctx = risk_context(store, ny_account(), FakeBroker().get_portfolio([]), T0, [ProposalState.PENDING])
    assert ctx.orders_today == 1


def test_mark_all_equity_skips_failing_accounts(tmp_path):
    store = Store(str(tmp_path / "db"))
    ok = FakeBroker(prices={"SPY": 100}, cash="5000", positions={"SPY": 10})
    bad = FakeBroker(prices={"SPY": 100})
    bad.fail_reads = RuntimeError("down")
    runtimes = {"ok": AccountRuntime(make_account("ok"), ok, "paper"),
                "bad": AccountRuntime(make_account("bad"), bad, "paper")}
    mark_all_equity(store, runtimes, T0)
    assert store.equity_marks_since("ok", T0) == [(T0, Decimal("6000"))]
    assert store.equity_marks_since("bad", T0) == []
