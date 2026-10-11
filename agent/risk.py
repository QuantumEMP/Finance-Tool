"""Hard risk limits. Pure functions: the caller supplies all state."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from agent.config import RiskLimits
from agent.models import Portfolio, ProposedOrder, Side, pct

# Allow 0.1% slack so prices rounded to the tick don't trip a limit by a cent.
TOLERANCE = Decimal("1.001")


@dataclass(frozen=True)
class Halt:
    reason: str
    kill_switch: bool = False


@dataclass(frozen=True)
class RiskContext:
    portfolio: Portfolio
    limits: RiskLimits
    allowlist: frozenset[str]
    orders_today: int
    day_start_equity: Decimal | None
    peak_equity: Decimal | None
    paused: bool


def check_account(ctx: RiskContext) -> Halt | None:
    if ctx.paused:
        return Halt("agent is paused")
    equity = ctx.portfolio.equity
    if equity <= 0:
        return Halt("account equity is zero")
    lim = ctx.limits
    if ctx.peak_equity and equity < ctx.peak_equity * (1 - pct(lim.drawdown_kill_pct)):
        return Halt(
            f"drawdown kill switch: equity {equity:.2f} is more than "
            f"{lim.drawdown_kill_pct}% below peak {ctx.peak_equity:.2f}",
            kill_switch=True,
        )
    if ctx.day_start_equity and equity < ctx.day_start_equity * (1 - pct(lim.daily_loss_halt_pct)):
        return Halt(
            f"daily loss halt: equity {equity:.2f} is more than "
            f"{lim.daily_loss_halt_pct}% below {ctx.day_start_equity:.2f} at the start of the day"
        )
    return None


def check_order(order: ProposedOrder, ctx: RiskContext) -> str | None:
    halt = check_account(ctx)
    if halt:
        return halt.reason
    lim, pf = ctx.limits, ctx.portfolio
    if order.symbol not in ctx.allowlist:
        return f"{order.symbol} is not on the allowlist"
    if order.qty <= 0 or order.limit_price <= 0:
        return "invalid quantity or price"
    if ctx.orders_today >= lim.max_orders_per_day:
        return f"daily order limit reached ({lim.max_orders_per_day})"
    if order.side is Side.SELL:
        # Sells only reduce exposure, so the order-size cap doesn't apply:
        # otherwise a position above max_order_pct could never be exited.
        if order.qty > pf.positions.get(order.symbol, Decimal(0)):
            return "cannot sell more than is held (no shorting)"
        return None
    equity, value = pf.equity, order.value
    if value > equity * pct(lim.max_order_pct) * TOLERANCE:
        return f"order value {value:.2f} exceeds {lim.max_order_pct}% of equity"
    if pf.position_value(order.symbol) + value > equity * pct(lim.max_position_pct) * TOLERANCE:
        return f"position would exceed {lim.max_position_pct}% of equity"
    if pf.free_cash - value < equity * pct(lim.min_cash_reserve_pct) / TOLERANCE:
        return f"would breach the {lim.min_cash_reserve_pct}% cash reserve"
    return None
