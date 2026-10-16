"""Answer-eval runner end to end: in-memory Qdrant, fake Voyage, fake Anthropic. No network."""

import json
import random
import re

import anthropic
import httpx
import pytest
from qdrant_client import QdrantClient

from fakes import DIM, FakeVoyage, make_message, word_count, write_corpus
from support_desk_assistant.config import (
    AnswerSettings,
    ChunkSettings,
    EmbedSettings,
    EvalSettings,
    IndexSettings,
    Settings,
)
from support_desk_assistant.evals import answers, provenance
from support_desk_assistant.evals.answers import (
    main,
    pick_gate_facts,
    pick_judged_facts,
    render_judge_review,
    select_items,
)
from support_desk_assistant.evals.draft_questions import CATEGORIES, Candidate
from support_desk_assistant.ingest.chunk import chunk_page
from support_desk_assistant.ingest.embed import Embedder, EmbeddingCache
from support_desk_assistant.ingest.index import sync

A = "https://www.canada.ca/en/services/benefits/ei/ei-regular-benefit/benefit-amount.html"
B = "https://www.canada.ca/en/services/benefits/ei/ei-regular-benefit/eligibility.html"
D = "https://www.canada.ca/en/services/benefits/ei/ei-sickness/benefit-amount.html"
PAGES = [
    (
        "ei",
        A,
        "EI regular benefits: How much you could receive",
        "## Weekly amount\n\nYou can receive 55% of your average weekly insurable earnings, "
        "up to a maximum of $729 a week.",
        "2026-01-05",
    ),
    (
        "ei",
        B,
        "EI regular benefits: Eligibility",
        "## Insured hours\n\nYou need 600 insured hours of work in the last 52 weeks.\n\n"
        "## When to apply\n\nApply for EI as soon as possible after you stop working, "
        "even if you have not received your record of employment.",
        "2026-01-05",
    ),
    (
        "ei",
        D,
        "EI sickness benefits: How much you could receive",
        "## Weekly amount\n\nSickness benefits pay 55% of your earnings, "
        "up to a maximum of $729 a week.",
        "2026-01-05",
    ),
]
FACT_MAX = "maximum of $729 a week"
FACT_APPLY = "Apply for EI as soon as possible"
Q_MAX = "What is the maximum EI regular benefit I could receive each week?"
Q_APPLY = "When should I apply for EI after I stop working?"
Q_SICK = "What is the maximum weekly amount for EI sickness benefits?"
Q_OOS = "Xylophone?"
Q_IND = "Can I get EI regular benefits while I travel abroad on vacation?"
Q_SP3 = "How many insured hours of work do I need for EI?"

ITEMS = [
    Candidate(id="sp-1", question=Q_MAX, category="single_page", answerable=True,
              key_facts=[FACT_MAX], gold_urls=[A], source_urls=[A]),
    Candidate(id="sp-2", question=Q_APPLY, category="single_page", answerable=True,
              key_facts=[FACT_APPLY], gold_urls=[B], source_urls=[B]),
    Candidate(id="cf-1", question=Q_SICK, category="confusable", answerable=True,
              key_facts=[FACT_MAX], gold_urls=[D], source_urls=[A, D]),
    Candidate(id="oos-1", question=Q_OOS, category="unanswerable_out_of_scope",
              answerable=False, key_facts=[], gold_urls=[], source_urls=[]),
    Candidate(id="ind-1", question=Q_IND, category="unanswerable_in_domain", answerable=False,
              key_facts=[], gold_urls=[], source_urls=[A], notes="No travel page in the corpus."),
    Candidate(id="sp-3", question=Q_SP3, category="single_page", answerable=True,
              key_facts=["600 insured hours of work"], gold_urls=[B], source_urls=[B]),
]  # fmt: skip


def reply(text, citations, abstained=False, reason=None, **kw):
    body = {"answer": text, "citations": citations, "abstained": abstained,
            "abstain_reason": reason}  # fmt: skip
    return make_message(json.dumps(body), **kw)


def verdicts(*items):
    return make_message(
        json.dumps(
            {"verdicts": [{"fact_index": i, "present": p, "reason": r} for i, p, r in items]}
        )
    )


_QUESTION_RE = re.compile(r"<question>\n(.*?)\n</question>", re.S)


class RoutingAnthropic:
    """Answers by question (answer calls and judge calls told apart by the output schema).
    Records every call; an Exception as a reply is raised."""

    def __init__(self, answer_replies=None, judge_replies=None):
        self.answer_replies = answer_replies or {}
        self.judge_replies = judge_replies or {}
        self.calls: list[dict] = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        schema = kwargs["output_config"]["format"]["schema"]
        question = _QUESTION_RE.search(kwargs["messages"][0]["content"]).group(1)
        table = self.judge_replies if "verdicts" in schema["properties"] else self.answer_replies
        response = table[question]
        if isinstance(response, Exception):
            raise response
        return response

    def judge_calls(self):
        return [c for c in self.calls if "verdicts" in c["output_config"]["format"]["schema"][
            "properties"]]  # fmt: skip


class NoAnthropic:
    """Any use of the Anthropic client fails the test."""

    @property
    def messages(self):
        raise AssertionError("Anthropic client used")


@pytest.fixture
def settings(tmp_path):
    (tmp_path / "answer.md").write_text("You answer from the chunks only.\n")
    (tmp_path / "judge.md").write_text("You check whether an answer states facts.\n")
    s = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        embed=EmbedSettings(output_dimension=DIM),
        index=IndexSettings(collection="test_v1"),
        answer=AnswerSettings(system_prompt_path=tmp_path / "answer.md", min_score=0.3),
        eval=EvalSettings(judge_prompt_path=tmp_path / "judge.md"),
    )
    write_corpus(s.data_dir, PAGES)
    s.eval_set_path.parent.mkdir(parents=True, exist_ok=True)
    s.eval_set_path.write_text("".join(c.model_dump_json() + "\n" for c in ITEMS))
    return s


@pytest.fixture
def qdrant(settings):
    client = QdrantClient(":memory:")
    chunks = [
        c
        for program, url, title, body, date in PAGES
        for c in chunk_page(
            f"# {title}\n\n{body}\n",
            url=url,
            program=program,
            date_modified=date,
            cfg=ChunkSettings(target_tokens=40, max_tokens=80, min_tokens=5),
            count=word_count,
        )
    ]
    sync(client, chunks, Embedder(FakeVoyage(), settings.embed), settings.index, ["ei"])
    CHUNK_IDS.clear()
    CHUNK_IDS.update({(c.url, " > ".join(c.heading_path)): c.id for c in chunks})
    yield client
    client.close()


CHUNK_IDS: dict[tuple[str, str], str] = {}


def cid(qdrant, url, heading):
    return CHUNK_IDS[(url, heading)]


CLEAN_GIT = {"sha": "abc1234", "dirty": False, "diff_sha256": None, "untracked": []}
DIRTY_GIT = {"sha": "abc1234", "dirty": True, "diff_sha256": "f" * 64, "untracked": ["x.py"]}


@pytest.fixture
def env(monkeypatch, settings, qdrant):
    """Patches settings, retrieval wiring, the Anthropic client factory, the git state
    (clean unless a test sets ``state["git"]``) and run ids (one per second, increasing)."""
    state = {"voyage": [], "client": NoAnthropic(), "git": CLEAN_GIT}
    run_ids = iter(f"20261016T{n:06d}Z" for n in range(1, 10_000))
    monkeypatch.setattr(provenance, "git_state", lambda: dict(state["git"]))
    monkeypatch.setattr(provenance, "new_run_id", lambda now=None: next(run_ids))

    def fake_connect(s):
        voyage = FakeVoyage()
        state["voyage"].append(voyage)
        cache = EmbeddingCache(s.embedding_cache_path)
        return Embedder(voyage, s.embed, cache), qdrant, cache

    monkeypatch.setattr(answers, "get_settings", lambda: settings)
    monkeypatch.setattr(answers, "connect", fake_connect)
    monkeypatch.setattr(answers, "make_anthropic_client", lambda s: state["client"])
    return state


def good_client(qdrant, **answer_kw):
    a = cid(qdrant, A, "Weekly amount")
    d = cid(qdrant, D, "Weekly amount")
    apply_ = cid(qdrant, B, "When to apply")
    hours = cid(qdrant, B, "Insured hours")
    return RoutingAnthropic(
        answer_replies={
            Q_MAX: reply("You can get up to a maximum of $729 a week.", [a], **answer_kw),
            Q_APPLY: reply("Apply right away once you stop working.", [apply_]),
            Q_SICK: reply("Up to a maximum of $729 a week.", [a, d]),
            Q_IND: reply("", [], abstained=True, reason="The documents don't cover travel."),
            Q_SP3: reply("You need 600 insured hours of work.", [hours]),
            Q_OOS: reply("", [], abstained=True, reason="Out of scope."),
        },
        judge_replies={Q_APPLY: verdicts((1, True, "says apply right away"))},
    )


def results_files(settings):
    return sorted(settings.eval_results_dir.glob("answers_*.summary.json"))


def runs_files(settings):
    return sorted(settings.runs_dir.glob("answers_*.summary.json"))


def load(path):
    return answers.load_report(path)


def md_of(summary_path):
    return summary_path.with_name(summary_path.name.replace(".summary.json", ".md"))


def rows_by_id(report):
    return {r["id"]: r for r in report["items"]}


# --- fixture sanity ----------------------------------------------------------------


def test_fixture_gates_only_the_out_of_scope_question(settings, qdrant):
    from support_desk_assistant.answer.pipeline import prepare

    embedder = Embedder(FakeVoyage(), settings.embed)
    gates = {
        c.id: prepare(c.question, settings=settings, embedder=embedder, qdrant=qdrant).gate
        for c in ITEMS
    }
    assert gates.pop("oos-1") == "abstain_low_score"
    assert set(gates.values()) == {"pass"}


# --- selection ---------------------------------------------------------------------


def test_limit_is_per_category_in_file_order():
    picked = select_items(ITEMS, limit=1)
    assert [c.id for c in picked] == ["sp-1", "cf-1", "oos-1", "ind-1"]
    assert [c.id for c in select_items(ITEMS, limit=2)] == [
        "sp-1", "sp-2", "cf-1", "oos-1", "ind-1"
    ]  # fmt: skip


def test_category_filter_and_limit_combine():
    assert [c.id for c in select_items(ITEMS, ["single_page"])] == ["sp-1", "sp-2", "sp-3"]
    picked = select_items(ITEMS, ["single_page", "confusable"], limit=1)
    assert [c.id for c in picked] == ["sp-1", "cf-1"]
    assert select_items(ITEMS) == ITEMS


# --- dry run -------------------------------------------------------------------------


def test_dry_run_sends_nothing_to_anthropic_and_prints_voyage_line_and_cap(env, settings, capsys):
    assert main([]) == 0  # env["client"] raises on any use: no Anthropic client at all
    out = capsys.readouterr().out
    voyage = next(line for line in out.splitlines() if line.startswith("Voyage:"))
    assert "query-embedding calls" in voyage and "Qdrant tunnel" in voyage
    assert "6 query embeddings (0 cached, 6 embedded now)" in voyage
    assert re.search(r"\d+ tokens billed, CA\$\d+\.\d+", voyage)
    assert "placeholder price" in voyage
    assert "vs cap CA$0.25" in out
    assert "Answers: 5 API call(s), 0 cached, 1 gate abstention(s) (free)" in out
    assert "dry run: nothing sent to Anthropic; add --yes to run" in out
    assert not results_files(settings) and not runs_files(settings)
    assert not settings.request_log_path.exists()
    # The query embeddings are cached: a second dry run embeds nothing.
    main([])
    assert "6 query embeddings (6 cached, 0 embedded now), 0 tokens billed" in (
        capsys.readouterr().out
    )
    assert env["voyage"][1].calls == []


def test_dry_run_judge_count_is_exact_once_answers_are_cached(env, settings, qdrant, capsys):
    env["client"] = good_client(qdrant)
    assert main(["--yes"]) == 0
    capsys.readouterr()
    env["client"] = NoAnthropic()
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "Answers: 0 API call(s), 5 cached, 1 gate abstention(s)" in out
    assert "Judge: 0 API call(s)" in out and "Estimate: <= CA$0.0000" in out


def test_dry_run_counts_one_judge_call_per_answerable_item_not_yet_answered(env, capsys):
    main(["--category", "single_page"])
    out = capsys.readouterr().out
    assert "Judge: 3 API call(s) (0 exact for known answers, 3 assumed" in out


def test_dry_run_over_the_cap_still_exits_0_but_warns(env, capsys):
    assert main(["--cap", "0.0000001"]) == 0
    out = capsys.readouterr().out
    assert "Over the cap" in out and "--limit" in out and "--category" in out


# --- full run --------------------------------------------------------------------------


def test_full_run_scores_writes_results_and_costs(env, settings, qdrant, capsys):
    client = env["client"] = good_client(qdrant)
    assert main(["--yes"]) == 0
    assert len(client.calls) == 6  # 5 answers (one gated) + 1 judge (sp-2)
    assert len(client.judge_calls()) == 1
    assert client.judge_calls()[0]["messages"][0]["content"].endswith(
        f"<facts>\n1. {FACT_APPLY}\n</facts>"
    )
    (path,) = results_files(settings)
    assert not runs_files(settings)
    assert path.name == "answers_test_v1_claude-haiku-5-5_20261016T000001Z.summary.json"
    assert md_of(path).exists()
    assert path.with_name(path.name.replace(".summary.json", ".jsonl")).exists()
    report = load(path)
    rows = rows_by_id(report)

    sp1 = rows["sp-1"]
    assert sp1["passed"] and sp1["facts"][0]["method"] == "numbers" and not sp1["judge_cache_hit"]
    assert sp1["retrieved"][0].keys() == {"chunk_id", "url", "score"}
    assert sp1["validator"] == {"ok": True, "failed_rules": []}
    assert sp1["cost_cad"]["answer"] > 0 and sp1["cost_cad"]["judge"] == 0

    sp2 = rows["sp-2"]
    assert sp2["facts"][0]["method"] == "judge" and sp2["facts"][0]["present"]
    assert sp2["passed"] and sp2["cost_cad"]["judge"] > 0

    cf = rows["cf-1"]
    assert (cf["gold_cited"], cf["decoy_cited"], cf["passed"]) == (True, True, False)
    assert cf["cited_urls"] == [A, D]

    oos = rows["oos-1"]
    assert (oos["gate"], oos["abstained_by"], oos["passed"]) == ("abstain_low_score", "gate", True)
    assert oos["cost_cad"] == {"answer": 0, "judge": 0}
    ind = rows["ind-1"]
    assert (ind["abstained_by"], ind["passed"], ind["facts"]) == ("model", True, [])
    assert ind["notes"] == "No travel page in the corpus."

    meta = report["meta"]
    assert meta["counts"] == {
        "items": 6,
        "answer_calls": 5,
        "answer_cache_hits": 0,
        "judge_calls": 1,
        "judge_cache_hits": 0,
        "judge_errors": 0,
        "gate_abstentions": 1,
    }
    cost = meta["cost_cad"]
    assert cost["answers"] > 0 and cost["judge"] > 0 and cost["voyage"] > 0
    assert cost["total"] == pytest.approx(cost["answers"] + cost["judge"] + cost["voyage"])
    assert meta["partial"] is False and meta["stopped_by_cap"] is False
    assert meta["filters"] == {"limit": None, "categories": None}
    out = capsys.readouterr().out
    assert "Actual cost: CA$" in out and str(path) in out


def test_results_meta_has_every_field_and_no_overall_number(env, settings, qdrant):
    env["client"] = good_client(qdrant)
    main(["--yes"])
    report = load(results_files(settings)[0])
    assert set(report) == {"meta", "metrics", "items"}
    assert set(report["meta"]) == {
        "created_at", "git_sha", "git_dirty", "dataset", "dataset_sha256", "collection", "k",
        "answer_model", "judge_model", "answer_prompt_sha256", "judge_prompt_sha256",
        "schema_version", "judge_schema_version", "min_score", "prices_as_of", "usd_cad",
        "embed_model", "filters", "partial", "stopped_by_cap", "counts", "cost_cad",
        "run_id", "provenance", "tokens", "cost_cad_list", "results_dir_reason",
    }  # fmt: skip
    meta = report["meta"]
    assert meta["dataset"] == "eval_set_v1.jsonl" and len(meta["dataset_sha256"]) == 64
    assert len(meta["answer_prompt_sha256"]) == len(meta["judge_prompt_sha256"]) == 64
    assert meta["answer_prompt_sha256"] != meta["judge_prompt_sha256"]
    assert (meta["collection"], meta["k"], meta["judge_model"]) == (
        "test_v1",
        5,
        "claude-haiku-5-5",
    )
    assert set(meta["cost_cad"]) == {"answers", "judge", "voyage", "total"}
    assert set(meta["cost_cad_list"]) == {"answers", "judge", "total"}
    assert meta["run_id"] == meta["provenance"]["run_id"] == "20261016T000001Z"
    assert meta["git_sha"] == "abc1234" and meta["git_dirty"] is False
    assert meta["results_dir_reason"] is None
    assert meta["provenance"]["corpus"]["points_count"] > 0
    scorer = meta["provenance"]["scorer"]["config"]
    assert scorer["scorer_version"] == answers.SCORER_VERSION
    assert scorer["atomic_fact_max_words"] == 6 and "with" in scorer["stopwords"]
    assert set(meta["tokens"]) == {"answer", "judge"}
    assert meta["tokens"]["answer"]["input_tokens"] == 5 * 1000
    assert meta["tokens"]["judge"]["output_tokens"] == 100
    assert set(report["metrics"]) == {
        "groups", "categories", "overall", "by_program", "abstention", "citation", "attribution"
    }  # fmt: skip
    assert set(report["metrics"]["categories"]) == set(CATEGORIES)
    assert report["metrics"]["categories"]["single_page"]["n"] == 3
    assert report["metrics"]["overall"]["answerable"]["n"] == 4
    assert report["metrics"]["overall"]["abstention_recall"]["n"] == 2
    assert set(report["metrics"]["by_program"]) == {"ei", "n/a"}
    md = md_of(results_files(settings)[0]).read_text()
    assert "## Overall" in md and "## By program" in md and "## Cost and tokens" in md
    assert md.index("## Abstention\n\n| category") < md.index("## Overall")
    assert "run 20261016T000001Z" in md
    assert not any(re.match(r"\| (overall|total|all)\b", r) for r in md.splitlines())
    assert set(rows_by_id(report)["sp-1"]) >= {
        "id", "category", "question", "answerable", "retrieved", "gate", "status", "abstained",
        "abstain_reason", "abstained_by", "answer", "citations", "cited_urls", "validator",
        "facts", "facts_fraction", "all_facts", "gold_cited", "decoy_cited", "page_hit",
        "fact_in_context", "attribution", "passed", "answered_confidently", "cache_hit",
        "judge_cache_hit", "cost_cad", "gold_urls", "tokens", "cost_cad_list",
        "answer_cache_key", "judge_cache_key",
    }  # fmt: skip


def test_second_full_run_makes_no_calls_and_costs_nothing(env, settings, qdrant):
    first = env["client"] = good_client(qdrant)
    assert main(["--yes"]) == 0
    before = rows_by_id(load(results_files(settings)[0]))
    env["client"] = NoAnthropic()
    assert main(["--yes"]) == 0
    first_path, second_path = results_files(settings)  # same day, different run ids
    report = load(second_path)
    assert report["meta"]["cost_cad"] == {"answers": 0, "judge": 0, "voyage": 0, "total": 0}
    counts = report["meta"]["counts"]
    assert (counts["answer_calls"], counts["judge_calls"]) == (0, 0)
    assert (counts["answer_cache_hits"], counts["judge_cache_hits"]) == (5, 1)
    after = rows_by_id(report)
    assert {i: r["passed"] for i, r in after.items()} == {i: r["passed"] for i, r in before.items()}
    assert after["sp-2"]["judge_cache_hit"] and after["sp-1"]["cache_hit"]
    assert len(first.calls) == 6


def test_unanswerable_and_gated_items_make_no_judge_call(env, settings, qdrant):
    client = env["client"] = good_client(qdrant)
    main(["--yes", "--category", "unanswerable_out_of_scope", "--category",
          "unanswerable_in_domain"])  # fmt: skip
    assert client.judge_calls() == []
    (call,) = client.calls  # oos-1 is gated: only ind-1 reaches the answer model
    assert call["messages"][0]["content"].endswith(f"{Q_IND}\n</question>")


# --- partial runs ---------------------------------------------------------------------


def test_limit_run_is_partial_and_goes_to_runs(env, settings, qdrant):
    env["client"] = good_client(qdrant)
    assert main(["--yes", "--limit", "1"]) == 0
    assert not results_files(settings)
    (path,) = runs_files(settings)
    report = load(path)
    assert [r["id"] for r in report["items"]] == ["sp-1", "cf-1", "oos-1", "ind-1"]
    assert report["meta"]["partial"] is True and report["meta"]["stopped_by_cap"] is False
    assert report["meta"]["filters"] == {"limit": 1, "categories": None}
    assert report["meta"]["results_dir_reason"] == "partial"
    assert "PARTIAL" in md_of(path).read_text()


def test_category_run_goes_to_runs_and_keeps_an_existing_full_result(env, settings, qdrant):
    env["client"] = good_client(qdrant)
    main(["--yes"])
    (full,) = results_files(settings)
    full_text = full.read_text()
    main(["--yes", "--category", "confusable"])
    assert full.read_text() == full_text  # never overwritten by a partial run
    (path,) = runs_files(settings)
    report = load(path)
    assert [r["id"] for r in report["items"]] == ["cf-1"]
    assert report["meta"]["filters"]["categories"] == ["confusable"]


def test_estimate_over_the_cap_refuses_before_any_call(env, settings, qdrant, capsys):
    env["client"] = good_client(qdrant)
    assert main(["--yes", "--cap", "0.0000001"]) == 2
    out = capsys.readouterr().out
    assert "Refused" in out and "--limit" in out
    assert env["client"].calls == []
    assert not results_files(settings) and not runs_files(settings)


def test_cap_reached_mid_run_stops_and_writes_partial_to_runs(env, settings, qdrant, capsys):
    # sp-1's answer bills 10M input tokens, far above its upper bound: before the next item,
    # spend + that item's upper bound passes the cap, so the run stops with one call made.
    client = env["client"] = good_client(qdrant, input_tokens=10_000_000)
    assert main(["--yes", "--cap", "0.05"]) == 1
    assert len(client.calls) == 1
    assert not results_files(settings)
    (path,) = runs_files(settings)
    report = load(path)
    meta = report["meta"]
    assert meta["stopped_by_cap"] is True and meta["partial"] is True
    assert [r["id"] for r in report["items"]] == ["sp-1"]
    assert meta["cost_cad"]["total"] >= 0.05
    assert "Stopped by the spend cap" in capsys.readouterr().out

    # The completed call is cached: the re-run resumes without paying for sp-1 again.
    resumed = env["client"] = good_client(qdrant)
    assert main(["--yes", "--cap", "0.05"]) == 0
    assert not any(Q_MAX in c["messages"][0]["content"] for c in resumed.calls)
    assert len(resumed.calls) == 5 and results_files(settings)


def test_api_error_exits_1_and_writes_nothing(env, settings, qdrant, capsys):
    client = env["client"] = good_client(qdrant)
    client.answer_replies[Q_APPLY] = anthropic.APIConnectionError(
        request=httpx.Request("POST", "https://api.anthropic.com")
    )
    assert main(["--yes"]) == 1
    err = capsys.readouterr().err
    assert "Anthropic API error" in err and "re-run resumes" in err
    assert not results_files(settings) and not runs_files(settings)


# --- setup errors ---------------------------------------------------------------------


def test_missing_judge_prompt_is_a_setup_error(env, settings, capsys):
    settings.eval.judge_prompt_path.unlink()
    assert main([]) == 2
    assert "not found" in capsys.readouterr().err
    assert env["voyage"] == []  # stopped before any Voyage call


def test_todo_answer_prompt_is_a_setup_error(env, settings, capsys):
    settings.answer.system_prompt_path.write_text("# TODO: answer system prompt\n")
    assert main([]) == 2
    assert "TODO" in capsys.readouterr().err


def test_missing_collection_is_a_setup_error(env, settings, capsys):
    settings.index.collection = "nope_v1"
    assert main([]) == 2
    assert "not found in Qdrant" in capsys.readouterr().err
    assert env["voyage"][0].calls == []


def test_unreachable_qdrant_is_a_setup_error_and_spends_nothing(env, monkeypatch, capsys):
    class Down:
        def collection_exists(self, name):
            raise httpx.ConnectError("connection refused")

    def connect(s):
        voyage = FakeVoyage()
        env["voyage"].append(voyage)
        return Embedder(voyage, s.embed), Down(), EmbeddingCache(s.embedding_cache_path)

    monkeypatch.setattr(answers, "connect", connect)
    assert main([]) == 2
    assert "tunnel" in capsys.readouterr().err
    assert env["voyage"][0].calls == []


def test_missing_voyage_key_is_a_setup_error(settings):
    settings.voyage_api_key = None
    with pytest.raises(answers.SetupError):
        answers.connect(settings)


def test_export_rejects_run_options():
    with pytest.raises(SystemExit) as exc:
        main(["--export", "--yes"])
    assert exc.value.code == 2


# --- hand-review export ----------------------------------------------------------------


def test_export_is_deterministic_with_sections_a_b_c(env, settings, qdrant, capsys):
    env["client"] = good_client(qdrant)
    main(["--yes"])
    assert main(["--export"]) == 0
    first = settings.judge_review_path.read_text()
    assert main(["--export", str(results_files(settings)[0])]) == 0
    assert settings.judge_review_path.read_text() == first
    jsonl = str(results_files(settings)[0]).replace(".summary.json", ".jsonl")
    assert main(["--export", jsonl]) == 0
    from_jsonl = settings.judge_review_path.read_text()
    assert from_jsonl.replace(".jsonl\n", ".summary.json\n", 1) == first
    assert "## A. Judged facts (n=1)" in first
    assert "## B. Numbers-gate facts (n=" in first and "not by the LLM" in first
    assert "## C. In-domain unanswerables (n=1)" in first
    assert "blind" in first and "--import-labels" in first
    a, rest = first.split("## B.")
    b, c = rest.split("## C.")
    assert FACT_APPLY in a and "Your verdict: ___" in a
    # Blind: no machine verdict line, no judge reason, no "marked present" hint.
    assert "says apply right away" not in first and "- Judge:" not in first
    assert "Number check (not the LLM):" not in first and "Marked present" not in first
    assert FACT_APPLY not in b
    assert "ind-1" in c and Q_IND in c and "abstained, by model" in c
    assert "No travel page in the corpus." in c and "The documents don't cover travel." in c
    assert all(len(line) < 400 for line in first.splitlines())


def test_export_without_results_is_an_error(env, capsys):
    assert main(["--export"]) == 2
    assert "no answers_*.summary.json" in capsys.readouterr().err


def test_export_prefers_results_then_runs(env, settings, qdrant):
    env["client"] = good_client(qdrant)
    main(["--yes", "--limit", "1"])
    assert answers.newest_results_file(settings) == runs_files(settings)[0]
    main(["--yes"])
    assert answers.newest_results_file(settings) == results_files(settings)[0]


def _row(i, category, facts, **kw):
    return {"id": i, "category": category, "question": f"q {i}", "answer": f"a {i}",
            "facts": facts, **kw}  # fmt: skip


def _fact(name, method, present, judge_error=False):
    return {"fact": name, "method": method, "present": present, "reason": "r",
            "judge_error": judge_error}  # fmt: skip


def synthetic_rows(n_present, n_absent):
    rows = []
    for i in range(n_present):
        cat = CATEGORIES[i % 3] if i % 3 != 2 else "time_sensitive"
        rows.append(_row(f"p{i}", cat, [_fact(f"fp{i}", "judge", True)]))
    for i in range(n_absent):
        rows.append(_row(f"a{i}", "multi_page", [_fact(f"fa{i}", "judge", False)]))
    rows.append(_row("e", "single_page", [_fact("err", "judge", False, judge_error=True)]))
    rows.append(_row("n", "single_page", [_fact("num", "numbers", True),
                                           _fact("gone", "numbers", False)]))  # fmt: skip
    return rows


@pytest.mark.parametrize(
    ("n_present", "n_absent", "want"),
    [(6, 6, (5, 5)), (2, 10, (2, 8)), (12, 1, (9, 1)), (1, 1, (1, 1))],
)
def test_judged_facts_balanced_between_present_and_absent(n_present, n_absent, want):
    picked = pick_judged_facts(synthetic_rows(n_present, n_absent), 10, random.Random(1))
    present = sum(1 for _, d in picked if d["present"])
    assert (present, len(picked) - present) == want
    assert all(d["method"] == "judge" and not d["judge_error"] for _, d in picked)


def test_judged_facts_spread_across_categories():
    picked = pick_judged_facts(synthetic_rows(9, 0), 3, random.Random(1))
    assert len({r["category"] for r, _ in picked}) == 3


def test_gate_section_holds_only_numbers_decided_present_facts():
    picked = pick_gate_facts(synthetic_rows(3, 3), 4, random.Random(1))
    assert [(r["id"], d["fact"]) for r, d in picked] == [("n", "num")]


def test_render_judge_review_same_input_same_output(settings):
    report = {"meta": {}, "items": synthetic_rows(30, 30)}
    first = render_judge_review(report, "x.json", settings)
    assert render_judge_review(report, "x.json", settings) == first
    # Defaults: 40 judged facts (calibration needs >= 30), every numbers-present fact (1).
    assert settings.eval.judge_review_n == 40 and settings.eval.judge_review_gate_n == 10
    assert first.count("Your verdict: ___") == 40 + 1
    assert "## C. In-domain unanswerables (n=0)" in first


# --- rows: tokens, list cost, cache keys -------------------------------------------------


def test_rows_carry_gold_urls_tokens_list_cost_and_cache_keys(env, settings, qdrant):
    env["client"] = good_client(qdrant)
    assert main(["--yes"]) == 0
    rows = rows_by_id(load(results_files(settings)[0]))
    zero = dict.fromkeys(
        ["input_tokens", "output_tokens", "cache_creation_input_tokens",
         "cache_read_input_tokens"], 0
    )  # fmt: skip

    sp1 = rows["sp-1"]
    assert sp1["gold_urls"] == [A]
    assert sp1["tokens"]["answer"] == zero | {"input_tokens": 1000, "output_tokens": 100}
    assert sp1["tokens"]["judge"] == zero
    assert sp1["cost_cad_list"]["answer"] == sp1["cost_cad"]["answer"] > 0
    assert re.fullmatch(r"[0-9a-f]{64}", sp1["answer_cache_key"])
    assert sp1["judge_cache_key"] is None  # decided by the number check: no judge call

    sp2 = rows["sp-2"]
    assert re.fullmatch(r"[0-9a-f]{64}", sp2["judge_cache_key"])
    assert sp2["tokens"]["judge"] == zero | {"input_tokens": 1000, "output_tokens": 100}
    assert sp2["cost_cad_list"]["judge"] == sp2["cost_cad"]["judge"] > 0

    oos = rows["oos-1"]  # gate abstention: no call, no key, no cost
    assert oos["gold_urls"] == [] and oos["answer_cache_key"] is None
    assert oos["judge_cache_key"] is None
    assert oos["tokens"] == {"answer": zero, "judge": zero}
    assert oos["cost_cad_list"] == {"answer": 0, "judge": 0}


def test_list_cost_on_a_cache_hit_equals_the_fresh_call_while_actual_cost_is_zero(
    env, settings, qdrant
):
    env["client"] = good_client(qdrant)
    main(["--yes"])
    env["client"] = NoAnthropic()
    main(["--yes"])
    first_path, second_path = results_files(settings)
    first, second = load(first_path), load(second_path)
    fresh, hit = rows_by_id(first), rows_by_id(second)
    for item in ("sp-1", "sp-2"):
        assert hit[item]["cache_hit"] and hit[item]["cost_cad"]["answer"] == 0
        assert hit[item]["cost_cad_list"] == fresh[item]["cost_cad_list"]
        assert hit[item]["cost_cad_list"]["answer"] == fresh[item]["cost_cad"]["answer"] > 0
        assert hit[item]["answer_cache_key"] == fresh[item]["answer_cache_key"]
        assert set(hit[item]["tokens"]["answer"].values()) == {0}
    assert hit["sp-2"]["cost_cad_list"]["judge"] == fresh["sp-2"]["cost_cad"]["judge"] > 0
    assert hit["sp-2"]["cost_cad"]["judge"] == 0
    assert second["meta"]["cost_cad"]["total"] == 0
    assert second["meta"]["cost_cad_list"] == first["meta"]["cost_cad_list"]
    assert set(second["meta"]["tokens"]["answer"].values()) == {0}
    md = md_of(second_path).read_text()
    assert "## Cost and tokens" in md and "list CA$" in md


# --- output files --------------------------------------------------------------------------


def test_write_report_three_files_and_load_report_from_either_path(env, settings, qdrant):
    env["client"] = good_client(qdrant)
    main(["--yes"])
    (summary_path,) = results_files(settings)
    rows_path = summary_path.with_name(summary_path.name.replace(".summary.json", ".jsonl"))
    lines = rows_path.read_text().splitlines()
    assert len(lines) == len(ITEMS)
    assert [json.loads(line)["id"] for line in lines] == [c.id for c in ITEMS]
    assert set(json.loads(summary_path.read_text())) == {"meta", "metrics"}

    from_summary, from_rows = load(summary_path), load(rows_path)
    assert from_summary == from_rows
    assert set(from_summary) == {"meta", "metrics", "items"}

    # Writing the loaded report again gives the same three files.
    out = answers.write_report(from_summary, settings.data_dir / "copy")
    assert [p.name for p in out] == [rows_path.name, summary_path.name, md_of(summary_path).name]
    assert out[0].read_text() == rows_path.read_text()
    assert out[1].read_text() == summary_path.read_text()
    with pytest.raises(ValueError):
        answers.load_report(md_of(summary_path))


def test_same_day_runs_get_different_stems(tmp_path):
    report = {
        "meta": {"collection": "canada docs/v1", "answer_model": "claude-haiku-5-5"},
        "metrics": answers.aggregate([]),
        "items": [],
    }
    a = answers.write_report(
        {**report, "meta": report["meta"] | {"run_id": "20261016T090000Z"}}, tmp_path
    )
    b = answers.write_report(
        {**report, "meta": report["meta"] | {"run_id": "20261016T170000Z"}}, tmp_path
    )
    assert a[1].name == "answers_canada-docs-v1_claude-haiku-5-5_20261016T090000Z.summary.json"
    assert set(a).isdisjoint(b) and all(p.exists() for p in (*a, *b))


# --- dirty tree rule -----------------------------------------------------------------------


def test_dirty_tree_goes_to_runs_unless_allow_dirty(env, settings, qdrant, capsys):
    env["client"] = good_client(qdrant)
    env["git"] = DIRTY_GIT
    assert main(["--yes"]) == 0
    assert not results_files(settings)
    (path,) = runs_files(settings)
    meta = load(path)["meta"]
    assert meta["partial"] is False and meta["results_dir_reason"] == "dirty tree"
    assert meta["git_dirty"] is True and meta["provenance"]["git"] == DIRTY_GIT
    out = capsys.readouterr().out
    assert "dirty" in out and "--allow-dirty" in out

    env["client"] = NoAnthropic()  # spend unaffected: everything is cached now
    assert main(["--yes", "--allow-dirty"]) == 0
    (path,) = results_files(settings)
    meta = load(path)["meta"]
    assert meta["git_dirty"] is True and meta["results_dir_reason"] is None
    assert "(dirty)" in md_of(path).read_text()


def test_partial_dirty_run_reason_is_partial(env, settings, qdrant):
    env["client"] = good_client(qdrant)
    env["git"] = DIRTY_GIT
    main(["--yes", "--limit", "1", "--allow-dirty"])
    (path,) = runs_files(settings)
    assert load(path)["meta"]["results_dir_reason"] == "partial"


# --- hard cap ------------------------------------------------------------------------------


def _plan(settings, qdrant, items):
    return answers.make_plan(
        items,
        settings=settings,
        embedder=Embedder(FakeVoyage(), settings.embed),
        qdrant=qdrant,
        k=5,
        model=settings.answer.model,
        judge_system="judge",
    )


def _bound_cad(settings, p):
    return answers.usd_to_cad(p.answer_usd + p.judge_usd, settings.answer)


def test_cap_check_before_the_first_item_runs_nothing(settings, qdrant):
    plan = _plan(settings, qdrant, ITEMS[:1])
    cap = plan.voyage.cad + _bound_cad(settings, plan.items[0]) / 2
    run = answers.run_plan(
        plan, settings=settings, k=5, judge_system="judge", client=NoAnthropic(), cap_cad=cap
    )
    assert run.items == [] and run.stopped_by_cap


def test_no_call_once_the_next_items_upper_bound_would_pass_the_cap(settings, qdrant):
    from support_desk_assistant.answer.cost import Usage, cost_usd

    items = select_items(ITEMS, ["single_page", "confusable"], limit=1)  # sp-1, cf-1
    plan = _plan(settings, qdrant, items)
    client = good_client(qdrant)
    big = 1_000_000  # sp-1 bills far more than its upper bound
    client.answer_replies[Q_MAX] = reply(
        "You can get up to a maximum of $729 a week.",
        [cid(qdrant, A, "Weekly amount")],
        input_tokens=big,
    )
    model = settings.answer.model
    actual_1 = answers.usd_to_cad(
        cost_usd(Usage(input_tokens=big, output_tokens=100), model, settings.answer),
        settings.answer,
    )
    bound_1, bound_2 = (_bound_cad(settings, p) for p in plan.items)
    assert actual_1 > bound_1  # so the first pre-item check passes
    # After sp-1 the spend is still under the cap (a post-item check would go on), but
    # adding cf-1's upper bound would pass it: cf-1 must never reach Anthropic.
    cap = plan.voyage.cad + actual_1 + bound_2 / 2
    run = answers.run_plan(
        plan, settings=settings, k=5, judge_system="judge", client=client, cap_cad=cap
    )
    assert run.total_cad < cap
    assert run.stopped_by_cap and [i.candidate.id for i in run.items] == ["sp-1"]
    assert len(client.calls) == 1
    assert not any(Q_SICK in c["messages"][0]["content"] for c in client.calls)


# --- blind sheet, label import, calibration ----------------------------------------------


def test_export_reveal_shows_machine_verdicts(env, settings, qdrant):
    env["client"] = good_client(qdrant)
    main(["--yes"])
    assert main(["--export", "--reveal"]) == 0
    sheet = settings.judge_review_path.read_text()
    assert "- Judge: present. says apply right away" in sheet
    assert "Number check (not the LLM): present" in sheet and "Marked present" in sheet
    assert "not blind" in sheet
    main(["--export"])
    blind = settings.judge_review_path.read_text()
    # Same facts, same order; only the verdict lines (and the intro) differ.
    fact_lines = [ln for ln in sheet.splitlines() if ln.startswith(("### ", "- Fact:"))]
    assert fact_lines == [ln for ln in blind.splitlines() if ln.startswith(("### ", "- Fact:"))]


def test_import_labels_then_calibrate_end_to_end(env, settings, qdrant, capsys, tmp_path):
    env["client"] = good_client(qdrant)
    main(["--yes"])
    assert main(["--export"]) == 0
    summary = results_files(settings)[0]
    filled = tmp_path / "filled.md"
    filled.write_text(
        settings.judge_review_path.read_text().replace("Your verdict: ___", "Your verdict: present")
    )
    capsys.readouterr()

    assert main(["--import-labels", str(filled), "--from", str(summary)]) == 0
    out = capsys.readouterr().out
    records = [json.loads(line) for line in settings.calibration_path.read_text().splitlines()]
    n = len(records)
    assert f"{n} added, 0 duplicate(s), 0 conflict(s), 0 unlabelled, 0 problem(s)" in out
    assert {r["source_run"] for r in records} == {"20261016T000001Z"}
    by_method = {r["method"] for r in records}
    assert by_method == {"judge", "numbers"}
    assert any(r["item_id"] == "sp-2" and r["method"] == "judge" for r in records)

    # Re-import (newest results by default): duplicates only, nothing appended.
    assert main(["--import-labels", str(filled)]) == 0
    assert f"0 added, {n} duplicate(s)" in capsys.readouterr().out
    # A different label for a stored fact is a conflict: not written, exit 1.
    filled.write_text(filled.read_text().replace("Your verdict: present", "Your verdict: absent"))
    assert main(["--import-labels", str(filled)]) == 1
    captured = capsys.readouterr()
    assert f"{n} conflict(s)" in captured.out and "conflict:" in captured.err
    assert len(settings.calibration_path.read_text().splitlines()) == n

    # Calibrate: the judge verdict for sp-2 is cached from the run, so no API call.
    env["client"] = NoAnthropic()
    assert main(["--calibrate"]) == 0
    out = capsys.readouterr().out
    assert "# Judge calibration" in out and "Judge: 0 call(s), 1 cache hit(s)" in out
    assert "git add -f" in out
    (json_path,) = settings.runs_dir.glob("judge_calibration_*.json")
    saved = json.loads(json_path.read_text())
    assert set(saved) >= {
        "run_id", "report", "calibration_sha256", "judge_prompt_sha256", "git"
    }  # fmt: skip
    assert saved["git"] == CLEAN_GIT and len(saved["calibration_sha256"]) == 64
    assert saved["report"]["judge"]["agreement"]["rate"] == 1.0
    assert json_path.with_suffix(".md").read_text().startswith("# Judge calibration")

    # A changed judge prompt misses the cache: one scripted judge call, which disagrees.
    settings.eval.judge_prompt_path.write_text("You check facts, version 2.\n")
    client = env["client"] = RoutingAnthropic(
        judge_replies={Q_APPLY: verdicts((1, False, "not stated"))}
    )
    assert main(["--calibrate", str(settings.calibration_path)]) == 0  # dry run: needs --yes
    out = capsys.readouterr().out
    assert client.calls == [] and "dry run: 1 judge call(s) would be made" in out
    assert len(list(settings.runs_dir.glob("judge_calibration_*.json"))) == 1  # no new report
    assert main(["--calibrate", str(settings.calibration_path), "--yes"]) == 0
    out = capsys.readouterr().out
    assert len(client.judge_calls()) == 1 and "Judge: 1 call(s), 0 cache hit(s)" in out
    assert "human present, machine absent" in out


def test_import_labels_problems_and_unreadable_files(env, settings, qdrant, capsys, tmp_path):
    assert main(["--import-labels", str(tmp_path / "missing.md")]) == 2
    sheet = tmp_path / "sheet.md"
    sheet.write_text("### A1. nope-1 (single_page)\n\n- Fact: x\n- Your verdict: present\n")
    assert main(["--import-labels", str(sheet)]) == 2  # no results file yet
    assert "no answers_*.summary.json" in capsys.readouterr().err
    env["client"] = good_client(qdrant)
    main(["--yes"])
    capsys.readouterr()
    assert main(["--import-labels", str(sheet)]) == 1
    captured = capsys.readouterr()
    assert "item not in the results file" in captured.err and "1 problem(s)" in captured.out


def test_calibrate_without_records_is_an_error(env, settings, capsys):
    assert main(["--calibrate"]) == 2
    assert "no calibration records" in capsys.readouterr().err


def _one_judge_record_file(tmp_path):
    from support_desk_assistant.evals.calibrate import CalibrationRecord, record_id

    fact = "Apply for EI as soon as possible"
    record = CalibrationRecord(
        id=record_id("sp-2", fact), item_id="sp-2", category="single_page", question=Q_APPLY,
        answer="Apply right away.", fact=fact, judge_facts=[fact], fact_index=1,
        method="judge", machine_present=True, human="present", source_run="r",
        labelled_at="2026-10-16",
    )  # fmt: skip
    path = tmp_path / "cal.jsonl"
    path.write_text(record.model_dump_json() + "\n")
    return path


def test_calibrate_api_error_exits_1(env, settings, capsys, tmp_path):
    path = _one_judge_record_file(tmp_path)
    env["client"] = RoutingAnthropic(
        judge_replies={
            Q_APPLY: anthropic.APIConnectionError(
                request=httpx.Request("POST", "https://api.anthropic.com")
            )
        }
    )
    assert main(["--calibrate", str(path), "--yes"]) == 1
    assert "Anthropic API error" in capsys.readouterr().err
    assert not list(settings.runs_dir.glob("judge_calibration_*"))


def test_calibrate_without_yes_never_calls_anthropic_on_a_cache_miss(
    env, settings, capsys, tmp_path
):
    path = _one_judge_record_file(tmp_path)
    env["client"] = NoAnthropic()  # any use fails the test
    assert main(["--calibrate", str(path)]) == 0
    out = capsys.readouterr().out
    assert "1 judge call group(s): 0 cached, 1 need an API call" in out
    assert "dry run: 1 judge call(s) would be made; nothing sent to Anthropic" in out
    assert not list(settings.runs_dir.glob("judge_calibration_*"))  # no report either


def test_calibrate_yes_is_refused_when_the_estimate_passes_the_cap(env, settings, capsys, tmp_path):
    path = _one_judge_record_file(tmp_path)
    env["client"] = NoAnthropic()
    assert main(["--calibrate", str(path), "--yes", "--cap", "0.000001"]) == 2
    out = capsys.readouterr().out
    assert "Refused" in out and "exceeds the cap CA$0.00" in out
    assert not list(settings.runs_dir.glob("judge_calibration_*"))
    # The default cap (EvalSettings.spend_cap_cad) is applied when --cap is not given.
    client = env["client"] = RoutingAnthropic(
        judge_replies={Q_APPLY: verdicts((1, True, "says apply right away"))}
    )
    assert main(["--calibrate", str(path), "--yes"]) == 0
    assert len(client.judge_calls()) == 1
    assert "Judge: 1 call(s)" in capsys.readouterr().out


# --- CLI misuse ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["--export", "--calibrate"],
        ["--import-labels", "s.md", "--export"],
        ["--import-labels", "s.md", "--calibrate"],
        ["--calibrate", "--limit", "1"],
        ["--calibrate", "--allow-dirty"],
        ["--calibrate", "--cap", "0"],
        ["--export", "--yes"],
        ["--export", "--cap", "1"],
        ["--import-labels", "s.md", "--yes"],
        ["--import-labels", "s.md", "--allow-dirty"],
        ["--export", "--allow-dirty"],
        ["--reveal"],
        ["--yes", "--reveal"],
        ["--calibrate", "--reveal"],
        ["--from", "r.summary.json"],
        ["--export", "--from", "r.summary.json"],
    ],
)
def test_cli_mode_misuse_is_a_usage_error(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2
    assert "error:" in capsys.readouterr().err
