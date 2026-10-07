"""Configuration (config.yaml) and secrets (.env / environment)."""
from __future__ import annotations

from datetime import time
from decimal import Decimal
from pathlib import Path
from typing import Literal, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


def _float_as_text(v):
    # YAML floats → Decimal via their text form, so 1.5 stays exactly 1.5.
    return str(v) if isinstance(v, float) else v


class RiskLimits(BaseModel):
    """All *_pct values are percent numbers: 20 means 20%."""

    max_position_pct: Decimal
    max_order_pct: Decimal
    min_cash_reserve_pct: Decimal
    daily_loss_halt_pct: Decimal
    drawdown_kill_pct: Decimal
    max_orders_per_day: int
    min_confidence: float
    proposal_ttl_minutes: int
    max_drift_pct: Decimal
    limit_buffer_pct: Decimal = Decimal("0.2")

    @field_validator("*", mode="before")
    @classmethod
    def floats_as_text(cls, v):
        return _float_as_text(v)


class Schedule(BaseModel):
    run_at: time
    timezone: str
    weekdays_only: bool = False

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(f"unknown timezone {v!r}") from e
        return v


class AccountConfig(BaseModel):
    name: str
    venue: Literal["alpaca", "luno"]
    mode: Literal["paper", "live"] = "paper"
    quote_currency: str
    allowlist: list[str] = Field(min_length=1)
    news_symbols: dict[str, str] = Field(default_factory=dict)
    periods_per_year: int
    paper_starting_cash: Decimal = Decimal("10000")
    paper_fee_pct: Decimal = Decimal("0.5")
    schedule: Schedule
    risk: RiskLimits

    @field_validator("paper_starting_cash", "paper_fee_pct", mode="before")
    @classmethod
    def floats_as_text(cls, v):
        return _float_as_text(v)


class AppConfig(BaseModel):
    accounts: list[AccountConfig] = Field(min_length=1)
    heartbeat: Schedule
    analyst_model: str = "claude-opus-5-5"
    db_path: str = "data/finance-agent.db"

    @model_validator(mode="after")
    def _unique_names(self) -> "AppConfig":
        names = [a.name for a in self.accounts]
        if len(set(names)) != len(names):
            raise ValueError("account names must be unique")
        return self


class Secrets(BaseModel):
    anthropic_api_key: str
    telegram_bot_token: str
    telegram_owner_chat_id: int
    alpaca_api_key: str | None = None
    alpaca_secret_key: str | None = None
    luno_api_key_id: str | None = None
    luno_api_secret: str | None = None
    live_trading_confirmed: bool = False


def load_config(path: str | Path) -> AppConfig:
    with open(path, encoding="utf-8") as f:
        return AppConfig.model_validate(yaml.safe_load(f))


def load_secrets(env: Mapping[str, str]) -> Secrets:
    def get(name: str) -> str | None:
        value = (env.get(name) or "").strip()
        return value or None

    return Secrets(
        anthropic_api_key=get("ANTHROPIC_API_KEY"),
        telegram_bot_token=get("TELEGRAM_BOT_TOKEN"),
        telegram_owner_chat_id=get("TELEGRAM_OWNER_CHAT_ID"),
        alpaca_api_key=get("ALPACA_API_KEY"),
        alpaca_secret_key=get("ALPACA_SECRET_KEY"),
        luno_api_key_id=get("LUNO_API_KEY_ID"),
        luno_api_secret=get("LUNO_API_SECRET"),
        live_trading_confirmed=(env.get("LIVE_TRADING_CONFIRMED") or "").strip().lower() == "yes",
    )


def effective_mode(account: AccountConfig, secrets: Secrets) -> Literal["paper", "live"]:
    return "live" if account.mode == "live" and secrets.live_trading_confirmed else "paper"
