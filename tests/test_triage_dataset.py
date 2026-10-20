"""Triage ticket drafting and review, offline: fake Anthropic client, temporary files."""

import json
from types import SimpleNamespace

import pytest

from support_desk_assistant.config import Settings
from support_desk_assistant.evals import triage_draft as td
from support_desk_assistant.evals import triage_review as tr
from support_desk_assistant.triage.schema import PRIORITIES, QUEUES

TITLES = {q: [f"{q} page {i}" for i in range(10)] for q in QUEUES if q != "other"}


@pytest.fixture
def settings(tmp_path):
    prompt = tmp_path / "draft.md"
    prompt.write_text("You write tickets.\n")
    s = Settings(data_dir=tmp_path / "data")
    s.triage_eval.draft_prompt_path = prompt
    return s


def reply(n, tag="t"):
    tickets = [
        {"subject": f"s{tag}{i}", "body": f"body {tag}{i}", "notes": "why"} for i in range(n)
    ]
    return {
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": json.dumps({"tickets": tickets})}],
        "usage": {"input_tokens": 500, "output_tokens": 300},
    }


class FakeClient:
    def __init__(self, shortfall=0):
        self.requests, self.shortfall = [], shortfall
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **request):
        self.requests.append(request)
        n = int(request["messages"][0]["content"].split("count: ")[1].split("\n")[0])
        return SimpleNamespace(to_dict=lambda: reply(n - self.shortfall, str(len(self.requests))))


def test_plan_covers_every_cell_with_the_configured_counts(settings):
    tasks = td.plan_tasks(settings, TITLES)
    assert len(tasks) == len(QUEUES) * len(PRIORITIES)
    assert {(t.queue, t.priority) for t in tasks} == {(q, p) for q in QUEUES for p in PRIORITIES}
    assert sum(len(t.styles) for t in tasks) == 5 * 4 * 3 + 4 * 2
    for t in tasks:
        assert len(t.styles) == (2 if t.queue == "other" else 3)
        assert set(t.styles) <= set(td.STYLES)
        assert len(t.hints) == 6
    assert {s for t in tasks for s in t.styles} == set(td.STYLES)


def test_plan_is_deterministic_and_uses_program_titles(settings):
    a, b = td.plan_tasks(settings, TITLES), td.plan_tasks(settings, TITLES)
    assert a == b
    ei = next(t for t in a if t.queue == "ei" and t.priority == "urgent")
    assert all(h.startswith("ei page") for h in ei.hints)
    other = next(t for t in a if t.queue == "other")
    assert set(other.hints) <= set(settings.triage_eval.other_topics)


def test_user_message_names_queue_priority_count_and_styles(settings):
    task = td.Task("cpp", "high", ("plain", "short"), ("A", "B"))
    msg = td.render_user_message(task)
    assert "queue: cpp" in msg and "priority: high" in msg and "count: 2" in msg
    assert "1. plain" in msg and "2. short" in msg and "- A" in msg


def test_run_tasks_assigns_ids_labels_and_styles_and_resumes_from_cache(settings):
    tasks = td.plan_tasks(settings, TITLES)[:3]
    system = "SYS"
    client = FakeClient()
    run = td.run_tasks(tasks, system=system, settings=settings, client=client)
    assert run.calls == 3 and run.cache_hits == 0 and not run.failures and not run.shortfalls
    assert [c.id for c in run.candidates] == [f"tc-{i:03d}" for i in range(1, 10)]
    assert {(c.queue, c.priority) for c in run.candidates} == {(t.queue, t.priority) for t in tasks}
    again = td.run_tasks(tasks, system=system, settings=settings, client=client)
    assert again.calls == 0 and again.cache_hits == 3 and len(client.requests) == 3
    assert [c.model_dump() for c in again.candidates] == [c.model_dump() for c in run.candidates]


def test_shortfall_is_reported(settings):
    tasks = td.plan_tasks(settings, TITLES)[:1]
    run = td.run_tasks(tasks, system="S", settings=settings, client=FakeClient(shortfall=1))
    assert len(run.candidates) == 2 and run.shortfalls == ["ei/urgent: asked for 3, got 2"]


def test_bad_output_is_a_failure_not_a_crash(settings):
    bad = SimpleNamespace(to_dict=lambda: {"stop_reason": "refusal", "content": []})
    client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: bad))
    run = td.run_tasks(
        td.plan_tasks(settings, TITLES)[:1], system="S", settings=settings, client=client
    )
    assert run.candidates == [] and run.failures == ["ei/urgent: refusal"]


def test_spend_cap_stops_before_the_call(settings):
    client = FakeClient()
    run = td.run_tasks(
        td.plan_tasks(settings, TITLES)[:3], system="S", settings=settings, client=client, cap_cad=0
    )
    assert client.requests == [] and run.candidates == []
    assert "spend cap" in run.failures[0]


def test_candidates_roundtrip(settings):
    run = td.run_tasks(
        td.plan_tasks(settings, TITLES)[:1], system="S", settings=settings, client=FakeClient()
    )
    td.write_candidates(settings.triage_candidates_path, run.candidates)
    assert td.load_candidates(settings.triage_candidates_path) == run.candidates
    assert td.load_candidates(settings.triage_candidates_path.with_name("none.jsonl")) == []


def candidates(settings, n=3):
    run = td.run_tasks(
        td.plan_tasks(settings, TITLES)[:1], system="S", settings=settings, client=FakeClient()
    )
    return run.candidates[:n]


def test_parse_decisions():
    text = (
        "# note\ntc-001 a ei urgent\ntc-002 r\n\ntc-003 accept CPP High\n"
        "bad line\ntc-004 a tax urgent\n"
    )
    decisions, problems = tr.parse_decisions(text)
    assert decisions == {
        "tc-001": tr.Decision("accept", "ei", "urgent"),
        "tc-002": tr.Decision("reject"),
        "tc-003": tr.Decision("accept", "cpp", "high"),
    }
    assert len(problems) == 2 and "line 6" in problems[0] and "line 7" in problems[1]


def test_parse_decisions_repeat_last_wins_and_warns():
    decisions, problems = tr.parse_decisions("tc-001 a ei urgent\ntc-001 r\n")
    assert decisions["tc-001"].action == "reject" and "twice" in problems[0]


def test_apply_accepts_relabels_rejects_and_is_idempotent(settings):
    cands = candidates(settings)  # ei/urgent drafts
    text = f"{cands[0].id} a ei urgent\n{cands[1].id} a oas high\n{cands[2].id} r\n"
    report = tr.apply_decisions(text, cands, settings)
    assert report.accepted == [("tc-001", "tt-001"), ("tc-002", "tt-002")]
    assert report.rejected == ["tc-003"] and report.relabelled == ["tc-002"]
    items = tr.load_set(settings.triage_set_path)
    assert [i["id"] for i in items] == ["tt-001", "tt-002"]
    assert (items[1]["queue"], items[1]["priority"]) == ("oas", "high")
    assert (items[1]["draft_queue"], items[1]["draft_priority"]) == ("ei", "urgent")
    assert items[1]["source_candidate"] == "tc-002"
    again = tr.apply_decisions(text, cands, settings)
    assert again.unchanged == ["tc-001", "tc-002", "tc-003"] and not again.accepted
    assert len(tr.load_set(settings.triage_set_path)) == 2


def test_apply_reports_conflicts_and_unknown_ids(settings):
    cands = candidates(settings)
    tr.apply_decisions(f"{cands[0].id} r\n", cands, settings)
    report = tr.apply_decisions(f"{cands[0].id} a ei urgent\ntc-999 r\n", cands, settings)
    assert not report.accepted
    assert any("already reject" in p for p in report.problems)
    assert any("no such candidate" in p for p in report.problems)


def test_status_lines_count_pending(settings):
    cands = candidates(settings)
    tr.apply_decisions(f"{cands[0].id} a ei urgent\n", cands, settings)
    first, second = tr.status_lines(cands, settings)
    assert "accepted 1, rejected 0, pending 2" in first and "1 items" in second


def test_page_has_every_card_with_selects_and_escapes_text(settings):
    cands = candidates(settings)
    cands[0] = cands[0].model_copy(
        update={"body": "<script>alert(1)</script> & more", "subject": ""}
    )
    page = tr.render_page(cands)
    for c in cands:
        assert f'data-id="{c.id}"' in page
    assert page.count('<select class="q">') == 3 and page.count('<select class="p">') == 3
    assert '<option value="ei" selected>' in page and '<option value="urgent" selected>' in page
    assert "<script>alert(1)" not in page and "&lt;script&gt;alert(1)" in page
    assert "(no subject)" in page
    assert "Copy decisions" in page


def test_export_writes_only_pending(settings):
    cands = candidates(settings)
    tr.apply_decisions(f"{cands[0].id} r\n", cands, settings)
    state = tr.load_state(settings.triage_review_state_path)
    tr.export_html([c for c in cands if c.id not in state], settings.triage_review_html_path)
    html = settings.triage_review_html_path.read_text()
    assert cands[0].id not in html and cands[1].id in html
