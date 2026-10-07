import pytest

from agent.indicators import compute


def test_empty_series_returns_none():
    assert compute("SPY", [], 252) is None


def test_constant_series():
    ind = compute("SPY", [100.0] * 60, 252)
    assert ind.last_close == 100.0
    assert ind.sma20 == 100.0 and ind.sma50 == 100.0
    assert ind.momentum_14d_pct == 0.0
    assert ind.volatility_30d_pct == 0.0
    assert ind.drawdown_pct == 0.0
    assert ind.trend == "flat"


def test_rising_series_is_uptrend():
    ind = compute("SPY", [float(i) for i in range(1, 61)], 252)
    assert ind.sma20 == pytest.approx(50.5)
    assert ind.sma50 == pytest.approx(35.5)
    assert ind.momentum_14d_pct == pytest.approx((60 / 46 - 1) * 100)
    assert ind.trend == "up"


def test_falling_series_is_downtrend():
    assert compute("SPY", [float(i) for i in range(60, 0, -1)], 252).trend == "down"


def test_drawdown_from_high():
    ind = compute("SPY", [100.0] * 10 + [50.0], 252)
    assert ind.drawdown_pct == pytest.approx(-50.0)


def test_short_history_gives_partial_indicators():
    ind = compute("NEW", [10.0, 11.0, 12.0], 365)
    assert ind.last_close == 12.0
    assert ind.sma20 is None and ind.sma50 is None
    assert ind.momentum_14d_pct is None and ind.volatility_30d_pct is None
    assert ind.trend == "unknown"


def test_non_positive_prices_do_not_crash():
    ind = compute("BAD", [0.0] * 40, 252)
    assert ind.volatility_30d_pct is None
    assert ind.momentum_14d_pct is None
    assert ind.drawdown_pct is None
