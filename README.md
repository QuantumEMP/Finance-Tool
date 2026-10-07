# Finance agent

Analyses an Alpaca account (US stocks/ETFs) and a Luno account (crypto) once a day,
proposes trades on Telegram, and executes **only the trades you approve**. Paper mode
is the default for both accounts.

> This is a personal tool, not financial advice. It can lose money. Run it in paper
> mode for several weeks and review `/report` before you consider live mode.

## How it decides

1. Code computes indicators (SMA20/50, 14-day momentum, 30-day volatility, drawdown).
2. Claude reads the indicators and recent headlines and returns BUY/SELL/HOLD,
   a confidence score and short reasoning for each allowlisted symbol.
3. Code sizes orders from confidence and the risk limits in `config.yaml`.
4. The risk guard can veto anything. Daily-loss halts and a drawdown kill switch pause the account.
5. You get a Telegram message per trade. Nothing happens unless you tap ✅ Approve
   before it expires. On approval, price drift and risk are re-checked first.

## Setup

### 1. API keys

- **Alpaca:** create a free account, switch to the *Paper* dashboard and generate API keys.
  Paper keys only work against the paper endpoint. For live mode you need separate live keys.
- **Luno:** Settings → API keys. Grant **only** the permissions to view balances and
  trade (place/cancel orders, view orders). **Do not grant send/withdraw permission.**
  Luno has no sandbox, so in paper mode the agent only reads prices and simulates trades locally.
- **Anthropic:** create an API key at console.anthropic.com.
- **Telegram:** message @BotFather, `/newbot`, and copy the token. Send any message to your
  new bot, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy
  `message.chat.id`. That is `TELEGRAM_OWNER_CHAT_ID`.

### 2. Install on the Ubuntu machine (24.04+, or Python 3.11+)

```bash
sudo useradd --system --no-create-home --home-dir /opt/finance-agent --shell /usr/sbin/nologin finance-agent
sudo mkdir -p /opt/finance-agent
sudo chown finance-agent: /opt/finance-agent
sudo -u finance-agent git clone <this repo> /opt/finance-agent   # or copy the folder
cd /opt/finance-agent
sudo -u finance-agent python3 -m venv .venv
sudo -u finance-agent .venv/bin/pip install .
sudo -u finance-agent mkdir -p data
sudo -u finance-agent cp .env.example .env
sudo chmod 600 .env
sudo -u finance-agent nano .env        # fill in keys; leave LIVE_TRADING_CONFIRMED=no
sudo cp deploy/finance-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now finance-agent
journalctl -u finance-agent -f
```

Edit `config.yaml` for your allowlists, Luno quote currency (ZAR/NGN/…), schedule
times and risk limits, then run `sudo systemctl restart finance-agent`.

## Telegram commands

| Command | What it does |
|---|---|
| `/run [alpaca\|luno]` | Run an analysis cycle now |
| `/status` | Mode, balances, positions, paused state |
| `/report [days]` | Proposals, fills and equity change (default 7 days) |
| `/pause` | Stop all proposals and executions immediately |
| `/resume` | Clear pause and any kill-switch pause |

## Going live (only after a good paper run)

Both switches are required, per account:

1. In `config.yaml`, set `mode: live` for that account.
2. In `.env`, set `LIVE_TRADING_CONFIRMED=yes` (and use live Alpaca keys for Alpaca).

Restart the service. The log and every proposal message show **LIVE** for live accounts.

### Before going live with Luno

Luno's API field names were implemented from the official SDKs but could not be checked
against the live API docs page. Before enabling live mode for Luno:

1. Do a read-only check first: in paper mode, `/status` should show the correct quote-currency cash (e.g. ZAR)
   and holdings, and `/run luno` should produce sensible prices.
2. Confirm the order endpoints (`POST /api/1/postorder`, `GET /api/exchange/3/order`)
   against https://www.luno.com/en/developers/api.

## Development

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"     # Windows: .venv\Scripts\pip
.venv/bin/python -m pytest
```
