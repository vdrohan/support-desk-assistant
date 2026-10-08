import json

import pytest

from support_desk_assistant.config import Settings
from support_desk_assistant.evals.retrieval import (
    DatasetError,
    EvalItem,
    answer_hit,
    breakdowns,
    build_report,
    first_relevant_rank,
    hit_rate,
    load_dataset,
    mean_reciprocal_rank,
    normalize,
    render_markdown,
    run_eval,
    score_analysis,
    validate_dataset,
    write_report,
)
from support_desk_assistant.retrieve import Hit

EI = "https://www.canada.ca/en/services/benefits/ei/eligibility.html"
EI2 = "https://www.canada.ca/en/services/benefits/ei/apply.html"
SIN = "https://www.canada.ca/en/employment-social-development/services/sin/apply.html"


def hit(url, text="", score=0.5, program="ei"):
    return Hit(score=score, program=program, title="T", heading_path=["H"], url=url, text=text)


def item(**kw):
    base = {
        "id": "ei-001",
        "question": "How many hours?",
        "category": "direct",
        "source": "generated",
        "expected_programs": ["ei"],
        "expected_urls": [EI],
        "key_facts": ["between 420 and 700 hours"],
    }
    return EvalItem(**(base | kw))


OOC = {"expected_programs": [], "expected_urls": [], "key_facts": [], "category": "out_of_corpus"}


# --- metrics ---------------------------------------------------------------------


def test_first_relevant_rank_and_cutoff():
    hits = [hit(EI2), hit(SIN), hit(EI), hit(EI)]
    assert first_relevant_rank(hits, [EI], k=5) == 3
    assert first_relevant_rank(hits, [EI], k=2) is None
    assert first_relevant_rank(hits, [SIN, EI], k=5) == 2  # any expected url counts


def test_hit_rate_and_mrr():
    ranks = [1, 3, None, 2]
    assert hit_rate(ranks, 1) == 0.25
    assert hit_rate(ranks, 5) == 0.75
    assert mean_reciprocal_rank(ranks) == pytest.approx((1 + 1 / 3 + 0 + 1 / 2) / 4)
    assert hit_rate([], 5) == 0.0 and mean_reciprocal_rank([]) == 0.0


def test_normalize():
    assert normalize("You’re  **NOT**\n eligible – sorry") == "you're not eligible - sorry"


def test_answer_hit_includes_heading_path():
    h = Hit(0.5, "sin", "Update", ["Your SIN stays the same unless your status changes"], SIN, "x")
    assert answer_hit([h], ["your sin stays the same"], [SIN], k=1)


def test_answer_hit_any_fact_any_expected_chunk_within_k():
    hits = [hit(EI2, "nothing"), hit(EI, "You need **between 420 and\n700 hours** of work")]
    assert answer_hit(hits, ["missing", "between 420 and 700 hours"], [EI], k=2)
    assert not answer_hit(hits, ["between 420 and 700 hours"], [EI], k=1)


def test_answer_hit_ignores_fact_on_unexpected_page():
    other = hit(EI2, "Sickness benefits: up to 26 weeks")  # right words, wrong benefit page
    assert not answer_hit([other], ["up to 26 weeks"], [EI], k=5)
    assert answer_hit([other, hit(EI, "up to 26 weeks")], ["up to 26 weeks"], [EI], k=5)


# --- schema / validator ----------------------------------------------------------


def test_schema_rules():
    item(**OOC, id="ooc-1")
    with pytest.raises(ValueError, match="out_of_corpus"):
        item(category="out_of_corpus")
    with pytest.raises(ValueError, match="answerable"):
        item(key_facts=[])
    with pytest.raises(ValueError, match="ambiguous"):
        item(category="ambiguous")
    with pytest.raises(ValueError):
        item(category="weird")
    item(
        id="gap-1", category="corpus_gap", expected_programs=["ei"], expected_urls=[], key_facts=[]
    )
    with pytest.raises(ValueError, match="corpus_gap"):
        item(category="corpus_gap", key_facts=[])  # still has urls
    with pytest.raises(ValueError, match="corpus_gap"):
        item(**(OOC | {"category": "corpus_gap"}))  # no programs


def test_load_dataset_reports_line_numbers(tmp_path):
    path = tmp_path / "ds.jsonl"
    path.write_text(item().model_dump_json() + "\n" + '{"id": "x"}\n')
    with pytest.raises(DatasetError, match="ds.jsonl:2"):
        load_dataset(path)


@pytest.fixture
def corpus(tmp_path):
    """Minimal manifest + processed pages + chunks.jsonl."""
    settings = Settings(_env_file=None, data_dir=tmp_path)
    rows = [
        {"url": EI, "program": "ei", "date_modified": None},
        {"url": SIN, "program": "sin", "date_modified": None},
    ]
    settings.manifest_path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    ei_text = "You need **between 420 and 700 hours**.\nLater scenario: mail it to the office."
    for program, slug, text in [
        ("ei", "services__benefits__ei__eligibility", ei_text),
        (
            "sin",
            "employment-social-development__services__sin__apply",
            "Bring a birth certificate.",
        ),
    ]:
        (tmp_path / "processed" / program).mkdir(parents=True, exist_ok=True)
        (tmp_path / "processed" / program / f"{slug}.md").write_text(text)
    chunks = [
        {"url": EI, "embed_text": "T > H\n\nYou need between 420 and 700 hours."},
        {"url": SIN, "embed_text": "T\n\nBring a birth certificate."},
    ]
    settings.chunks_path.write_text("".join(json.dumps(c) + "\n" for c in chunks))
    return settings


def test_validator_passes_on_valid_dataset(corpus):
    sin = item(
        id="sin-1", expected_programs=["sin"], expected_urls=[SIN], key_facts=["birth certificate"]
    )
    result = validate_dataset([item(), sin, item(**OOC, id="ooc-1")], corpus)
    assert result.ok, result.errors
    assert result.unreachable_facts == {}


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        ({"key_facts": ["between 420 and 7000 hours"]}, "not verbatim"),
        ({"expected_urls": [EI2]}, "not in manifest"),
        ({"expected_programs": ["sin"]}, "!= programs of expected_urls"),
        ({"expected_programs": ["nope"]}, "unknown programs"),
    ],
)
def test_validator_fails_loudly(corpus, bad, message):
    result = validate_dataset([item(**bad)], corpus)
    assert not result.ok
    assert any(message in e for e in result.errors), result.errors


def test_validator_duplicate_ids(corpus):
    assert any("duplicate id" in e for e in validate_dataset([item(), item()], corpus).errors)


def test_validator_warns_on_fact_unreachable_by_chunking(corpus):
    result = validate_dataset([item(key_facts=["mail it to the office"])], corpus)
    assert result.ok  # on the page: not an error
    assert result.unreachable_facts == {"ei-001": ["mail it to the office"]}
    assert any("unreachable" in w for w in result.warnings)


# --- runner ----------------------------------------------------------------------


class FakeSearch:
    """Unfiltered: SIN page first (wrong), EI second. Filtered to ei: EI first."""

    def __init__(self):
        self.calls = []

    def __call__(self, question, programs, k):
        self.calls.append((question, programs, k))
        ei = hit(EI, "between 420 and 700 hours", score=0.7)
        sin = hit(SIN, "birth certificate", score=0.8, program="sin")
        if programs:
            return [h for h in (ei, sin) if h.program in programs][:k]
        if "visa" in question:
            return [hit(EI2, "unrelated", score=0.41)]
        return [sin, ei][:k]


def test_run_eval_modes_and_metrics():
    gap = {"category": "corpus_gap", "expected_urls": [], "key_facts": []}
    items = [
        item(),
        item(id="ei-002", source="human", category="colloquial", key_facts=["not there"]),
        item(**OOC, id="ooc-1", question="Do I need a visa?"),
        item(**gap, id="gap-1", question="What does a passport visa cost?"),
    ]
    search = FakeSearch()
    results = run_eval(items, search, k=5)

    # unfiltered for all 4, oracle only for the 2 answerable; oracle filters by expected programs
    assert [(c[1]) for c in search.calls] == [None, ["ei"], None, ["ei"], None, None]
    m = breakdowns(results, 5)
    assert m["unfiltered"]["overall"] == {
        "n": 2, "page_hit@1": 0.0, "page_hit@5": 1.0, "mrr@5": 0.5, "answer_hit@5": 0.5,
    }  # fmt: skip
    assert m["oracle"]["overall"]["page_hit@1"] == 1.0
    assert m["unfiltered"]["source"]["human"]["n"] == 1
    assert m["unfiltered"]["category"]["out_of_corpus"] == {"n": 0}

    assert m["unfiltered"]["category"]["corpus_gap"] == {"n": 0}

    s = score_analysis(results)
    assert s["out_of_corpus_scores"] == {"ooc-1": 0.41}  # corpus gap not mixed in
    assert s["out_of_corpus"]["n"] == 1
    assert s["corpus_gap"]["gap-1"]["top1_score"] == 0.41
    assert s["threshold_at_max_ooc"]["answerable_refused"] == 0


def test_report_json_and_markdown(tmp_path, corpus):
    dataset = tmp_path / "retrieval_v1.jsonl"
    items = [item(), item(**OOC, id="ooc-1", question="Do I need a visa?")]
    dataset.write_text("".join(i.model_dump_json() + "\n" for i in items))
    results = run_eval(items, FakeSearch(), k=5)
    report = build_report(
        results,
        k=5,
        collection="test_v1",
        dataset_path=dataset,
        settings=corpus,
        validation=validate_dataset(items, corpus),
    )
    json_path, md_path = write_report(report, tmp_path / "results")
    assert json_path.name.startswith("test_v1_") and json_path.suffix == ".json"
    saved = json.loads(json_path.read_text())
    assert saved["meta"]["questions"] == 2 and saved["meta"]["k"] == 5
    assert [m["mode"] for m in saved["misses"]] == []  # page hit@5 and answer hit in both modes

    md = md_path.read_text()
    assert md == render_markdown(report)
    assert "| unfiltered | 1 | 0.00 | 1.00 | 0.50 | 1.00 |" in md
    assert "## By source (oracle)" in md
    assert "| out_of_corpus | 1 | 0.410" in md
