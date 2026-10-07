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
