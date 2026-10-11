from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from agent.cycle import CycleRunner
from agent.models import Candle, ProposalState, Side
from agent.runtime import AccountRuntime, resume
from agent.store import Store
from tests.fakes import Clock, FakeAnalyst, FakeBroker, FakeNews, make_account

T0 = datetime(2026, 10, 7, 14, 0, tzinfo=timezone.utc)


@pytest.fixture
def env(tmp_path):
    store = Store(str(tmp_path / "db.sqlite"))
    clock = Clock(T0)
    broker = FakeBroker(prices={"SPY": 100, "QQQ": 200})
    sleeps = []

    def make(analyst, account=None):
        rt = AccountRuntime(account or make_account(), broker, "paper")
        return CycleRunner(store, analyst, FakeNews(), clock, sleep=sleeps.append), rt

    return store, clock, broker, sleeps, make


def test_proposes_pending_order(env):
    store, clock, broker, _, make = env
    runner, rt = make(FakeAnalyst({"SPY": ("BUY", 0.8)}))
    res = runner.run(rt)
    assert res.alerts == [] and res.skipped_reason is None
    [p] = res.proposals
    assert (p.order.symbol, p.order.side, p.order.qty) == ("SPY", Side.BUY, Decimal("7"))
    assert p.state is ProposalState.PENDING
    assert p.expires_at == T0 + timedelta(minutes=60)
    assert p.pct_of_equity == pytest.approx(7.014)
    assert store.get_proposal(p.id) == p
    assert store.peak_equity("test") == Decimal("10000")
    assert len(p.id) == 32


def test_paused_skips_without_analysis(env):
    store, _, _, _, make = env
    analyst = FakeAnalyst({"SPY": ("BUY", 0.8)})
    runner, rt = make(analyst)
    store.set_flag("paused", "1")
    res = runner.run(rt)
    assert res.skipped_reason == "paused" and analyst.calls == []


def test_market_closed_skips(env):
    _, _, broker, _, make = env
    broker.market_open = False
    analyst = FakeAnalyst({"SPY": ("BUY", 0.8)})
    runner, rt = make(analyst)
    assert runner.run(rt).skipped_reason == "market closed" and analyst.calls == []


def test_broker_failure_alerts_after_retries(env):
    store, _, broker, sleeps, make = env
    broker.fail_reads = RuntimeError("exchange down")
    runner, rt = make(FakeAnalyst({"SPY": ("BUY", 0.8)}))
    res = runner.run(rt)
    assert res.proposals == [] and "exchange down" in res.alerts[0]
    assert sleeps == [1.0, 2.0]
    assert store.events("test", "error")


def test_analyst_error_alerts_and_proposes_nothing(env):
    _, _, _, _, make = env
    runner, rt = make(FakeAnalyst({}, error="Claude API error: x"))
    res = runner.run(rt)
    assert res.proposals == [] and any("analyst" in a for a in res.alerts)


def test_no_duplicate_while_pending(env):
    store, _, _, _, make = env
    runner, rt = make(FakeAnalyst({"SPY": ("BUY", 0.8)}))
    runner.run(rt)
    assert runner.run(rt).proposals == []
    assert len(store.proposals_in_state(ProposalState.PENDING)) == 1


def test_drawdown_kill_switch_pauses_account(env):
    store, _, _, _, make = env
    store.mark_equity("test", T0 - timedelta(days=1), Decimal("20000"))
    analyst = FakeAnalyst({"SPY": ("BUY", 0.8)})
    runner, rt = make(analyst)
    res = runner.run(rt)
    assert "drawdown" in res.skipped_reason and analyst.calls == []
    assert store.get_flag("paused:test") == "1"
    assert any("/resume" in a for a in res.alerts)


def test_resume_after_kill_switch_lets_next_cycle_run(env):
    store, clock, broker, _, make = env
    store.mark_equity("test", T0 - timedelta(days=1), Decimal("20000"))
    analyst = FakeAnalyst({"SPY": ("BUY", 0.8)})
    runner, rt = make(analyst)
    assert "drawdown" in runner.run(rt).skipped_reason

    clock.advance(hours=1)
    assert resume(store, clock()) == ["test"]
    assert store.get_flag("paused:test") is None

    clock.advance(days=1)
    res = runner.run(rt)
    assert res.skipped_reason is None and len(res.proposals) == 1
    assert store.get_flag("paused:test") is None
    assert store.peak_equity("test") == Decimal("10000")


def test_kill_switch_still_trips_on_new_drawdown_after_resume(env):
    store, clock, broker, _, make = env
    store.mark_equity("test", T0 - timedelta(days=1), Decimal("20000"))
    runner, rt = make(FakeAnalyst({}))
    runner.run(rt)
    resume(store, clock())

    clock.advance(days=1)
    assert runner.run(rt).skipped_reason is None
    broker.cash = Decimal("7000")
    clock.advance(days=1)
    res = runner.run(rt)
    assert "drawdown" in res.skipped_reason and "10000.00" in res.skipped_reason
    assert store.get_flag("paused:test") == "1"


def test_resume_of_manual_pause_keeps_peak(env):
    store, clock, _, _, _ = env
    store.mark_equity("test", T0 - timedelta(days=1), Decimal("20000"))
    store.set_flag("paused", "1")
    assert resume(store, clock()) == []
    assert store.get_flag("paused") is None
    assert store.peak_equity("test") == Decimal("20000")


def test_daily_order_limit_vetoes_extra(env):
    _, _, _, _, make = env
    runner, rt = make(FakeAnalyst({"SPY": ("BUY", 0.8), "QQQ": ("BUY", 0.8)}),
                      make_account(max_orders_per_day=1))
    res = runner.run(rt)
    assert [p.order.symbol for p in res.proposals] == ["QQQ"]
    assert len(res.vetoes) == 1 and "SPY" in res.vetoes[0]


def test_overnight_daily_loss_halts(env):
    store, _, _, _, make = env
    store.mark_equity("test", T0 - timedelta(days=1), Decimal("10400"))
    analyst = FakeAnalyst({"SPY": ("BUY", 0.8)})
    runner, rt = make(analyst)
    res = runner.run(rt)
    assert "daily loss" in res.skipped_reason and analyst.calls == []
    assert store.get_flag("paused:test") is None


def test_no_duplicate_while_submitted(env):
    store, _, _, _, make = env
    runner, rt = make(FakeAnalyst({"SPY": ("BUY", 0.8)}))
    [p] = runner.run(rt).proposals
    store.transition(p.id, [ProposalState.PENDING], ProposalState.SUBMITTED, T0)
    assert runner.run(rt).proposals == []


def test_news_exception_becomes_empty_headlines(env):
    _, _, _, _, make = env
    runner, rt = make(FakeAnalyst({"SPY": ("BUY", 0.8)}))

    class Boom:
        def headlines(self, *a, **k):
            raise RuntimeError("news down")

    runner._news = Boom()
    res = runner.run(rt)
    assert len(res.proposals) == 1


def test_full_exit_of_large_position_is_proposed(env):
    # 20 SPY @ 100 is 20% of equity, twice the 10% order cap; a confident SELL must exit it.
    store, clock, broker, _, make = env
    broker.cash, broker.positions = Decimal("8000"), {"SPY": Decimal("20")}
    runner, rt = make(FakeAnalyst({"SPY": ("SELL", 1.0)}))
    res = runner.run(rt)
    assert res.vetoes == []
    [p] = res.proposals
    assert (p.order.side, p.order.qty) == (Side.SELL, Decimal("20"))


def test_indicators_ignore_todays_in_progress_bar(env):
    _, _, broker, _, make = env
    # Daily bars stamped at midnight New York (04:00 UTC), as Alpaca returns them; the
    # last one is today's, 30 minutes old, with a very different price.
    start = datetime(2026, 7, 10, 4, 0, tzinfo=timezone.utc)
    closes = [Decimal(100 + i) for i in range(89)] + [Decimal(500)]
    bars = [Candle(start + timedelta(days=i), c, c, c, c, Decimal(1)) for i, c in enumerate(closes)]
    assert bars[-1].ts.date() == T0.date()
    broker.get_candles = lambda symbol, days: bars
    analyst = FakeAnalyst({})
    runner, rt = make(analyst)
    runner.run(rt)
    indicators = analyst.calls[0][2]
    assert indicators["SPY"].last_close == 188.0
