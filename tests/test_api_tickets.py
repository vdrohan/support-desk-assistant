"""POST/GET /tickets with a stub triage function and a temporary SQLite store."""

import anthropic
import httpx
import pytest
from fastapi.testclient import TestClient

from support_desk_assistant.api.app import create_app
from support_desk_assistant.config import Settings, TriageSettings
from support_desk_assistant.triage.pipeline import (
    STATUS_OK,
    STATUS_REFUSAL,
    SystemPromptNotReady,
    TriageOutcome,
)
from support_desk_assistant.triage.schema import TriageResult
from support_desk_assistant.triage.store import TicketStore


def ok(queue="ei", priority="urgent"):
    return TriageOutcome(
        status=STATUS_OK,
        result=TriageResult(rationale="Because.", queue=queue, priority=priority),
        model="claude-haiku-5-5",
        cost_cad=0.0004,
        latency_ms=700,
    )


class Stub:
    def __init__(self, result):
        self.result, self.calls = result, []

    def __call__(self, subject, body):
        self.calls.append((subject, body))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def make(tmp_path, result=None, **settings_kw):
    stub = Stub(result or ok())
    app = create_app(
        Settings(**settings_kw),
        ask_fn=lambda *a: None,
        triage_fn=stub,
        store=TicketStore(tmp_path / "t.sqlite"),
    )
    return TestClient(app, raise_server_exceptions=False), stub


def test_create_then_get(tmp_path):
    client, stub = make(tmp_path)
    r = client.post("/tickets", json={"subject": " Help ", "body": " My EI payment stopped "})
    assert r.status_code == 201
    t = r.json()
    assert t["id"] == 1 and t["status"] == "triaged"
    assert (t["queue"], t["priority"], t["rationale"]) == ("ei", "urgent", "Because.")
    assert (t["subject"], t["body"]) == ("Help", "My EI payment stopped")  # stripped
    assert stub.calls == [("Help", "My EI payment stopped")]
    assert client.get("/tickets/1").json() == t


def test_subject_is_optional(tmp_path):
    client, stub = make(tmp_path)
    assert client.post("/tickets", json={"body": "hello"}).status_code == 201
    assert stub.calls == [("", "hello")]


def test_failed_triage_is_kept_for_a_person(tmp_path):
    failed = TriageOutcome(status=STATUS_REFUSAL, result=None, model="m")
    client, _ = make(tmp_path, failed)
    t = client.post("/tickets", json={"body": "x"}).json()
    assert t["status"] == "triage_failed"
    assert t["queue"] is None and t["priority"] is None and t["rationale"] is None
    assert client.get(f"/tickets/{t['id']}").json()["status"] == "triage_failed"


def test_list_filters_and_paging(tmp_path):
    client, stub = make(tmp_path)
    for queue, priority in [("ei", "urgent"), ("cpp", "low"), ("ei", "low")]:
        stub.result = ok(queue, priority)
        client.post("/tickets", json={"body": f"{queue}-{priority}"})
    assert [t["id"] for t in client.get("/tickets").json()] == [3, 2, 1]
    assert [t["id"] for t in client.get("/tickets?queue=ei").json()] == [3, 1]
    assert [t["id"] for t in client.get("/tickets?queue=ei&priority=low").json()] == [3]
    assert [t["id"] for t in client.get("/tickets?limit=1&offset=1").json()] == [2]


@pytest.mark.parametrize(
    "payload",
    [{}, {"body": ""}, {"body": "   "}, {"body": "x" * 4001}, {"body": "b", "subject": "s" * 201}],
)
def test_bad_tickets_are_422_and_not_triaged(tmp_path, payload):
    client, stub = make(tmp_path)
    assert client.post("/tickets", json=payload).status_code == 422
    assert stub.calls == [] and client.get("/tickets").json() == []


@pytest.mark.parametrize(
    "query", ["queue=tax", "priority=asap", "limit=0", "limit=101", "offset=-1"]
)
def test_bad_list_queries_are_422(tmp_path, query):
    client, _ = make(tmp_path)
    assert client.get(f"/tickets?{query}").status_code == 422


def test_unknown_ticket_is_404(tmp_path):
    client, _ = make(tmp_path)
    assert client.get("/tickets/7").status_code == 404
    assert client.get("/tickets/abc").status_code == 422


def test_limits_come_from_settings(tmp_path):
    client, _ = make(tmp_path, triage=TriageSettings(max_ticket_chars=5, page_size_max=2))
    assert client.post("/tickets", json={"body": "123456"}).status_code == 422
    assert client.post("/tickets", json={"body": "12345"}).status_code == 201
    assert client.get("/tickets?limit=3").status_code == 422


@pytest.mark.parametrize(
    "error, status",
    [
        (SystemPromptNotReady("missing"), 503),
        (anthropic.APIConnectionError(request=httpx.Request("POST", "https://x.test")), 502),
    ],
)
def test_upstream_failures_store_nothing(tmp_path, error, status):
    client, _ = make(tmp_path, error)
    r = client.post("/tickets", json={"body": "x"})
    assert r.status_code == status and "x.test" not in r.text
    assert client.get("/tickets").json() == []
