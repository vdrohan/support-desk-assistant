import json
import re

import anthropic
import httpx
import pytest

from fakes import FakeAnthropic, make_message
from support_desk_assistant.answer.cache import ResponseCache
from support_desk_assistant.answer.pipeline import (
    GATE_LOW_SCORE,
    GATE_PASS,
    STATUS_GATE,
    STATUS_MAX_TOKENS,
    STATUS_OK,
    STATUS_REFUSAL,
    gate_abstention,
)
from support_desk_assistant.answer.schema import AnswerResult
from support_desk_assistant.answer.validate import validate_answer
from support_desk_assistant.config import AnswerSettings, EvalSettings, Settings
from support_desk_assistant.evals.answers import (
    FactDecision,
    JudgeError,
    JudgeVerdict,
    PendingJudgeError,
    aggregate,
    apply_judge_verdicts,
    build_judge_request,
    cited_urls,
    content_words,
    decide_fact,
    decide_facts,
    estimate_judge_call,
    judge_cache_key,
    judge_schema,
    load_judge_prompt,
    numeric_tokens,
    parse_judge,
    pending_facts,
    render_judge_message,
    render_markdown,
    run_judge,
    score_item,
)
from support_desk_assistant.evals.draft_questions import Candidate
from support_desk_assistant.evals.retrieval import normalize
from support_desk_assistant.retrieve import Hit

S = EvalSettings()
A = "https://www.canada.ca/en/services/benefits/ei/ei-regular-benefit/benefit-amount.html"
B = "https://www.canada.ca/en/services/benefits/ei/ei-regular-benefit/eligibility.html"
D = "https://www.canada.ca/en/services/benefits/ei/ei-sickness/benefit-amount.html"
FACT_HOURS = "600 insured hours of work"
FACT_MAX = "maximum of $729 a week"
FACT_LONG = "Expect a decision by mail within 28 days from when we receive the application"
FACT_WORDS = "Apply for EI as soon as possible"


def decide(fact, answer, failed=False):
    return decide_fact(fact, answer, abstained_or_failed=failed, settings=S)


def hit(url, chunk_id, text="", score=0.6):
    return Hit(
        score=score, program="ei", title="T", heading_path=["H"], url=url, text=text,
        chunk_id=chunk_id,
    )  # fmt: skip


def cand(category="single_page", **kw):
    answerable = not category.startswith("unanswerable")
    base = {
        "id": f"{category}-1",
        "question": "How much EI could I get each week?",
        "category": category,
        "answerable": answerable,
        "key_facts": [FACT_MAX] if answerable else [],
        "gold_urls": [A] if answerable else [],
        "source_urls": [A] if answerable else [],
    }
    return Candidate(**(base | kw))


def answer(text="", citations=(), abstained=False, reason=None):
    return AnswerResult(
        answer=text, citations=list(citations), abstained=abstained, abstain_reason=reason
    )


GOOD = "You can get up to a maximum of $729 a week [a1]."
HITS = [
    hit(A, "a1", "You can receive a maximum of $729 a week."),
    hit(B, "b1", "You need 600 insured hours of work."),
    hit(D, "d1", "Sickness benefits pay up to $729 a week."),
]


def score(c, result, hits=HITS, gate=GATE_PASS, status=STATUS_OK, k=5):
    validation = validate_answer(result, [h.chunk_id for h in hits])
    return score_item(
        c,
        result=result,
        hits=hits,
        gate=gate,
        status=status,
        validation=validation,
        k=k,
        settings=S,
    )


# --- numeric tokens and content words --------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("$1,507.65", ["1507.65"]),
        ("1507.65", ["1507.65"]),
        ("$5,000 and $15,000", ["5000", "15000"]),
        ("increased by 2.0%", ["2%"]),
        ("5 percent", ["5%"]),
        ("you will get a letter around your 64th birthday", ["64"]),
        ("from 8:30 a.m. to 5:00 p.m.", ["8:30", "5"]),
        ("within 90 days.", ["90"]),
        ("in 2025, the threshold", ["2025"]),
    ],
)
def test_numeric_tokens_canonicalise(text, expected):
    assert [t.canonical for t in numeric_tokens(normalize(text))] == expected


def test_percent_is_not_a_bare_number():
    assert numeric_tokens("5%")[0].canonical != numeric_tokens("5")[0].canonical


def test_number_words_count_only_when_asked():
    assert numeric_tokens("every two weeks") == []
    assert [t.canonical for t in numeric_tokens("every two weeks", number_words=True)] == ["2"]
    assert numeric_tokens("someone often", number_words=True) == []


def test_content_words_skip_short_and_stop_words_and_fold_plurals():
    assert content_words(normalize(FACT_HOURS), S) == ["insured", "hour", "work"]
    assert content_words(normalize(FACT_MAX), S) == ["maximum", "week"]
    assert content_words(normalize("$1,507.65"), S) == []


# --- fact decisions on real facts ------------------------------------------------


def test_money_fact_with_no_content_words_is_decided_by_numbers():
    d = decide("$981.90", "You would repay $981.90 of your OAS.")
    assert (d.method, d.present) == ("numbers", True)
    d = decide("$981.90", "You would repay about $981.")
    assert (d.method, d.present, d.reason) == ("numbers", False, "missing: $981.90")


def test_short_digit_fact_present_when_numbers_and_words_match():
    d = decide(FACT_HOURS, "You need 600 insured hours of work in the last year.")
    assert (d.method, d.present) == ("numbers", True)
    d = decide(FACT_MAX, "Benefits are paid up to a maximum of $729 a week.")
    assert (d.method, d.present) == ("numbers", True)


def test_canonical_forms_match_across_fact_and_answer():
    assert decide("CPP benefits increased by 2.0% for 2026.", "Up 2% for 2026.").method == "judge"
    assert decide("increased by 2.0%", "It increased by 2%.").present is True
    assert decide("increased by 5%", "It increased by 5.").present is False
    assert decide("submit reports every 2 weeks", "Submit reports every two weeks.").present
    assert decide("until 5:00 p.m.", "Open until 5 p.m.").present is True


def test_paraphrased_content_word_goes_to_judge():
    d = decide(FACT_HOURS, "You need 600 hours of insurable employment.")
    assert (d.method, d.present) == ("judge", None)
    assert "insured" in d.reason


def test_long_digit_fact_goes_to_judge_even_when_verbatim():
    d = decide(FACT_LONG, f"{FACT_LONG}.")
    assert (d.method, d.present) == ("judge", None)
    assert d.pending


def test_no_digit_fact_goes_to_judge():
    d = decide(FACT_WORDS, "Apply for EI as soon as possible.")
    assert (d.method, d.present, d.reason) == ("judge", None, "no number")


@pytest.mark.parametrize("text, failed", [("$981.90", True), ("", False), ("  ", False)])
def test_abstained_failed_or_empty_answer_decides_every_fact_absent(text, failed):
    for fact in ("$981.90", FACT_LONG, FACT_WORDS):
        d = decide(fact, text, failed=failed)
        assert (d.method, d.present, d.judge_error) == ("none", False, False)


# --- wrong-context cases (maintainer requirement) --------------------------------


@pytest.mark.parametrize(
    "text",
    ["The minimum is $729 a week.", "There is a $729 fee you pay each week."],
)
def test_wrong_context_a_number_present_but_content_word_missing_goes_to_judge(text):
    d = decide(FACT_MAX, text)
    assert (d.method, d.present) == ("judge", None)
    assert d.reason == "content word missing: maximum"


def test_wrong_context_b_number_missing_is_absent_without_judge():
    decisions = [decide(FACT_HOURS, "You need 420 insured hours of work.")]
    assert (decisions[0].method, decisions[0].present) == ("numbers", False)
    assert decisions[0].reason == "missing: 600"
    assert pending_facts(decisions) == []


def test_wrong_context_c_long_fact_number_elsewhere_judge_says_absent():
    text = "A passport takes 28 days to print. Decisions on EI go out by mail."
    decisions = [decide(FACT_LONG, text)]
    assert (decisions[0].method, decisions[0].present) == ("judge", None)
    assert pending_facts(decisions) == [FACT_LONG]
    (resolved,) = apply_judge_verdicts(decisions, [JudgeVerdict(False, "28 days is about print")])
    assert (resolved.method, resolved.present, resolved.judge_error) == ("judge", False, False)
    assert resolved.reason == "28 days is about print"


def test_wrong_context_d_known_limitation_short_fact_auto_present_in_wrong_context():
    """KNOWN LIMITATION: a short digit fact whose number and every content word appear
    in the answer is marked present without the judge, even when the answer uses them in
    a different context. This is why the hand-review export has section B (numbers-gate
    facts marked present)."""
    text = "There is no maximum. A $729 fee is charged once a week."
    d = decide(FACT_MAX, text)
    assert (d.method, d.present) == ("numbers", True)


# --- judge verdicts --------------------------------------------------------------


def test_apply_judge_verdicts_fills_only_pending_facts_in_order():
    decisions = [
        decide(FACT_WORDS, "Apply soon."),
        decide("$981.90", "$981.90"),
        decide(FACT_LONG, "28 days"),
    ]
    out = apply_judge_verdicts(decisions, [JudgeVerdict(True, "ok"), JudgeVerdict(False, "no")])
    assert [d.present for d in out] == [True, True, False]
    assert out[1] == decisions[1]
    assert not any(d.pending for d in out)


def test_apply_judge_error_marks_pending_facts_absent_with_flag():
    decisions = [decide(FACT_WORDS, "Apply soon."), decide("$981.90", "$981.90")]
    out = apply_judge_verdicts(decisions, JudgeError("refusal"))
    assert (out[0].present, out[0].judge_error, out[0].reason) == (
        False,
        True,
        "judge error: refusal",
    )
    assert (out[1].present, out[1].judge_error) == (True, False)


def test_apply_judge_verdicts_rejects_wrong_count():
    with pytest.raises(ValueError):
        apply_judge_verdicts([decide(FACT_WORDS, "Apply soon.")], [])


# --- item scoring: answerable ----------------------------------------------------


def test_single_page_pass():
    s = score(cand(), answer(GOOD, ["a1"]))
    assert s.passed and s.all_facts and s.facts_fraction == 1.0
    assert (s.gold_cited, s.decoy_cited, s.page_hit, s.fact_in_context) == (True, None, True, True)
    assert s.cited_urls == (A,)
    assert s.attribution is None and s.abstained_by is None and s.answered_confidently is None


def test_time_sensitive_scored_like_single_page():
    s = score(cand("time_sensitive"), answer(GOOD, ["a1"]))
    assert s.passed and s.decoy_cited is None


def test_multi_page_needs_every_gold_page_cited_and_retrieved():
    c = cand("multi_page", key_facts=[FACT_MAX, FACT_HOURS], gold_urls=[A, B], source_urls=[A, B])
    text = "Up to a maximum of $729 a week [a1] once you have 600 insured hours of work [b1]."
    one = score(c, answer(text, ["a1"]))
    assert (one.gold_cited, one.page_hit, one.passed, one.attribution) == (
        False,
        True,
        False,
        "generation",
    )
    both = score(c, answer(text, ["a1", "b1"]))
    assert both.gold_cited and both.passed
    missing_page = score(c, answer(text, ["a1"]), hits=HITS[:1])
    assert (missing_page.page_hit, missing_page.attribution) == (False, "retrieval_page")


def test_confusable_gold_and_decoy_cited_are_separate():
    c = cand("confusable", source_urls=[A, D])
    gold_only = score(c, answer(GOOD, ["a1"]))
    assert (gold_only.gold_cited, gold_only.decoy_cited, gold_only.passed) == (True, False, True)
    both = score(c, answer(GOOD, ["a1", "d1"]))
    assert (both.gold_cited, both.decoy_cited, both.passed) == (True, True, False)
    decoy_only = score(c, answer(GOOD, ["d1"]))
    assert (decoy_only.gold_cited, decoy_only.decoy_cited) == (False, True)


@pytest.mark.parametrize(
    "gate, status, result, by",
    [
        (GATE_LOW_SCORE, STATUS_GATE, gate_abstention(), "gate"),
        (GATE_PASS, STATUS_OK, answer(abstained=True, reason="not covered"), "model"),
    ],
)
def test_false_abstention_fails_and_records_who_abstained(gate, status, result, by):
    s = score(cand(), result, gate=gate, status=status)
    assert s.abstained and s.abstained_by == by and not s.passed
    assert [d.method for d in s.facts] == ["none"]
    assert s.attribution == "generation"  # page and fact were retrieved


def test_category_false_abstention_rate_excludes_incomplete_generations():
    # A max_tokens fallback sets abstained=True but is a generation failure, not a false
    # abstention (decisions.md #23); only the gate/model abstention counts.
    truncated = score(
        cand(id="sp-1"),
        answer(abstained=True, reason="cut off"),
        status=STATUS_MAX_TOKENS,
    )
    declined = score(cand(id="sp-2"), answer(abstained=True, reason="not covered"))
    answered = score(cand(id="sp-3"), answer(GOOD, ["a1"]))
    sp = aggregate([truncated, declined, answered])["categories"]["single_page"]
    assert sp["false_abstention_rate"] == 0.333  # 1 of 3; the truncation is excluded


def test_failed_generation_decides_facts_none():
    s = score(cand(), answer(GOOD, ["a1"]), status=STATUS_REFUSAL)
    assert [d.method for d in s.facts] == ["none"] and not s.passed


def test_validator_failure_blocks_pass():
    s = score(cand(), answer(GOOD, ["zzz"]))
    assert not s.validator_ok and not s.passed


def test_attribution_retrieval_page_chunk_generation():
    wrong = answer("It depends [a1].", ["a1"])
    assert score(cand(), wrong, hits=[HITS[2]]).attribution == "retrieval_page"
    other_chunk = [hit(A, "a1", "How to apply."), HITS[2]]
    assert score(cand(), wrong, hits=other_chunk).attribution == "retrieval_chunk"
    assert score(cand(), wrong).attribution == "generation"


def test_page_hit_only_counts_top_k():
    hits = [HITS[2], HITS[1], HITS[0]]
    assert score(cand(), answer(GOOD, ["a1"]), hits=hits, k=2).page_hit is False
    assert score(cand(), answer(GOOD, ["a1"]), hits=hits, k=3).page_hit is True


def test_cited_urls_dedupe_and_skip_unknown_chunks():
    hits = [hit(A, "a1"), hit(A, "a2"), hit(B, "b1")]
    assert cited_urls(["a2", "x", "a1", "b1"], hits) == (A, B)


# --- item scoring: unanswerable --------------------------------------------------


def test_unanswerable_gate_abstention_passes():
    s = score(cand("unanswerable_out_of_scope"), gate_abstention(), gate=GATE_LOW_SCORE,
              status=STATUS_GATE)  # fmt: skip
    assert s.passed and s.abstained_by == "gate" and s.answered_confidently is False
    assert (s.facts_fraction, s.all_facts, s.gold_cited, s.decoy_cited) == (None,) * 4
    assert (s.page_hit, s.fact_in_context, s.attribution) == (None, None, None)


def test_unanswerable_model_abstention_passes():
    s = score(cand("unanswerable_in_domain"), answer(abstained=True, reason="not covered"))
    assert s.passed and s.abstained_by == "model"


def test_unanswerable_confident_answer_fails_even_if_it_says_not_covered():
    text = "The documents don't cover this [a1]."
    s = score(cand("unanswerable_in_domain"), answer(text, ["a1"]))
    assert not s.passed and s.answered_confidently is True and s.validator_ok


def test_unanswerable_abstention_with_bad_validator_fails():
    s = score(cand("unanswerable_out_of_scope"), answer("x", abstained=True, reason="r"))
    assert not s.validator_ok and not s.passed


def test_unanswerable_incomplete_generation_is_not_a_correct_abstention():
    """A refusal/max_tokens fallback abstention must not count as the model declining."""
    fallback = answer(abstained=True, reason="Answer generation did not complete (refusal).")
    s = score(cand("unanswerable_out_of_scope"), fallback, status=STATUS_REFUSAL)
    assert s.abstained and s.validator_ok and not s.passed


def test_answerable_item_without_facts_is_rejected():
    with pytest.raises(ValueError):
        score(cand(key_facts=[]), answer(GOOD, ["a1"]))


# --- pending judge facts ---------------------------------------------------------


def test_pending_judge_facts_block_pass_fail_until_resolved():
    c = cand(key_facts=[FACT_MAX, FACT_WORDS])
    s = score(c, answer(GOOD + " Apply soon.", ["a1"]))
    assert s.pending_judge
    for attr in ("passed", "all_facts", "facts_fraction", "attribution"):
        with pytest.raises(PendingJudgeError):
            getattr(s, attr)
    with pytest.raises(PendingJudgeError):
        s.to_dict()
    with pytest.raises(PendingJudgeError):
        aggregate([s])

    yes = s.with_judge_verdicts([JudgeVerdict(True, "stated")])
    assert yes.passed and yes.facts_fraction == 1.0
    no = s.with_judge_verdicts([JudgeVerdict(False, "not stated")])
    assert not no.passed and no.facts_fraction == 0.5 and no.attribution == "generation"
    err = s.with_judge_verdicts(JudgeError("max_tokens"))
    assert not err.passed and err.facts[1].judge_error


def test_to_dict_has_every_scored_field():
    row = score(cand(), answer(GOOD, ["a1"])).to_dict()
    for key in (
        "id", "category", "question", "answerable", "gate", "status", "abstained",
        "abstained_by", "validator_ok", "cited_urls", "gold_cited", "decoy_cited", "page_hit",
        "fact_in_context", "facts", "facts_fraction", "all_facts", "passed",
        "answered_confidently", "attribution",
    ):  # fmt: skip
        assert key in row
    assert row["facts"][0] == {
        "fact": FACT_MAX,
        "method": "numbers",
        "present": True,
        "reason": "numbers and content words present",
        "judge_error": False,
    }


# --- aggregation and markdown ----------------------------------------------------


def sample_scores():
    conf = cand("confusable", source_urls=[A, D])
    return [
        score(cand(id="sp-1"), answer(GOOD, ["a1"])),
        score(cand(id="sp-2"), answer("It depends [a1].", ["a1"]), hits=[HITS[2]]),
        score(cand("confusable", id="cf-1", source_urls=[A, D]), answer(GOOD, ["a1", "d1"])),
        score(conf, answer(GOOD, ["a1"])),
        score(
            cand("unanswerable_out_of_scope", id="oos-1"),
            gate_abstention(),
            gate=GATE_LOW_SCORE,
            status=STATUS_GATE,
        ),
        score(cand("unanswerable_in_domain", id="ind-1"), answer("It is $5 [a1].", ["a1"])),
    ]


def test_aggregate_per_category_with_n_and_no_overall():
    agg = aggregate(sample_scores(), costs_cad={"sp-1": 0.001, "sp-2": 0.002})
    cats = agg["categories"]
    assert set(cats) == {
        "single_page",
        "multi_page",
        "confusable",
        "unanswerable_out_of_scope",
        "unanswerable_in_domain",
        "time_sensitive",
    }
    assert not {"overall", "total", "all"} & set(agg)
    sp = cats["single_page"]
    assert (sp["n"], sp["pass_rate"], sp["decoy_cited_rate"], sp["cost_cad"]) == (
        2,
        0.5,
        None,
        0.003,
    )
    assert sp["attribution"] == {"retrieval_page": 1, "retrieval_chunk": 0, "generation": 0}
    # "It depends" lacks $729: decided absent by numbers, no judge.
    assert sp["fact_methods"] == {"numbers": 2, "judge": 0, "none": 0}
    cf = cats["confusable"]
    assert (cf["n"], cf["gold_cited_rate"], cf["decoy_cited_rate"], cf["pass_rate"]) == (
        2,
        1.0,
        0.5,
        0.5,
    )
    assert cats["multi_page"]["n"] == 0 and cats["multi_page"]["pass_rate"] is None
    oos, ind = cats["unanswerable_out_of_scope"], cats["unanswerable_in_domain"]
    assert (oos["n"], oos["correct_abstention_rate"], oos["abstained_by_gate"]) == (1, 1.0, 1)
    assert (ind["correct_abstention_rate"], ind["answered_confidently"]) == (0.0, 1)
    assert agg["groups"] == {
        "single_page": ["single_page"],
        "hard": ["multi_page", "confusable", "time_sensitive"],
        "abstention": ["unanswerable_out_of_scope", "unanswerable_in_domain"],
    }


def test_aggregate_counts_methods_and_judge_errors():
    c = cand(key_facts=[FACT_MAX, FACT_WORDS])
    s = score(c, answer(GOOD, ["a1"])).with_judge_verdicts(JudgeError("refusal"))
    m = aggregate([s])["categories"]["single_page"]
    assert m["fact_methods"] == {"numbers": 1, "judge": 1, "none": 0}
    assert m["judge_errors"] == 1


def table_rows(md):
    return [line for line in md.splitlines() if line.startswith("| ") and "---" not in line]


def test_markdown_tables_carry_n_and_separate_gold_and_decoy_columns():
    scores = sample_scores()
    md = render_markdown({"metrics": aggregate(scores), "items": [s.to_dict() for s in scores]})
    header = next(r for r in table_rows(md) if "gold cited" in r)
    cols = [c.strip() for c in header.strip("|").split("|")]
    assert cols[:2] == ["category", "n"]
    assert "gold cited" in cols and "decoy cited" in cols
    assert cols.index("decoy cited") != cols.index("gold cited")
    for row in table_rows(md):
        cells = [c.strip() for c in row.strip("|").split("|")]
        assert cells[1] == "n" or cells[1].isdigit(), row
    assert "| single_page | 2 |" in md
    sp_row = next(r for r in table_rows(md) if r.startswith("| single_page | 2 | 0.50"))
    assert sp_row.split("|")[8].strip() == "-"  # decoy cited: confusable only
    # Without breakdown metrics there is no Overall section; never an overall table row.
    assert "## Overall" not in md and "## Cost and tokens" not in md
    assert not any(re.match(r"\| (overall|total|all)\b", r) for r in table_rows(md))
    assert "# Answer eval" not in md  # no meta, no header


def test_markdown_empty_category_renders_zero_and_dashes():
    md = render_markdown({"metrics": aggregate([]), "items": []})
    row = next(r for r in table_rows(md) if r.startswith("| multi_page |"))
    cells = [c.strip() for c in row.strip("|").split("|")]
    assert cells[1] == "0" and set(cells[2:]) == {"-"}
    abst = next(r for r in table_rows(md) if r.startswith("| unanswerable_in_domain |"))
    assert set(c.strip() for c in abst.strip("|").split("|")[2:]) == {"-"}
    assert "None." in md


def test_markdown_failures_list_and_meta_header():
    c = cand(key_facts=[FACT_MAX, FACT_HOURS])
    s = score(c, answer(GOOD, ["a1"]))
    meta = {"collection": "canada_docs_v1", "answer_model": "claude-haiku-5-5", "k": 5}
    md = render_markdown({"meta": meta, "metrics": aggregate([s]), "items": [s.to_dict()]})
    assert md.startswith("# Answer eval: canada_docs_v1, claude-haiku-5-5")
    assert "**single_page-1** (single_page, generation)" in md
    assert f"missing (numbers): {FACT_HOURS}" in md


def test_fact_decision_is_frozen():
    d = FactDecision("f", "numbers", True, "r")
    with pytest.raises(AttributeError):
        d.present = False  # type: ignore[misc]


# --- Judge -------------------------------------------------------------------------

QUESTION = "How much EI could I get each week?"
FACT_DEPOSIT = "Payments are made by direct deposit"
# FACT_MAX is auto-present here; FACT_WORDS and FACT_DEPOSIT (no digits) go to the judge.
JUDGE_ANSWER = GOOD + " Apply as soon as you stop working."
JUDGE_FACTS = [FACT_MAX, FACT_WORDS, FACT_DEPOSIT]


@pytest.fixture
def jsettings(tmp_path):
    prompt = tmp_path / "judge.md"
    prompt.write_text("You check whether an answer states facts.\n")
    return Settings(data_dir=tmp_path, eval=EvalSettings(judge_prompt_path=prompt))


def judge_decisions(answer_text=JUDGE_ANSWER, failed=False):
    return decide_facts(JUDGE_FACTS, answer_text, abstained_or_failed=failed, settings=S)


def verdicts_json(*items):
    return json.dumps(
        {"verdicts": [{"fact_index": i, "present": p, "reason": r} for i, p, r in items]}
    )


def judge_reply(*items, **kw):
    return make_message(verdicts_json(*items), **kw)


def raw(text="", stop_reason="end_turn"):
    return make_message(text, stop_reason=stop_reason).to_dict()


def test_judge_message_numbers_facts_and_shows_no_chunks():
    msg = render_judge_message(QUESTION, JUDGE_ANSWER, [FACT_WORDS, FACT_LONG])
    assert msg.startswith(f"<question>\n{QUESTION}\n</question>\n<answer>\n{JUDGE_ANSWER}\n")
    assert msg.endswith(f"<facts>\n1. {FACT_WORDS}\n2. {FACT_LONG}\n</facts>")
    assert "<chunk" not in msg and "[a1]" in msg  # the answer text as written, nothing else


def test_judge_request_shape(jsettings):
    request = build_judge_request("SYS", "USER", jsettings)
    profile = jsettings.answer.models[jsettings.eval.judge_model]
    assert request["model"] == jsettings.eval.judge_model
    assert request["max_tokens"] == jsettings.eval.judge_max_tokens
    assert request["system"] == [{"type": "text", "text": "SYS"}]  # caching off by default
    assert request["messages"] == [{"role": "user", "content": "USER"}]
    assert request["thinking"] == {"type": profile.thinking}
    assert request["output_config"] == {
        "effort": profile.effort,
        "format": {"type": "json_schema", "schema": judge_schema()},
    }
    assert not {"temperature", "top_p", "top_k"} & request.keys()
    assert judge_schema()["properties"]["verdicts"]["type"] == "array"


def test_judge_schema_puts_reason_before_verdict():
    # Decision 25: the model writes fields in schema order, so the reason must come first.
    item = judge_schema()["$defs"]["JudgeItem"]
    assert list(item["properties"]) == ["fact_index", "reason", "present"]


def test_judge_request_cache_control_only_with_prompt_caching(jsettings):
    cached = jsettings.model_copy(update={"answer": AnswerSettings(prompt_caching=True)})
    block = build_judge_request("SYS", "USER", cached)["system"][0]
    assert block["cache_control"] == {"type": "ephemeral"}
    # cache_control doesn't change the output, so it isn't part of the key.
    assert judge_cache_key("SYS", "USER", cached) == judge_cache_key("SYS", "USER", jsettings)


def test_judge_cache_key_depends_on_inputs(jsettings):
    key = judge_cache_key("SYS", "USER", jsettings)
    assert key != judge_cache_key("SYS2", "USER", jsettings)
    assert key != judge_cache_key("SYS", "USER2", jsettings)
    version = jsettings.eval.judge_schema_version
    bumped = jsettings.model_copy(
        update={"eval": jsettings.eval.model_copy(update={"judge_schema_version": version + 1})}
    )
    assert key != judge_cache_key("SYS", "USER", bumped)


def test_parse_judge_ok_reorders_by_fact_index():
    parsed = parse_judge(raw(verdicts_json((2, False, "not stated"), (1, True, "stated"))), 2)
    assert parsed == [JudgeVerdict(True, "stated"), JudgeVerdict(False, "not stated")]


@pytest.mark.parametrize(
    ("items", "cause"),
    [
        (((1, True, "x"),), "missing fact_index: [2]"),
        (((1, True, "x"), (1, False, "y")), "duplicate fact_index: [1]"),
        (((1, True, "x"), (2, True, "y"), (3, True, "z")), "out of range: [3]"),
        (((0, True, "x"), (2, True, "y")), "out of range: [0]"),
    ],
)
def test_parse_judge_wrong_indices_is_an_error(items, cause):
    parsed = parse_judge(raw(verdicts_json(*items)), 2)
    assert isinstance(parsed, JudgeError) and cause in parsed.reason


@pytest.mark.parametrize(
    ("response", "cause"),
    [
        (raw("", "refusal"), "refusal"),
        (raw('{"verdicts": [', "max_tokens"), "max_tokens"),
        (raw("not json"), "invalid JSON"),
        (raw(""), "invalid JSON"),
        (raw('{"verdicts": [{"fact_index": 1}]}'), "schema"),
        ({}, "invalid JSON"),
    ],
)
def test_parse_judge_never_raises(response, cause):
    parsed = parse_judge(response, 1)
    assert isinstance(parsed, JudgeError) and cause in parsed.reason


def test_load_judge_prompt(jsettings):
    assert load_judge_prompt(jsettings).startswith("You check")


def test_run_judge_decides_pending_facts_and_caches(jsettings):
    decisions = judge_decisions()
    assert [d.method for d in decisions] == ["numbers", "judge", "judge"]
    client = FakeAnthropic(judge_reply((1, True, "says apply soon"), (2, False, "no deposit")))
    run = run_judge(QUESTION, JUDGE_ANSWER, decisions, settings=jsettings, system="SYS",
                    client=client)  # fmt: skip
    assert run.calls == 1 and not run.cache_hit and run.error is None
    assert [d.present for d in run.decisions] == [True, True, False]
    assert run.decisions[1].reason == "says apply soon"
    assert run.decisions[0] == decisions[0]  # numbers decision untouched
    assert run.usage.input_tokens == 1000 and run.cost_usd > 0
    assert run.cost_cad == pytest.approx(run.cost_usd * jsettings.answer.usd_cad)
    user = client.calls[0]["messages"][0]["content"]
    assert user.endswith(f"<facts>\n1. {FACT_WORDS}\n2. {FACT_DEPOSIT}\n</facts>")

    replay = FakeAnthropic()
    again = run_judge(QUESTION, JUDGE_ANSWER, decisions, settings=jsettings, system="SYS",
                      client=replay)  # fmt: skip
    assert replay.calls == [] and again.calls == 0 and again.cache_hit
    assert again.cost_usd == 0 and again.cost_cad == 0 and again.usage.input_tokens == 0
    assert again.decisions == run.decisions


@pytest.mark.parametrize(
    "decisions",
    [
        judge_decisions(failed=True),  # abstained / failed generation: method "none"
        judge_decisions(answer_text=""),  # empty answer
        decide_facts([FACT_MAX, "$981.90"], GOOD, abstained_or_failed=False, settings=S),
        decide_facts(["$981.90"], GOOD, abstained_or_failed=False, settings=S),  # absent
    ],
)
def test_run_judge_without_pending_facts_makes_no_call(jsettings, decisions):
    client = FakeAnthropic()
    run = run_judge(QUESTION, GOOD, decisions, settings=jsettings, system="SYS", client=client)
    assert client.calls == [] and run.calls == 0 and not run.cache_hit
    assert run.decisions == tuple(decisions) and run.cost_cad == 0 and run.error is None
    assert not any(jsettings.eval_cache_dir.glob("*.json"))


def test_run_judge_api_error_propagates_and_is_not_cached(jsettings):
    error = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))
    with pytest.raises(anthropic.APIConnectionError):
        run_judge(QUESTION, JUDGE_ANSWER, judge_decisions(), settings=jsettings, system="SYS",
                  client=FakeAnthropic(error))  # fmt: skip
    assert not any(jsettings.eval_cache_dir.glob("*.json"))
    # The next run calls the API again.
    client = FakeAnthropic(judge_reply((1, True, "a"), (2, True, "b")))
    run = run_judge(QUESTION, JUDGE_ANSWER, judge_decisions(), settings=jsettings, system="SYS",
                    client=client)  # fmt: skip
    assert run.calls == 1 and len(client.calls) == 1


@pytest.mark.parametrize(
    ("reply", "cause"),
    [
        (make_message("", stop_reason="refusal"), "refusal"),
        (make_message('{"verdicts": [', stop_reason="max_tokens"), "max_tokens"),
        (make_message("nope"), "invalid JSON"),
        (judge_reply((1, True, "a")), "missing fact_index"),
    ],
)
def test_run_judge_error_marks_pending_facts_absent_and_is_cached(jsettings, reply, cause):
    decisions = judge_decisions()
    run = run_judge(QUESTION, JUDGE_ANSWER, decisions, settings=jsettings, system="SYS",
                    client=FakeAnthropic(reply))  # fmt: skip
    assert run.calls == 1 and cause in run.error
    assert run.decisions[0] == decisions[0]
    for d in run.decisions[1:]:
        assert d.present is False and d.judge_error and d.reason.startswith("judge error:")
    # A completed (if unusable) response is cached: no re-spend on the re-run.
    replay = FakeAnthropic()
    again = run_judge(QUESTION, JUDGE_ANSWER, decisions, settings=jsettings, system="SYS",
                      client=replay)  # fmt: skip
    assert replay.calls == [] and again.cache_hit and again.decisions == run.decisions


def test_estimate_judge_call_known_answer(jsettings):
    cache = ResponseCache(jsettings.eval_cache_dir)
    decisions = judge_decisions()
    calls, usd = estimate_judge_call(QUESTION, JUDGE_ANSWER, decisions, "SYS", jsettings, cache)
    assert calls == 1
    profile = jsettings.answer.models[jsettings.eval.judge_model]
    assert usd >= jsettings.eval.judge_max_tokens * profile.output_usd_per_mtok / 1e6

    run_judge(QUESTION, JUDGE_ANSWER, decisions, settings=jsettings, system="SYS",
              client=FakeAnthropic(judge_reply((1, True, "a"), (2, True, "b"))))  # fmt: skip
    assert estimate_judge_call(QUESTION, JUDGE_ANSWER, decisions, "SYS", jsettings, cache) == (
        0,
        0.0,
    )
    nothing_pending = judge_decisions(failed=True)
    assert estimate_judge_call(
        QUESTION, JUDGE_ANSWER, nothing_pending, "SYS", jsettings, cache
    ) == (0, 0.0)


def test_estimate_judge_call_unknown_answer_is_one_call_upper_bound(jsettings):
    cache = ResponseCache(jsettings.eval_cache_dir)
    calls, usd = estimate_judge_call(
        QUESTION, None, None, "SYS", jsettings, cache, facts=JUDGE_FACTS
    )
    known_calls, known_usd = estimate_judge_call(
        QUESTION, JUDGE_ANSWER, judge_decisions(), "SYS", jsettings, cache
    )
    assert calls == known_calls == 1
    assert usd > known_usd  # the unknown answer is bounded by the answer model's max_tokens
