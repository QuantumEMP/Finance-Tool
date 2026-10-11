"""Shared value types used across the agent."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum

ZERO = Decimal(0)


def pct(value: Decimal) -> Decimal:
    """Convert a percentage number (20 means 20%) to a fraction."""
    return Decimal(value) / Decimal(100)


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class ProposalState(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    SUBMITTED = "SUBMITTED"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FAILED = "FAILED"


class OrderStatus(str, Enum):
    SUBMITTED = "SUBMITTED"  # accepted, still open
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"  # closed with a partial fill
    REJECTED = "REJECTED"  # closed with nothing filled


@dataclass(frozen=True)
class Candle:
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True)
class OrderConstraints:
    min_qty: Decimal
    qty_step: Decimal
    price_tick: Decimal
    min_notional: Decimal = ZERO  # smallest order value the venue accepts


@dataclass(frozen=True)
class OrderResult:
    client_order_id: str
    status: OrderStatus
    filled_qty: Decimal = ZERO
    avg_price: Decimal | None = None
    reason: str = ""


@dataclass(frozen=True)
class Portfolio:
    """Cash and holdings of one account, keyed by trading symbol.

    `cash` is the full cash balance (it counts toward equity). `spendable` is the part
    not already tied up in open buy orders; None means all of `cash` is free.
    """

    cash: Decimal
    positions: dict[str, Decimal] = field(default_factory=dict)
    prices: dict[str, Decimal] = field(default_factory=dict)
    spendable: Decimal | None = None

    @property
    def free_cash(self) -> Decimal:
        """Cash available for new buys: never more than the balance itself."""
        return self.cash if self.spendable is None else min(self.cash, self.spendable)

    def position_value(self, symbol: str) -> Decimal:
        return self.positions.get(symbol, ZERO) * self.prices.get(symbol, ZERO)

    @property
    def equity(self) -> Decimal:
        return self.cash + sum((self.position_value(s) for s in self.positions), ZERO)


@dataclass(frozen=True)
class Signal:
    symbol: str
    action: str  # "BUY" | "SELL" | "HOLD"
    confidence: float
    reasoning: str


@dataclass(frozen=True)
class ProposedOrder:
    account: str
    symbol: str
    side: Side
    qty: Decimal
    limit_price: Decimal
    ref_price: Decimal  # market price when proposed; used for drift check
    confidence: float
    reasoning: str

    @property
    def value(self) -> Decimal:
        return self.qty * self.limit_price


@dataclass
class Proposal:
    id: str  # 32 lowercase hex chars; also the broker client_order_id
    order: ProposedOrder
    state: ProposalState
    created_at: datetime
    expires_at: datetime
    pct_of_equity: float
    reason: str = ""
    filled_qty: Decimal = ZERO
    avg_price: Decimal | None = None
    message_id: int | None = None
