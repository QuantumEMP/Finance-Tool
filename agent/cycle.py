"""One analysis cycle for one account:
fetch → indicators → news → Claude → allocate → risk → PENDING proposals."""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Callable

from agent.allocator import propose_orders
from agent.indicators import compute
from agent.models import Proposal, ProposalState
from agent.retry import with_retry
from agent.risk import check_account, check_order
from agent.runtime import ACTIVE_STATES, AccountRuntime, is_paused, risk_context
from agent.store import Store

log = logging.getLogger(__name__)
CANDLE_DAYS = 90
NEWS_HOURS = 48
BAR_PERIOD = timedelta(days=1)


@dataclass
class CycleResult:
    account: str
    proposals: list[Proposal] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)
    vetoes: list[str] = field(default_factory=list)
    skipped_reason: str | None = None


class CycleRunner:
    def __init__(self, store: Store, analyst, news, now: Callable[[], datetime],
                 new_id: Callable[[], str] = lambda: uuid.uuid4().hex,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._store, self._analyst, self._news = store, analyst, news
        self._now, self._new_id, self._sleep = now, new_id, sleep

    def _retry(self, fn):
        return with_retry(fn, sleep=self._sleep)

    def run(self, rt: AccountRuntime) -> CycleResult:
        cfg, broker, store = rt.config, rt.broker, self._store
        now = self._now()
        res = CycleResult(cfg.name)

        if is_paused(store, cfg.name):
            res.skipped_reason = "paused"
            return res
        try:
            if not self._retry(broker.is_market_open):
                res.skipped_reason = "market closed"
                return res
            portfolio = self._retry(lambda: broker.get_portfolio(cfg.allowlist))
            candles = {s: self._retry(lambda s=s: broker.get_candles(s, CANDLE_DAYS))
                       for s in cfg.allowlist}
            constraints = {s: self._retry(lambda s=s: broker.constraints(s)) for s in cfg.allowlist}
        except Exception as e:
            log.exception("%s: broker error", cfg.name)
            store.record_event(now, cfg.name, "error", {"stage": "fetch", "error": str(e)})
            res.alerts.append(f"{cfg.name}: broker error, skipped this cycle: {e}")
            return res

        store.mark_equity(cfg.name, now, portfolio.equity)
        ctx = risk_context(store, cfg, portfolio, now, ACTIVE_STATES)
        halt = check_account(ctx)
        if halt:
            if halt.kill_switch:
                store.set_flag(f"paused:{cfg.name}", "1")
                res.alerts.append(f"🛑 {cfg.name}: {halt.reason}. Account paused; send /resume to re-enable.")
            store.record_event(now, cfg.name, "halt", {"reason": halt.reason})
            res.skipped_reason = halt.reason
            return res

        indicators = {}
        for s, cs in candles.items():
            # A daily bar is only a close once its day is over. At 10:00 New York (or any
            # time on Luno) the latest bar is today's, still in progress: leave it out.
            closes = [float(c.close) for c in cs if c.ts + BAR_PERIOD <= now]
            ind = compute(s, closes, cfg.periods_per_year)
            if ind is not None:
                indicators[s] = ind
        try:
            headlines = self._news.headlines({s: cfg.news_symbols.get(s, s) for s in cfg.allowlist},
                                             now - timedelta(hours=NEWS_HOURS))
        except Exception as e:
            log.warning("%s: news provider failed: %s", cfg.name, e)
            headlines = {}
        analysis = self._analyst.analyse(cfg.name, cfg.allowlist, indicators, headlines, portfolio)
        store.record_event(now, cfg.name, "analysis", {
            "raw": analysis.raw, "error": analysis.error,
            "signals": {s: vars(sig) for s, sig in analysis.signals.items()},
        })
        if analysis.error:
            res.alerts.append(f"{cfg.name}: analyst failed, holding everything: {analysis.error}")

        pending = store.open_symbols(cfg.name)
        orders = [o for o in propose_orders(cfg.name, analysis.signals, portfolio, constraints, cfg.risk,
                                          cfg.trading_fee_pct)
                  if o.symbol not in pending]
        orders_today = ctx.orders_today
        for order in orders:
            veto = check_order(order, replace(ctx, orders_today=orders_today))
            if veto:
                res.vetoes.append(f"{order.side.value} {order.symbol}: {veto}")
                store.record_event(now, cfg.name, "veto", {"symbol": order.symbol, "reason": veto})
                continue
            proposal = Proposal(
                id=self._new_id(), order=order, state=ProposalState.PENDING, created_at=now,
                expires_at=now + timedelta(minutes=cfg.risk.proposal_ttl_minutes),
                pct_of_equity=float(order.value / portfolio.equity * 100),
            )
            store.add_proposal(proposal)
            res.proposals.append(proposal)
            orders_today += 1
        return res
