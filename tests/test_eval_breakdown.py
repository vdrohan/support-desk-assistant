import json
from typing import Any

from support_desk_assistant.evals.breakdown import (
    load_program_map,
    render_summary_markdown,
    summarize,
)

Z = 1.96
EI = "https://example.test/ei"
CPP = "https://example.test/cpp"
OAS = "https://example.test/oas"
UNMAPPED = "https://example.test/elsewhere"
PROGRAM_OF = {EI: "ei", CPP: "cpp", OAS: "oas"}


def _row(**kw: Any) -> dict[str, Any]:
    """An answered, passing single_page item by default."""
    row = {
        "id": "q",
        "category": "single_page",
        "answerable": True,
        "gold_urls": [EI],
        "status": "ok",
        "gate": "pass",
        "abstained": False,
        "abstained_by": None,
        "validator_ok": True,
        "cited_urls": [EI],
        "gold_cited": True,
        "facts_fraction": 1.0,
        "passed": True,
        "attribution": None,
    }
    row.update(kw)
    return row


def _unanswerable(**kw: Any) -> dict[str, Any]:
    """A correct gate abstention on an unanswerable item by default."""
    base = {
        "category": "unanswerable_out_of_scope",
        "answerable": False,
        "gold_urls": [],
        "abstained": True,
        "abstained_by": "gate",
        "gate": "low_score",
        "cited_urls": [],
        "gold_cited": None,
        "facts_fraction": None,
        "passed": True,
    }
    return _row(**(base | kw))


def _failed(attribution: str, **kw: Any) -> dict[str, Any]:
    return _row(**({"passed": False, "attribution": attribution, "facts_fraction": 0.0} | kw))


def _abstention_rows() -> list[dict[str, Any]]:
    return [
        _unanswerable(id="gate"),  # correct, by gate
        _unanswerable(id="model", abstained_by="model", gate="pass"),  # correct, by model
        _failed("generation", id="false_abs", abstained=True, abstained_by="model"),
        _unanswerable(id="missed", abstained=False, abstained_by=None, passed=False),
        # Refusal fallback: abstained but not intentional, not a pass.
        _unanswerable(
            id="refusal", status="refusal", abstained_by="model", gate="pass", passed=False
        ),
        _row(id="ok"),
    ]


def test_abstention_precision_recall_and_counts():
    a = summarize(_abstention_rows(), program_of=PROGRAM_OF, z=Z)["abstention"]
    # tp = 2 (gate, model); unanswerable = 4 (gate, model, missed, refusal).
    assert a["recall"]["k"] == 2 and a["recall"]["n"] == 4
    # Intentional abstentions: gate, model, false_abs (refusal excluded).
    assert a["precision"]["k"] == 2 and a["precision"]["n"] == 3
    assert a["precision"]["rate"] == 0.667
    assert a["false_abstentions"] == 1
    assert a["missed"] == 2  # missed + refusal
    assert a["generation_failures"] == 1
    assert a["by_gate"] == 1
    assert a["by_model"] == 2


def test_overall_never_blends_and_matches_abstention_recall():
    s = summarize(_abstention_rows(), program_of=PROGRAM_OF, z=Z)
    o = s["overall"]
    assert o["n_items"] == 6
    assert (o["answerable"]["k"], o["answerable"]["n"]) == (1, 2)
    assert o["abstention_recall"] == s["abstention"]["recall"]


def test_by_program_multi_program_unknown_and_na():
    rows = [
        _row(id="both", gold_urls=[EI, CPP]),
        _failed("generation", id="ei_fail", gold_urls=[EI], facts_fraction=0.5),
        _row(id="lost", gold_urls=[UNMAPPED]),
        _unanswerable(id="u1"),
        _unanswerable(id="u2", passed=False, abstained=False, abstained_by=None),
    ]
    bp = summarize(rows, program_of=PROGRAM_OF, z=Z)["by_program"]
    assert list(bp) == ["cpp", "ei", "unknown", "n/a"]
    assert bp["cpp"]["n"] == 1 and bp["cpp"]["pass"]["k"] == 1
    assert bp["ei"]["n"] == 2 and bp["ei"]["pass"]["k"] == 1
    assert bp["ei"]["mean_facts_fraction"] == 0.75
    assert bp["unknown"]["n"] == 1
    assert bp["n/a"]["n"] == 2
    assert bp["n/a"]["correct_abstention"]["k"] == 1


def test_citation_denominator_excludes_abstentions_and_failures():
    rows = [
        _row(id="a"),
        _row(id="b", validator_ok=False, cited_urls=[EI, UNMAPPED], gold_cited=True),
        _unanswerable(id="gate_abs", validator_ok=True),
        _failed("generation", id="max_tok", status="max_tokens", abstained=True),
        _row(id="no_cite", cited_urls=[], gold_cited=False, passed=False),
    ]
    c = summarize(rows, program_of=PROGRAM_OF, z=Z)["citation"]
    assert c["n_eligible"] == 3
    assert (c["validity"]["k"], c["validity"]["n"]) == (2, 3)
    assert (c["gold_cited"]["k"], c["gold_cited"]["n"]) == (2, 3)
    # a: 1/1, b: 1/2; no_cite has no citations and is left out.
    assert c["citation_precision"] == {"mean": 0.75, "n": 2}


def test_macro_and_micro_differ_on_skewed_set():
    rows = [_row(id=f"s{i}") for i in range(9)] + [
        _failed("retrieval_page", id="m", category="multi_page")
    ]
    o = summarize(rows, program_of=PROGRAM_OF, z=Z)["overall"]
    assert o["answerable"]["rate"] == 0.9
    assert o["macro_pass_rate"] == 0.5


def test_macro_none_without_answerable():
    o = summarize([_unanswerable()], program_of=PROGRAM_OF, z=Z)["overall"]
    assert o["macro_pass_rate"] is None
    assert o["answerable"]["rate"] is None


def test_attribution_counts_and_shares():
    rows = [
        _failed("retrieval_page", id="p1"),
        _failed("retrieval_page", id="p2"),
        _failed("retrieval_chunk", id="c1"),
        _failed("generation", id="g1"),
        _row(id="ok"),
        _unanswerable(id="u", passed=False, abstained=False, abstained_by=None),
    ]
    a = summarize(rows, program_of=PROGRAM_OF, z=Z)["attribution"]
    assert a["n_failed_answerable"] == 4
    assert (a["retrieval_page"], a["retrieval_chunk"], a["generation"]) == (2, 1, 1)
    assert a["shares"]["retrieval_page"]["rate"] == 0.5


def test_attribution_empty_is_none_safe():
    a = summarize([_row()], program_of=PROGRAM_OF, z=Z)["attribution"]
    assert a["n_failed_answerable"] == 0
    assert a["shares"]["generation"]["rate"] is None


def test_markdown_renders_with_n_and_ci():
    s = summarize(_abstention_rows(), program_of=PROGRAM_OF, z=Z)
    text = "\n".join(render_summary_markdown(s))
    for heading in (
        "## Overall",
        "## By program",
        "## Abstention",
        "## Citation validity",
        "## Failure attribution",
    ):
        assert heading in text
    assert "| n |" in text or " n |" in text
    assert "0.67 [" in text
    assert "n=" in text


def test_markdown_renders_empty_run():
    text = "\n".join(render_summary_markdown(summarize([], program_of={}, z=Z)))
    assert "## Overall" in text
    assert " - |" in text


def test_load_program_map(tmp_path):
    path = tmp_path / "manifest.jsonl"
    path.write_text(
        json.dumps({"url": EI, "program": "ei"})
        + "\n\n"
        + json.dumps({"url": CPP, "program": "cpp", "title": "x"})
        + "\n",
        encoding="utf-8",
    )
    assert load_program_map(path) == {EI: "ei", CPP: "cpp"}
    assert load_program_map(tmp_path / "missing.jsonl") == {}
