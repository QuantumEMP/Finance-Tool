"""Per-account runtime bundle and shared risk-context assembly."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from agent.brokers.base import Broker
from agent.config import AccountConfig
from agent.models import Portfolio, ProposalState
from agent.risk import RiskContext
from agent.store import Store

PS = ProposalState
# Proposals that count toward max_orders_per_day when creating new proposals...
ACTIVE_STATES = (PS.PENDING, PS.APPROVED, PS.SUBMITTED, PS.FILLED, PS.PARTIALLY_FILLED)
# ...and when re-checking at execution time (other pending proposals don't count).
EXECUTED_STATES = (PS.SUBMITTED, PS.FILLED, PS.PARTIALLY_FILLED)


@dataclass
class AccountRuntime:
    config: AccountConfig
    broker: Broker
    mode: str  # "paper" | "live"


def is_paused(store: Store, account: str) -> bool:
    return store.get_flag("paused") == "1" or store.get_flag(f"paused:{account}") == "1"


def day_start(now: datetime) -> datetime:
    return now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def risk_context(store: Store, cfg: AccountConfig, portfolio: Portfolio, now: datetime,
                 counted_states: Iterable[ProposalState]) -> RiskContext:
    since = day_start(now)
    reference = store.last_equity_before(cfg.name, since)
    if reference is None:
        reference = store.first_equity_since(cfg.name, since)
    return RiskContext(
        portfolio=portfolio,
        limits=cfg.risk,
        allowlist=frozenset(cfg.allowlist),
        orders_today=store.count_proposals(cfg.name, since, counted_states),
        day_start_equity=reference,
        peak_equity=store.peak_equity(cfg.name),
        paused=is_paused(store, cfg.name),
    )
