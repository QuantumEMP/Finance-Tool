"""Wire config, secrets, brokers, analyst and bot together; run until stopped."""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from agent.analyst import Analyst
from agent.bot import TradingBot
from agent.brokers.alpaca import AlpacaBroker
from agent.brokers.luno import LunoBroker
from agent.brokers.paper import PaperBroker
from agent.config import AccountConfig, Secrets, effective_mode, load_config, load_secrets
from agent.cycle import CycleRunner
from agent.executor import Executor
from agent.news import AlpacaNews, NoNews
from agent.runtime import AccountRuntime
from agent.store import Store

log = logging.getLogger("agent")


def build_runtime(acct: AccountConfig, secrets: Secrets, store: Store) -> AccountRuntime:
    mode = effective_mode(acct, secrets)
    if acct.venue == "alpaca":
        if not (secrets.alpaca_api_key and secrets.alpaca_secret_key):
            raise ValueError(f"{acct.name}: ALPACA_API_KEY and ALPACA_SECRET_KEY are required")
        broker = AlpacaBroker.from_keys(secrets.alpaca_api_key, secrets.alpaca_secret_key,
                                        paper=(mode == "paper"))
    else:
        if not (secrets.luno_api_key_id and secrets.luno_api_secret):
            raise ValueError(f"{acct.name}: LUNO_API_KEY_ID and LUNO_API_SECRET are required")
        market = LunoBroker(secrets.luno_api_key_id, secrets.luno_api_secret, acct.quote_currency)
        broker = market if mode == "live" else PaperBroker(
            market, store, acct.name, acct.paper_starting_cash, acct.paper_fee_pct)
    if acct.mode == "live" and mode == "paper":
        log.warning("%s: mode is live in config but LIVE_TRADING_CONFIRMED is not 'yes'; "
                    "running in PAPER mode", acct.name)
    if mode == "live":
        log.warning("%s: LIVE TRADING ENABLED", acct.name)
    return AccountRuntime(acct, broker, mode)


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs request URLs at INFO; Telegram URLs contain the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    config = load_config(argv[0] if argv else "config.yaml")
    secrets = load_secrets(os.environ)
    Path(config.db_path).parent.mkdir(parents=True, exist_ok=True)
    store = Store(config.db_path)
    runtimes = {a.name: build_runtime(a, secrets, store) for a in config.accounts}
    for name, rt in runtimes.items():
        log.info("account %s: venue=%s mode=%s", name, rt.config.venue, rt.mode)

    analyst = Analyst(anthropic.Anthropic(api_key=secrets.anthropic_api_key), config.analyst_model)
    news = (AlpacaNews(secrets.alpaca_api_key, secrets.alpaca_secret_key)
            if secrets.alpaca_api_key and secrets.alpaca_secret_key else NoNews())

    def now() -> datetime:
        return datetime.now(timezone.utc)

    app = TradingBot(
        token=secrets.telegram_bot_token, owner_chat_id=secrets.telegram_owner_chat_id,
        config=config, runtimes=runtimes, store=store,
        cycle=CycleRunner(store, analyst, news, now), executor=Executor(store, runtimes, now), now=now,
    ).build()
    log.info("starting Telegram polling")
    app.run_polling()
