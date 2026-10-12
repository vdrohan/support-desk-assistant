"""Question drafting: sampling, request building, parsing and the auto-checks.
No network, no key: Anthropic is a fake client and the corpus is written to tmp_path."""

import json

import anthropic
import httpx
import pytest

from fakes import FakeAnthropic, make_message, write_corpus
from support_desk_assistant.config import EvalSettings, Settings
from support_desk_assistant.evals import draft_questions as dq
from support_desk_assistant.evals.draft_questions import (
    CATEGORIES,
    Candidate,
    DraftedQuestion,
    Task,
    assemble,
    build_request,
    check_candidate,
    cosine,
    eligible_pages,
    estimate,
    existing_questions,
    jaccard,
    load_candidates,
    load_pages,
    near_duplicates,
    parse_response,
    plan_tasks,
    render_user_message,
    request_key,
    run_tasks,
    strip_header,
    tfidf_vectors,
    write_candidates,
)

BASE = "https://www.canada.ca/en/services/benefits"
EI_SICK = f"{BASE}/ei/ei-sickness.html"
EI_REG = f"{BASE}/ei/ei-regular-benefit.html"
EI_ELIG = f"{BASE}/ei/ei-regular-benefit/eligibility.html"
CPP_DIS = f"{BASE}/publicpensions/cpp/cpp-disability-benefit.html"
OAS = f"{BASE}/publicpensions/old-age-security/payments.html"
SIN = "https://www.canada.ca/en/employment-social-development/services/sin/apply.html"
PASS = "https://www.canada.ca/en/immigration-refugees-citizenship/services/passports/fees.html"
SHORT = f"{BASE}/ei/short.html"
LONG = f"{BASE}/ei/long.html"
BROKEN = f"{BASE}/ei/broken.html"


def body(topic: str, extra: str = "", repeats: int = 30) -> str:
    return (f"The {topic} rules apply to your situation. " * repeats) + "\n\n" + extra


PAGES = [
    (
        "ei",
        EI_SICK,
        "EI sickness benefits",
        body(
            "sickness illness injury medical certificate benefit",
            "You can get up to 26 weeks. The maximum is $729 a week.",
        ),
        "2026-09-01",
    ),
    (
        "ei",
        EI_REG,
        "EI regular benefits",
        body("regular job loss record employment", "Apply as soon as you stop working."),
        "2026-09-02",
    ),
    (
        "ei",
        EI_ELIG,
        "EI eligibility",
        body("eligibility hours insurable employment", "You need between 420 and 700 hours."),
        "2026-09-03",
    ),
    (
        "cpp",
        CPP_DIS,
        "CPP disability benefit",
        body(
            "sickness illness injury medical disability benefit", "The monthly amount is $1,606.78."
        ),
        "2026-09-04",
    ),
    (
        "oas",
        OAS,
        "OAS payments",
        body("old age pension payment date residence", "Payments are made monthly."),
        "2026-09-05",
    ),
    (
        "sin",
        SIN,
        "Apply for a SIN",
        body("social insurance number documents identity", "Bring original documents."),
        "2026-09-06",
    ),
    (
        "passport",
        PASS,
        "Passport fees",
        body("passport fee price cost renewal", "An adult passport costs $120."),
        "2026-09-07",
    ),
    ("ei", SHORT, "Short page", "Only a few words here.", "2026-09-08"),
    ("ei", LONG, "Long page", "word " * 3500, "2026-09-09"),
    (
        "ei",
        BROKEN,
        "Broken fees",
        body("fees", "[Error loading fee. Check the fee list.]"),
        "2026-09-10",
    ),
]


@pytest.fixture
def settings(tmp_path):
    write_corpus(tmp_path, PAGES)
    (tmp_path / "draft.md").write_text("You draft questions.\n")
    return Settings(
        data_dir=tmp_path,
        eval=EvalSettings(draft_prompt_path=tmp_path / "draft.md"),
    )


@pytest.fixture
def pages(settings):
    return load_pages(settings)


def candidate(**kw):
    base = {
        "id": "single_page-01",
        "question": "How many hours do I need before I can claim?",
        "category": "single_page",
        "answerable": True,
        "key_facts": ["between 420 and 700 hours"],
        "gold_urls": [EI_ELIG],
        "source_urls": [EI_ELIG],
        "notes": "",
    }
    return Candidate(**(base | kw))


def drafted(**kw):
    base = {
        "question": "How many hours do I need?",
        "key_facts": ["420 and 700"],
        "gold_urls": [],
        "notes": "tests hours",
    }
    return DraftedQuestion(**(base | kw))


def reply(*questions):
    return json.dumps({"questions": [q.model_dump() for q in questions]})


# --- corpus loading -------------------------------------------------------------


def test_strip_header_removes_source_and_date_lines():
    text = "Source: https://x\nDate modified: 2026-01-01\n\n# Title\n\nBody."
    assert strip_header(text) == "# Title\n\nBody."


def test_load_pages_reads_manifest_order_and_skips_missing_files(settings, tmp_path):
    (tmp_path / "manifest.jsonl").open("a").write(
        json.dumps({"url": "https://x/missing.html", "title": "m", "program": "ei"}) + "\n"
    )
    pages = load_pages(settings)
    assert list(pages)[0] == EI_SICK and "https://x/missing.html" not in pages
    page = pages[EI_ELIG]
    assert page.program == "ei" and page.date_modified == "2026-09-03"
    assert page.words == len(page.text.split()) and "between 420 and 700 hours" in page.norm
    assert not page.text.startswith("Source:")


def test_eligible_pages_filters_length_and_loading_placeholders(settings, pages):
    urls = {p.url for p in eligible_pages(pages.values(), settings)}
    assert SHORT not in urls and LONG not in urls and BROKEN not in urls
    assert {EI_SICK, EI_ELIG, CPP_DIS, PASS} <= urls


# --- sampling ---------------------------------------------------------------------


def plan(settings, pages, mix, seed=1):
    return plan_tasks(list(pages.values()), mix, seed, settings)


def test_plan_matches_the_mix_and_is_deterministic(settings, pages):
    mix = {
        "single_page": 3,
        "multi_page": 1,
        "confusable": 1,
        "time_sensitive": 1,
        "unanswerable_in_domain": 1,
        "unanswerable_out_of_scope": 2,
    }
    tasks = plan(settings, pages, mix)
    counts = {}
    for t in tasks:
        counts[t.category] = counts.get(t.category, 0) + t.count
    assert counts == mix
    assert tasks == plan(settings, pages, mix)  # same seed, same plan
    assert [t.category for t in tasks] == sorted(
        (t.category for t in tasks), key=dq.CATEGORIES.index
    )


def test_single_page_round_robins_across_programs(settings, pages):
    tasks = plan(settings, pages, {"single_page": 5})
    assert {t.pages[0].program for t in tasks} == {"ei", "cpp", "oas", "sin", "passport"}


def test_pages_are_not_reused_while_the_pool_has_fresh_ones(settings, pages):
    mix = {"single_page": 3, "multi_page": 1, "confusable": 1}
    urls = [p.url for t in plan(settings, pages, mix) for p in t.pages]
    assert len(urls) == len(set(urls))


def test_ineligible_pages_are_never_sampled(settings, pages):
    for seed in range(5):
        urls = {p.url for t in plan(settings, pages, {"single_page": 6}, seed) for p in t.pages}
        assert not urls & {SHORT, LONG, BROKEN}


def test_multi_page_pairs_are_related_pages_of_one_program(settings, pages):
    (task,) = plan(settings, pages, {"multi_page": 1})
    a, b = task.pages
    assert a.program == b.program and dq.related(a, b)
    assert dq.related(pages[EI_REG], pages[EI_ELIG])  # parent / child
    assert dq.related(pages[EI_SICK], pages[EI_REG])  # siblings
    assert not dq.related(pages[EI_SICK], pages[CPP_DIS])


def test_confusable_pairs_cross_programs_and_similar_pages_score_higher(settings, pages):
    (task,) = plan(settings, pages, {"confusable": 1})
    assert task.pages[0].program != task.pages[1].program
    eligible = eligible_pages(pages.values(), settings)
    vec = tfidf_vectors(eligible)
    assert cosine(vec[EI_SICK], vec[CPP_DIS]) > cosine(vec[EI_SICK], vec[PASS])


def test_time_sensitive_only_picks_pages_with_amounts(settings, pages):
    for seed in range(5):
        for t in plan(settings, pages, {"time_sensitive": 2}, seed):
            assert "$" in t.pages[0].text


def test_out_of_scope_is_one_pageless_task(settings, pages):
    (task,) = plan(settings, pages, {"unanswerable_out_of_scope": 3})
    assert task == Task("unanswerable_out_of_scope", 3, ())


def test_unknown_category_in_the_mix_is_an_error(settings, pages):
    with pytest.raises(ValueError, match="nope"):
        plan(settings, pages, {"nope": 1})


# --- request ----------------------------------------------------------------------


def test_user_message_carries_category_count_and_page_text(settings, pages):
    msg = render_user_message(Task("single_page", 1, (pages[EI_ELIG],)), pages.values())
    assert "<category>single_page</category>" in msg and "<count>1</count>" in msg
    assert f'url="{EI_ELIG}"' in msg and "between 420 and 700 hours" in msg
    assert "Source:" not in msg  # header lines are attributes, not text


def test_out_of_scope_message_lists_titles_not_pages(settings, pages):
    msg = render_user_message(Task("unanswerable_out_of_scope", 2), pages.values())
    assert "<titles>" in msg and "ei: EI sickness benefits" in msg
    assert "<pages>" not in msg and "$729" not in msg


def test_request_uses_structured_output_and_no_sampling_params(settings):
    req = build_request("sys", "user", settings)
    assert (
        req["model"] == "claude-haiku-5-5" and req["max_tokens"] == settings.eval.draft_max_tokens
    )
    assert req["thinking"] == {"type": "disabled"}
    assert req["output_config"]["format"]["type"] == "json_schema"
    assert not {"temperature", "top_p", "top_k"} & set(req)
    assert "questions" in req["output_config"]["format"]["schema"]["properties"]


def test_request_key_depends_on_prompt_and_message(settings):
    assert request_key("a", "u", settings) == request_key("a", "u", settings)
    assert request_key("a", "u", settings) != request_key("b", "u", settings)
    assert request_key("a", "u", settings) != request_key("a", "v", settings)


def test_parse_response_statuses():
    ok = {"stop_reason": "end_turn", "content": [{"type": "text", "text": reply(drafted())}]}
    parsed, status = parse_response(ok)
    assert status == "ok" and parsed.questions[0].question == "How many hours do I need?"
    assert parse_response({"stop_reason": "refusal"}) == (None, "refusal")
    assert parse_response({"stop_reason": "max_tokens", "content": []}) == (None, "max_tokens")
    bad = {"stop_reason": "end_turn", "content": [{"type": "text", "text": "{nope"}]}
    assert parse_response(bad) == (None, "invalid_output")


# --- assembling and checking -------------------------------------------------------


def test_assemble_derives_gold_urls_by_category(pages):
    one = Task("single_page", 1, (pages[EI_ELIG],))
    assert assemble(one, drafted(gold_urls=["x"]), "single_page-01").gold_urls == [EI_ELIG]
    two = Task("multi_page", 1, (pages[EI_REG], pages[EI_ELIG]))
    assert assemble(two, drafted(), "multi_page-01").gold_urls == [EI_REG, EI_ELIG]
    pair = Task("confusable", 1, (pages[EI_SICK], pages[CPP_DIS]))
    c = assemble(pair, drafted(gold_urls=[CPP_DIS]), "confusable-01")
    assert c.gold_urls == [CPP_DIS] and c.source_urls == [EI_SICK, CPP_DIS]
    un = Task("unanswerable_in_domain", 1, (pages[EI_ELIG],))
    c = assemble(un, drafted(key_facts=[]), "unanswerable_in_domain-01")
    assert c.gold_urls == [] and c.answerable is False and c.source_urls == [EI_ELIG]


def test_time_sensitive_notes_record_the_page_date(pages):
    task = Task("time_sensitive", 1, (pages[EI_SICK],))
    c = assemble(task, drafted(key_facts=["$729 a week"], notes="tests the maximum"), "t-01")
    assert "date_modified 2026-09-01" in c.notes and "crawled 2026-10-06" in c.notes


def test_check_candidate_accepts_a_grounded_item(pages):
    assert check_candidate(candidate(), pages) == []


def test_key_fact_match_ignores_case_whitespace_and_curly_quotes(pages):
    c = candidate(key_facts=["BETWEEN 420  and 700 hours"])
    assert check_candidate(c, pages) == []


@pytest.mark.parametrize(
    ("changes", "fragment"),
    [
        ({"key_facts": ["exactly 999 hours"]}, "not verbatim"),
        ({"key_facts": []}, "needs 1-3 key_facts"),
        ({"key_facts": ["a", "b", "c", "d"]}, "needs 1-3 key_facts"),
        ({"gold_urls": ["https://x/nope.html"], "source_urls": []}, "gold_url not in the corpus"),
        ({"gold_urls": [EI_REG]}, "not among the source pages"),
        ({"answerable": False}, "contradicts category"),
        ({"question": "Hours?"}, "too short"),
        ({"source_urls": ["https://x/nope.html"]}, "source_url not in the corpus"),
    ],
)
def test_check_candidate_reports_each_problem(pages, changes, fragment):
    assert any(fragment in p for p in check_candidate(candidate(**changes), pages))


def test_unanswerable_items_must_be_empty(pages):
    c = candidate(
        category="unanswerable_in_domain", answerable=False, key_facts=["x"], gold_urls=[EI_ELIG]
    )
    problems = check_candidate(c, pages)
    assert "unanswerable items must have no key_facts" in problems
    assert "unanswerable items must have no gold_urls" in problems
    out = candidate(
        category="unanswerable_out_of_scope", answerable=False, key_facts=[], gold_urls=[]
    )
    assert any("no source_urls" in p for p in check_candidate(out, pages))
    ok = out.model_copy(update={"source_urls": []})
    assert check_candidate(ok, pages) == []


def test_multi_page_needs_a_key_fact_from_every_gold_page(pages):
    both = candidate(
        category="multi_page",
        key_facts=["between 420 and 700 hours", "as soon as you stop working"],
        gold_urls=[EI_ELIG, EI_REG],
        source_urls=[EI_ELIG, EI_REG],
    )
    assert check_candidate(both, pages) == []
    one_sided = both.model_copy(update={"key_facts": ["between 420 and 700 hours"]})
    assert any("no key fact comes from gold page" in p for p in check_candidate(one_sided, pages))


def test_confusable_gold_must_be_one_of_the_sources(pages):
    c = candidate(
        category="confusable",
        key_facts=["The monthly amount is $1,606.78"],
        gold_urls=[CPP_DIS],
        source_urls=[EI_SICK, CPP_DIS],
    )
    assert check_candidate(c, pages) == []
    wrong = c.model_copy(update={"gold_urls": [OAS]})
    assert any("not among the source pages" in p for p in check_candidate(wrong, pages))


def test_near_duplicates_within_and_against_existing():
    qs = {
        "a": "How many hours do I need for EI?",
        "b": "How many hours do I need for EI",
        "c": "Passport fee?",
    }
    found = near_duplicates(
        qs, {"retrieval_v1.jsonl:human-01": "How many hours do I need for EI?"}, 0.7
    )
    assert found["a"] == ["b", "retrieval_v1.jsonl:human-01"] and "c" not in found
    assert jaccard("one two", "one two") == 1.0 and jaccard("", "") == 0.0


def test_existing_questions_reads_both_files_namespaced_and_skips_missing(settings):
    # Neither file exists yet: nothing to dedup against, and no error.
    assert existing_questions(settings) == {}

    settings.eval_dir.mkdir(parents=True, exist_ok=True)
    retrieval = settings.eval_dir / settings.eval.retrieval_dataset
    retrieval.write_text(
        json.dumps({"id": "human-01", "question": "How many hours do I need?"})
        + "\n\n"  # blank lines are ignored
        + json.dumps({"id": "human-02", "question": "When is my OAS paid?"})
        + "\n"
    )
    # The same id in the answer eval set must not collide with the retrieval dataset's.
    settings.eval_set_path.write_text(
        json.dumps({"id": "human-01", "question": "What does a passport cost?"}) + "\n"
    )

    assert existing_questions(settings) == {
        f"{settings.eval.retrieval_dataset}:human-01": "How many hours do I need?",
        f"{settings.eval.retrieval_dataset}:human-02": "When is my OAS paid?",
        f"{settings.eval.eval_set_file}:human-01": "What does a passport cost?",
    }


def test_default_draft_settings_agree_with_the_code_that_reads_them():
    cfg = Settings()
    mix = cfg.eval.draft_mix
    # plan_tasks raises on an unknown category, so a typo here would only show up mid-run.
    assert set(mix) <= set(CATEGORIES)
    assert all(n > 0 for n in mix.values())
    assert sum(mix.values()) == 40  # the size the config comment promises
    # build_request takes prices and the request profile from AnswerSettings.models.
    assert cfg.eval.draft_model in cfg.answer.models
    assert cfg.eval.draft_min_page_words < cfg.eval.draft_max_page_words
    assert 0 < cfg.eval.duplicate_jaccard <= 1


# --- running ------------------------------------------------------------------------


def two_tasks(pages):
    return [
        Task("single_page", 1, (pages[EI_ELIG],)),
        Task("unanswerable_out_of_scope", 2),
    ]


def test_run_tasks_assembles_ids_and_replays_from_cache(settings, pages):
    system = "You draft."
    tasks = two_tasks(pages)
    client = FakeAnthropic(
        make_message(reply(drafted())),
        make_message(
            reply(
                drafted(question="Can I get an Ontario Works cheque?", key_facts=[]),
                drafted(question="How do I pay provincial income tax?", key_facts=[]),
            )
        ),
    )
    run = run_tasks(tasks, list(pages.values()), system=system, settings=settings, client=client)
    assert [c.id for c in run.candidates] == [
        "single_page-01",
        "unanswerable_out_of_scope-01",
        "unanswerable_out_of_scope-02",
    ]
    assert run.calls == 2 and run.cache_hits == 0 and run.failures == []
    assert run.usage.input_tokens == 2000 and len(client.calls) == 2

    again = run_tasks(
        tasks, list(pages.values()), system=system, settings=settings, client=FakeAnthropic()
    )
    assert again.calls == 0 and again.cache_hits == 2
    assert [c.question for c in again.candidates] == [c.question for c in run.candidates]
    assert estimate(tasks, list(pages.values()), system, settings) == (0, 0.0)


def test_estimate_counts_only_uncached_tasks(settings, pages):
    calls, usd = estimate(two_tasks(pages), list(pages.values()), "You draft.", settings)
    assert calls == 2 and usd > 0


def test_failed_generations_and_shortfalls_are_reported_not_hidden(settings, pages):
    client = FakeAnthropic(
        make_message("", stop_reason="refusal"),
        make_message(reply(drafted(question="Can I get an Ontario Works cheque?", key_facts=[]))),
    )
    run = run_tasks(
        two_tasks(pages), list(pages.values()), system="s", settings=settings, client=client
    )
    assert run.failures == ["single_page: refusal"]
    assert run.shortfalls == ["unanswerable_out_of_scope: asked for 2, got 1"]
    assert len(run.candidates) == 1


def test_api_error_propagates_and_completed_calls_stay_cached(settings, pages):
    error = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))
    client = FakeAnthropic(make_message(reply(drafted())), error)
    with pytest.raises(anthropic.APIConnectionError):
        run_tasks(
            two_tasks(pages), list(pages.values()), system="s", settings=settings, client=client
        )
    calls, _ = estimate(two_tasks(pages), list(pages.values()), "s", settings)
    assert calls == 1


def test_candidates_round_trip_in_category_order(tmp_path):
    items = [
        candidate(id="time_sensitive-01", category="time_sensitive"),
        candidate(id="single_page-02"),
        candidate(id="single_page-01"),
    ]
    path = tmp_path / "c.jsonl"
    write_candidates(path, items)
    assert [c.id for c in load_candidates(path)] == [
        "single_page-01",
        "single_page-02",
        "time_sensitive-01",
    ]


# --- CLI ----------------------------------------------------------------------------


@pytest.fixture
def cli(settings, monkeypatch, tmp_path):
    settings.eval.draft_mix = {"single_page": 1, "unanswerable_out_of_scope": 1}
    monkeypatch.setattr(dq, "get_settings", lambda: settings)
    return settings


def test_dry_run_lists_the_sample_and_makes_no_call(cli, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(dq, "make_anthropic_client", lambda s: pytest.fail("API used"))
    out = tmp_path / "cand.jsonl"
    assert dq.main(["--dry-run", "--out", str(out)]) == 0
    text = capsys.readouterr().out
    assert "2 drafting calls" in text and "single_page:" in text and not out.exists()


def test_full_run_writes_candidates_and_refuses_to_overwrite(cli, tmp_path, capsys, monkeypatch):
    client = FakeAnthropic(
        make_message(reply(drafted(key_facts=["Payments are made monthly"]))),
        make_message(reply(drafted(question="Can I get an Ontario Works cheque?", key_facts=[]))),
    )
    monkeypatch.setattr(dq, "make_anthropic_client", lambda s: client)
    out = tmp_path / "cand.jsonl"
    assert dq.main(["--yes", "--out", str(out)]) == 0
    assert len(load_candidates(out)) == 2
    text = capsys.readouterr().out
    assert "Actual cost: CA$" in text and "single_page" in text
    assert dq.main(["--yes", "--out", str(out)]) == 2  # exists, no --force
    assert "use --force" in capsys.readouterr().out


def test_declining_the_confirmation_spends_nothing(cli, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(dq, "make_anthropic_client", lambda s: pytest.fail("API used"))
    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert dq.main(["--out", str(tmp_path / "cand.jsonl")]) == 1
    assert "nothing spent" in capsys.readouterr().out
