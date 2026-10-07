import pytest

from agent.retry import with_retry


def test_retries_then_succeeds():
    calls, sleeps = [], []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("blip")
        return "ok"

    assert with_retry(flaky, sleep=sleeps.append) == "ok"
    assert sleeps == [1.0, 2.0]


def test_gives_up_after_attempts():
    sleeps = []
    with pytest.raises(RuntimeError, match="down"):
        with_retry(lambda: (_ for _ in ()).throw(RuntimeError("down")), sleep=sleeps.append)
    assert sleeps == [1.0, 2.0]
