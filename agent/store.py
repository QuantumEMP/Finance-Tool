"""SQLite persistence: proposals, events, equity marks, flags, paper holdings/orders.

One connection in autocommit mode, guarded by a lock: the bot calls into the
store from worker threads (asyncio.to_thread).
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable

from agent.models import (
    OrderResult, OrderStatus, Proposal, ProposalState, ProposedOrder, Side,
)

_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"


def ts(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("naive datetime; use timezone-aware UTC")
    return dt.astimezone(timezone.utc).strftime(_FMT)


def parse_ts(s: str) -> datetime:
    return datetime.strptime(s, _FMT).replace(tzinfo=timezone.utc)


SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    id TEXT PRIMARY KEY,
    account TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty TEXT NOT NULL,
    limit_price TEXT NOT NULL,
    ref_price TEXT NOT NULL,
    confidence REAL NOT NULL,
    reasoning TEXT NOT NULL,
    pct_of_equity REAL NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    filled_qty TEXT NOT NULL DEFAULT '0',
    avg_price TEXT,
    message_id INTEGER
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, account TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS equity_marks (account TEXT NOT NULL, ts TEXT NOT NULL, equity TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS flags (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS paper_holdings (
    account TEXT NOT NULL, asset TEXT NOT NULL, qty TEXT NOT NULL, PRIMARY KEY (account, asset)
);
CREATE TABLE IF NOT EXISTS paper_orders (
    client_order_id TEXT PRIMARY KEY, account TEXT NOT NULL, status TEXT NOT NULL,
    filled_qty TEXT NOT NULL, avg_price TEXT, reason TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            self._conn.close()

    def __del__(self) -> None:
        """Ensure the database connection is closed on cleanup."""
        try:
            self.close()
        except Exception:
            pass

    def _query(self, sql: str, params: Iterable = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    def _write(self, sql: str, params: Iterable = ()) -> int:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).rowcount

    # --- proposals -------------------------------------------------------

    def add_proposal(self, p: Proposal) -> None:
        o = p.order
        self._write(
            "INSERT INTO proposals (id, account, symbol, side, qty, limit_price, ref_price,"
            " confidence, reasoning, pct_of_equity, state, created_at, expires_at, updated_at,"
            " reason, filled_qty, avg_price, message_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (p.id, o.account, o.symbol, o.side.value, str(o.qty), str(o.limit_price),
             str(o.ref_price), o.confidence, o.reasoning, p.pct_of_equity, p.state.value,
             ts(p.created_at), ts(p.expires_at), ts(p.created_at), p.reason,
             str(p.filled_qty), None if p.avg_price is None else str(p.avg_price), p.message_id),
        )

    @staticmethod
    def _to_proposal(r: sqlite3.Row) -> Proposal:
        order = ProposedOrder(
            account=r["account"], symbol=r["symbol"], side=Side(r["side"]),
            qty=Decimal(r["qty"]), limit_price=Decimal(r["limit_price"]),
            ref_price=Decimal(r["ref_price"]), confidence=r["confidence"],
            reasoning=r["reasoning"],
        )
        return Proposal(
            id=r["id"], order=order, state=ProposalState(r["state"]),
            created_at=parse_ts(r["created_at"]), expires_at=parse_ts(r["expires_at"]),
            pct_of_equity=r["pct_of_equity"], reason=r["reason"],
            filled_qty=Decimal(r["filled_qty"]),
            avg_price=None if r["avg_price"] is None else Decimal(r["avg_price"]),
            message_id=r["message_id"],
        )

    def get_proposal(self, pid: str) -> Proposal | None:
        rows = self._query("SELECT * FROM proposals WHERE id = ?", (pid,))
        return self._to_proposal(rows[0]) if rows else None

    def transition(self, pid: str, from_states: Iterable[ProposalState], to: ProposalState,
                   now: datetime, *, reason: str | None = None,
                   filled_qty: Decimal | None = None, avg_price: Decimal | None = None) -> bool:
        """Atomically move pid to `to` only if it is currently in one of `from_states`."""
        sets, params = ["state = ?", "updated_at = ?"], [to.value, ts(now)]
        if reason is not None:
            sets.append("reason = ?")
            params.append(reason)
        if filled_qty is not None:
            sets.append("filled_qty = ?")
            params.append(str(filled_qty))
        if avg_price is not None:
            sets.append("avg_price = ?")
            params.append(str(avg_price))
        states = [s.value for s in from_states]
        sql = (f"UPDATE proposals SET {', '.join(sets)} WHERE id = ?"
               f" AND state IN ({','.join('?' * len(states))})")
        return self._write(sql, (*params, pid, *states)) == 1

    def proposals_in_state(self, state: ProposalState) -> list[Proposal]:
        rows = self._query("SELECT * FROM proposals WHERE state = ? ORDER BY created_at", (state.value,))
        return [self._to_proposal(r) for r in rows]

    def proposals_since(self, since: datetime) -> list[Proposal]:
        rows = self._query("SELECT * FROM proposals WHERE created_at >= ? ORDER BY created_at", (ts(since),))
        return [self._to_proposal(r) for r in rows]

    def count_proposals(self, account: str, since: datetime, states: Iterable[ProposalState]) -> int:
        values = [s.value for s in states]
        rows = self._query(
            f"SELECT COUNT(*) AS n FROM proposals WHERE account = ? AND created_at >= ?"
            f" AND state IN ({','.join('?' * len(values))})",
            (account, ts(since), *values),
        )
        return rows[0]["n"]

    def pending_symbols(self, account: str) -> set[str]:
        rows = self._query("SELECT symbol FROM proposals WHERE account = ? AND state = ?",
                           (account, ProposalState.PENDING.value))
        return {r["symbol"] for r in rows}

    def open_symbols(self, account: str) -> set[str]:
        """Symbols with a proposal still in flight (pending, approved or submitted)."""
        states = (ProposalState.PENDING, ProposalState.APPROVED, ProposalState.SUBMITTED)
        rows = self._query(
            f"SELECT symbol FROM proposals WHERE account = ? AND state IN ({','.join('?' * len(states))})",
            (account, *(s.value for s in states)))
        return {r["symbol"] for r in rows}

    def set_message_id(self, pid: str, message_id: int) -> None:
        self._write("UPDATE proposals SET message_id = ? WHERE id = ?", (message_id, pid))

    # --- events ------------------------------------------------------------

    def record_event(self, now: datetime, account: str, kind: str, payload: dict) -> None:
        self._write("INSERT INTO events (ts, account, kind, payload) VALUES (?,?,?,?)",
                    (ts(now), account, kind, json.dumps(payload, default=str)))

    def events(self, account: str, kind: str) -> list[dict]:
        rows = self._query("SELECT payload FROM events WHERE account = ? AND kind = ? ORDER BY id",
                           (account, kind))
        return [json.loads(r["payload"]) for r in rows]

    # --- equity ------------------------------------------------------------

    def mark_equity(self, account: str, now: datetime, equity: Decimal) -> None:
        self._write("INSERT INTO equity_marks (account, ts, equity) VALUES (?,?,?)",
                    (account, ts(now), str(equity)))

    def peak_equity(self, account: str) -> Decimal | None:
        """Highest equity mark since the last peak reset (or ever, if never reset)."""
        since = self.get_flag(f"peak_reset:{account}") or ""
        rows = self._query("SELECT equity FROM equity_marks WHERE account = ? AND ts >= ?",
                           (account, since))
        return max((Decimal(r["equity"]) for r in rows), default=None)

    def reset_peak(self, account: str, now: datetime) -> None:
        """Start a new drawdown baseline: only marks from `now` on count toward the peak."""
        self.set_flag(f"peak_reset:{account}", ts(now))

    def first_equity_since(self, account: str, since: datetime) -> Decimal | None:
        rows = self._query(
            "SELECT equity FROM equity_marks WHERE account = ? AND ts >= ? ORDER BY ts LIMIT 1",
            (account, ts(since)))
        return Decimal(rows[0]["equity"]) if rows else None

    def last_equity_before(self, account: str, before: datetime) -> Decimal | None:
        rows = self._query(
            "SELECT equity FROM equity_marks WHERE account = ? AND ts < ? ORDER BY ts DESC LIMIT 1",
            (account, ts(before)))
        return Decimal(rows[0]["equity"]) if rows else None

    def equity_marks_since(self, account: str, since: datetime) -> list[tuple[datetime, Decimal]]:
        rows = self._query(
            "SELECT ts, equity FROM equity_marks WHERE account = ? AND ts >= ? ORDER BY ts",
            (account, ts(since)))
        return [(parse_ts(r["ts"]), Decimal(r["equity"])) for r in rows]

    # --- flags -------------------------------------------------------------

    def set_flag(self, key: str, value: str | None) -> None:
        if value is None:
            self._write("DELETE FROM flags WHERE key = ?", (key,))
        else:
            self._write("INSERT INTO flags (key, value) VALUES (?, ?)"
                        " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    def get_flag(self, key: str) -> str | None:
        rows = self._query("SELECT value FROM flags WHERE key = ?", (key,))
        return rows[0]["value"] if rows else None

    def flags_with_prefix(self, prefix: str) -> list[str]:
        rows = self._query("SELECT key FROM flags WHERE key LIKE ? ORDER BY key", (prefix + "%",))
        return [r["key"] for r in rows]

    # --- paper trading -----------------------------------------------------

    def paper_holdings(self, account: str) -> dict[str, Decimal]:
        rows = self._query("SELECT asset, qty FROM paper_holdings WHERE account = ?", (account,))
        return {r["asset"]: Decimal(r["qty"]) for r in rows}

    def set_paper_holding(self, account: str, asset: str, qty: Decimal) -> None:
        self._write("INSERT INTO paper_holdings (account, asset, qty) VALUES (?,?,?)"
                    " ON CONFLICT(account, asset) DO UPDATE SET qty = excluded.qty",
                    (account, asset, str(qty)))

    def add_paper_order(self, account: str, r: OrderResult) -> None:
        self._write(
            "INSERT INTO paper_orders (client_order_id, account, status, filled_qty, avg_price, reason)"
            " VALUES (?,?,?,?,?,?)",
            (r.client_order_id, account, r.status.value, str(r.filled_qty),
             None if r.avg_price is None else str(r.avg_price), r.reason))

    def get_paper_order(self, cid: str) -> OrderResult | None:
        rows = self._query("SELECT * FROM paper_orders WHERE client_order_id = ?", (cid,))
        if not rows:
            return None
        r = rows[0]
        return OrderResult(r["client_order_id"], OrderStatus(r["status"]), Decimal(r["filled_qty"]),
                           None if r["avg_price"] is None else Decimal(r["avg_price"]), r["reason"])
