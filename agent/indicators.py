"""Pure technical indicators over daily closes. No I/O."""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class Indicators:
    symbol: str
    last_close: float
    sma20: float | None
    sma50: float | None
    momentum_14d_pct: float | None
    volatility_30d_pct: float | None  # annualised
    drawdown_pct: float | None  # from the high of the last 90 closes; <= 0
    trend: str  # "up" | "down" | "flat" | "unknown"


def _sma(closes: Sequence[float], n: int) -> float | None:
    return sum(closes[-n:]) / n if len(closes) >= n else None


def compute(symbol: str, closes: Sequence[float], periods_per_year: int) -> Indicators | None:
    if not closes:
        return None
    last = closes[-1]
    sma20, sma50 = _sma(closes, 20), _sma(closes, 50)

    momentum = None
    if len(closes) >= 15 and closes[-15] > 0:
        momentum = (last / closes[-15] - 1) * 100

    volatility = None
    window = closes[-31:]
    if len(window) == 31 and all(c > 0 for c in window):
        returns = [math.log(b / a) for a, b in zip(window, window[1:])]
        volatility = statistics.stdev(returns) * math.sqrt(periods_per_year) * 100

    high = max(closes[-90:])
    drawdown = (last / high - 1) * 100 if high > 0 else None

    if sma20 is None or sma50 is None:
        trend = "unknown"
    elif sma20 > sma50 * 1.01:
        trend = "up"
    elif sma20 < sma50 * 0.99:
        trend = "down"
    else:
        trend = "flat"

    return Indicators(symbol, last, sma20, sma50, momentum, volatility, drawdown, trend)
