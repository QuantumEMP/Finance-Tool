"""The only code path that places orders.

approve(): PENDING → APPROVED → (re-checks) → SUBMITTED → FILLED/PARTIALLY_FILLED/FAILED.
Each step is a guarded SQL transition, so double taps and restarts can't double-trade.
place_limit_order is never retried; after an error we look the order up by its id.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from agent.models import OrderResult, OrderStatus, Proposal, ProposalState
from agent.retry import with_retry
from agent.risk import check_order
from agent.runtime import EXECUTED_STATES, AccountRuntime, is_paused, risk_context
from agent.store import Store

log = logging.getLogger(__name__)
PS = ProposalState
RECONCILE_GRACE = timedelta(minutes=10)


@dataclass(frozen=True)
class ExecutionOutcome:
    proposal_id: str
    state: ProposalState | None
    message: str


def _fmt(x) -> str:
    return "?" if x is None else format(x, "f")


class Executor:
    def __init__(self, store: Store, runtimes: dict[str, AccountRuntime], now: Callable[[], datetime],
                 sleep: Callable[[float], None] = time.sleep, poll_attempts: int = 5,
                 poll_interval: float = 2.0) -> None:
        self._store, self._runtimes, self._now, self._sleep = store, runtimes, now, sleep
        self._poll_attempts, self._poll_interval = poll_attempts, poll_interval

    # --- owner decisions ---------------------------------------------------

    def reject(self, pid: str) -> ExecutionOutcome:
        if self._store.transition(pid, [PS.PENDING], PS.REJECTED, self._now(), reason="rejected by owner"):
            return ExecutionOutcome(pid, PS.REJECTED, "❌ Rejected.")
        return self._already(pid)

    def approve(self, pid: str) -> ExecutionOutcome:
        store = self._store
        p = store.get_proposal(pid)
        if p is None:
            return ExecutionOutcome(pid, None, "Unknown proposal.")
        if p.state is not PS.PENDING:
            return self._already(pid)
        now = self._now()
        o = p.order
        rt = self._runtimes.get(o.account)

        if rt is None or is_paused(store, o.account):
            reason = "agent is paused" if rt else "account is no longer configured"
            if store.transition(pid, [PS.PENDING], PS.CANCELLED, now, reason=reason):
                return ExecutionOutcome(pid, PS.CANCELLED, f"🚫 Cancelled: {reason}")
            return self._already(pid)
        if now >= p.expires_at:
            if store.transition(pid, [PS.PENDING], PS.EXPIRED, now, reason="approved after expiry"):
                return ExecutionOutcome(pid, PS.EXPIRED, "⌛ Expired before approval; not executed.")
            return self._already(pid)
        if not store.transition(pid, [PS.PENDING], PS.APPROVED, now):
            return self._already(pid)

        broker, cfg = rt.broker, rt.config
        try:
            price = with_retry(lambda: broker.get_price(o.symbol), sleep=self._sleep)
            portfolio = with_retry(lambda: broker.get_portfolio(cfg.allowlist), sleep=self._sleep)
        except Exception as e:
            return self._cancel(pid, f"could not re-check the market ({e})")
        if price <= 0 or o.ref_price <= 0:
            return self._cancel(pid, "exchange returned an invalid price")
        drift = abs(price - o.ref_price) / o.ref_price * 100
        if drift > cfg.risk.max_drift_pct:
            return self._cancel(pid, f"price moved {drift:.2f}% since the proposal "
                                     f"(limit {cfg.risk.max_drift_pct}%)")
        veto = check_order(o, risk_context(store, cfg, portfolio, now, EXECUTED_STATES))
        if veto:
            return self._cancel(pid, veto)

        if not store.transition(pid, [PS.APPROVED], PS.SUBMITTED, now):
            return self._already(pid)
        try:
            result = broker.place_limit_order(o.symbol, o.side, o.qty, o.limit_price, client_order_id=pid)
        except Exception as e:
            log.exception("submit failed for %s", pid)
            store.record_event(now, o.account, "submit_error", {"proposal": pid, "error": str(e)})
            return self._poll(pid, rt, submit_error=str(e))
        if result.status is OrderStatus.SUBMITTED:
            return self._poll(pid, rt)
        return self._apply(pid, result)

    # --- background housekeeping -------------------------------------------

    def reconcile(self) -> list[ExecutionOutcome]:
        outcomes = []
        for p in self._store.proposals_in_state(PS.SUBMITTED):
            rt = self._runtimes.get(p.order.account)
            if rt is None:
                continue
            try:
                result = rt.broker.get_order(p.id)
            except Exception:
                log.warning("reconcile: could not fetch %s", p.id)
                continue
            if result is not None and result.status is not OrderStatus.SUBMITTED:
                outcomes.append(self._apply(p.id, result))
            elif result is None and self._now() >= p.expires_at + RECONCILE_GRACE:
                if self._store.transition(p.id, [PS.SUBMITTED], PS.FAILED, self._now(),
                                          reason="order never appeared on the exchange"):
                    outcomes.append(ExecutionOutcome(
                        p.id, PS.FAILED, "❗ Order failed: it never appeared on the exchange."))
        for p in self._store.proposals_in_state(PS.APPROVED):
            if self._now() >= p.expires_at + RECONCILE_GRACE and self._store.transition(
                    p.id, [PS.APPROVED], PS.CANCELLED, self._now(), reason="interrupted before submission"):
                outcomes.append(ExecutionOutcome(p.id, PS.CANCELLED,
                                                 "🚫 Cancelled: interrupted before submission"))
        return outcomes

    def expire_stale(self) -> list[Proposal]:
        now, expired = self._now(), []
        for p in self._store.proposals_in_state(PS.PENDING):
            if now >= p.expires_at and self._store.transition(
                    p.id, [PS.PENDING], PS.EXPIRED, now, reason="not approved in time"):
                expired.append(p)
        return expired

    # --- helpers -------------------------------------------------------------

    def _already(self, pid: str) -> ExecutionOutcome:
        p = self._store.get_proposal(pid)
        if p is None:
            return ExecutionOutcome(pid, None, "Unknown proposal.")
        return ExecutionOutcome(pid, p.state, f"Already {p.state.value.lower()}; nothing done.")

    def _cancel(self, pid: str, reason: str) -> ExecutionOutcome:
        self._store.transition(pid, [PS.APPROVED], PS.CANCELLED, self._now(), reason=reason)
        return ExecutionOutcome(pid, PS.CANCELLED, f"🚫 Cancelled: {reason}")

    def _poll(self, pid: str, rt: AccountRuntime, submit_error: str | None = None) -> ExecutionOutcome:
        last: OrderResult | None = None
        errored = False
        for attempt in range(self._poll_attempts):
            if attempt:
                self._sleep(self._poll_interval)
            try:
                last = rt.broker.get_order(pid)
                errored = False
            except Exception:
                errored = True
                continue
            if last is not None and last.status is not OrderStatus.SUBMITTED:
                return self._apply(pid, last)
        if errored or submit_error is not None:
            return ExecutionOutcome(pid, PS.SUBMITTED, "⚠️ Order status unknown; please check the "
                                                       "exchange. I'll keep checking.")
        return ExecutionOutcome(pid, PS.SUBMITTED, "⏳ Order placed; waiting for a fill. I'll report back.")

    def _apply(self, pid: str, result: OrderResult) -> ExecutionOutcome:
        state = {OrderStatus.FILLED: PS.FILLED, OrderStatus.PARTIALLY_FILLED: PS.PARTIALLY_FILLED,
                 OrderStatus.REJECTED: PS.FAILED}[result.status]
        if not self._store.transition(pid, [PS.SUBMITTED], state, self._now(), reason=result.reason,
                                      filled_qty=result.filled_qty, avg_price=result.avg_price):
            return self._already(pid)
        o = self._store.get_proposal(pid).order
        if state is PS.FILLED:
            msg = f"✅ Filled: {o.side.value} {_fmt(result.filled_qty)} {o.symbol} @ {_fmt(result.avg_price)}"
        elif state is PS.PARTIALLY_FILLED:
            msg = (f"◐ Partially filled: {_fmt(result.filled_qty)} of {_fmt(o.qty)} {o.symbol} "
                   f"@ {_fmt(result.avg_price)}")
        else:
            msg = f"❗ Order not filled: {result.reason or 'rejected by the exchange'}"
        return ExecutionOutcome(pid, state, msg)
