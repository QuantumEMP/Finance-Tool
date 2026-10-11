"""Turn analyst signals into concrete limit orders. Pure; no I/O."""
from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from agent.config import RiskLimits
from agent.models import ZERO, OrderConstraints, Portfolio, ProposedOrder, Side, Signal, pct


def round_down(x: Decimal, step: Decimal) -> Decimal:
    return (x / step).to_integral_value(rounding=ROUND_FLOOR) * step


def round_up(x: Decimal, step: Decimal) -> Decimal:
    return (x / step).to_integral_value(rounding=ROUND_CEILING) * step


def target_buy_value(confidence: float, equity: Decimal, position_value: Decimal,
                     cash: Decimal, limits: RiskLimits) -> Decimal:
    """Money to spend on one BUY: confidence-scaled order cap, bounded by the
    position cap and by cash above the reserve. Never negative."""
    by_order = equity * pct(limits.max_order_pct) * Decimal(str(confidence))
    by_position = equity * pct(limits.max_position_pct) - position_value
    by_cash = cash - equity * pct(limits.min_cash_reserve_pct)
    return max(ZERO, min(by_order, by_position, by_cash))


def propose_orders(account: str, signals: dict[str, Signal], portfolio: Portfolio,
                   constraints: dict[str, OrderConstraints], limits: RiskLimits) -> list[ProposedOrder]:
    equity = portfolio.equity
    if equity <= 0:
        return []
    buffer = pct(limits.limit_buffer_pct)
    cash_left = portfolio.free_cash
    orders: list[ProposedOrder] = []

    for symbol in sorted(signals):
        sig = signals[symbol]
        if sig.action not in ("BUY", "SELL") or sig.confidence < limits.min_confidence:
            continue
        price = portfolio.prices.get(symbol, ZERO)
        c = constraints.get(symbol)
        if price <= 0 or c is None:
            continue

        if sig.action == "BUY":
            limit = round_down(price * (1 + buffer), c.price_tick)
            value = target_buy_value(sig.confidence, equity, portfolio.position_value(symbol),
                                     cash_left, limits)
            if limit <= 0:
                continue
            qty = round_down(value / limit, c.qty_step)
            if qty <= 0 or qty < c.min_qty:
                continue
            cash_left -= qty * limit
            side = Side.BUY
        else:
            held = portfolio.positions.get(symbol, ZERO)
            if held <= 0:
                continue
            qty = round_down(held * Decimal(str(sig.confidence)), c.qty_step)
            if held - qty < c.min_qty:
                qty = held  # don't leave an unsellable remainder
            if qty <= 0 or qty < c.min_qty:
                continue
            limit = round_up(price * (1 - buffer), c.price_tick)
            side = Side.SELL

        orders.append(ProposedOrder(account, symbol, side, qty, limit, price,
                                    sig.confidence, sig.reasoning))
    return orders
