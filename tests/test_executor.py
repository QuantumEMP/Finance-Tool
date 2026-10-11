from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from agent.executor import Executor
from agent.models import OrderResult, OrderStatus, Proposal, ProposalState, ProposedOrder, Side
from agent.runtime import AccountRuntime
from agent.store import Store
from tests.fakes import Clock, FakeBroker, make_account

T0 = datetime(2026, 10, 7, 14, 0, tzinfo=timezone.utc)
PS = ProposalState
PID = "a" * 32


def make_pending(store, pid=PID, qty="5", limit="100.20", ref="100", side=Side.BUY):
    order = ProposedOrder("test", "SPY", side, Decimal(qty), Decimal(limit), Decimal(ref), 0.8, "r")
    p = Proposal(pid, order, PS.PENDING, T0, T0 + timedelta(minutes=60), 5.0)
    store.add_proposal(p)
    return p


@pytest.fixture
def env(tmp_path):
    path = str(tmp_path / "db.sqlite")
    store = Store(path)
    clock = Clock(T0)
    broker = FakeBroker(prices={"SPY": 100})
    runtimes = {"test": AccountRuntime(make_account(), broker, "paper")}

    def executor(s=None):
        return Executor(s or store, runtimes, clock, sleep=lambda _: None,
                        poll_attempts=3, poll_interval=0)

    return path, store, clock, broker, executor


def state(store, pid=PID):
    return store.get_proposal(pid).state


def test_approve_fills(env):
    _, store, _, broker, executor = env
    make_pending(store)
    out = executor().approve(PID)
    assert out.state is PS.FILLED and "Filled" in out.message
    assert broker.placed == [("SPY", Side.BUY, Decimal("5"), Decimal("100.20"), PID)]
    p = store.get_proposal(PID)
    assert p.filled_qty == Decimal("5") and p.avg_price == Decimal("100.20")


def test_double_approve_places_once(env):
    _, store, _, broker, executor = env
    make_pending(store)
    ex = executor()
    ex.approve(PID)
    out = ex.approve(PID)
    assert "Already" in out.message and len(broker.placed) == 1


def test_reject_then_approve(env):
    _, store, _, broker, executor = env
    make_pending(store)
    ex = executor()
    assert ex.reject(PID).state is PS.REJECTED
    assert "Already rejected" in ex.approve(PID).message
    assert broker.placed == []


def test_unknown_proposal(env):
    *_, executor = env
    assert executor().approve("f" * 32).message == "Unknown proposal."


def test_expired_not_executed(env):
    _, store, clock, broker, executor = env
    make_pending(store)
    clock.advance(minutes=61)
    assert executor().approve(PID).state is PS.EXPIRED
    assert broker.placed == []


def test_drift_cancels(env):
    _, store, _, broker, executor = env
    make_pending(store)
    broker.prices["SPY"] = Decimal("102")
    out = executor().approve(PID)
    assert out.state is PS.CANCELLED and "moved" in out.message and broker.placed == []


def test_zero_price_cancels(env):
    _, store, _, broker, executor = env
    make_pending(store)
    broker.prices["SPY"] = Decimal("0")
    assert executor().approve(PID).state is PS.CANCELLED


def test_risk_recheck_cancels(env):
    _, store, _, broker, executor = env
    make_pending(store)
    broker.cash = Decimal("1000")
    out = executor().approve(PID)
    assert out.state is PS.CANCELLED and "exceeds" in out.message


def test_second_approval_counts_open_first_order_against_reserve(env):
    # Equity 10000 (1500 cash + 8500 QQQ), reserve 10% = 1000. Each buy of 4 SPY is ~401.
    # The first rests unfilled; the balance stays 1500, but only ~1099 is still free.
    _, store, _, broker, executor = env
    broker.cash = Decimal("1500")
    broker.positions = {"QQQ": Decimal("85")}
    broker.prices["QQQ"] = Decimal("100")
    broker.place_status = OrderStatus.SUBMITTED
    second = "b" * 32
    make_pending(store, qty="4")
    make_pending(store, pid=second, qty="4")
    ex = executor()
    assert ex.approve(PID).state is PS.SUBMITTED
    out = ex.approve(second)
    assert out.state is PS.CANCELLED and "cash reserve" in out.message
    assert [p[4] for p in broker.placed] == [PID]


def test_paused_cancels(env):
    _, store, _, broker, executor = env
    make_pending(store)
    store.set_flag("paused", "1")
    assert executor().approve(PID).state is PS.CANCELLED and broker.placed == []


def test_market_read_failure_cancels(env):
    _, store, _, broker, executor = env
    make_pending(store)
    broker.fail_reads = RuntimeError("timeout")
    out = executor().approve(PID)
    assert out.state is PS.CANCELLED and broker.placed == []


def test_submit_error_never_landed_fails_after_grace(env):
    _, store, clock, broker, executor = env
    make_pending(store)
    broker.fail_place = RuntimeError("connection reset")
    ex = executor()
    out = ex.approve(PID)
    assert out.state is PS.SUBMITTED and "unknown" in out.message
    clock.advance(minutes=71)
    [out] = ex.reconcile()
    assert out.state is PS.FAILED and state(store) is PS.FAILED
    assert len(broker.placed) == 1


def test_submit_error_order_lands_late_is_reconciled(env):
    _, store, _, broker, executor = env
    make_pending(store)
    broker.fail_place = RuntimeError("connection reset")
    ex = executor()
    assert ex.approve(PID).state is PS.SUBMITTED
    broker.orders[PID] = OrderResult(PID, OrderStatus.FILLED, Decimal("5"), Decimal("100.20"))
    [out] = ex.reconcile()
    assert out.state is PS.FILLED and len(broker.placed) == 1


def test_not_found_before_grace_stays_submitted(env):
    _, store, clock, broker, executor = env
    make_pending(store)
    broker.fail_place = RuntimeError("connection reset")
    ex = executor()
    ex.approve(PID)
    clock.advance(minutes=30)
    assert ex.reconcile() == [] and state(store) is PS.SUBMITTED


def test_orphaned_approved_is_cancelled_after_grace(env):
    _, store, clock, broker, executor = env
    make_pending(store)
    store.transition(PID, [PS.PENDING], PS.APPROVED, T0)
    clock.advance(minutes=71)
    [out] = executor().reconcile()
    assert out.state is PS.CANCELLED and broker.placed == []


def test_reconcile_race_no_duplicate(env):
    _, store, _, broker, executor = env
    make_pending(store)
    broker.place_status = OrderStatus.SUBMITTED
    ex = executor()
    ex.approve(PID)
    broker.orders[PID] = OrderResult(PID, OrderStatus.FILLED, Decimal("5"), Decimal("100.1"))
    assert len(ex.reconcile()) == 1
    assert ex.reconcile() == []


def test_submit_error_but_order_landed_is_found_not_resubmitted(env):
    _, store, _, broker, executor = env
    make_pending(store)
    broker.raise_after_place = RuntimeError("read timeout")
    out = executor().approve(PID)
    assert out.state is PS.FILLED and len(broker.placed) == 1


def test_status_unknown_stays_submitted(env):
    _, store, _, broker, executor = env
    make_pending(store)
    broker.place_status = OrderStatus.SUBMITTED
    ex = executor()
    broker.raise_after_place = RuntimeError("read timeout")
    original_get = broker.get_order
    broker.get_order = lambda cid: (_ for _ in ()).throw(RuntimeError("still down"))
    out = ex.approve(PID)
    assert out.state is PS.SUBMITTED and "unknown" in out.message
    broker.get_order = original_get


def test_open_order_then_reconcile(env):
    _, store, _, broker, executor = env
    make_pending(store)
    broker.place_status = OrderStatus.SUBMITTED
    ex = executor()
    out = ex.approve(PID)
    assert out.state is PS.SUBMITTED and "waiting" in out.message
    broker.orders[PID] = OrderResult(PID, OrderStatus.FILLED, Decimal("5"), Decimal("100.1"))
    [done] = ex.reconcile()
    assert done.state is PS.FILLED and state(store) is PS.FILLED
    assert ex.reconcile() == []


def test_exchange_rejection_is_failed(env):
    _, store, _, broker, executor = env
    make_pending(store)
    broker.place_status = OrderStatus.REJECTED
    assert executor().approve(PID).state is PS.FAILED


def test_approve_after_restart(env):
    path, store, _, broker, executor = env
    make_pending(store)
    out = executor(Store(path)).approve(PID)
    assert out.state is PS.FILLED and len(broker.placed) == 1


def test_expiry_enforced_after_restart(env):
    path, store, clock, broker, executor = env
    make_pending(store)
    clock.advance(hours=2)
    assert executor(Store(path)).approve(PID).state is PS.EXPIRED and broker.placed == []


def test_expire_stale(env):
    _, store, clock, _, executor = env
    make_pending(store)
    make_pending(store, pid="b" * 32)
    clock.advance(minutes=60)
    expired = executor().expire_stale()
    assert {p.id for p in expired} == {PID, "b" * 32}
    assert state(store) is PS.EXPIRED
    assert executor().expire_stale() == []
