"""Cost basis and realised P&L from this tool's own fills (average-cost method).

Only trades the agent made are known here: units bought outside the tool have no
recorded cost, so a sell is matched against the tracked units only. Figures are
before fees.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from agent.models import ZERO, Proposal, ProposalState, Side

FILLED_STATES = (ProposalState.FILLED, ProposalState.PARTIALLY_FILLED)


@dataclass
class Lot:
    qty: Decimal = ZERO
    cost: Decimal = ZERO  # total cost of `qty`

    @property
    def avg_cost(self) -> Decimal | None:
        return self.cost / self.qty if self.qty > 0 else None


@dataclass
class Realised:
    at: datetime
    symbol: str
    qty: Decimal
    pnl: Decimal


@dataclass
class Book:
    lots: dict[str, Lot] = field(default_factory=dict)
    realised: list[Realised] = field(default_factory=list)

    def realised_since(self, since: datetime) -> Decimal:
        return sum((r.pnl for r in self.realised if r.at >= since), ZERO)


def build_book(proposals: list[Proposal]) -> Book:
    """Replay filled proposals (oldest first) into per-symbol lots and realised P&L."""
    book = Book()
    fills = [p for p in proposals
             if p.state in FILLED_STATES and p.filled_qty > 0 and p.avg_price is not None]
    for p in sorted(fills, key=lambda p: p.created_at):
        lot = book.lots.setdefault(p.order.symbol, Lot())
        qty, price = p.filled_qty, p.avg_price
        if p.order.side is Side.BUY:
            lot.qty += qty
            lot.cost += qty * price
            continue
        matched = min(qty, lot.qty)
        if matched <= 0:
            continue
        avg = lot.cost / lot.qty
        book.realised.append(Realised(p.created_at, p.order.symbol, matched, matched * (price - avg)))
        lot.cost -= matched * avg
        lot.qty -= matched
    return book
