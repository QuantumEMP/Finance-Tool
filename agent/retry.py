"""Retry with exponential backoff. For READS only: never wrap order placement."""
from __future__ import annotations

import time
from typing import Callable, TypeVar

T = TypeVar("T")


def with_retry(fn: Callable[[], T], attempts: int = 3, base_delay: float = 1.0,
               sleep: Callable[[float], None] = time.sleep) -> T:
    for attempt in range(attempts):
        try:
            return fn()
        except Exception:
            if attempt == attempts - 1:
                raise
            sleep(base_delay * 2 ** attempt)
    raise AssertionError("unreachable")
