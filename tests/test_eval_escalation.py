"""Escalation eval, offline: hand-built rows with known counts, fake triage, temp directories."""

import json

import pytest

from support_desk_assistant.config import EscalationEvalSettings, EscalationSettings, Settings
from support_desk_assistant.evals import escalation as esc
from support_desk_assistant.evals import triage as ev
from support_desk_assistant.evals.provenance import file_sha256
from support_desk_assistant.triage.pipeline import TriageOutcome
from support_desk_assistant.triage.schema import TriageResult

Z = 1.96

# The rules before tuning: urgent on, medium floors, no second opinion. The hand counts assume them.
RULES = EscalationSettings(
    on_other_queue=True,
    on_urgent=True,
    min_queue_confidence="medium",
    min_priority_confidence="medium",
    on_disagreement=False,
)


def row(i, true, pred, conf=("high", "high"), ambiguous=False, status="ok"):
    ok = status == "ok"
    return ev.Row(
        id=f"e-{i:02d}",
        style="plain",
        subject=f"s{i}",
        body=f"body {i}",
        queue=true[0],
        priority=true[1],
        status=status,
        pred_queue=pred[0] if ok else None,
        pred_priority=pred[1] if ok else None,
        pred_queue_confidence=conf[0] if ok else None,
        pred_priority_confidence=conf[1] if ok else None,
        rationale="r" if ok else None,
        ambiguous=ambiguous,
    )


# Ten rows, counts worked out by hand (default policy: other + urgent on, floors "medium").
ROWS = [
    row(1, ("ei", "normal"), ("ei", "normal")),  # right, left alone
    row(2, ("ei", "low"), ("ei", "low")),  # right, left alone
    row(3, ("cpp", "high"), ("cpp", "normal"), ("high", "low"), ambiguous=True),  # caught: low conf
    row(4, ("oas", "normal"), ("other", "normal"), ("low", "high")),  # caught: other + low conf
    row(5, ("ei", "urgent"), ("ei", "urgent")),  # right, escalated because urgent
    row(6, ("passport", "urgent"), ("passport", "high"), ("high", "medium"), ambiguous=True),
    row(7, ("sin", "high"), ("sin", "urgent")),  # caught: predicted urgent
    row(8, ("cpp", "low"), ("cpp", "low"), ("medium", "medium")),  # right, left alone
    row(9, ("ei", "normal"), ("ei", "low")),  # under-triage nobody reviews
    row(10, ("ei", "high"), (None, None), status="refusal"),  # failed: always escalated
]


def test_result_of_rebuilds_the_classifier_result_or_none():
    r = esc.result_of(ROWS[2])
    assert (r.queue, r.priority, r.queue_confidence, r.priority_confidence) == (
        "cpp",
        "normal",
        "high",
        "low",
    )
    assert esc.result_of(ROWS[9]) is None
    assert esc.result_of(row(11, ("ei", "low"), ("ei", "low"), conf=(None, None))) is None


def test_policy_metrics_match_the_hand_counts():
    m = esc.policy_metrics(ROWS, RULES, Z)
    assert m["n"] == 10
    assert (m["escalated"]["k"], m["escalated"]["n"]) == (5, 10)
    assert (m["error_rate"]["k"], m["error_rate"]["n"]) == (6, 10)
    assert (m["error_capture"]["k"], m["error_capture"]["n"]) == (4, 6)
    assert (m["escalation_precision"]["k"], m["escalation_precision"]["n"]) == (4, 5)
    assert (m["residual_error"]["k"], m["residual_error"]["n"]) == (2, 5)
    assert (m["urgent_unreached"]["k"], m["urgent_unreached"]["n"]) == (1, 2)
    assert (m["under_triage_capture"]["k"], m["under_triage_capture"]["n"]) == (1, 3)
    assert (m["ambiguous_capture"]["k"], m["ambiguous_capture"]["n"]) == (1, 2)
    assert m["random_capture"] == 0.5
    # Two rows are ambiguous (r3, r6) and both are errors; r3 is caught. Of the eight others,
    # four are errors (r4, r7, r9, r10) and three of those are caught.
    assert (m["error_rate_unsure"]["k"], m["error_rate_unsure"]["n"]) == (2, 2)
    assert (m["error_rate_clear"]["k"], m["error_rate_clear"]["n"]) == (4, 8)
    assert (m["error_capture_unsure"]["k"], m["error_capture_unsure"]["n"]) == (1, 2)
    assert (m["error_capture_clear"]["k"], m["error_capture_clear"]["n"]) == (3, 4)
    assert m["reasons"] == {
        "no_program_queue": 1,
        "urgent": 2,
        "low_queue_confidence": 1,
        "low_priority_confidence": 1,
        "triage_failed": 1,
    }


def test_an_urgent_ticket_labelled_urgent_is_reached_even_without_the_urgent_rule():
    no_urgent_rule = RULES.model_copy(update={"on_urgent": False})
    m = esc.policy_metrics(ROWS, no_urgent_rule, Z)
    # r5 is urgent and labelled urgent (reached); r6 is urgent, labelled high, not flagged.
    assert (m["urgent_unreached"]["k"], m["urgent_unreached"]["n"]) == (1, 2)
    only_labelled = [row(1, ("ei", "urgent"), ("ei", "urgent"))]
    assert esc.policy_metrics(only_labelled, no_urgent_rule, Z)["urgent_unreached"]["k"] == 0
    missed = [row(2, ("ei", "urgent"), ("ei", "high"))]
    assert esc.policy_metrics(missed, no_urgent_rule, Z)["urgent_unreached"]["k"] == 1
    flagged_low = [row(3, ("ei", "urgent"), ("ei", "high"), ("high", "low"))]
    assert esc.policy_metrics(flagged_low, RULES, Z)["urgent_unreached"]["k"] == 0


def test_row_second_agrees_is_none_until_a_second_pass_runs():
    r = row(1, ("ei", "low"), ("ei", "low"))
    assert r.second_agrees is None
    r.second_status, r.second_queue, r.second_priority = "ok", "ei", "low"
    assert r.second_agrees is True
    r.second_priority = "high"
    assert r.second_agrees is False
    r.second_status = "refusal"
    assert r.second_agrees is False


def test_the_second_opinion_adds_a_sweep_dimension_and_a_named_policy():
    rows = [row(i, ("ei", "low"), ("ei", "low")) for i in range(4)]
    assert len(esc.sweep(rows, RULES, Z)) == 18
    for r, status, prio in zip(
        rows, ("ok", "ok", "ok", "refusal"), ("low", "high", "low", None), strict=True
    ):
        r.second_status, r.second_queue, r.second_priority = status, "ei", prio
    out = esc.sweep(rows, RULES, Z)
    assert len(out) == 36
    only = next(s for s in out if s["disagree"] and not s["rules"] and s["queue_floor"] == "low")
    assert only["escalated"]["k"] == 2  # one disagreed, one failed
    names = esc.compared_policies(RULES, with_second=True)
    assert "second opinion only" in names and "second opinion only" not in esc.compared_policies(
        RULES
    )
    assert esc.policy_metrics(rows, names["second opinion only"], Z)["escalated"]["k"] == 2


def test_an_empty_split_does_not_divide_by_zero():
    m = esc.policy_metrics([], RULES, Z)
    assert m["escalated"]["rate"] is None and m["random_capture"] is None
    assert not any(t["met"] for t in esc.check_targets(m, EscalationEvalSettings()))


def test_targets_are_checked_as_written():
    m = esc.policy_metrics(ROWS, RULES, Z)
    strict = EscalationEvalSettings()  # 0.40 / 0.60 / 0.05
    met = {t["target"].split(" (")[0]: t["met"] for t in esc.check_targets(m, strict)}
    assert list(met.values()) == [False, True, False, False]
    lax = EscalationEvalSettings(
        max_escalation_rate=0.6, min_error_capture=0.9, max_urgent_unreached=0.5
    )
    got = [t["met"] for t in esc.check_targets(m, lax)]
    assert got == [True, False, False, True]  # capture 0.67 < 0.9; interval low 0.3 < rate 0.5


def test_beating_random_needs_the_interval_above_the_rate():
    # 40 errors, 36 caught by escalating 40 of 100 rows: lower bound ~0.77 > 0.40.
    rows = [row(i, ("ei", "low"), ("ei", "low")) for i in range(60)]
    rows += [row(100 + i, ("ei", "low"), ("ei", "high"), ("high", "low")) for i in range(36)]
    rows += [row(200 + i, ("ei", "low"), ("ei", "high")) for i in range(4)]
    m = esc.policy_metrics(rows, RULES, Z)
    by = {t["target"]: t for t in esc.check_targets(m, EscalationEvalSettings())}
    beats = by["error capture interval above the escalation rate (beats random)"]
    assert beats["met"] and beats["value"] > m["escalated"]["rate"]


def test_sweep_covers_every_pair_of_floors_with_rules_off_and_on():
    out = esc.sweep(ROWS, RULES, Z)
    assert len(out) == 18
    nothing = next(
        s
        for s in out
        if not s["rules"] and s["queue_floor"] == "low" and s["priority_floor"] == "low"
    )
    assert nothing["escalated"]["k"] == 1  # only the failed triage
    everything = next(
        s
        for s in out
        if s["rules"] and s["queue_floor"] == "high" and s["priority_floor"] == "high"
    )
    assert everything["escalated"]["k"] > 5


def test_compared_policies_isolate_each_signal():
    policies = esc.compared_policies(RULES)
    rules = policies["rules only (other queue, urgent)"]
    conf = policies["confidence only (configured floors)"]
    assert rules.on_urgent and rules.min_queue_confidence == "low"
    assert not conf.on_urgent and not conf.on_other_queue and conf.min_queue_confidence == "medium"
    assert policies["configured policy"] == RULES


@pytest.fixture
def settings(tmp_path):
    prompt = tmp_path / "triage.md"
    prompt.write_text("You triage tickets.\n")
    s = Settings(data_dir=tmp_path / "data")
    s.triage.system_prompt_path = prompt
    s.escalation = RULES.model_copy()
    return s


def write_set(settings, split="dev"):
    path = settings.triage_set_path
    path.parent.mkdir(parents=True, exist_ok=True)
    items = [
        {
            "split": split,
            "label_source": "silver",
            "labelled_by": None,
            "id": f"t2-{i:03d}",
            "subject": f"s{i}",
            "body": f"body {i}",
            "queue": "ei",
            "priority": "urgent" if i == 1 else "low",
            "style": "plain",
        }
        for i in range(1, 5)
    ]
    path.write_text("".join(json.dumps(i) + "\n" for i in items))
    return path


def fake_triage(subject, body, settings, client=None):
    result = TriageResult(
        rationale="r",
        queue="ei",
        priority="high" if body == "body 1" else "low",  # the one urgent ticket is under-triaged
        queue_confidence="high",
        priority_confidence="low" if body == "body 1" else "high",
    )
    return TriageOutcome(status="ok", result=result, model=settings.triage.model)


def test_main_dry_run_then_a_dev_run_stays_local(settings, monkeypatch, capsys):
    write_set(settings)
    monkeypatch.setattr(esc, "get_settings", lambda: settings)
    assert esc.main([]) == 0
    assert "dry run" in capsys.readouterr().out
    monkeypatch.setattr(ev, "triage_ticket", fake_triage)
    assert esc.main(["--yes"]) == 0
    out = capsys.readouterr().out
    assert "dev split" in out and str(settings.runs_dir) in out
    md = next(settings.runs_dir.glob("escalation_*.md")).read_text()
    for needle in (
        "## Targets (fixed before the test run)",
        "## Configured policy",
        "## Signals side by side",
        "## Confidence floors",
        "## Errors nobody would review (0)",
    ):
        assert needle in md
    jsonl = next(settings.runs_dir.glob("escalation_*.jsonl")).read_text().splitlines()
    first = json.loads(jsonl[0])
    assert first["escalated"] is True and first["reasons"] == ["low_priority_confidence"]
    assert (
        not list(settings.eval_results_dir.glob("escalation_*"))
        if settings.eval_results_dir.exists()
        else True
    )


def test_main_with_a_second_opinion_scores_both_passes_and_records_it(
    settings, monkeypatch, capsys
):
    write_set(settings)
    monkeypatch.setattr(esc, "get_settings", lambda: settings)
    calls = []

    def triage(subject, body, settings, client=None):
        calls.append((settings.triage.thinking, settings.triage.max_tokens))
        out = fake_triage(subject, body, settings)
        if settings.triage.thinking == "adaptive" and body == "body 2":  # the second pass differs
            out.result = out.result.model_copy(update={"priority": "urgent"})
        return out

    monkeypatch.setattr(ev, "triage_ticket", triage)
    assert esc.main(["--yes", "--second-opinion"]) == 0
    assert {c[0] for c in calls} == {None, "adaptive"} and 2000 in {c[1] for c in calls}
    summary = json.loads(next(settings.runs_dir.glob("escalation_*.summary.json")).read_text())
    assert summary["meta"]["second_opinion"]["thinking"] == "adaptive"
    assert summary["metrics"]["second_opinion"] is True
    assert "second opinion only" in summary["metrics"]["policies"]
    rows = [
        json.loads(line)
        for line in next(settings.runs_dir.glob("escalation_*.jsonl")).read_text().splitlines()
    ]
    assert [r["second_status"] for r in rows] == ["ok"] * 4
    assert rows[1]["second_priority"] == "urgent" and rows[0]["second_priority"] == "high"


def test_a_policy_that_needs_the_second_opinion_refuses_to_run_without_it(
    settings, monkeypatch, capsys
):
    write_set(settings)
    settings.escalation.on_disagreement = True
    monkeypatch.setattr(esc, "get_settings", lambda: settings)
    assert esc.main(["--yes"]) == 2
    assert "add --second-opinion" in capsys.readouterr().out


def test_main_without_a_set_explains(settings, monkeypatch, capsys):
    monkeypatch.setattr(esc, "get_settings", lambda: settings)
    assert esc.main([]) == 2
    assert "no triage set" in capsys.readouterr().out


def test_the_test_split_is_scored_once(settings, monkeypatch, capsys):
    dataset = write_set(settings, split="test")
    monkeypatch.setattr(esc, "get_settings", lambda: settings)
    monkeypatch.setattr(ev, "triage_ticket", fake_triage)
    settings.eval_results_dir.mkdir(parents=True, exist_ok=True)
    previous = settings.eval_results_dir / "escalation_prior.summary.json"
    previous.write_text(
        json.dumps({"meta": {"split": "test", "dataset": {"sha256": file_sha256(dataset)}}})
    )
    assert esc.main(["--split", "test", "--yes"]) == 2
    assert "already scored" in capsys.readouterr().out
    # A triage-eval result for the same dataset does not count as an escalation run.
    previous.rename(settings.eval_results_dir / "triage_prior.summary.json")
    assert esc.main(["--split", "test", "--yes", "--allow-dirty"]) == 0
