"""Ticket routes: ``POST /tickets`` triages and stores, ``GET /tickets/{id}``, ``GET /tickets``."""

from collections.abc import Callable
from functools import lru_cache

import anthropic
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from support_desk_assistant.answer.pipeline import make_anthropic_client
from support_desk_assistant.config import Settings
from support_desk_assistant.triage.escalate import agrees, escalate, second_opinion_settings
from support_desk_assistant.triage.pipeline import (
    STATUS_OK,
    SystemPromptNotReady,
    TriageOutcome,
    triage_ticket,
)
from support_desk_assistant.triage.schema import Priority, Queue
from support_desk_assistant.triage.store import STATUS_FAILED, STATUS_TRIAGED, Ticket, TicketStore

TriageFn = Callable[[str, str], TriageOutcome]


class TicketIn(BaseModel):
    subject: str = ""
    body: str


class TicketOut(BaseModel):
    id: int
    created_at: str
    subject: str
    body: str
    status: str  # triaged, or triage_failed (kept for a person to triage)
    queue: str | None
    priority: str | None
    rationale: str | None
    model: str
    cost_cad: float
    latency_ms: int
    queue_confidence: str | None
    priority_confidence: str | None
    needs_review: bool  # a person should check this triage before anyone acts on it
    escalation_reasons: list[str]  # why; empty when needs_review is false


def make_triage_fn(settings: Settings) -> TriageFn:
    @lru_cache(maxsize=1)
    def client() -> anthropic.Anthropic:
        return make_anthropic_client(settings)

    def run(subject: str, body: str) -> TriageOutcome:
        return triage_ticket(subject, body, settings=settings, client=client())

    return run


def make_second_opinion_fn(settings: Settings) -> TriageFn:
    """The same model asked again with thinking on (decisions.md #29)."""
    return make_triage_fn(second_opinion_settings(settings))


def _out(ticket: Ticket) -> TicketOut:
    return TicketOut(**ticket.__dict__)


def add_ticket_routes(
    app: FastAPI,
    settings: Settings,
    triage_fn: TriageFn | None = None,
    store: TicketStore | None = None,
    second_fn: TriageFn | None = None,
) -> None:
    run = triage_fn or make_triage_fn(settings)
    # The second pass is built only for the real triage function: an injected ``triage_fn``
    # (tests) runs no second pass unless a ``second_fn`` is injected too.
    second = second_fn or (make_second_opinion_fn(settings) if triage_fn is None else None)
    cfg = settings.triage

    @lru_cache(maxsize=1)
    def tickets() -> TicketStore:
        return store or TicketStore(settings.tickets_db_path)

    # Plain ``def`` handlers: FastAPI runs them in worker threads (blocking Claude and SQLite).
    @app.post("/tickets", response_model=TicketOut, status_code=201)
    def create_ticket(body: TicketIn) -> TicketOut:
        subject, text = body.subject.strip(), body.body.strip()
        if not text:
            raise HTTPException(422, "body must not be empty")
        if len(text) > cfg.max_ticket_chars:
            raise HTTPException(422, f"body is longer than {cfg.max_ticket_chars} characters")
        if len(subject) > cfg.max_subject_chars:
            raise HTTPException(422, f"subject is longer than {cfg.max_subject_chars} characters")
        try:
            outcome = run(subject, text)
            ok = outcome.status == STATUS_OK and outcome.result is not None
            second_outcome = None
            if ok and settings.escalation.on_disagreement and second is not None:
                second_outcome = second(subject, text)
        except SystemPromptNotReady as exc:
            raise HTTPException(503, str(exc)) from exc
        except anthropic.APIError as exc:
            raise HTTPException(502, "upstream model service error") from exc
        r = outcome.result
        second_agrees = None
        if second_outcome is not None and r is not None:
            second_result = second_outcome.result if second_outcome.status == STATUS_OK else None
            second_agrees = agrees(r, second_result)
        escalation = escalate(r if ok else None, settings.escalation, second_agrees)
        extra = second_outcome
        cost_cad = outcome.cost_cad + (extra.cost_cad if extra else 0.0)
        latency_ms = outcome.latency_ms + (extra.latency_ms if extra else 0)
        ticket = tickets().create(
            subject=subject,
            body=text,
            status=STATUS_TRIAGED if ok else STATUS_FAILED,
            queue=r.queue if ok and r else None,
            priority=r.priority if ok and r else None,
            rationale=r.rationale if ok and r else None,
            model=outcome.model,
            cost_cad=cost_cad,
            latency_ms=latency_ms,
            queue_confidence=r.queue_confidence if ok and r else None,
            priority_confidence=r.priority_confidence if ok and r else None,
            needs_review=escalation.needs_review,
            escalation_reasons=escalation.reasons,
        )
        return _out(ticket)

    @app.get("/tickets/{ticket_id}", response_model=TicketOut)
    def get_ticket(ticket_id: int) -> TicketOut:
        ticket = tickets().get(ticket_id)
        if ticket is None:
            raise HTTPException(404, "ticket not found")
        return _out(ticket)

    @app.get("/tickets", response_model=list[TicketOut])
    def list_tickets(
        queue: Queue | None = None,
        priority: Priority | None = None,
        needs_review: bool | None = None,
        limit: int = Query(50, ge=1),
        offset: int = Query(0, ge=0),
    ) -> list[TicketOut]:
        if limit > cfg.page_size_max:
            raise HTTPException(422, f"limit must be at most {cfg.page_size_max}")
        found = tickets().list(
            queue=queue, priority=priority, needs_review=needs_review, limit=limit, offset=offset
        )
        return [_out(t) for t in found]
