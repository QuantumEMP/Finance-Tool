"""Test doubles and builders shared across test modules."""
from __future__ import annotations

from datetime import time
from decimal import Decimal

from agent.config import AccountConfig, RiskLimits, Schedule
from agent.models import OrderConstraints

WHOLE = OrderConstraints(Decimal("1"), Decimal("1"), Decimal("0.01"))


def make_limits(**overrides) -> RiskLimits:
    base = dict(
        max_position_pct=20, max_order_pct=10, min_cash_reserve_pct=10,
        daily_loss_halt_pct=3, drawdown_kill_pct=15, max_orders_per_day=5,
        min_confidence=0.6, proposal_ttl_minutes=60, max_drift_pct=1,
        limit_buffer_pct=Decimal("0.2"),
    )
    base.update(overrides)
    return RiskLimits(**base)


def make_account(name="test", allowlist=("SPY", "QQQ"), **risk_overrides) -> AccountConfig:
    return AccountConfig(
        name=name, venue="alpaca", quote_currency="USD", allowlist=list(allowlist),
        periods_per_year=252, schedule=Schedule(run_at=time(10, 0), timezone="UTC"),
        risk=make_limits(**risk_overrides),
    )


# --- appended in Task 6 ---
from datetime import datetime, timedelta, timezone

from agent.models import Candle, OrderResult, OrderStatus, Portfolio


class Clock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


class FakeBroker:
    """In-memory broker. Fills instantly unless told otherwise."""

    def __init__(self, name="fake", prices=None, cash="10000", positions=None,
                 constraints=WHOLE, market_open=True):
        self.name = name
        self.prices = {k: Decimal(str(v)) for k, v in (prices or {}).items()}
        self.cash = Decimal(str(cash))
        self.positions = {k: Decimal(str(v)) for k, v in (positions or {}).items()}
        self.default_constraints = constraints
        self.market_open = market_open
        self.orders: dict[str, OrderResult] = {}
        self.placed: list[tuple] = []
        self.place_status = OrderStatus.FILLED
        self.fail_place: Exception | None = None         # raise; order never landed
        self.raise_after_place: Exception | None = None  # order landed, then raise
        self.fail_reads: Exception | None = None
        self.cancelled: list[str] = []
        self.fail_cancel: Exception | None = None
        self.cancel_pending = False  # cancel accepted, but the order still reads as open

    def _check(self):
        if self.fail_reads:
            raise self.fail_reads

    def is_market_open(self):
        return self.market_open

    def get_portfolio(self, symbols):
        self._check()
        return Portfolio(self.cash,
                         {s: q for s, q in self.positions.items() if s in symbols and q > 0},
                         {s: self.prices[s] for s in symbols if s in self.prices})

    def get_price(self, symbol):
        self._check()
        return self.prices[symbol]

    def get_candles(self, symbol, days):
        self._check()
        p = self.prices[symbol]
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        return [Candle(start + timedelta(days=i), p, p, p, p, Decimal(1)) for i in range(days)]

    def constraints(self, symbol):
        return self.default_constraints

    def place_limit_order(self, symbol, side, qty, limit_price, client_order_id):
        self.placed.append((symbol, side, qty, limit_price, client_order_id))
        if self.fail_place:
            raise self.fail_place
        filled = qty if self.place_status is OrderStatus.FILLED else Decimal(0)
        result = OrderResult(client_order_id, self.place_status, filled,
                             limit_price if filled else None)
        self.orders[client_order_id] = result
        if self.raise_after_place:
            raise self.raise_after_place
        return result

    def get_order(self, client_order_id):
        self._check()
        return self.orders.get(client_order_id)

    def cancel_order(self, client_order_id):
        self.cancelled.append(client_order_id)
        if self.fail_cancel:
            raise self.fail_cancel
        o = self.orders.get(client_order_id)
        if o is None or o.status is not OrderStatus.SUBMITTED or self.cancel_pending:
            return
        st = OrderStatus.PARTIALLY_FILLED if o.filled_qty > 0 else OrderStatus.REJECTED
        self.orders[client_order_id] = OrderResult(client_order_id, st, o.filled_qty, o.avg_price,
                                                   "canceled")


# --- appended in Task 10 ---
from agent.analyst import AnalysisResult
from agent.models import Signal


class FakeAnalyst:
    def __init__(self, signals: dict[str, tuple[str, float]], error: str | None = None):
        self.signals, self.error, self.calls = signals, error, []

    def analyse(self, account, allowlist, indicators, headlines, portfolio):
        self.calls.append((account, list(allowlist), dict(indicators), dict(headlines)))
        sigs = {s: Signal(s, *self.signals.get(s, ("HOLD", 0.0)), "fake reasoning")
                for s in allowlist}
        return AnalysisResult(sigs, "{}", self.error)


class FakeNews:
    def headlines(self, symbol_map, since, limit=50):
        return {}
