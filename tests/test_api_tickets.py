"""POST/GET /tickets with a stub triage function and a temporary SQLite store."""

import anthropic
import httpx
import pytest
from fastapi.testclient import TestClient

from support_desk_assistant.api.app import create_app
from support_desk_assistant.config import EscalationSettings, Settings, TriageSettings
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
        result=TriageResult(
            rationale="Because.",
            queue=queue,
            priority=priority,
            queue_confidence="high",
            priority_confidence="high",
        ),
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


def test_an_urgent_ticket_keeps_its_confidences_and_is_not_doubtful_by_default(tmp_path):
    client, _ = make(tmp_path)
    t = client.post("/tickets", json={"body": "My EI payment stopped"}).json()
    assert (t["queue_confidence"], t["priority_confidence"]) == ("high", "high")
    assert t["priority"] == "urgent" and t["needs_review"] is False  # it tops the queue instead


def test_the_urgent_rule_can_be_switched_on(tmp_path):
    client, _ = make(tmp_path, escalation=EscalationSettings(on_urgent=True))
    t = client.post("/tickets", json={"body": "My EI payment stopped"}).json()
    assert t["needs_review"] is True and t["escalation_reasons"] == ["urgent"]


def test_a_confident_ordinary_ticket_is_left_alone(tmp_path):
    client, _ = make(tmp_path, ok("ei", "normal"))
    t = client.post("/tickets", json={"body": "How do I update my address?"}).json()
    assert t["needs_review"] is False and t["escalation_reasons"] == []


def test_low_confidence_escalates_and_the_floor_comes_from_settings(tmp_path):
    unsure = TriageOutcome(
        status=STATUS_OK,
        result=TriageResult(
            rationale="r",
            queue="ei",
            priority="normal",
            queue_confidence="medium",
            priority_confidence="low",
        ),
        model="m",
    )
    client, _ = make(tmp_path, unsure)  # shipped floors: queue below high, priority off
    t = client.post("/tickets", json={"body": "x"}).json()
    assert t["escalation_reasons"] == ["low_queue_confidence"]
    strict, _ = make(
        tmp_path / "strict",
        unsure,
        escalation=EscalationSettings(min_queue_confidence="low", min_priority_confidence="medium"),
    )
    t = strict.post("/tickets", json={"body": "x"}).json()
    assert t["escalation_reasons"] == ["low_priority_confidence"]


def test_needs_review_filter(tmp_path):
    client, stub = make(tmp_path)
    for queue, priority in [("ei", "urgent"), ("cpp", "low"), ("other", "low")]:
        stub.result = ok(queue, priority)
        client.post("/tickets", json={"body": f"{queue}-{priority}"})
    assert [t["id"] for t in client.get("/tickets?needs_review=true").json()] == [3]
    assert [t["id"] for t in client.get("/tickets?needs_review=false").json()] == [2, 1]
    assert client.get("/tickets?needs_review=maybe").status_code == 422


def second(result, cost=0.0009, latency=1200):
    def run(subject, body):
        return TriageOutcome(
            status=STATUS_OK,
            result=result,
            model="claude-haiku-5-5",
            cost_cad=cost,
            latency_ms=latency,
        )

    return run


def made(tmp_path, first, second_fn, **settings_kw):
    app = create_app(
        Settings(**settings_kw),
        ask_fn=lambda *a: None,
        triage_fn=Stub(first),
        store=TicketStore(tmp_path / "t.sqlite"),
        second_fn=second_fn,
    )
    return TestClient(app, raise_server_exceptions=False)


def test_a_disagreeing_second_pass_escalates_and_its_cost_is_added(tmp_path):
    other = ok("ei", "high").result  # the first pass says urgent
    client = made(tmp_path, ok("ei", "urgent"), second(other))
    t = client.post("/tickets", json={"body": "x"}).json()
    assert t["needs_review"] is True and t["escalation_reasons"] == ["second_opinion_differs"]
    assert t["priority"] == "urgent"  # the stored label is still the first pass
    assert t["cost_cad"] == pytest.approx(0.0004 + 0.0009) and t["latency_ms"] == 700 + 1200


def test_an_agreeing_second_pass_leaves_the_ticket_alone(tmp_path):
    first = ok("ei", "normal")
    t = made(tmp_path, first, second(first.result)).post("/tickets", json={"body": "x"}).json()
    assert t["needs_review"] is False and t["escalation_reasons"] == []


def test_a_second_pass_that_gives_nothing_counts_as_disagreement(tmp_path):
    nothing = lambda s, b: TriageOutcome(status=STATUS_REFUSAL, result=None, model="m")  # noqa: E731
    t = made(tmp_path, ok("ei", "normal"), nothing).post("/tickets", json={"body": "x"}).json()
    assert t["escalation_reasons"] == ["second_opinion_differs"]


def test_no_second_pass_runs_when_the_rule_is_off_or_triage_failed(tmp_path):
    calls = []

    def spy(subject, body):
        calls.append(body)
        return ok("ei", "high")

    off = made(
        tmp_path / "a",
        ok("ei", "normal"),
        spy,
        escalation=EscalationSettings(on_disagreement=False),
    )
    assert off.post("/tickets", json={"body": "x"}).json()["escalation_reasons"] == []
    failed = TriageOutcome(status=STATUS_REFUSAL, result=None, model="m")
    on = made(tmp_path / "b", failed, spy)
    assert on.post("/tickets", json={"body": "x"}).json()["escalation_reasons"] == ["triage_failed"]
    assert calls == []


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
    assert t["queue_confidence"] is None and t["priority_confidence"] is None
    assert t["needs_review"] is True and t["escalation_reasons"] == ["triage_failed"]
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
