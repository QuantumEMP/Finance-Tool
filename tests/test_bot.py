from agent.bot import is_owner, keyboard, parse_callback

PID = "a" * 32


def test_parse_callback_valid():
    assert parse_callback(f"approve:{PID}") == ("approve", PID)
    assert parse_callback(f"reject:{PID}") == ("reject", PID)


def test_parse_callback_rejects_everything_else():
    for bad in [None, "", "approve:xyz", f"delete:{PID}", f"approve:{PID.upper()}",
                f"approve:{PID}x", f" approve:{PID}"]:
        assert parse_callback(bad) is None


def test_is_owner():
    assert is_owner(123, 123) is True
    assert is_owner(456, 123) is False
    assert is_owner(None, 123) is False


def test_keyboard_callback_data():
    row = keyboard(PID).inline_keyboard[0]
    assert [b.callback_data for b in row] == [f"approve:{PID}", f"reject:{PID}"]
