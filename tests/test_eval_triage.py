"""Triage eval runner, offline: fake client, tiny dataset, temporary directories."""

import json
from types import SimpleNamespace

import pytest

from support_desk_assistant.config import Settings
from support_desk_assistant.evals import triage as ev

Z = 1.96


def item(i, queue, priority, style="plain"):
    return {
        "id": f"tt-{i:03d}",
        "subject": f"s{i}",
        "body": f"body {i}",
        "queue": queue,
        "priority": priority,
        "style": style,
    }


ITEMS = [
    item(1, "ei", "urgent"),
    item(2, "ei", "low", "short"),
    item(3, "cpp", "high", "emotional"),
    item(4, "oas", "normal"),
]
# What the fake model answers for each ticket body.
ANSWERS = {
    "body 1": ("ei", "urgent"),  # right
    "body 2": ("ei", "normal"),  # priority over-triage by one level
    "body 3": ("oas", "high"),  # wrong queue
    "body 4": ("oas", "urgent"),  # over-triage by two levels
}


class FakeClient:
    def __init__(self, answers=ANSWERS, fail=()):
        self.requests, self.answers, self.fail = [], answers, set(fail)
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **request):
        self.requests.append(request)
        body = request["messages"][0]["content"].split("<body>")[1].split("</body>")[0]
        if body in self.fail:
            return SimpleNamespace(to_dict=lambda: {"stop_reason": "refusal", "content": []})
        queue, priority = self.answers[body]
        text = json.dumps({"rationale": f"r-{body}", "queue": queue, "priority": priority})
        raw = {
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 400, "output_tokens": 60},
        }
        return SimpleNamespace(to_dict=lambda: raw)


@pytest.fixture
def settings(tmp_path):
    prompt = tmp_path / "triage.md"
    prompt.write_text("You triage tickets.\n")
    s = Settings(data_dir=tmp_path / "data")
    s.triage.system_prompt_path = prompt
    return s


def rows_for(settings, client=None, items=ITEMS):
    rows, notes = ev.run_items(items, settings=settings, client=client or FakeClient())
    assert notes == []
    return rows


def test_rows_carry_predictions_and_correctness(settings):
    rows = rows_for(settings)
    assert [(r.queue_ok, r.priority_ok) for r in rows] == [
        (True, True),
        (True, False),
        (False, True),
        (True, False),
    ]
    assert rows[3].pred_priority == "urgent" and rows[3].rationale == "r-body 4"
    assert [r.level_gap for r in rows] == [0, 1, 0, 2]  # positive: predicted more urgent


def test_summary_metrics(settings):
    s = ev.summarize(rows_for(settings), Z)
    assert s["n"] == 4 and s["generation_failures"] == 0
    assert (s["queue_accuracy"]["k"], s["queue_accuracy"]["n"]) == (3, 4)
    assert s["priority_accuracy"]["k"] == 2 and s["both_correct"]["k"] == 1
    assert s["priority_within_one"]["k"] == 3  # the two-level miss is excluded
    assert (s["urgent_recall"]["k"], s["urgent_recall"]["n"]) == (1, 1)
    assert (s["urgent_precision"]["k"], s["urgent_precision"]["n"]) == (1, 2)
    assert s["over_triage"]["k"] == 2 and s["under_triage"]["k"] == 0
    assert (s["per_queue"]["oas"]["precision"]["k"], s["per_queue"]["oas"]["precision"]["n"]) == (
        1,
        2,
    )
    assert s["per_queue"]["cpp"]["recall"]["k"] == 0
    assert s["queue_confusion"]["cpp"] == {"oas": 1}
    assert s["by_style"]["short"]["priority"]["k"] == 0


def test_signs_of_the_level_gap():
    low = ev.Row("a", "plain", "", "b", "ei", "urgent", pred_priority="low", status="ok")
    high = ev.Row("b", "plain", "", "b", "ei", "low", pred_priority="urgent", status="ok")
    assert low.level_gap == -3  # true urgent, predicted low: under-triage
    assert high.level_gap == 3  # true low, predicted urgent: over-triage


def test_failed_generations_count_as_wrong_and_are_reported(settings):
    rows = rows_for(settings, FakeClient(fail={"body 1"}))
    s = ev.summarize(rows, Z)
    assert s["generation_failures"] == 1
    assert s["queue_accuracy"]["k"] == 2 and s["urgent_recall"]["k"] == 0
    assert s["urgent_precision"]["n"] == 1  # only the one real urgent prediction


def test_second_run_is_served_from_cache(settings):
    client = FakeClient()
    first = rows_for(settings, client)
    second = rows_for(settings, client)
    assert len(client.requests) == 4
    assert all(r.cache_hit for r in second) and sum(r.cost_cad for r in second) == 0
    assert sum(r.cost_cad for r in first) > 0
    assert sum(r.cost_cad_list for r in second) == pytest.approx(sum(r.cost_cad for r in first))


def test_cap_stops_before_the_item(settings):
    client = FakeClient()
    rows, notes = ev.run_items(ITEMS, settings=settings, client=client, cap_cad=0)
    assert rows == [] and client.requests == [] and "tt-001" in notes[0]


def test_markdown_and_files(settings, tmp_path):
    rows = rows_for(settings)
    dataset = tmp_path / "triage_set_v1.jsonl"
    dataset.write_text("".join(json.dumps(i) + "\n" for i in ITEMS))
    meta = ev.build_meta(settings, dataset, rows, "20261020T000000Z")
    summary = ev.summarize(rows, Z)
    md = ev.render_markdown(meta, summary, rows)
    for needle in (
        "## Overall",
        "urgent recall",
        "## Per queue",
        "## Confusion",
        "Misses (3)",
        "tt-003",
    ):
        assert needle in md
    paths = ev.write_results(tmp_path / "out", meta, summary, rows)
    assert [p.suffix for p in paths] == [".jsonl", ".json", ".md"]
    lines = [json.loads(line) for line in paths[0].read_text().splitlines()]
    assert len(lines) == 4 and lines[2]["queue_ok"] is False and lines[0]["pred_queue"] == "ei"
    assert json.loads(paths[1].read_text())["meta"]["dataset"]["name"] == "triage_set_v1.jsonl"


def test_main_dry_run_then_partial_goes_to_runs(settings, tmp_path, monkeypatch, capsys):
    dataset = settings.triage_set_path
    dataset.parent.mkdir(parents=True, exist_ok=True)
    dataset.write_text("".join(json.dumps(i) + "\n" for i in ITEMS))
    monkeypatch.setattr(ev, "get_settings", lambda: settings)
    assert ev.main([]) == 0
    assert "dry run" in capsys.readouterr().out
    monkeypatch.setattr(
        ev,
        "triage_ticket",
        lambda s, b, settings, client=None: (
            ev.triage_ticket.__wrapped__(s, b, settings, client)
            if False
            else _fake_triage(s, b, settings)
        ),
    )
    assert ev.main(["--yes", "--limit", "2"]) == 0
    out = capsys.readouterr().out
    assert "partial run" in out and str(settings.runs_dir) in out


def _fake_triage(subject, body, settings):
    from support_desk_assistant.triage.pipeline import TriageOutcome
    from support_desk_assistant.triage.schema import TriageResult

    queue, priority = ANSWERS[body]
    return TriageOutcome(
        status="ok",
        result=TriageResult(rationale="r", queue=queue, priority=priority),
        model=settings.triage.model,
    )


def test_main_without_a_set_explains(settings, monkeypatch, capsys):
    monkeypatch.setattr(ev, "get_settings", lambda: settings)
    assert ev.main([]) == 2
    assert "no triage set" in capsys.readouterr().out
