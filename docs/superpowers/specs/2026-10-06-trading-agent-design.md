# Trading Agent — Design Spec

**Date:** 2026-10-06
**Status:** Draft — awaiting review

## 1. Purpose

An AI-assisted trading agent that analyses market data for two separate
accounts (Luno for crypto, Alpaca for US stocks/ETFs), proposes trades with
plain-English reasoning, and executes a trade **only after the owner approves
it on Telegram**.

The agent is decision support with execution, not an autonomous trader. It
makes no claim to find "the best" investment; it must prove its signals in
paper mode before live use is considered.

### Decisions made

| Topic | Decision |
|---|---|
| Venues | Luno (crypto) and Alpaca (stocks/ETFs) |
| Autonomy | Approve every trade individually |
| Analysis | Claude (LLM) + code-computed technical indicators + news headlines |
| Universe | Small fixed allowlist per account, editable in config |
| Accounts | Separate budgets; no cross-account money movement; one combined report |
| Approvals | Telegram bot with Approve / Reject buttons |
| Architecture | One always-on Python process (scheduler + bot + SQLite) |
| Host | Separate always-on Ubuntu machine, run as a systemd service |
| Language | Python 3.11+ |

### Out of scope (v1)

Leverage, margin, shorting, options, DEX/self-custody wallets, transfers
between accounts, withdrawals, backtesting engine, web dashboard, multiple
users.

## 2. Components

```
finance-agent/
  config.yaml          # allowlists, risk limits, schedule, modes (no secrets)
  .env                 # secrets — never committed
  agent/
    main.py            # entry point: scheduler + Telegram bot in one process
    config.py          # load + validate config.yaml and .env
    data/              # candles, prices, news (Alpaca news API)
    indicators.py      # pure: trend, momentum, volatility, drawdown
    analyst.py         # Claude call → schema-validated signals
    allocator.py       # pure: signals + balances → proposed orders
    risk.py            # pure: hard limits, veto with reason
    approvals.py       # proposal state machine
    executor.py        # final re-checks → broker.place_order()
    brokers/
      base.py          # Broker interface
      alpaca.py        # Alpaca paper or live endpoint
      luno.py          # Luno live API
      paper.py         # local simulator: real prices, simulated fills
    bot.py             # Telegram: proposals, buttons, commands
    store.py           # SQLite persistence
  tests/
  deploy/finance-agent.service   # systemd unit
```

**Key boundary:** only `executor.py` may call `place_order`, and only for a
proposal that is APPROVED, unexpired, within price-drift tolerance, and passes
a fresh risk check.

### Broker interface (`brokers/base.py`)

```python
class Broker(Protocol):
    name: str
    def get_balances(self) -> dict[str, Decimal]: ...
    def get_price(self, symbol: str) -> Decimal: ...
    def get_candles(self, symbol: str, days: int) -> list[Candle]: ...
    def place_limit_order(self, symbol: str, side: Side, qty: Decimal,
                          limit_price: Decimal, client_order_id: str) -> OrderResult: ...
    def get_order(self, client_order_id: str) -> OrderResult: ...
    def min_order(self, symbol: str) -> OrderConstraints: ...  # min size, step, tick
```

`PaperBroker` wraps a real broker for market data and simulates fills and
balances locally (persisted in SQLite). Luno has no sandbox, so Luno paper
mode always uses `PaperBroker`. Alpaca paper mode uses Alpaca's own paper
endpoint.

### Modes

Each account has `mode: paper | live` in `config.yaml` (default `paper`).
Live requires **both** `mode: live` **and** `LIVE_TRADING_CONFIRMED=yes` in
`.env`. If either is missing the account runs in paper mode and the startup
log says so.

## 3. Data flow (one cycle)

Triggered by the scheduler (default: Alpaca daily 30 min after US market
open on trading days; Luno daily at a configured local time) or by `/run`.
Each account is processed independently.

1. **Fetch:** balances, 90 days of daily candles for allowlisted symbols,
   news headlines from the last 48h (Alpaca news API; for crypto, headlines
   for BTC/ETH where available).
2. **Compute:** per asset: SMA20, SMA50, 14-day momentum (% change),
   30-day annualised volatility, drawdown from 90-day high.
3. **Analyse:** send indicators, headlines, current holdings and cash to
   Claude via the Anthropic API using structured output. Expected shape:
   `[{symbol, signal: BUY|SELL|HOLD, confidence: 0..1, reasoning: str}]`.
   Validate against a schema; any invalid entry or non-allowlisted symbol
   → HOLD. Total failure → all HOLD + alert.
4. **Allocate:** signals with confidence ≥ `min_confidence` become target
   orders sized as a % of account equity (scaled by confidence, capped by
   `max_order_pct`), rounded to the broker's size/tick constraints. SELL
   reduces an existing position only; no shorts.
5. **Risk check:** each order is checked against the limits in §4. Vetoes are
   stored with the reason.
6. **Propose:** surviving orders are saved as PENDING with `expires_at`, and
   one Telegram message per proposal is sent: side, qty, symbol, est. value,
   % of account, confidence, reasoning, buttons [Approve] [Reject].
7. **Execute (on Approve):** in one DB transaction: proposal must be PENDING
   and unexpired → mark APPROVED. Then re-fetch price; if drift >
   `max_drift_pct` → CANCELLED. Re-run risk check; fail → CANCELLED.
   Otherwise place a limit order with `client_order_id = proposal_id` →
   SUBMITTED → poll until FILLED / PARTIAL / REJECTED. Notify the result.
8. **Record:** every step is persisted: inputs, Claude's raw response and
   reasoning, vetoes, decision and actor, order and fill details.

### Proposal states

```
PENDING → APPROVED → SUBMITTED → FILLED | PARTIALLY_FILLED | FAILED
        → REJECTED
        → EXPIRED
APPROVED → CANCELLED (drift or risk re-check failed)
```

Transitions are guarded in SQL (`UPDATE … WHERE state = 'PENDING'`), so a
double tap or replay cannot execute twice.

## 4. Risk limits and safety

### Configurable defaults (`config.yaml`)

Conservative engineering defaults, intended to be tuned by the owner. Not
financial advice.

| Limit | Alpaca | Luno |
|---|---|---|
| `max_position_pct` | 20% | 10% |
| `max_order_pct` | 10% | 5% |
| `min_cash_reserve_pct` | 10% | 20% |
| `daily_loss_halt_pct` | 3% | 5% |
| `drawdown_kill_pct` (from peak equity) | 15% | 20% |
| `max_orders_per_day` | 5 | 5 |
| `min_confidence` | 0.6 | 0.6 |
| `proposal_ttl_minutes` | 60 | 60 |
| `max_drift_pct` | 1.0% | 1.5% |
| Order type | limit | limit |

- **Daily loss halt:** no new proposals for that account until the next day.
- **Drawdown kill switch:** pauses that account until `/resume`. Peak equity
  is persisted, so it survives restarts.

### Fixed rules (not configurable)

- Allowlisted symbols only. No leverage, margin, shorting or options.
- The bot ignores all updates whose chat ID ≠ `TELEGRAM_OWNER_CHAT_ID`.
- `/pause` stops all proposals and executions immediately; `/resume` re-enables them.
- Luno API key must be created **without withdrawal permission**. Documented
  in the README setup steps; the agent never calls withdrawal endpoints.
- Secrets live only in `.env` (mode 600), never in logs or Telegram messages.
- Any error on the trade path → no trade + alert.

## 5. Telegram bot

Commands: `/run [alpaca|luno]`, `/status` (balances, positions, mode, paused?),
`/report [days]` (proposals, approvals, fills, P&L), `/pause`, `/resume`.
Buttons carry `approve:<proposal_id>` / `reject:<proposal_id>`; after a tap
the message is edited to show the outcome and the buttons are removed.
Long polling only, so no inbound ports are needed.

A daily heartbeat message reports that the agent is alive, plus proposals
made, approvals, and balances.

## 6. Error handling

| Failure | Behaviour |
|---|---|
| Broker/data API error or timeout | 3 retries with exponential backoff → skip account this cycle + alert |
| Claude API error / invalid output | All HOLD + alert |
| Exchange rejects order | FAILED with reason → alert |
| Order status unknown after submit | Poll `get_order(client_order_id)`; never re-submit |
| Telegram unreachable | Proposals expire unapproved; logged |
| Process crash | systemd `Restart=on-failure`; state reloaded from SQLite, expiry still enforced |

The duplicate-order protection relies on `client_order_id`. Alpaca supports
it natively. Luno's support must be verified during implementation; if it is
absent, the Luno adapter records the exchange order ID immediately and
reconciles open orders before any retry.

## 7. Testing

1. **Unit (pytest):** `indicators`, `allocator`, `risk`, with edge cases
   (zero balance, at-limit sizes, rounding to Luno min size/step, SELL with
   no position).
2. **Integration with fakes:** `FakeBroker` + `FakeAnalyst` drive a full
   cycle: propose → approve → execute; expiry; drift cancel; risk re-check
   cancel; kill switch persisting across restart.
3. **Approval security:** wrong chat ID ignored; double approve executes once;
   approving an expired proposal fails.
4. **Paper run:** Alpaca paper + Luno `PaperBroker` on the Ubuntu host for
   several weeks, reviewed with `/report`, before any live consideration.

No test may hit a live trading endpoint.

## 8. Deployment

- Ubuntu host, Python venv, `deploy/finance-agent.service` systemd unit with
  `Restart=on-failure`, running as a dedicated non-root user.
- Logs go to journald (`journalctl -u finance-agent`); the SQLite DB lives in
  the service user's data dir.
- README covers: creating API keys (Luno trade-only, Alpaca paper), creating
  the Telegram bot with BotFather, finding the owner chat ID, filling `.env`,
  and installing the service.

## 9. Dependencies

`anthropic`, `alpaca-py`, `python-telegram-bot` (v21+, asyncio),
`APScheduler`, `httpx` (Luno REST), `pydantic` (config and signal schemas),
`python-dotenv`, `pytest`. SQLite via the standard library.
