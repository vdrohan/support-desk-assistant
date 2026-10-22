"""FastAPI app: ``POST /ask`` over the same prepare/execute path as ``sda ask``.

The answer is only returned as an answer if ``validate_answer`` passes; otherwise the
response is an abstention with status ``validation_failed`` (docs/decisions.md #27).
"""

from collections.abc import Callable
from functools import lru_cache

import anthropic
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse
from voyageai.error import VoyageError

from support_desk_assistant.answer.pipeline import (
    STATUS_GATE,
    STATUS_OK,
    Outcome,
    SystemPromptNotReady,
    execute,
    prepare,
)
from support_desk_assistant.api.tickets import TriageFn, add_ticket_routes
from support_desk_assistant.config import Settings, get_settings
from support_desk_assistant.ingest.embed import Embedder, make_voyage_client
from support_desk_assistant.triage.store import TicketStore

STATUS_VALIDATION_FAILED = "validation_failed"
VALIDATION_FAILED_REASON = "I could not verify the sources for this answer, so I am not showing it."

AskFn = Callable[[str, str | None, int | None], Outcome]


class AskRequest(BaseModel):
    question: str = Field(min_length=1)
    program: str | None = None
    k: int | None = Field(default=None, ge=1)


class CitationOut(BaseModel):
    n: int
    title: str
    url: str


class AskResponse(BaseModel):
    abstained: bool
    answer: str
    abstain_reason: str | None
    citations: list[CitationOut]
    status: str  # ok, gate, validation_failed, or a generation failure (max_tokens, refusal, ...)
    model: str
    cost_cad: float
    latency_ms: int
    cache_hit: bool
    # No usable answer: a person should take the question (docs/decisions.md #29).
    escalate: bool
    escalate_reason: str | None  # no_relevant_sources, model_abstained, validation_failed, or the
    # generation failure (max_tokens, refusal, invalid_output)


def escalation_for(abstained: bool, status: str) -> tuple[bool, str | None]:
    """Escalate whenever the response carries no answer. No model call: the signals already
    exist (the retrieval gate, the model's abstention, citation validation, generation failures)."""
    if status == STATUS_VALIDATION_FAILED:
        return True, STATUS_VALIDATION_FAILED
    if status == STATUS_GATE:
        return True, "no_relevant_sources"
    if status != STATUS_OK:
        return True, status
    if abstained:
        return True, "model_abstained"
    return False, None


def make_ask_fn(settings: Settings) -> AskFn:
    """Real clients, built on first use so the app imports without API keys."""

    @lru_cache(maxsize=1)
    def clients() -> tuple[Embedder, QdrantClient]:
        embedder = Embedder(make_voyage_client(settings), settings.embed)
        qdrant = QdrantClient(url=settings.qdrant_url, timeout=int(settings.index.timeout_s))
        return embedder, qdrant

    def ask(question: str, program: str | None, k: int | None) -> Outcome:
        embedder, qdrant = clients()
        prepared = prepare(
            question, settings=settings, embedder=embedder, qdrant=qdrant, program=program, k=k
        )
        return execute(prepared, settings=settings)

    return ask


def to_response(outcome: Outcome) -> AskResponse:
    result = outcome.result
    status = outcome.status
    if outcome.validation is not None and not outcome.validation.ok:
        # Never show an answer whose citations did not check out.
        return AskResponse(
            abstained=True,
            answer="",
            abstain_reason=VALIDATION_FAILED_REASON,
            citations=[],
            status=STATUS_VALIDATION_FAILED,
            model=outcome.model,
            cost_cad=outcome.cost_cad,
            latency_ms=outcome.latency_ms,
            cache_hit=outcome.cache_hit,
            escalate=True,
            escalate_reason=STATUS_VALIDATION_FAILED,
        )
    escalate, reason = escalation_for(result.abstained, status)
    by_id = {h.chunk_id: h for h in outcome.hits}
    citations = [
        CitationOut(n=n, title=by_id[cid].title, url=by_id[cid].url)
        for n, cid in enumerate(result.citations, 1)
        if cid in by_id
    ]
    return AskResponse(
        abstained=result.abstained,
        answer=result.answer,
        abstain_reason=result.abstain_reason,
        citations=citations,
        status=status,
        model=outcome.model,
        cost_cad=outcome.cost_cad,
        latency_ms=outcome.latency_ms,
        cache_hit=outcome.cache_hit,
        escalate=escalate,
        escalate_reason=reason,
    )


def create_app(
    settings: Settings | None = None,
    ask_fn: AskFn | None = None,
    triage_fn: TriageFn | None = None,
    store: TicketStore | None = None,
    second_fn: TriageFn | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    ask = ask_fn or make_ask_fn(settings)
    app = FastAPI(title="support-desk-assistant", version="0.1.0")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # A plain ``def``: FastAPI runs it in a worker thread, so the blocking Voyage, Qdrant and
    # Anthropic calls do not stall the event loop.
    @app.post("/ask", response_model=AskResponse)
    def ask_endpoint(body: AskRequest) -> AskResponse:
        api = settings.api
        if len(body.question) > api.max_question_chars:
            raise HTTPException(422, f"question is longer than {api.max_question_chars} characters")
        if body.program is not None and body.program not in settings.crawl.seeds:
            raise HTTPException(422, f"program must be one of {sorted(settings.crawl.seeds)}")
        if body.k is not None and body.k > api.max_k:
            raise HTTPException(422, f"k must be at most {api.max_k}")
        try:
            return to_response(ask(body.question.strip(), body.program, body.k))
        except SystemPromptNotReady as exc:
            raise HTTPException(503, str(exc)) from exc
        except (ResponseHandlingException, UnexpectedResponse) as exc:
            raise HTTPException(503, "search index unavailable") from exc
        except (anthropic.APIError, VoyageError) as exc:
            raise HTTPException(502, "upstream model service error") from exc

    add_ticket_routes(app, settings, triage_fn=triage_fn, store=store, second_fn=second_fn)
    return app
