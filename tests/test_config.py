from datetime import time
from decimal import Decimal
from pathlib import Path

import pytest

from agent.config import effective_mode, load_config, load_secrets

CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"
ENV = {"ANTHROPIC_API_KEY": "a", "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_OWNER_CHAT_ID": "123"}


def test_repo_config_loads_with_spec_defaults():
    cfg = load_config(CONFIG)
    assert [a.name for a in cfg.accounts] == ["alpaca", "luno"]
    alpaca, luno = cfg.accounts
    assert all(a.mode == "paper" for a in cfg.accounts)
    assert alpaca.schedule.run_at == time(10, 0)
    assert alpaca.schedule.weekdays_only is True
    assert alpaca.risk.max_position_pct == Decimal("20")
    assert luno.risk.max_position_pct == Decimal("10")
    assert luno.risk.max_drift_pct == Decimal("1.5")
    assert luno.paper_fee_pct == Decimal("0.6")
    assert cfg.analyst_model == "claude-opus-5-5"


def _write(tmp_path, text):
    p = tmp_path / "c.yaml"
    p.write_text(text)
    return p


def test_duplicate_account_names_rejected(tmp_path):
    text = CONFIG.read_text().replace("name: luno", "name: alpaca")
    with pytest.raises(ValueError, match="unique"):
        load_config(_write(tmp_path, text))


def test_unknown_timezone_rejected(tmp_path):
    text = CONFIG.read_text().replace("America/New_York", "Mars/Olympus")
    with pytest.raises(ValueError, match="timezone"):
        load_config(_write(tmp_path, text))


def test_secrets_parse_owner_id_and_default_not_live():
    s = load_secrets(ENV)
    assert s.telegram_owner_chat_id == 123
    assert s.live_trading_confirmed is False
    assert s.luno_api_key_id is None


def test_missing_required_secret_raises():
    with pytest.raises(ValueError):
        load_secrets({})


@pytest.mark.parametrize(
    "value,expected",
    [("yes", True), (" YES ", True), ("true", False), ("1", False), ("", False), ("y", False)],
)
def test_live_confirmation_requires_exact_yes(value, expected):
    assert load_secrets({**ENV, "LIVE_TRADING_CONFIRMED": value}).live_trading_confirmed is expected


def test_effective_mode_needs_both_switches():
    acct = load_config(CONFIG).accounts[1]
    live_acct = acct.model_copy(update={"mode": "live"})
    confirmed = load_secrets({**ENV, "LIVE_TRADING_CONFIRMED": "yes"})
    unconfirmed = load_secrets(ENV)
    assert effective_mode(live_acct, unconfirmed) == "paper"
    assert effective_mode(acct, confirmed) == "paper"
    assert effective_mode(live_acct, confirmed) == "live"
