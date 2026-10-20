"""SQLite ticket store. One short-lived connection per call, so it is safe from FastAPI's
worker threads. Tickets hold whatever the writer typed: see docs/decisions.md #28."""

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

STATUS_TRIAGED = "triaged"
STATUS_FAILED = "triage_failed"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL,
    queue TEXT,
    priority TEXT,
    rationale TEXT,
    model TEXT NOT NULL,
    cost_cad REAL NOT NULL,
    latency_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS tickets_queue_priority ON tickets (queue, priority);
"""


@dataclass(frozen=True)
class Ticket:
    id: int
    created_at: str
    subject: str
    body: str
    status: str
    queue: str | None
    priority: str | None
    rationale: str | None
    model: str
    cost_cad: float
    latency_ms: int


class TicketStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def create(
        self,
        *,
        subject: str,
        body: str,
        status: str,
        queue: str | None,
        priority: str | None,
        rationale: str | None,
        model: str,
        cost_cad: float,
        latency_ms: int,
    ) -> Ticket:
        created_at = datetime.now(UTC).isoformat(timespec="seconds")
        with closing(self._connect()) as conn, conn:
            cur = conn.execute(
                "INSERT INTO tickets (created_at, subject, body, status, queue, priority,"
                " rationale, model, cost_cad, latency_ms) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    created_at,
                    subject,
                    body,
                    status,
                    queue,
                    priority,
                    rationale,
                    model,
                    cost_cad,
                    latency_ms,
                ),
            )
            ticket_id = cur.lastrowid
        ticket = self.get(ticket_id) if ticket_id is not None else None
        assert ticket is not None
        return ticket

    def get(self, ticket_id: int) -> Ticket | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        return Ticket(**row) if row else None

    def list(
        self,
        *,
        queue: str | None = None,
        priority: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Ticket]:
        clauses, args = [], []
        if queue is not None:
            clauses.append("queue = ?")
            args.append(queue)
        if priority is not None:
            clauses.append("priority = ?")
            args.append(priority)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT * FROM tickets{where} ORDER BY id DESC LIMIT ? OFFSET ?",  # noqa: S608
                (*args, limit, offset),
            ).fetchall()
        return [Ticket(**r) for r in rows]
