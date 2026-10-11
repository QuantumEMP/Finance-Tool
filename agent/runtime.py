"""Per-account runtime bundle and shared risk-context assembly."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable
from zoneinfo import ZoneInfo

from agent.brokers.base import Broker
from agent.config import AccountConfig
from agent.models import Portfolio, ProposalState
from agent.risk import RiskContext
from agent.store import Store

log = logging.getLogger(__name__)
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


def resume(store: Store, now: datetime) -> list[str]:
    """Clear the global pause and every kill-switch pause.

    Accounts paused by the drawdown kill switch get a fresh peak baseline; otherwise
    the next cycle would measure the same drawdown against the old peak and pause
    again. Returns the names of those accounts.
    """
    store.set_flag("paused", None)
    reset = []
    for key in store.flags_with_prefix("paused:"):
        account = key.removeprefix("paused:")
        store.reset_peak(account, now)
        store.set_flag(key, None)
        reset.append(account)
    return reset


def day_start(now: datetime, tz: str = "UTC") -> datetime:
    """Midnight at the start of `now`'s day in time zone `tz`, as a UTC datetime."""
    local = now.astimezone(ZoneInfo(tz)).replace(hour=0, minute=0, second=0, microsecond=0)
    return local.astimezone(timezone.utc)


def mark_all_equity(store: Store, runtimes: dict[str, AccountRuntime], now: datetime) -> None:
    """Record every account's equity. Run regularly (not only at the daily cycle) so the
    daily-loss reference is the account's value at its local midnight, not whenever
    the previous cycle happened to run."""
    for name, rt in runtimes.items():
        try:
            equity = rt.broker.get_portfolio(rt.config.allowlist).equity
        except Exception as e:
            log.warning("%s: could not mark equity: %s", name, e)
            continue
        if equity > 0:
            store.mark_equity(name, now, equity)


def risk_context(store: Store, cfg: AccountConfig, portfolio: Portfolio, now: datetime,
                 counted_states: Iterable[ProposalState]) -> RiskContext:
    since = day_start(now, cfg.schedule.timezone)
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
        fee_pct=cfg.trading_fee_pct,
    )
