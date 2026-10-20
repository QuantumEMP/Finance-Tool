import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent.brokers.paper import PaperBroker
from agent.config import load_config
from agent.hosted import App, StateError, build_paper_runtime, load_state, parse_args, save_state
from agent.models import ProposalState
from agent.runtime import AccountRuntime
from agent.store import Store
from tests.fakes import Clock, FakeBroker, FakeNews, make_account

T0 = datetime(2026, 10, 7, 14, 0, tzinfo=timezone.utc)
CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"


@pytest.fixture
def app(tmp_path):
    store = Store(str(tmp_path / "db.sqlite"))
    broker = FakeBroker(prices={"SPY": 100, "QQQ": 200})
    rt = AccountRuntime(make_account(), broker, "paper")
    out: list[str] = []
    return App(load_config(CONFIG), store, {"test": rt}, FakeNews(), Clock(T0), out.append), broker, out


def write_signals(tmp_path, signals) -> Path:
    path = tmp_path / "signals.json"
    path.write_text(json.dumps({"signals": signals}))
    return path


def test_snapshot_prints_analysis_input_without_proposing(app):
    a, broker, out = app
    a.snapshot("test")
    assert "ready for analysis" in out[0] and '"allowed_symbols"' in out[0]
    assert a.store.proposals_since(T0) == [] and broker.placed == []


def test_snapshot_reports_skip(app):
    a, broker, out = app
    broker.market_open = False
    a.snapshot("test")
    assert "skipped (market closed)" in out[-1]


def test_trade_places_paper_orders_from_signals(app, tmp_path):
    a, broker, out = app
    path = write_signals(tmp_path, [{"symbol": "SPY", "signal": "BUY", "confidence": 0.8,
                                      "reasoning": "uptrend"}])
    a.trade("test", path, execute=True)
    [p] = a.store.proposals_since(T0)
    assert p.state is ProposalState.FILLED and len(broker.placed) == 1
    assert any(line.startswith("→ ") for line in out)


def test_trade_without_execute_leaves_pending(app, tmp_path):
    a, broker, _ = app
    path = write_signals(tmp_path, [{"symbol": "SPY", "signal": "BUY", "confidence": 0.8,
                                      "reasoning": "uptrend"}])
    a.trade("test", path, execute=False)
    [p] = a.store.proposals_since(T0)
    assert p.state is ProposalState.PENDING and broker.placed == []


def test_invalid_signals_file_holds_everything(app, tmp_path):
    a, broker, out = app
    path = tmp_path / "signals.json"
    path.write_text("not json")
    a.trade("test", path, execute=True)
    assert broker.placed == [] and any("invalid" in line for line in out)


def test_state_round_trip(tmp_path):
    db = tmp_path / "a.db"
    store = Store(str(db))
    store.set_flag("paused", "1")
    store.close()
    state = tmp_path / "state"
    save_state(db, state, T0)
    restored = tmp_path / "b.db"
    assert load_state(state, restored)
    assert Store(str(restored)).get_flag("paused") == "1"


def test_state_is_chunked_and_old_parts_removed(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.hosted.STATE_PART_CHARS", 10)
    db = tmp_path / "a.db"
    Store(str(db)).close()
    state = tmp_path / "state"
    (state).mkdir()
    (state / "db-999.b64").write_text("stale")
    save_state(db, state, T0)
    manifest = json.loads((state / "manifest.json").read_text())
    assert manifest["parts"] > 1 and not (state / "db-999.b64").exists()
    assert load_state(state, tmp_path / "b.db")


def test_missing_state_is_a_fresh_start(tmp_path):
    assert load_state(tmp_path / "nothing", tmp_path / "b.db") is False


def test_corrupt_state_refuses_to_load(tmp_path):
    db = tmp_path / "a.db"
    Store(str(db)).close()
    state = tmp_path / "state"
    save_state(db, state, T0)
    (state / "db-000.b64").write_text("AAAA")
    with pytest.raises(StateError):
        load_state(state, tmp_path / "b.db")


def test_runtimes_are_paper_even_when_config_says_live(tmp_path):
    cfg = load_config(CONFIG)
    luno = cfg.accounts[1].model_copy(update={"mode": "live"})
    env = {"LUNO_BASIC_AUTH": "abc", "LIVE_TRADING_CONFIRMED": "yes"}
    rt = build_paper_runtime(luno, env, Store(str(tmp_path / "db")))
    assert rt.mode == "paper" and isinstance(rt.broker, PaperBroker)


def test_missing_keys_raise(tmp_path):
    cfg = load_config(CONFIG)
    with pytest.raises(ValueError, match="ALPACA_API_KEY"):
        build_paper_runtime(cfg.accounts[0], {}, Store(str(tmp_path / "db")))
    with pytest.raises(ValueError, match="LUNO_BASIC_AUTH"):
        build_paper_runtime(cfg.accounts[1], {}, Store(str(tmp_path / "db")))


def test_parse_args():
    args = parse_args(["--state-dir", "/mnt/memory/s", "trade", "luno", "s.json", "--no-execute"])
    assert (args.command, args.account, args.no_execute) == ("trade", "luno", True)
    assert args.state_dir == Path("/mnt/memory/s")


def test_main_restores_and_saves_state_between_runs(tmp_path, monkeypatch, capsys):
    from agent import hosted

    cfg = tmp_path / "config.yaml"
    text = CONFIG.read_text().replace("data/finance-agent.db", str(tmp_path / "work" / "db.sqlite"))
    cfg.write_text(text)
    class FakeLuno(FakeBroker):
        BASE_URL = "https://api.luno.com"

        def __init__(self, *a, **k):
            super().__init__(name="luno", prices={"XBTZAR": 1000, "ETHZAR": 50})

    monkeypatch.setattr(hosted, "LunoBroker", FakeLuno)
    monkeypatch.setattr(hosted.AlpacaBroker, "from_keys",
                        classmethod(lambda cls, *a, **k: FakeBroker(prices={"SPY": 1})))
    env = {"ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s", "LUNO_BASIC_AUTH": "t"}
    monkeypatch.setattr(hosted, "AlpacaNews", lambda *a: FakeNews())
    state = tmp_path / "state"
    hosted.main(["--config", str(cfg), "--state-dir", str(state), "pause"], env)
    assert "starting fresh" in capsys.readouterr().out
    hosted.main(["--config", str(cfg), "--state-dir", str(state), "status"], env)
    out = capsys.readouterr().out
    assert "starting fresh" not in out and "PAUSED" in out
