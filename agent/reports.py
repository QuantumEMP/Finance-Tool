"""Human-readable text for Telegram. Pure formatting, plus broker reads for /status."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal

from agent.models import Proposal, ProposalState
from agent.runtime import AccountRuntime, day_start
from agent.store import Store

MAX_REASONING = 1000


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
        for s, q in sorted(pf.positions.items()):
            lines.append(f"  {s}: {format(q, 'f')} ≈ {money(pf.position_value(s))} {ccy}")
    return "\n".join(lines).strip()


def report_text(store: Store, accounts: list[str], now: datetime, days: int) -> str:
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
            first, last = marks[0][1], marks[-1][1]
            change = (last / first - 1) * 100 if first else Decimal(0)
            lines.append(f"  equity {money(first)} → {money(last)} ({change:+.2f}%)")
        for p in mine:
            if p.state in (ProposalState.FILLED, ProposalState.PARTIALLY_FILLED):
                avg = "?" if p.avg_price is None else format(p.avg_price, "f")
                lines.append(f"  {p.created_at:%m-%d} {p.order.side.value} {format(p.filled_qty, 'f')} "
                             f"{p.order.symbol} @ {avg}")
    return "\n".join(lines)


def heartbeat_text(store: Store, runtimes: dict[str, AccountRuntime], now: datetime) -> str:
    today = [p for p in store.proposals_since(day_start(now))]
    done = sum(p.state in (ProposalState.FILLED, ProposalState.PARTIALLY_FILLED) for p in today)
    return f"💓 Alive. Today: {len(today)} proposal(s), {done} filled.\n\n" + status_text(store, runtimes)
