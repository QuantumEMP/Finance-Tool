# Finance agent on Claude Console (paper trading)

These files run the finance agent as a **Claude Managed Agent** in your Claude Console
account instead of on the Ubuntu machine. It stays on **paper trading**.

## How it works

- Two schedules start a fresh session: **alpaca** on weekdays at 10:00 New York time,
  **luno** every day at 09:00 Johannesburg time (the same times as `config.yaml`).
- Each session downloads this repository's `main` branch, installs it, and runs
  `python -m agent.hosted`. Claude in the session is the analyst: it reads the snapshot,
  writes BUY/SELL/HOLD signals, and the code sizes the orders, applies every risk limit in
  `config.yaml` and places the **paper** orders that pass. Nobody approves each trade.
- Each run writes a report you can open in the session in Console
  (`/mnt/session/outputs/<account>-<date>.md`) and ends with a short summary.
- The database (proposals, fills, equity history, Luno paper balances, pause flags) is kept in
  the `finance-agent-state` memory store between runs.
- Telegram is not used. Console secrets are filled into request headers, and the Telegram bot
  token has to sit in the URL, so it can't be kept out of the sandbox safely.

### Why it can't trade live

- `agent/hosted.py` builds every account in paper mode and ignores `mode: live` and
  `LIVE_TRADING_CONFIRMED`.
- The environment only lets the sandbox reach PyPI, `codeload.github.com`,
  `paper-api.alpaca.markets`, `data.alpaca.markets` and `api.luno.com`. Alpaca's live host is
  not on the list.
- For Luno, create a key with **view-only** permission (balances/market data). Paper mode
  only reads prices, so the key never needs trade or withdraw rights.

## Secrets to enter in the Console

Add these to the `finance-agent-paper` vault (Console → **Vaults**), each as an
**environment variable** credential with **header** injection (the Console default):

| Secret name | Value | Allowed hosts |
|---|---|---|
| `ALPACA_API_KEY` | Alpaca **paper** API key ID | `paper-api.alpaca.markets`, `data.alpaca.markets` |
| `ALPACA_SECRET_KEY` | Alpaca **paper** secret key | `paper-api.alpaca.markets`, `data.alpaca.markets` |
| `LUNO_BASIC_AUTH` | base64 of `KEY_ID:SECRET` for a view-only Luno key (see below) | `api.luno.com` |

To make `LUNO_BASIC_AUTH` on Windows PowerShell (replace both values, keep the colon):

```powershell
[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("YOUR_KEY_ID:YOUR_SECRET"))
```

No Anthropic API key or Telegram token is needed. Never put any of these in a file.

## Upload steps

You need the Anthropic CLI (`ant`, version 1.34 or later). On Windows, install it inside
WSL (Ubuntu): see https://github.com/anthropics/anthropic-cli/releases. Run everything from
the repository root (the folder that contains `agents/`).

1. **Sign in:** `ant auth login`, and pick the Console workspace you want the agent in.
2. **Create the agent, environment, vault and memory store:**
   ```sh
   ant apply --dry-run -v agents/finance-agent/agent.md agents/finance-agent/environment.yaml \
     agents/finance-agent/vault.yaml agents/finance-agent/memory_store-state.yaml
   ant apply agents/finance-agent/agent.md agents/finance-agent/environment.yaml \
     agents/finance-agent/vault.yaml agents/finance-agent/memory_store-state.yaml
   ```
   This writes `claude-lock.json` at the repo root. Commit it, or the next apply makes duplicates.
3. **Add the three secrets** in Console → Vaults → `finance-agent-paper`, as in the table above.
4. **Point the schedules at the vault.** Get its ID:
   ```sh
   jq -r '.resources["./agents/finance-agent/vault.yaml"].id' claude-lock.json
   ```
   and put it in both deployment files: `vault_ids: [vlt_...]`.
5. **Create both schedules and pause them straight away**, so nothing runs before you've tested:
   ```sh
   ant apply agents/finance-agent/deployment-luno.yaml agents/finance-agent/deployment-alpaca.yaml &&
     for f in luno alpaca; do
       ant beta:deployments pause --deployment-id "$(jq -r ".resources[\"./agents/finance-agent/deployment-$f.yaml\"].id" claude-lock.json)"
     done
   ```
6. **Test one run** (manual runs work while paused):
   ```sh
   LUNO=$(jq -r '.resources["./agents/finance-agent/deployment-luno.yaml"].id' claude-lock.json)
   ant beta:deployments run --deployment-id "$LUNO"
   ```
   Open the session in Console (Managed Agents → Sessions) and read the report. Try the alpaca
   one too; outside US market hours it should report "market closed" and stop.
7. **Turn the schedules on** once a run looks right:
   `ant beta:deployments unpause --deployment-id <id>` for each.

Before you unpause alpaca, **stop the Ubuntu service** (`sudo systemctl disable --now
finance-agent`) or use a different Alpaca paper account. Both would otherwise trade the same
paper account with separate records. The Console agent starts with fresh records (Luno paper
cash starts again at `paper_starting_cash`).

## Changing things later

- Risk limits, allowlists or times: edit `config.yaml` (and the cron in the deployment files
  if you change times), merge to `main`. The next run picks it up.
- Instructions to Claude: edit `agent.md`, then `ant apply` (creates a new agent version).
- Pause trading: `ant beta:deployments pause --deployment-id <id>`, or start a session and ask
  it to run `pause`.
- Each run is capped at $3 (`budget` in the deployment files).
