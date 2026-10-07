from pathlib import Path

import pytest

from agent.brokers.luno import LunoBroker
from agent.brokers.paper import PaperBroker
from agent.config import load_config, load_secrets
from agent.main import build_runtime
from agent.store import Store

CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"
BASE = {"ANTHROPIC_API_KEY": "a", "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_OWNER_CHAT_ID": "1",
        "LUNO_API_KEY_ID": "id", "LUNO_API_SECRET": "s"}


@pytest.fixture
def luno():
    return load_config(CONFIG).accounts[1]


def test_luno_paper_wraps_in_paper_broker(tmp_path, luno):
    rt = build_runtime(luno, load_secrets(BASE), Store(str(tmp_path / "db")))
    assert rt.mode == "paper" and isinstance(rt.broker, PaperBroker)


def test_luno_live_without_confirmation_stays_paper(tmp_path, luno):
    rt = build_runtime(luno.model_copy(update={"mode": "live"}), load_secrets(BASE),
                       Store(str(tmp_path / "db")))
    assert rt.mode == "paper" and isinstance(rt.broker, PaperBroker)


def test_luno_live_with_confirmation_is_live(tmp_path, luno):
    secrets = load_secrets({**BASE, "LIVE_TRADING_CONFIRMED": "yes"})
    rt = build_runtime(luno.model_copy(update={"mode": "live"}), secrets, Store(str(tmp_path / "db")))
    assert rt.mode == "live" and isinstance(rt.broker, LunoBroker)


def test_missing_venue_keys_raise(tmp_path):
    alpaca = load_config(CONFIG).accounts[0]
    with pytest.raises(ValueError, match="ALPACA_API_KEY"):
        build_runtime(alpaca, load_secrets(BASE), Store(str(tmp_path / "db")))
