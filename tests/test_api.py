"""POST /ask and GET /health, offline: the ask function is a stub or the pipeline is patched."""

import anthropic
import httpx
import pytest
from fastapi.testclient import TestClient
from qdrant_client.http.exceptions import ResponseHandlingException

from support_desk_assistant.answer.pipeline import Outcome, SystemPromptNotReady
from support_desk_assistant.answer.schema import AnswerResult
from support_desk_assistant.answer.validate import ValidationResult
from support_desk_assistant.api import app as api_app
from support_desk_assistant.api.app import create_app
from support_desk_assistant.config import Settings
from support_desk_assistant.retrieve import Hit


def hit(chunk_id: str, title: str = "Apply for EI", url: str = "https://example.test/ei") -> Hit:
    return Hit(
        score=0.6,
        program="ei",
        title=title,
        heading_path=["A"],
        url=url,
        text="t",
        chunk_id=chunk_id,
    )


OK = ValidationResult()


def outcome(result: AnswerResult, *, validation=OK, status="ok", hits=None):
    return Outcome(
        question="q",
        result=result,
        hits=hits
        if hits is not None
        else [hit("c1"), hit("c2", "Second", "https://example.test/2")],
        gate="pass",
        model="claude-haiku-5-5",
        status=status,
        validation=validation,
        cost_cad=0.0012,
        latency_ms=850,
    )


ANSWER = AnswerResult(
    abstained=False,
    abstain_reason=None,
    answer="Apply as soon as you stop working.",
    citations=["c2", "c1"],
)
ABSTAIN = AnswerResult(
    abstained=True, abstain_reason="Not covered by the documents.", answer="", citations=[]
)


class Recorder:
    def __init__(self, result: Outcome | Exception):
        self.result, self.calls = result, []

    def __call__(self, question, program, k):
        self.calls.append((question, program, k))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def client_for(result, **settings_kw) -> tuple[TestClient, Recorder]:
    rec = Recorder(result)
    return TestClient(
        create_app(Settings(**settings_kw), ask_fn=rec), raise_server_exceptions=False
    ), rec


def test_health():
    client, _ = client_for(outcome(ANSWER))
    assert client.get("/health").json() == {"status": "ok"}


def test_answer_with_numbered_citations_in_order():
    client, rec = client_for(outcome(ANSWER))
    r = client.post("/ask", json={"question": "  How soon do I apply?  ", "program": "ei", "k": 3})
    assert r.status_code == 200
    body = r.json()
    assert body["abstained"] is False
    assert body["answer"] == "Apply as soon as you stop working."
    assert body["citations"] == [
        {"n": 1, "title": "Second", "url": "https://example.test/2"},
        {"n": 2, "title": "Apply for EI", "url": "https://example.test/ei"},
    ]
    assert body["status"] == "ok" and body["model"] == "claude-haiku-5-5"
    assert body["cost_cad"] == 0.0012 and body["latency_ms"] == 850 and body["cache_hit"] is False
    assert rec.calls == [("How soon do I apply?", "ei", 3)]


def test_abstention_passes_through():
    client, _ = client_for(outcome(ABSTAIN))
    body = client.post("/ask", json={"question": "Minimum wage in Ontario?"}).json()
    assert body["abstained"] is True and body["answer"] == ""
    assert body["abstain_reason"] == "Not covered by the documents."
    assert body["citations"] == []


def test_unvalidated_answer_is_not_shown():
    bad = ValidationResult(failed_rules=("citation_not_retrieved",))
    client, _ = client_for(outcome(ANSWER, validation=bad))
    body = client.post("/ask", json={"question": "q"}).json()
    assert body["abstained"] is True and body["answer"] == "" and body["citations"] == []
    assert body["status"] == "validation_failed"
    assert "could not verify" in body["abstain_reason"]


def test_generation_failure_status_is_reported():
    failed = AnswerResult(
        abstained=True,
        abstain_reason="Answer generation did not complete (max_tokens).",
        answer="",
        citations=[],
    )
    client, _ = client_for(outcome(failed, status="max_tokens"))
    body = client.post("/ask", json={"question": "q"}).json()
    assert body["abstained"] is True and body["status"] == "max_tokens"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"question": ""},
        {"question": "x" * 1001},
        {"question": "q", "program": "tax"},
        {"question": "q", "k": 0},
        {"question": "q", "k": 21},
    ],
)
def test_bad_requests_are_422_and_never_reach_the_pipeline(payload):
    client, rec = client_for(outcome(ANSWER))
    assert client.post("/ask", json=payload).status_code == 422
    assert rec.calls == []


def test_limits_come_from_settings():
    from support_desk_assistant.config import ApiSettings

    client, rec = client_for(outcome(ANSWER), api=ApiSettings(max_question_chars=5, max_k=2))
    assert client.post("/ask", json={"question": "123456"}).status_code == 422
    assert client.post("/ask", json={"question": "12345", "k": 3}).status_code == 422
    assert client.post("/ask", json={"question": "12345", "k": 2}).status_code == 200


@pytest.mark.parametrize(
    "error, status",
    [
        (SystemPromptNotReady("prompt missing"), 503),
        (ResponseHandlingException(ConnectionError("down")), 503),
        (anthropic.APIConnectionError(request=httpx.Request("POST", "https://x.test")), 502),
    ],
)
def test_upstream_failures_map_to_http_errors(error, status):
    client, _ = client_for(error)
    r = client.post("/ask", json={"question": "q"})
    assert r.status_code == status
    assert "x.test" not in r.text and "down" not in r.text  # no upstream detail leaks


def test_default_ask_fn_builds_clients_once_and_uses_the_pipeline(monkeypatch):
    built, prepared_with = [], []
    monkeypatch.setattr(api_app, "make_voyage_client", lambda s: built.append("voyage") or "v")
    monkeypatch.setattr(api_app, "Embedder", lambda client, cfg: ("embedder", client))
    monkeypatch.setattr(api_app, "QdrantClient", lambda **kw: built.append("qdrant") or "q")

    def fake_prepare(question, **kw):
        prepared_with.append((question, kw["program"], kw["k"], kw["embedder"], kw["qdrant"]))
        return "prepared"

    monkeypatch.setattr(api_app, "prepare", fake_prepare)
    monkeypatch.setattr(api_app, "execute", lambda prepared, settings: outcome(ANSWER))

    ask = api_app.make_ask_fn(Settings())
    assert built == []  # nothing is built until the first request
    ask("q1", "ei", 4)
    ask("q2", None, None)
    assert built == ["voyage", "qdrant"]
    assert prepared_with[0] == ("q1", "ei", 4, ("embedder", "v"), "q")
    assert prepared_with[1][1:3] == (None, None)
