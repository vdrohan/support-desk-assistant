import json

import pytest

from fakes import FakeAnthropic, make_message
from support_desk_assistant.config import Settings
from support_desk_assistant.evals.calibrate import (
    CalibrationRecord,
    append_records,
    import_labels,
    load_records,
    parse_sheet,
    record_id,
    render_calibration_markdown,
    run_calibration,
)

SYSTEM = "judge system"


class NoAnthropic:
    """Any use of the Anthropic client fails the test."""

    @property
    def messages(self):
        raise AssertionError("Anthropic client used")


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, data_dir=tmp_path / "data", logs_dir=tmp_path / "logs")


def verdicts(*items):
    return make_message(
        json.dumps(
            {"verdicts": [{"fact_index": i, "present": p, "reason": r} for i, p, r in items]}
        )
    )


def rec(item_id, fact, *, method="judge", judge_facts=None, machine=True, human="present",
        category="single_page", question=None, answer="The answer."):  # fmt: skip
    if method == "judge":
        judge_facts = judge_facts or [fact]
        fact_index = judge_facts.index(fact) + 1
    else:
        judge_facts, fact_index = [], None
    return CalibrationRecord(
        id=record_id(item_id, fact),
        item_id=item_id,
        category=category,
        question=question or f"Question {item_id}?",
        answer=answer,
        fact=fact,
        judge_facts=judge_facts,
        fact_index=fact_index,
        method=method,
        machine_present=machine,
        human=human,
        source_run="answers_test.json",
        labelled_at="2026-10-16",
    )


# --- Records ---------------------------------------------------------------------


def test_record_id_is_deterministic_and_fact_sensitive():
    a = record_id("ev-001", "You need 600 hours")
    assert a == record_id("ev-001", "You need 600 hours")
    assert a.startswith("ev-001:") and len(a.split(":")[1]) == 8
    assert a != record_id("ev-001", "You need 700 hours")
    assert a != record_id("ev-002", "You need 600 hours")


def test_record_rejects_inconsistent_fact_index():
    good = rec("ev-001", "fact b", judge_facts=["fact a", "fact b"])
    data = good.model_dump() | {"fact_index": 1}
    with pytest.raises(ValueError):
        CalibrationRecord.model_validate(data)
    with pytest.raises(ValueError):
        CalibrationRecord.model_validate(good.model_dump() | {"extra": 1})


def test_append_dedups_and_reports_conflicts_without_overwriting(tmp_path):
    path = tmp_path / "eval" / "cal.jsonl"
    r1 = rec("ev-001", "fact one", human="present")
    r2 = rec("ev-002", "fait çà", method="numbers", human="absent")
    first = append_records(path, [r1, r2])
    assert first.added == [r1.id, r2.id] and not first.duplicates and not first.conflicts

    flipped = r1.model_copy(update={"human": "absent"})
    r3 = rec("ev-003", "fact three")
    second = append_records(path, [r1, flipped, r3, r3])
    assert second.added == [r3.id]
    assert second.duplicates == [r1.id, r3.id]
    assert second.conflicts == [r1.id]

    loaded = load_records(path)
    assert [r.id for r in loaded] == [r1.id, r2.id, r3.id]
    assert loaded[0].human == "present"  # not overwritten
    lines = path.read_text(encoding="utf-8").splitlines()
    assert "fait çà" in lines[1]  # ensure_ascii False
    assert list(json.loads(lines[0])) == list(CalibrationRecord.model_fields)


def test_load_missing_is_empty_blank_lines_skipped_bad_line_named(tmp_path):
    path = tmp_path / "cal.jsonl"
    assert load_records(path) == []
    good = rec("ev-001", "fact").model_dump_json()
    path.write_text(f"{good}\n\n{{not json\n", encoding="utf-8")
    with pytest.raises(ValueError, match="line 3"):
        load_records(path)
    path.write_text(f"\n{good}\n\n", encoding="utf-8")
    assert len(load_records(path)) == 1


# --- Sheet -----------------------------------------------------------------------

SHEET = """# Judge review

Write present or absent after each 'Your verdict'.

## A. Judged facts (n=3)

### A1. ev-001 (single_page)

- Question: How many hours?
- Answer: You need 600 hours.
- Fact: You need 600 insured hours
- Your verdict: Present

### A2. ev-002 (multi_page)

- Question: Who?
- Answer: Anyone.
- Fact: Rate is 55% (max $729) — "weekly" · résumé [see §2]
- Your verdict:   ABSENT

### A3. ev-003 (confusable)

- Question: Q?
- Answer: A.
- Fact: Something
- Your verdict: ___

## B. Numbers-gate facts (n=2)

### B1. ev-004 (single_page)

- Question: Q?
- Answer: A.
- Fact: 5 weeks
- Your verdict: maybe

### B2. ev-005 (time_sensitive)

- Question: Q?
- Answer: A.
- Fact: Due 30 April
- Your verdict:

## C. In-domain unanswerables (n=1)

### C1. ev-099

- Question: Q?
- Handled: answered (not abstained) (FAIL)
- Your verdict: present
"""


def test_parse_sheet_labels_unlabelled_problems_and_ignores_section_c():
    p = parse_sheet(SHEET)
    assert [(lb.tag, lb.item_id, lb.verdict) for lb in p.labels] == [
        ("A1", "ev-001", "present"),
        ("A2", "ev-002", "absent"),
    ]
    assert p.labels[1].fact == 'Rate is 55% (max $729) — "weekly" · résumé [see §2]'
    assert p.labels[0].line_no == 12
    assert p.unlabelled == 2
    assert len(p.problems) == 1
    assert "line 35" in p.problems[0] and "maybe" in p.problems[0]


# --- Import ----------------------------------------------------------------------


def fact(text, method, present, reason="r", judge_error=False):
    return {"fact": text, "method": method, "present": present, "reason": reason,
            "judge_error": judge_error}  # fmt: skip


def row(item_id, facts, category="single_page"):
    return {"id": item_id, "category": category, "question": f"Q {item_id}?",
            "answer": f"A {item_id}.", "facts": facts}  # fmt: skip


def block(tag, item_id, fact_text, verdict, category="single_page"):
    return (
        f"\n### {tag}. {item_id} ({category})\n\n- Question: q\n- Answer: a\n"
        f"- Fact: {fact_text}\n- Your verdict: {verdict}\n"
    )


def test_import_labels_happy_path_with_two_judge_facts_and_numbers():
    rows = [
        row(
            "ev-001",
            [
                fact("600 hours", "numbers", False),
                fact("first j", "judge", True),
                fact("second j", "judge", False),
            ],
        ),  # fmt: skip
        row("ev-002", [fact("$729 weekly", "numbers", True)], category="multi_page"),
    ]
    sheet = (
        block("A1", "ev-001", "second j", "present")
        + block("A2", "ev-001", "first j", "absent")
        # Tag B but the row says judge, and vice versa: the row wins.
        + block("A3", "ev-002", "$729 weekly", "present")
    )
    result = import_labels(sheet, rows, source_run="run.json", labelled_at="2026-10-16")
    assert result.problems == [] and result.unlabelled == 0
    second, first, numbers = result.records
    assert second.method == "judge" and second.judge_facts == ["first j", "second j"]
    assert second.fact_index == 2 and second.machine_present is False
    assert second.human == "present" and second.id == record_id("ev-001", "second j")
    assert first.fact_index == 1 and first.machine_present is True and first.human == "absent"
    assert numbers.method == "numbers" and numbers.judge_facts == [] and numbers.fact_index is None
    assert numbers.category == "multi_page" and numbers.question == "Q ev-002?"
    assert numbers.answer == "A ev-002." and numbers.source_run == "run.json"


def test_import_labels_problem_kinds_are_skipped():
    rows = [
        row(
            "ev-001",
            [
                fact("judged", "judge", True),
                fact("none f", "none", False),
                fact("errored", "judge", False, judge_error=True),
            ],
        ),  # fmt: skip
    ]
    sheet = (
        block("A1", "ev-404", "judged", "present")
        + block("A2", "ev-001", "not a fact", "present")
        + block("A3", "ev-001", "none f", "absent")
        + block("A4", "ev-001", "errored", "absent")
        + block("A5", "ev-001", "judged", "present")
        + block("A6", "ev-001", "judged", "absent")
        + block("A7", "ev-001", "judged", "bogus")
    )
    result = import_labels(sheet, rows, source_run="r", labelled_at="2026-10-16")
    assert [r.fact for r in result.records] == ["judged"]
    assert result.records[0].human == "present"
    joined = "\n".join(result.problems)
    assert len(result.problems) == 6
    for needle in ("not in the results", "fact not found", "method none", "judge error",
                   "duplicate", "bogus"):  # fmt: skip
        assert needle in joined


# --- Calibration run -------------------------------------------------------------


def test_run_calibration_one_call_per_item_then_cache_hits(settings):
    item1 = ["fact a", "fact b"]
    records = [
        # Judge said a present, b absent at labelling time.
        rec("ev-001", "fact a", judge_facts=item1, machine=True, human="present"),
        rec("ev-001", "fact b", judge_facts=item1, machine=False, human="present"),
        rec("ev-002", "fact c", machine=True, human="absent", category="confusable"),
    ]
    client = FakeAnthropic(
        verdicts((1, True, "stated"), (2, False, "not stated")),
        verdicts((1, True, "says so")),
    )
    report = run_calibration(records, settings=settings, system=SYSTEM, client=client)
    assert len(client.calls) == 2 and report["calls"] == 2 and report["cache_hits"] == 0
    content = client.calls[0]["messages"][0]["content"]
    assert "1. fact a\n2. fact b" in content
    j = report["judge"]
    assert j["n"] == 3 and j["n_errors"] == 0 and j["drift"] == 0
    assert j["agreement"]["k"] == 1 and j["agreement"]["n"] == 3
    # human present, machine absent -> fn (false absent); human absent, machine present -> fp.
    assert j["confusion"] == {"tp": 1, "fp": 1, "fn": 1, "tn": 0}
    assert j["false_present"]["k"] == 1 and j["false_present"]["n"] == 1
    assert j["false_absent"]["k"] == 1 and j["false_absent"]["n"] == 2
    assert j["per_category"]["single_page"]["n"] == 2
    assert j["per_category"]["confusable"]["k"] == 0
    reasons = {d["fact"]: (d["human"], d["machine"], d["reason"]) for d in j["disagreements"]}
    assert reasons == {
        "fact b": ("present", "absent", "not stated"),
        "fact c": ("absent", "present", "says so"),
    }
    assert report["cost_cad"] > 0

    again = run_calibration(records, settings=settings, system=SYSTEM, client=NoAnthropic())
    assert again["calls"] == 0 and again["cache_hits"] == 2 and again["cost_cad"] == 0
    assert again["judge"]["agreement"] == j["agreement"]


def test_run_calibration_judge_error_excluded_and_drift_counted(settings):
    records = [
        rec("ev-001", "fact a", machine=True, human="present"),
        rec("ev-002", "fact b", machine=True, human="absent"),  # judge now says absent
    ]
    client = FakeAnthropic(make_message("not json"), verdicts((1, False, "missing")))
    report = run_calibration(records, settings=settings, system=SYSTEM, client=client)
    j = report["judge"]
    assert j["n"] == 1 and j["n_errors"] == 1 and j["drift"] == 1
    assert j["agreement"]["rate"] == 1.0
    assert j["disagreements"] == []


def test_numbers_records_need_no_client_and_kappa_orientation(settings):
    records = [
        rec("ev-1", "f1", method="numbers", machine=True, human="present"),
        rec("ev-2", "f2", method="numbers", machine=True, human="absent"),
        rec("ev-3", "f3", method="numbers", machine=False, human="absent"),
        rec("ev-4", "f4", method="numbers", machine=False, human="absent"),
    ]
    report = run_calibration(records, settings=settings, system=SYSTEM, client=NoAnthropic())
    n = report["numbers"]
    assert report["calls"] == 0 and report["judge"]["n"] == 0
    assert n["n"] == 4 and n["confusion"] == {"tp": 1, "fp": 1, "fn": 0, "tn": 2}
    assert n["false_present"] == {"k": 1, "n": 3, "rate": 0.333, "ci95": n["false_present"]["ci95"]}
    assert n["false_absent"]["k"] == 0 and n["false_absent"]["n"] == 1
    assert n["kappa"] == 0.5
    assert n["disagreements"][0]["fact"] == "f2" and n["disagreements"][0]["reason"] == ""
    assert "n_errors" not in n and "drift" not in n


def test_empty_input_and_markdown_warning(settings):
    report = run_calibration([], settings=settings, system=SYSTEM, client=NoAnthropic())
    assert report["n_records"] == 0 and report["below_min_n"] is True
    for key in ("judge", "numbers"):
        s = report[key]
        assert s["n"] == 0 and s["agreement"]["rate"] is None and s["kappa"] is None
        assert s["false_present"]["rate"] is None and s["per_category"] == {}
    md = render_calibration_markdown(report)
    assert "WARNING" in md and "n=0" in md and f"min_n={settings.eval.calibration_min_n}" in md
    assert "blind" in md


def test_markdown_sections_ci_style_and_no_warning_at_min_n(settings):
    s = settings.model_copy(
        update={"eval": settings.eval.model_copy(update={"calibration_min_n": 1})}
    )
    records = [
        rec("ev-001", "fact a", machine=True, human="absent"),
        rec("ev-002", "f2", method="numbers", machine=True, human="present"),
    ]
    client = FakeAnthropic(verdicts((1, True, "it says")))
    report = run_calibration(records, settings=s, system=SYSTEM, client=client)
    assert report["below_min_n"] is False
    md = render_calibration_markdown(report)
    assert "WARNING" not in md
    assert "## LLM judge vs human" in md and "## Number check vs human" in md
    assert "inflates scores" in md and "deflates scores" in md
    assert "1.00 [0.21, 1.00] (1/1)" in md  # numbers agreement, Wilson CI
    assert "**ev-001** (single_page): human absent, machine present" in md
    assert "Judge calls: 1, cache hits: 0" in md


def test_round_trip_through_the_real_review_sheet(settings):
    from support_desk_assistant.evals.answers import render_judge_review

    rows = [
        row(
            "ev-001",
            [
                fact("You need 600 insured hours", "numbers", True, "ok"),
                fact("Apply online (within 4 weeks)", "judge", True, "said"),
            ],
        ),  # fmt: skip
        row("ev-002", [fact("Benefits stop when you work", "judge", False, "not said")]),
    ]
    sheet = render_judge_review({"items": rows}, "run.json", settings)
    blind = "\n".join(
        ln for ln in sheet.splitlines() if not ln.startswith(("- Judge:", "- Number check"))
    )
    filled = blind.replace("- Your verdict: ___", "- Your verdict: absent")
    result = import_labels(filled, rows, source_run="run.json", labelled_at="2026-10-16")
    assert result.problems == [] and result.unlabelled == 0
    assert sorted((r.item_id, r.method) for r in result.records) == [
        ("ev-001", "judge"),
        ("ev-001", "numbers"),
        ("ev-002", "judge"),
    ]
    assert parse_sheet(blind).unlabelled == 3
