"""Review helper: excerpts, accept/edit/reject flow and resumability. Offline."""

import json
import sys

import pytest

from fakes import write_corpus
from support_desk_assistant.config import EvalSettings, Settings
from support_desk_assistant.evals import review as rv
from support_desk_assistant.evals.draft_questions import Candidate, load_candidates, load_pages
from support_desk_assistant.evals.review import (
    KeywordIndex,
    ReviewContext,
    ReviewState,
    accept,
    apply_decisions,
    apply_edits,
    build_card_view,
    export_html,
    highlight,
    highlight_html,
    load_eval_set,
    load_state,
    next_eval_id,
    parse_decisions,
    pending,
    relevant_excerpt,
    render_card,
    review_loop,
    save_state,
    status_lines,
    supporting_pages,
)

EI = "https://www.canada.ca/en/services/benefits/ei/ei-regular-benefit/eligibility.html"
OAS = "https://www.canada.ca/en/services/benefits/publicpensions/old-age-security/payments.html"

ELIG_BODY = "\n\n".join(
    [
        "Intro paragraph about the program.",
        "Second paragraph, filler.",
        "Third paragraph, filler.",
        "You need between 420 and 700 hours of insurable employment.",
        "Fifth paragraph, filler.",
        "Sixth paragraph, filler.",
        "Seventh paragraph, filler.",
    ]
)
OAS_BODY = "Old age security payments are made monthly. Residence in Canada is required.\n\n" + (
    "The pension payment date rules apply. " * 40
)


@pytest.fixture
def settings(tmp_path):
    write_corpus(
        tmp_path,
        [
            ("ei", EI, "EI eligibility", ELIG_BODY, "2026-09-03"),
            ("oas", OAS, "OAS payments", OAS_BODY, "2026-09-05"),
        ],
    )
    return Settings(data_dir=tmp_path, eval=EvalSettings())


def make(**kw):
    base = {
        "id": "single_page-01",
        "question": "How many hours do I need before I can claim?",
        "category": "single_page",
        "answerable": True,
        "key_facts": ["between 420 and 700 hours"],
        "gold_urls": [EI],
        "source_urls": [EI],
        "notes": "",
    }
    return Candidate(**(base | kw))


@pytest.fixture
def ctx(settings, tmp_path):
    pages = load_pages(settings)
    return ReviewContext(
        settings=settings,
        pages=pages,
        index=KeywordIndex(pages.values()),
        eval_path=tmp_path / "eval_set_v1.jsonl",
        state_path=tmp_path / "review_state.json",
        state=ReviewState(),
    )


def drive(queue, ctx, answers, edit_fn=None):
    """Run the loop with scripted keypresses; returns (counts, printed output)."""
    script = iter(answers)
    out: list[str] = []
    kwargs = {"edit_fn": edit_fn} if edit_fn else {}
    counts = review_loop(queue, ctx, input_fn=lambda _: next(script), print_fn=out.append, **kwargs)
    return counts, "\n".join(out)


# --- display ------------------------------------------------------------------------


def test_highlight_marks_facts_flexibly():
    text = "You need Between 420\nand 700 hours."
    assert (
        highlight(text, ["between 420 and 700 hours"], color=False)
        == "You need »Between 420\nand 700 hours«."
    )
    assert "\033[" in highlight(text, ["between 420 and 700 hours"], color=True)
    assert highlight("nothing here", ["absent"], color=False) == "nothing here"


def test_excerpt_keeps_the_fact_block_plus_context_and_marks_gaps():
    out = relevant_excerpt(ELIG_BODY, ["between 420 and 700 hours"], context=1, fallback_chars=50)
    assert "Third paragraph" in out and "between 420" in out and "Fifth paragraph" in out
    assert "Intro paragraph" not in out and "Seventh" not in out
    assert out.startswith("[...]") and out.endswith("[...]")


def test_excerpt_without_a_fact_is_the_head_of_the_page():
    out = relevant_excerpt(ELIG_BODY, [], context=1, fallback_chars=20)
    assert out.startswith("Intro paragraph") and out.endswith("[...]") and len(out) < 40


def test_keyword_index_finds_pages_that_share_the_questions_words(ctx):
    found = ctx.index.matches("When are old age security payments made?", n=2)
    assert found[0][0].url == OAS and "monthly" in found[0][2]
    assert ctx.index.matches("old age payments", exclude=[OAS]) == []


def test_supporting_pages_lists_gold_first_then_non_gold_sources_and_skips_unknown(ctx):
    missing = "https://www.canada.ca/en/not-in-the-corpus.html"
    c = make(category="multi_page", gold_urls=[OAS, missing], source_urls=[EI, OAS, missing])

    roles = [(role, page.url) for role, page in supporting_pages(c, ctx)]

    # OAS is gold and also a source: listed once, as gold. The URL outside the corpus is
    # dropped rather than raising, so a stale candidate still renders.
    assert roles == [("GOLD", OAS), ("SOURCE (not gold)", EI)]
    assert supporting_pages(make(gold_urls=[], source_urls=[]), ctx) == []


def test_card_shows_gold_page_facts_and_blocked_accept(ctx):
    bad = make(key_facts=["exactly 999 hours"])
    card = render_card(bad, (1, 3), ctx)
    assert "[1/3] single_page-01" in card and "PROBLEMS (accept is blocked)" in card
    assert "GOLD: [ei] EI eligibility" in card
    good = render_card(make(), (1, 3), ctx)
    assert "Auto-check: OK" in good and "»between 420 and 700 hours«" in good


def test_unanswerable_card_lists_closest_corpus_pages(ctx):
    c = make(
        category="unanswerable_in_domain",
        answerable=False,
        key_facts=[],
        gold_urls=[],
        question="When are my old age security payments made?",
        source_urls=[EI],
    )
    card = render_card(c, (1, 1), ctx)
    assert "SOURCE (not gold)" in card and "Closest pages elsewhere" in card
    assert "[oas] OAS payments" in card


# --- state and ids ------------------------------------------------------------------


def test_state_round_trip_and_corrupt_file_is_empty(tmp_path):
    path = tmp_path / "s.json"
    state = ReviewState(
        decisions={"a": {"action": "reject", "eval_id": None}}, edits={"b": {"x": 1}}
    )
    save_state(path, state)
    assert load_state(path) == state
    path.write_text("{broken")
    assert load_state(path) == ReviewState()
    assert load_state(tmp_path / "missing.json") == ReviewState()


def test_next_eval_id_continues_from_the_highest():
    assert next_eval_id([]) == "ev-001"
    assert next_eval_id(["ev-001", "ev-007", "other"]) == "ev-008"


# --- accept / reject ----------------------------------------------------------------


def test_accept_appends_with_a_final_id_and_records_the_decision(ctx):
    assert accept(make(), ctx) == "ev-001"
    assert accept(make(id="single_page-02"), ctx) == "ev-002"
    items = load_eval_set(ctx.eval_path)
    assert [i.id for i in items] == ["ev-001", "ev-002"]
    assert items[0].source_urls == [EI] and items[0].gold_urls == [EI]
    assert load_state(ctx.state_path).decisions["single_page-02"] == {
        "action": "accept",
        "eval_id": "ev-002",
    }


def test_accept_refuses_an_ungrounded_item_and_writes_nothing(ctx):
    with pytest.raises(ValueError, match="not verbatim"):
        accept(make(key_facts=["exactly 999 hours"]), ctx)
    assert not ctx.eval_path.exists() and ctx.state.decisions == {}


def test_loop_accept_reject_skip_and_quit(ctx):
    queue = [make(id="c1"), make(id="c2"), make(id="c3"), make(id="c4")]
    counts, _ = drive(queue, ctx, ["a", "r", "s", "q"])
    assert counts == {"accepted": 1, "rejected": 1, "skipped": 1}
    assert [d["action"] for d in ctx.state.decisions.values()] == ["accept", "reject"]
    assert [c.id for c in pending(queue, ctx.state)] == ["c3", "c4"]  # resumable


def test_loop_blocks_a_bad_accept_then_edit_fixes_it(ctx):
    bad = make(id="c1", key_facts=["exactly 999 hours"])
    fixed = bad.model_copy(update={"key_facts": ["between 420 and 700 hours"]})
    counts, out = drive([bad], ctx, ["a", "e", "a"], edit_fn=lambda c: fixed)
    assert "cannot accept" in out and "accepted as ev-001" in out
    assert counts["accepted"] == 1
    assert load_eval_set(ctx.eval_path)[0].key_facts == ["between 420 and 700 hours"]


def test_an_edit_survives_quitting_before_accept(ctx):
    original = make(id="c1")
    edited = original.model_copy(update={"question": "What is the hours rule for a claim?"})
    drive([original], ctx, ["e", "q"], edit_fn=lambda c: edited)
    state = load_state(ctx.state_path)
    (reloaded,) = apply_edits([original], state)
    assert reloaded.question == "What is the hours rule for a claim?" and reloaded.id == "c1"


def test_a_failed_edit_keeps_the_original(ctx):
    def boom(c):
        raise OSError("editor not found")

    counts, out = drive([make(id="c1")], ctx, ["e", "r"], edit_fn=boom)
    assert "edit discarded" in out and counts["rejected"] == 1


def test_full_page_key_shows_everything_and_unknown_keys_are_ignored(ctx):
    _, out = drive([make()], ctx, ["x", "f", "s"])
    assert "a, e, r, s, f or q" in out
    assert out.count("Intro paragraph") == 1  # only in the full-page render


def test_near_duplicate_of_an_accepted_item_is_flagged_not_blocked(ctx):
    accept(make(id="c1"), ctx)
    counts, out = drive([make(id="c2")], ctx, ["a"])
    assert "Near-duplicate of: ev-001" in out and counts["accepted"] == 1


def test_eof_quits_cleanly(ctx):
    def eof(_):
        raise EOFError

    out: list[str] = []
    counts = review_loop([make()], ctx, input_fn=eof, print_fn=out.append)
    assert counts == {"accepted": 0, "rejected": 0, "skipped": 0}


def test_status_counts_by_category(ctx):
    cands = [make(id="c1"), make(id="c2"), make(id="c3", category="time_sensitive")]
    state = ReviewState(decisions={"c2": {"action": "reject", "eval_id": None}})
    lines = status_lines(
        cands, state, [make(id="ev-001")], {"single_page": 16, "time_sensitive": 2}
    )
    single = next(line for line in lines if line.startswith("single_page"))
    assert single.split() == ["single_page", "1", "1", "1", "16"]
    assert lines[-1].split() == ["total", "1", "1", "2", "18"]


# --- editor and CLI -----------------------------------------------------------------


def test_edit_in_editor_applies_the_file_but_keeps_the_id(tmp_path, monkeypatch):
    script = tmp_path / "ed.py"
    script.write_text(
        "import json, sys\n"
        "p = sys.argv[1]; d = json.load(open(p))\n"
        "d['question'] = 'Edited question about hours?'; d['id'] = 'tampered'\n"
        "json.dump(d, open(p, 'w'))\n"
    )
    monkeypatch.setenv("VISUAL", f"{sys.executable} {script}")
    out = rv.edit_in_editor(make(id="c1"))
    assert out.question == "Edited question about hours?" and out.id == "c1"


def test_cli_status_and_missing_candidates(settings, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(rv, "get_settings", lambda: settings)
    cand = tmp_path / "candidates.jsonl"
    assert rv.main(["--candidates", str(cand), "--state", str(tmp_path / "s.json")]) == 2
    cand.write_text(json.dumps(make().model_dump()) + "\n")
    args = [
        "--candidates",
        str(cand),
        "--eval-set",
        str(tmp_path / "e.jsonl"),
        "--state",
        str(tmp_path / "s.json"),
    ]
    assert rv.main([*args, "--status"]) == 0
    assert "single_page" in capsys.readouterr().out


def test_cli_session_accepts_and_resumes(settings, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(rv, "get_settings", lambda: settings)
    cand = tmp_path / "candidates.jsonl"
    cand.write_text("".join(json.dumps(make(id=i).model_dump()) + "\n" for i in ("c1", "c2")))
    args = [
        "--candidates",
        str(cand),
        "--eval-set",
        str(tmp_path / "e.jsonl"),
        "--state",
        str(tmp_path / "s.json"),
        "--no-color",
    ]
    keys = iter(["a", "q"])
    monkeypatch.setattr("builtins.input", lambda _: next(keys))
    assert rv.main(args) == 0
    assert "accepted 1" in capsys.readouterr().out
    keys = iter(["r"])
    monkeypatch.setattr("builtins.input", lambda _: next(keys))
    assert rv.main(args) == 0
    out = capsys.readouterr().out
    assert "1 candidate(s) to review; 1 already in" in out
    assert len(load_candidates(tmp_path / "e.jsonl")) == 1


# --- phone page: export ---------------------------------------------------------------


def export(queue, ctx, tmp_path):
    path = tmp_path / "review.html"
    export_html(queue, ctx, path)
    return path.read_text(encoding="utf-8")


def test_highlight_html_escapes_and_cannot_match_inside_an_entity():
    out = highlight_html("Fish & chips <b>amp</b>", ["amp", "fish & chips"])
    assert out == "<mark>Fish &amp; chips</mark> &lt;b&gt;<mark>amp</mark>&lt;/b&gt;"
    assert highlight_html("a b", ["a b", "b"]) == "<mark>a b</mark>"  # overlap merges
    assert highlight_html("plain & text", []) == "plain &amp; text"


def test_export_card_has_the_review_fields_and_marks_the_facts(ctx, tmp_path):
    page = export([make(notes="Tests the hours rule.")], ctx, tmp_path)
    assert "single_page-01" in page and "single_page" in page
    assert "How many hours do I need before I can claim?" in page
    assert "Tests the hours rule." in page
    assert "<mark>between 420 and 700 hours</mark>" in page
    assert f'href="{EI}"' in page and "EI eligibility" in page
    # the trimmed section: neighbours of the fact block, not the whole page
    assert "Third paragraph" in page and "Intro paragraph" not in page
    assert page.count('class="btn accept"') == 1 and page.count('class="btn reject"') == 1
    assert "Closest pages elsewhere" not in page  # answerable item


def test_export_is_offline_and_escapes_candidate_text(ctx, tmp_path):
    evil = make(question="Is <script>alert(1)</script> & co. covered?", notes='say "hi" <b>')
    page = export([evil], ctx, tmp_path)
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; co." in page
    assert "&lt;b&gt;" in page
    for external in ("src=", "<link", "@import", "url(", "http-equiv"):
        assert external not in page
    assert page.count("<script>") == 1  # only our own inline script


def test_export_unanswerable_card_lists_keyword_matches(ctx, tmp_path):
    c = make(
        id="u-1",
        category="unanswerable_in_domain",
        answerable=False,
        key_facts=[],
        gold_urls=[],
        question="When are my old age security payments made?",
        source_urls=[EI],
    )
    page = export([c], ctx, tmp_path)
    assert "should abstain" in page and "SOURCE (not gold)" in page
    assert "Closest pages elsewhere in the corpus" in page
    assert "[oas] OAS payments" in page and "Old age security payments are made monthly" in page


def test_export_disables_accept_for_an_ungrounded_candidate(ctx, tmp_path):
    page = export([make(key_facts=["exactly 999 hours"])], ctx, tmp_path)
    assert "Accept is disabled" in page
    assert 'data-act="a" aria-pressed="false" disabled' in page
    assert 'data-act="r" aria-pressed="false">' in page  # reject stays usable


def test_card_fingerprint_follows_the_content_not_the_id(ctx):
    # a decision kept on the phone must not follow an id onto a redrafted question
    base = build_card_view(make(), ctx).fingerprint
    assert build_card_view(make(), ctx).fingerprint == base
    assert build_card_view(make(question="A different question?"), ctx).fingerprint != base
    assert build_card_view(make(id="single_page-99"), ctx).fingerprint == base


def test_export_cli_writes_only_pending_candidates(settings, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(rv, "get_settings", lambda: settings)
    cand = tmp_path / "candidates.jsonl"
    cand.write_text("".join(json.dumps(make(id=i).model_dump()) + "\n" for i in ("c1", "c2")))
    state = tmp_path / "s.json"
    save_state(state, ReviewState(decisions={"c1": {"action": "reject", "eval_id": None}}))
    html = tmp_path / "out" / "review.html"
    argv = ["--candidates", str(cand), "--state", str(state), "--html", str(html), "--export"]
    assert rv.main(argv) == 0
    page = html.read_text(encoding="utf-8")
    assert 'data-id="c2"' in page and 'data-id="c1"' not in page
    assert "wrote 1 candidate(s)" in capsys.readouterr().out
    assert not list(html.parent.glob("*.tmp"))
    # nothing pending: say so and leave the file alone
    save_state(state, ReviewState(decisions={i: {"action": "reject"} for i in ("c1", "c2")}))
    html.unlink()
    assert rv.main(argv) == 0 and not html.exists()


def test_export_default_path_is_data_eval_review_html(settings):
    assert settings.review_html_path == settings.eval_dir / "review.html"


# --- phone page: --apply ------------------------------------------------------------


def test_parse_decisions_accepts_the_copy_format_and_tolerates_noise():
    text = "\ufeff# from my phone\r\nc1 a\r\n\r\nc2\tR\n  c3 accept  \nc4\u00a0reject\n"
    decisions, problems = parse_decisions(text)
    assert decisions == {"c1": "accept", "c2": "reject", "c3": "accept", "c4": "reject"}
    assert problems == []


def test_parse_decisions_reports_bad_lines_and_lets_the_last_duplicate_win():
    decisions, problems = parse_decisions("c1 a\nc2\nc3 x\nc4 a extra\nc1 r\nc1 r\n")
    assert decisions == {"c1": "reject"}
    assert len(problems) == 4
    assert problems[0].startswith("line 2:") and problems[1].startswith("line 3:")
    assert problems[2].startswith("line 4:")
    assert problems[3] == "line 5: c1 appears twice; using reject"  # only the real change


def test_apply_records_accepts_and_rejects_like_the_terminal(ctx):
    queue = [
        make(id="c1"),
        make(id="c2", question="Another hours question?"),
        make(id="c3", question="How long must I have worked to claim?"),
    ]
    report = apply_decisions("c1 a\nc2 r\nc3 a\n", queue, ctx)
    assert report.accepted == [("c1", "ev-001"), ("c3", "ev-002")]
    assert report.rejected == ["c2"] and report.problems == []
    items = load_eval_set(ctx.eval_path)
    assert [i.id for i in items] == ["ev-001", "ev-002"]
    state = load_state(ctx.state_path)
    assert state.decisions["c1"] == {"action": "accept", "eval_id": "ev-001"}
    assert state.decisions["c2"] == {"action": "reject", "eval_id": None}
    assert pending(queue, state) == []


def test_apply_twice_changes_nothing(ctx):
    queue = [make(id="c1"), make(id="c2", question="A different question about claims?")]
    text = "c1 a\nc2 r\n"
    apply_decisions(text, queue, ctx)
    eval_bytes = ctx.eval_path.read_bytes()
    state_bytes = ctx.state_path.read_bytes()
    again = apply_decisions(text, queue, ctx)
    assert again.accepted == [] and again.rejected == [] and again.problems == []
    assert sorted(again.unchanged) == ["c1", "c2"]
    assert ctx.eval_path.read_bytes() == eval_bytes
    assert ctx.state_path.read_bytes() == state_bytes


def test_apply_resumes_when_the_file_grows(ctx):
    queue = [
        make(id="c1"),
        make(id="c2", question="What counts as insurable employment?"),
        make(id="c3", question="Do I qualify with fewer weeks of work?"),
    ]
    apply_decisions("c1 a\n", queue, ctx)
    report = apply_decisions("c1 a\nc2 a\nc3 r\n", queue, ctx)
    assert report.unchanged == ["c1"] and report.accepted == [("c2", "ev-002")]
    assert [i.id for i in load_eval_set(ctx.eval_path)] == ["ev-001", "ev-002"]


def test_apply_does_not_duplicate_when_the_state_file_was_lost(ctx):
    queue = [make(id="c1")]
    apply_decisions("c1 a\n", queue, ctx)
    ctx.state.decisions.clear()  # what a deleted review_state.json looks like
    ctx.state_path.unlink()
    report = apply_decisions("c1 a\n", queue, ctx)
    assert report.accepted == [] and report.problems == []
    assert len(load_eval_set(ctx.eval_path)) == 1
    assert load_state(ctx.state_path).decisions["c1"] == {"action": "accept", "eval_id": "ev-001"}


def test_apply_leaves_an_earlier_decision_alone_and_says_so(ctx):
    queue = [make(id="c1"), make(id="c2", question="A second, different question?")]
    apply_decisions("c1 r\nc2 a\n", queue, ctx)
    report = apply_decisions("c1 a\nc2 r\n", queue, ctx)
    assert report.accepted == [] and report.rejected == []
    assert len(report.problems) == 2 and "left unchanged" in report.problems[0]
    state = load_state(ctx.state_path)
    assert state.decisions["c1"]["action"] == "reject"
    assert state.decisions["c2"]["action"] == "accept"
    assert len(load_eval_set(ctx.eval_path)) == 1


def test_apply_skips_unknown_and_ungrounded_ids_but_applies_the_rest(ctx):
    queue = [make(id="c1"), make(id="c2", key_facts=["exactly 999 hours"])]
    report = apply_decisions("nope a\nc2 a\nc1 a\n", queue, ctx)
    assert report.accepted == [("c1", "ev-001")]
    assert any("nope: no such candidate" in p for p in report.problems)
    assert any(p.startswith("c2: cannot accept") for p in report.problems)
    assert "c2" not in load_state(ctx.state_path).decisions  # still pending, fixable


def test_apply_uses_edits_made_in_the_terminal(ctx):
    original = make(id="c1")
    edited = original.model_copy(update={"question": "What is the hours rule for a claim?"})
    ctx.state.edits["c1"] = edited.model_dump()
    (cand,) = apply_edits([original], ctx.state)
    apply_decisions("c1 a\n", [cand], ctx)
    assert load_eval_set(ctx.eval_path)[0].question == "What is the hours rule for a claim?"


def test_apply_cli_exit_codes_and_idempotent_rerun(settings, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(rv, "get_settings", lambda: settings)
    cand = tmp_path / "candidates.jsonl"
    cand.write_text(
        "".join(
            json.dumps(make(id=i, question=f"Question {i} about hours?").model_dump()) + "\n"
            for i in ("c1", "c2")
        )
    )
    base = ["--candidates", str(cand), "--eval-set", str(tmp_path / "e.jsonl")]
    base += ["--state", str(tmp_path / "s.json")]
    decisions = tmp_path / "decisions.txt"
    assert rv.main([*base, "--apply", str(tmp_path / "missing.txt")]) == 2
    decisions.write_text("c1 a\nc2 r\n")
    assert rv.main([*base, "--apply", str(decisions)]) == 0
    assert "accepted 1, rejected 1, already recorded 0, problems 0" in capsys.readouterr().out
    assert rv.main([*base, "--apply", str(decisions)]) == 0
    assert "accepted 0, rejected 0, already recorded 2, problems 0" in capsys.readouterr().out
    assert len(load_candidates(tmp_path / "e.jsonl")) == 1
    decisions.write_text("c1 a\ngarbage\n")
    assert rv.main([*base, "--apply", str(decisions)]) == 1  # applied what it could, then fails
