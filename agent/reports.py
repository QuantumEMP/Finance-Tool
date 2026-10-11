"""Human-readable text for Telegram. Pure formatting, plus broker reads for /status."""
from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal

from agent.models import Proposal, ProposalState, Side
from agent.pnl import build_book
from agent.runtime import AccountRuntime, day_start
from agent.store import Store

log = logging.getLogger(__name__)
MAX_REASONING = 1000
# Confidence buckets for judging whether higher-confidence signals did better.
BUCKETS = ((0.0, 0.7, "<0.7"), (0.7, 0.8, "0.7–0.8"), (0.8, 0.9, "0.8–0.9"), (0.9, 1.01, "≥0.9"))


def money(x: Decimal) -> str:
    return f"{x:,.2f}"


def format_proposal(p: Proposal, currency: str, mode: str) -> str:
    o = p.order
    tag = "🧪 PAPER" if mode == "paper" else "💰 LIVE"
    reasoning = o.reasoning if len(o.reasoning) <= MAX_REASONING else o.reasoning[:MAX_REASONING] + "…"
    return (
        f"{tag} · {o.account}\n"
        f"{o.side.value} {format(o.qty, 'f')} {o.symbol} @ {format(o.limit_price, 'f')} {currency}\n"
        f"≈ {money(o.value)} {currency} ({p.pct_of_equity:.1f}% of account)\n"
        f"Confidence {o.confidence:.2f}\n\n"
        f"{reasoning}\n\n"
        f"Expires {p.expires_at:%H:%M} UTC"
    )


def status_text(store: Store, runtimes: dict[str, AccountRuntime]) -> str:
    lines = []
    if store.get_flag("paused") == "1":
        lines.append("⏸ Agent is PAUSED (send /resume)")
    for name, rt in runtimes.items():
        paused = store.get_flag(f"paused:{name}") == "1"
        header = f"{name} ({rt.mode}{', paused' if paused else ''})"
        try:
            pf = rt.broker.get_portfolio(rt.config.allowlist)
        except Exception as e:
            lines.append(f"\n{header}: could not fetch balances: {e}")
            continue
        ccy = rt.config.quote_currency
        lines.append(f"\n{header}\n  equity {money(pf.equity)} {ccy}, cash {money(pf.cash)} {ccy}")
        lots = build_book(store.filled_proposals(name)).lots
        for s, q in sorted(pf.positions.items()):
            line = f"  {s}: {format(q, 'f')} ≈ {money(pf.position_value(s))} {ccy}"
            lot = lots.get(s)
            if lot and lot.qty > 0 and s in pf.prices:
                # Cost is known only for units the agent bought.
                tracked = min(lot.qty, q)
                unrealised = tracked * (pf.prices[s] - lot.avg_cost)
                line += f", avg cost {money(lot.avg_cost)}, unrealised {unrealised:+,.2f}"
            lines.append(line)
    return "\n".join(lines).strip()


def signal_return(p: Proposal, price: Decimal) -> float | None:
    """How the market moved since a proposal, in the direction it called (percent)."""
    ref = p.order.ref_price
    if ref <= 0:
        return None
    move = float(price / ref - 1) * 100
    return move if p.order.side is Side.BUY else -move


def signal_outcomes(proposals: list[Proposal], prices: dict[str, Decimal]) -> list[str]:
    """Average move since each signal, by confidence bucket. Covers every proposal,
    approved or not, since the question is whether the signal itself was right."""
    rows = []
    for lo, hi, label in BUCKETS:
        rets = [r for p in proposals if lo <= p.order.confidence < hi and p.order.symbol in prices
                and (r := signal_return(p, prices[p.order.symbol])) is not None]
        if rets:
            rows.append(f"    {label}: {sum(rets) / len(rets):+.2f}% avg over {len(rets)}")
    return rows


def benchmark_return(rt: AccountRuntime, since: datetime, now: datetime) -> float | None:
    """Equal-weight buy-and-hold of the allowlist from `since` to now, in percent."""
    days = max(1, (now - since).days) + 7
    rets = []
    for s in rt.config.allowlist:
        candles = [c for c in rt.broker.get_candles(s, days) if c.ts <= since]
        if not candles or candles[-1].close <= 0:
            continue
        rets.append(float(rt.broker.get_price(s) / candles[-1].close - 1) * 100)
    return sum(rets) / len(rets) if rets else None


def report_text(store: Store, accounts: list[str], now: datetime, days: int,
                runtimes: dict[str, AccountRuntime] | None = None) -> str:
    """Proposals, fills and performance per account. With `runtimes`, also fetches
    current prices to compare with buy-and-hold and score signals by confidence."""
    since = now - timedelta(days=days)
    proposals = store.proposals_since(since)
    lines = [f"📊 Last {days} day(s)"]
    if not proposals:
        lines.append("No proposals in this period.")
    for account in accounts:
        mine = [p for p in proposals if p.order.account == account]
        marks = store.equity_marks_since(account, since)
        if not mine and len(marks) < 2:
            continue
        counts = Counter(p.state.value.lower() for p in mine)
        lines.append(f"\n{account}: " + (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "no proposals"))
        if len(marks) >= 2:
            (t0, first), (t1, last) = marks[0], marks[-1]
            flows = store.net_cash_flow(account, t0, t1)
            change = (last - flows - first) / first * 100 if first else Decimal(0)
            line = f"  equity {money(first)} → {money(last)} ({change:+.2f}%"
            line += f" excluding net deposits of {money(flows)})" if flows else ")"
            lines.append(line)
        realised = build_book(store.filled_proposals(account)).realised_since(since)
        if realised:
            lines.append(f"  realised P&L {realised:+,.2f} (average cost, before fees)")
        rt = (runtimes or {}).get(account)
        if rt is not None:
            lines.extend(_market_comparison(rt, mine, since, now))
        for p in mine:
            if p.state in (ProposalState.FILLED, ProposalState.PARTIALLY_FILLED):
                avg = "?" if p.avg_price is None else format(p.avg_price, "f")
                lines.append(f"  {p.created_at:%m-%d} {p.order.side.value} {format(p.filled_qty, 'f')} "
                             f"{p.order.symbol} @ {avg}")
    return "\n".join(lines)


def _market_comparison(rt: AccountRuntime, proposals: list[Proposal], since: datetime,
                       now: datetime) -> list[str]:
    lines = []
    try:
        bench = benchmark_return(rt, since, now)
        if bench is not None:
            lines.append(f"  buy-and-hold of the allowlist: {bench:+.2f}% (fully invested, equal weights)")
        symbols = sorted({p.order.symbol for p in proposals})
        prices = {s: rt.broker.get_price(s) for s in symbols}
    except Exception as e:
        log.warning("%s: report market data failed: %s", rt.config.name, e)
        return [f"  (could not fetch prices for the benchmark: {e})"]
    outcomes = signal_outcomes(proposals, prices)
    if outcomes:
        lines.append("  move since signal, by confidence:")
        lines.extend(outcomes)
    return lines


def heartbeat_text(store: Store, runtimes: dict[str, AccountRuntime], now: datetime) -> str:
    today = [p for p in store.proposals_since(day_start(now))]
    done = sum(p.state in (ProposalState.FILLED, ProposalState.PARTIALLY_FILLED) for p in today)
    return f"💓 Alive. Today: {len(today)} proposal(s), {done} filled.\n\n" + status_text(store, runtimes)
