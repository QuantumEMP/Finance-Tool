from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from telegram import Chat, Message, Update, User
from telegram.ext import CommandHandler

from agent.bot import TradingBot, is_owner, is_owner_update, keyboard, owner_filter, parse_callback, parse_cash_flow
from agent.config import Schedule
from agent.models import Proposal, ProposalState, ProposedOrder, Side
from agent.store import Store

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


def test_is_owner_update():
    assert is_owner_update(123, 123, 123) is True
    assert is_owner_update(123, 456, 123) is False
    assert is_owner_update(None, 123, 123) is False
    assert is_owner_update(123, None, 123) is False


def test_keyboard_callback_data():
    row = keyboard(PID).inline_keyboard[0]
    assert [b.callback_data for b in row] == [f"approve:{PID}", f"reject:{PID}"]


OWNER = 1001
T0 = datetime(2026, 10, 7, 14, 0, tzinfo=timezone.utc)


def make_bot(store, executor):
    config = SimpleNamespace(heartbeat=Schedule(run_at="08:00", timezone="UTC"))
    return TradingBot(token="123:abc", owner_chat_id=OWNER, config=config, runtimes={},
                      store=store, cycle=None, executor=executor, now=lambda: T0)


def message_update(chat_id, user_id):
    chat = Chat(id=chat_id, type="private" if chat_id == user_id else "group")
    msg = Message(message_id=1, date=T0, chat=chat, text="/run",
                  from_user=User(id=user_id, first_name="x", is_bot=False))
    return Update(update_id=1, message=msg)


def test_owner_filter_requires_owner_user_not_just_owner_chat():
    f = owner_filter(OWNER)
    assert f.check_update(message_update(OWNER, OWNER))
    # Another member of the owner's chat (if it were a group) is ignored...
    assert not f.check_update(message_update(OWNER, 2002))
    # ...and so is the owner writing from some other chat.
    assert not f.check_update(message_update(3003, OWNER))


def test_commands_use_owner_filter(tmp_path):
    app = make_bot(Store(str(tmp_path / "db")), None).build()
    handlers = [h for h in app.handlers[0] if isinstance(h, CommandHandler)]
    assert {"run", "pause", "resume", "deposit", "withdraw"} <= {c for h in handlers for c in h.commands}
    for h in handlers:
        assert h.filters.check_update(message_update(OWNER, OWNER))
        assert not h.filters.check_update(message_update(OWNER, 2002))


class ExplodingExecutor:
    def approve(self, pid):
        raise RuntimeError("boom")

    def reject(self, pid):
        raise RuntimeError("boom")


def test_decide_reports_unexpected_errors(tmp_path):
    store = Store(str(tmp_path / "db"))
    order = ProposedOrder("test", "SPY", Side.BUY, Decimal(1), Decimal(100), Decimal(100), 0.8, "r")
    store.add_proposal(Proposal(PID, order, ProposalState.PENDING, T0, T0 + timedelta(hours=1), 1.0))
    text = make_bot(store, ExplodingExecutor())._decide("approve", PID)
    assert "Approve failed" in text and "boom" in text and "pending" in text


def test_decide_passes_outcome_through(tmp_path):
    executor = SimpleNamespace(approve=lambda pid: SimpleNamespace(message="✅ Filled"),
                               reject=lambda pid: SimpleNamespace(message="❌ Rejected."))
    bot = make_bot(Store(str(tmp_path / "db")), executor)
    assert bot._decide("approve", PID) == "✅ Filled"
    assert bot._decide("reject", PID) == "❌ Rejected."


def test_parse_cash_flow():
    accounts = ["alpaca", "luno"]
    assert parse_cash_flow(["luno", "1,500.50"], accounts) == ("luno", Decimal("1500.50"))
    assert "Unknown account" in parse_cash_flow(["x", "1"], accounts)
    assert "not a number" in parse_cash_flow(["luno", "abc"], accounts)
    for bad in (["luno", "-5"], ["luno", "0"], ["luno", "NaN"], ["luno", "Infinity"]):
        assert "positive" in parse_cash_flow(bad, accounts)
    assert "account and an amount" in parse_cash_flow(["luno"], accounts)
