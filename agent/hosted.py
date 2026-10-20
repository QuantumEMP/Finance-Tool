"""Command line for running the agent as a Claude Managed Agent (Claude Console).

In this mode the hosted Claude session is the analyst: `snapshot` prints the same data the
Telegram service sends to Claude, the session writes its signals to a JSON file, and
`trade` runs them through the usual allocator, risk guard and executor. Telegram is not
used, and every account runs in PAPER mode whatever config.yaml or the environment says.

Each session starts in a fresh container, so the SQLite database is restored from
--state-dir (a memory store mount) before every command and saved back after it.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Mapping

import httpx
from pydantic import ValidationError

from agent.analyst import AnalysisResult, AnalystOutput, _hold_all, build_prompt, validate_signals
from agent.brokers.alpaca import AlpacaBroker
from agent.brokers.luno import LunoBroker
from agent.brokers.paper import PaperBroker
from agent.config import AccountConfig, AppConfig, load_config
from agent.cycle import CycleRunner
from agent.executor import Executor
from agent.news import AlpacaNews, NoNews
from agent.reports import format_proposal, report_text, status_text
from agent.runtime import AccountRuntime, mark_all_equity, resume
from agent.store import Store

log = logging.getLogger("agent.hosted")
STATE_PART_CHARS = 90_000  # memory store files hold at most 100 KB each
MANIFEST = "manifest.json"


# --- state: the SQLite file, gzipped and base64-chunked into a directory --------

class StateError(Exception):
    pass


def save_state(db_path: Path, state_dir: Path, now: datetime) -> None:
    data = db_path.read_bytes()
    text = base64.b64encode(gzip.compress(data)).decode("ascii")
    parts = [text[i:i + STATE_PART_CHARS] for i in range(0, len(text), STATE_PART_CHARS)] or [""]
    state_dir.mkdir(parents=True, exist_ok=True)
    for i, part in enumerate(parts):
        (state_dir / f"db-{i:03d}.b64").write_text(part, encoding="ascii")
    manifest = {"parts": len(parts), "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data), "saved_at": now.isoformat()}
    (state_dir / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    for old in state_dir.glob("db-*.b64"):
        if int(old.stem.removeprefix("db-")) >= len(parts):
            old.unlink()


def load_state(state_dir: Path, db_path: Path) -> bool:
    """Restore the database. False when there is no saved state yet (first run)."""
    manifest_path = state_dir / MANIFEST
    if not manifest_path.exists():
        return False
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    try:
        text = "".join((state_dir / f"db-{i:03d}.b64").read_text(encoding="ascii")
                       for i in range(manifest["parts"]))
        data = gzip.decompress(base64.b64decode(text))
    except (OSError, ValueError, EOFError) as e:
        raise StateError(f"saved state in {state_dir} is unreadable: {e}") from e
    if hashlib.sha256(data).hexdigest() != manifest["sha256"]:
        raise StateError(f"saved state in {state_dir} does not match its checksum")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.write_bytes(data)
    return True


# --- runtimes: paper only, keys from the environment ---------------------------

def build_paper_runtime(acct: AccountConfig, env: Mapping[str, str], store: Store) -> AccountRuntime:
    def get(name: str) -> str | None:
        return (env.get(name) or "").strip() or None

    if acct.venue == "alpaca":
        key, secret = get("ALPACA_API_KEY"), get("ALPACA_SECRET_KEY")
        if not (key and secret):
            raise ValueError(f"{acct.name}: ALPACA_API_KEY and ALPACA_SECRET_KEY are required")
        broker = AlpacaBroker.from_keys(key, secret, paper=True)
    else:
        # Console secrets are substituted into request headers at egress, so the key id and
        # secret come pre-encoded as one value (base64 of "id:secret") for the Basic header.
        token = get("LUNO_BASIC_AUTH")
        if not token:
            raise ValueError(f"{acct.name}: LUNO_BASIC_AUTH is required")
        http = httpx.Client(base_url=LunoBroker.BASE_URL, headers={"Authorization": f"Basic {token}"},
                            timeout=15.0)
        market = LunoBroker("", "", acct.quote_currency, http=http)
        broker = PaperBroker(market, store, acct.name, acct.paper_starting_cash, acct.paper_fee_pct)
    if acct.mode == "live":
        log.warning("%s: mode is live in config.yaml; the Console runner trades PAPER only", acct.name)
    return AccountRuntime(acct, broker, "paper")


# --- analysts: Claude in the session decides, these only pass data in and out ---

class SnapshotTaken(Exception):
    def __init__(self, prompt: str) -> None:
        super().__init__("snapshot taken")
        self.prompt = prompt


class SnapshotAnalyst:
    """Stops the cycle at the analysis step and hands back what Claude would be sent."""

    def analyse(self, account, allowlist, indicators, headlines, portfolio) -> AnalysisResult:
        raise SnapshotTaken(build_prompt(account, allowlist, indicators, headlines, portfolio))


class FileAnalyst:
    """Reads the session's signals from a JSON file. Anything invalid becomes HOLD."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def analyse(self, account, allowlist, indicators, headlines, portfolio) -> AnalysisResult:
        try:
            raw = self._path.read_text(encoding="utf-8")
            parsed = AnalystOutput.model_validate_json(raw)
        except (OSError, ValidationError) as e:
            error = f"signals file {self._path} is invalid: {e}"
            return AnalysisResult(_hold_all(allowlist, error), "", error)
        return AnalysisResult(validate_signals(parsed.signals, allowlist), raw)


# --- commands ------------------------------------------------------------------

class App:
    def __init__(self, config: AppConfig, store: Store, runtimes: dict[str, AccountRuntime],
                 news, now: Callable[[], datetime], out: Callable[[str], None] = print) -> None:
        self.config, self.store, self.runtimes = config, store, runtimes
        self.news, self.now, self.out = news, now, out

    def _runtime(self, name: str) -> AccountRuntime:
        if name not in self.runtimes:
            raise SystemExit(f"Unknown account {name!r}. Options: {', '.join(self.runtimes)}")
        return self.runtimes[name]

    def housekeeping(self) -> None:
        executor = Executor(self.store, self.runtimes, self.now)
        lines = [f"Expired, not executed: {p.order.side.value} {p.order.symbol} ({p.order.account})"
                 for p in executor.expire_stale()]
        lines += [o.message for o in executor.reconcile()]
        mark_all_equity(self.store, self.runtimes, self.now())
        self.out("\n".join(lines) or "Housekeeping: nothing to update.")

    def snapshot(self, account: str) -> None:
        rt = self._runtime(account)
        try:
            result = CycleRunner(self.store, SnapshotAnalyst(), self.news, self.now).run(rt)
        except SnapshotTaken as taken:
            self.out(f"{account} (PAPER): ready for analysis.\n\n{taken.prompt}")
            return
        for alert in result.alerts:
            self.out(f"ALERT: {alert}")
        reason = result.skipped_reason or "see the alert above"
        self.out(f"{account}: skipped ({reason}). Do not run trade for this account today.")

    def trade(self, account: str, signals: Path, execute: bool) -> None:
        rt = self._runtime(account)
        result = CycleRunner(self.store, FileAnalyst(signals), self.news, self.now).run(rt)
        for alert in result.alerts:
            self.out(f"ALERT: {alert}")
        if result.skipped_reason:
            self.out(f"{account}: skipped ({result.skipped_reason}).")
            return
        for v in result.vetoes:
            self.out(f"Vetoed by risk limits: {v}")
        if not result.proposals:
            self.out(f"{account}: no trades proposed.")
            return
        executor = Executor(self.store, self.runtimes, self.now)
        cfg = rt.config
        for p in result.proposals:
            self.out(format_proposal(p, cfg.quote_currency, rt.mode, cfg.trading_fee_pct))
            if execute:
                self.out(f"→ {executor.approve(p.id).message}")
            else:
                self.out(f"→ pending; proposal id {p.id}")

    def decide(self, action: str, pid: str) -> None:
        executor = Executor(self.store, self.runtimes, self.now)
        self.out((executor.approve if action == "approve" else executor.reject)(pid).message)

    def status(self) -> None:
        self.out(status_text(self.store, self.runtimes) or "No accounts configured.")

    def report(self, days: int) -> None:
        self.out(report_text(self.store, list(self.runtimes), self.now(), days, self.runtimes))

    def pause(self) -> None:
        self.store.set_flag("paused", "1")
        self.out("Paused. No proposals or executions until resume.")

    def resume(self) -> None:
        reset = resume(self.store, self.now())
        self.out("Resumed." + (f" Drawdown peak reset for {', '.join(reset)}." if reset else ""))

    def cash_flow(self, account: str, amount: str, sign: int) -> None:
        self._runtime(account)
        try:
            value = Decimal(amount.replace(",", ""))
        except InvalidOperation:
            raise SystemExit(f"{amount!r} is not a number.") from None
        if not value.is_finite() or value <= 0:
            raise SystemExit("The amount must be a positive number.")
        self.store.add_cash_flow(account, self.now(), sign * value)
        self.out(f"Recorded a {'deposit' if sign > 0 else 'withdrawal'} of {value:,.2f} for {account}.")


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m agent.hosted", description=__doc__.splitlines()[0])
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--state-dir", type=Path, help="directory the database is restored from and saved to")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("housekeeping", help="expire stale proposals, settle open orders, mark equity")
    sub.add_parser("snapshot", help="print the analysis input for one account").add_argument("account")
    t = sub.add_parser("trade", help="size, risk-check and place paper orders from a signals file")
    t.add_argument("account")
    t.add_argument("signals", type=Path)
    t.add_argument("--no-execute", action="store_true", help="leave proposals pending instead")
    for name in ("approve", "reject"):
        sub.add_parser(name).add_argument("proposal_id")
    sub.add_parser("status")
    sub.add_parser("report").add_argument("days", nargs="?", type=int, default=7)
    sub.add_parser("pause")
    sub.add_parser("resume")
    for name in ("deposit", "withdraw"):
        cf = sub.add_parser(name)
        cf.add_argument("account")
        cf.add_argument("amount")
    return p.parse_args(argv)


def run(args: argparse.Namespace, app: App) -> None:
    c = args.command
    if c == "housekeeping":
        app.housekeeping()
    elif c == "snapshot":
        app.snapshot(args.account)
    elif c == "trade":
        app.trade(args.account, args.signals, execute=not args.no_execute)
    elif c in ("approve", "reject"):
        app.decide(c, args.proposal_id)
    elif c == "status":
        app.status()
    elif c == "report":
        app.report(max(1, min(args.days, 365)))
    elif c == "pause":
        app.pause()
    elif c == "resume":
        app.resume()
    else:
        app.cash_flow(args.account, args.amount, sign=1 if c == "deposit" else -1)


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> None:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    env = os.environ if env is None else env
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    def now() -> datetime:
        return datetime.now(timezone.utc)

    config = load_config(args.config)
    if args.state_dir:
        # Work on a scratch copy so a local database at config.db_path is never touched.
        db_path = Path(tempfile.mkdtemp(prefix="finance-agent-")) / "finance-agent.db"
        if not load_state(args.state_dir, db_path):
            print(f"No saved state in {args.state_dir}; starting fresh.")
    else:
        db_path = Path(config.db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
    store = Store(str(db_path))
    try:
        runtimes = {a.name: build_paper_runtime(a, env, store) for a in config.accounts}
        key, secret = env.get("ALPACA_API_KEY"), env.get("ALPACA_SECRET_KEY")
        news = AlpacaNews(key, secret) if key and secret else NoNews()
        run(args, App(config, store, runtimes, news, now))
    finally:
        store.close()
        if args.state_dir:
            save_state(db_path, args.state_dir, now())


if __name__ == "__main__":
    main()
