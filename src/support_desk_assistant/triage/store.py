"""SQLite ticket store. One short-lived connection per call, so it is safe from FastAPI's
worker threads. Tickets hold whatever the writer typed: see docs/decisions.md #28."""

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from support_desk_assistant.triage.escalate import REASON_FAILED

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
    latency_ms INTEGER NOT NULL,
    queue_confidence TEXT,
    priority_confidence TEXT,
    needs_review INTEGER NOT NULL DEFAULT 0,
    escalation_reasons TEXT NOT NULL DEFAULT '',
    reviewed_at TEXT,
    final_queue TEXT,
    final_priority TEXT,
    review_note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS tickets_queue_priority ON tickets (queue, priority);
"""

# Columns added by decision 29; a database created before them gets them from _migrate.
_ESCALATION_COLUMNS = {
    "queue_confidence": "TEXT",
    "priority_confidence": "TEXT",
    "needs_review": "INTEGER NOT NULL DEFAULT 0",
    "escalation_reasons": "TEXT NOT NULL DEFAULT ''",
}

# Columns added by decision 30 (a person's override); the machine's queue and priority stay as
# they were, so the two can be compared.
_REVIEW_COLUMNS = {
    "reviewed_at": "TEXT",
    "final_queue": "TEXT",
    "final_priority": "TEXT",
    "review_note": "TEXT NOT NULL DEFAULT ''",
}


def _migrate(conn: sqlite3.Connection) -> None:
    have = {r["name"] for r in conn.execute("PRAGMA table_info(tickets)")}
    for name, ddl in {**_ESCALATION_COLUMNS, **_REVIEW_COLUMNS}.items():
        if name not in have:
            conn.execute(f"ALTER TABLE tickets ADD COLUMN {name} {ddl}")  # noqa: S608
    if "needs_review" not in have:
        # A ticket whose triage failed before escalation existed still needs a person.
        conn.execute(
            "UPDATE tickets SET needs_review = 1, escalation_reasons = ? WHERE status = ?",
            (REASON_FAILED, STATUS_FAILED),
        )
    conn.execute("CREATE INDEX IF NOT EXISTS tickets_needs_review ON tickets (needs_review)")


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
    queue_confidence: str | None = None
    priority_confidence: str | None = None
    needs_review: bool = False
    escalation_reasons: tuple[str, ...] = ()
    reviewed_at: str | None = None
    final_queue: str | None = None
    final_priority: str | None = None
    review_note: str = ""


def _ticket(row: sqlite3.Row) -> Ticket:
    data = dict(row)
    data["needs_review"] = bool(data["needs_review"])
    data["escalation_reasons"] = tuple(r for r in data["escalation_reasons"].split(",") if r)
    return Ticket(**data)


class TicketStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            _migrate(conn)

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
        queue_confidence: str | None = None,
        priority_confidence: str | None = None,
        needs_review: bool = False,
        escalation_reasons: tuple[str, ...] = (),
    ) -> Ticket:
        created_at = datetime.now(UTC).isoformat(timespec="seconds")
        with closing(self._connect()) as conn, conn:
            cur = conn.execute(
                "INSERT INTO tickets (created_at, subject, body, status, queue, priority,"
                " rationale, model, cost_cad, latency_ms, queue_confidence, priority_confidence,"
                " needs_review, escalation_reasons) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                    queue_confidence,
                    priority_confidence,
                    int(needs_review),
                    ",".join(escalation_reasons),
                ),
            )
            ticket_id = cur.lastrowid
        ticket = self.get(ticket_id) if ticket_id is not None else None
        assert ticket is not None
        return ticket

    def get(self, ticket_id: int) -> Ticket | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        return _ticket(row) if row else None

    def review(
        self, ticket_id: int, *, final_queue: str, final_priority: str, note: str
    ) -> Ticket | None:
        """Record a person's decision. Reviewing again replaces the earlier decision."""
        reviewed_at = datetime.now(UTC).isoformat(timespec="seconds")
        with closing(self._connect()) as conn, conn:
            cur = conn.execute(
                "UPDATE tickets SET reviewed_at = ?, final_queue = ?, final_priority = ?,"
                " review_note = ? WHERE id = ?",
                (reviewed_at, final_queue, final_priority, note, ticket_id),
            )
            if cur.rowcount == 0:
                return None
        return self.get(ticket_id)

    def list(
        self,
        *,
        queue: str | None = None,
        priority: str | None = None,
        needs_review: bool | None = None,
        reviewed: bool | None = None,
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
        if needs_review is not None:
            clauses.append("needs_review = ?")
            args.append(int(needs_review))
        if reviewed is not None:
            clauses.append("reviewed_at IS NOT NULL" if reviewed else "reviewed_at IS NULL")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT * FROM tickets{where} ORDER BY id DESC LIMIT ? OFFSET ?",  # noqa: S608
                (*args, limit, offset),
            ).fetchall()
        return [_ticket(r) for r in rows]
