"""Triage eval data pipeline, offline: scenario drafting, silver labelling, blind gold review,
set building. Fake Anthropic client, temporary files."""

import json
from collections import Counter
from types import SimpleNamespace

import pytest

from support_desk_assistant.config import Settings
from support_desk_assistant.evals import triage_draft as td
from support_desk_assistant.evals import triage_label as tl
from support_desk_assistant.evals import triage_review as tr
from support_desk_assistant.triage.schema import PRIORITIES, QUEUES

TITLES = {q: [f"{q} page {i}" for i in range(10)] for q in td.PROGRAMS}


@pytest.fixture
def settings(tmp_path):
    prompt = tmp_path / "draft.md"
    prompt.write_text("You write tickets.\n")
    triage_prompt = tmp_path / "triage.md"
    triage_prompt.write_text("You triage tickets.\n")
    s = Settings(data_dir=tmp_path / "data")
    s.triage_eval.draft_prompt_path = prompt
    s.triage.system_prompt_path = triage_prompt
    return s


# --- drafting --------------------------------------------------------------------


def draft_reply(n, tag="t"):
    tickets = [{"subject": f"s{tag}{i}", "body": f"body {tag}{i}"} for i in range(n)]
    return {
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": json.dumps({"tickets": tickets})}],
        "usage": {"input_tokens": 500, "output_tokens": 300},
    }


class FakeDrafter:
    def __init__(self, shortfall=0):
        self.requests, self.shortfall = [], shortfall
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **request):
        self.requests.append(request)
        n = int(request["messages"][0]["content"].split("tickets: ")[1].split("\n")[0])
        reply = draft_reply(n - self.shortfall, str(len(self.requests)))
        return SimpleNamespace(to_dict=lambda: reply)


def test_specs_follow_the_configured_mix(settings):
    cfg = settings.triage_eval
    specs = td.plan_specs(settings)
    total = sum(cfg.style_counts.values())
    assert len(specs) == total
    assert Counter(s.style for s in specs) == Counter(cfg.style_counts)
    assert sum(s.program == "other" for s in specs) == round(total * cfg.other_share)
    per_program = Counter(s.program for s in specs if s.program != "other")
    assert max(per_program.values()) - min(per_program.values()) <= 1
    assert all(s.topic in cfg.other_topics for s in specs if s.program == "other")
    assert all(s.topic == "" for s in specs if s.program != "other")
    real = [s for s in specs if s.style != "greeting"]
    high = sum(s.situation in cfg.high_stakes_situations for s in real) / len(real)
    assert 0.3 < high < 0.5
    assert {s.situation for s in specs if s.style == "greeting"} == {td.NO_SITUATION}
    assert specs == td.plan_specs(settings)  # deterministic


def test_the_drafter_never_sees_a_label_or_the_rules(settings):
    tasks = td.plan_tasks(settings, TITLES)
    forbidden = ("priority", "urgent", "queue", "high", "normal", "low")
    for task in tasks:
        words = set(td.render_user_message(task).lower().replace(";", " ").split())
        assert not words & set(forbidden)
    for situation in (
        *settings.triage_eval.high_stakes_situations,
        *settings.triage_eval.other_situations,
    ):
        assert "urgent" not in situation and "priority" not in situation
    prompt = (td.Path(__file__).resolve().parents[1] / "prompts" / "triage_draft.md").read_text()
    for needle in ("urgent:", "high:", "normal:", "low:", "priority"):
        assert needle not in prompt.lower().replace("how urgent", "")


def test_tasks_group_by_program(settings):
    tasks = td.plan_tasks(settings, TITLES)
    assert sum(len(t.specs) for t in tasks) == sum(settings.triage_eval.style_counts.values())
    for t in tasks:
        assert 1 <= len(t.specs) <= 3 and {s.program for s in t.specs} == {t.program}
        assert (len(t.hints) == 6) == (t.program != "other")
        assert all(h.startswith(t.program) for h in t.hints)


def test_user_message_lists_each_ticket(settings):
    task = td.Task("cpp", (td.Spec("cpp", "", "asking how to apply", "short"),), ("a", "b"))
    msg = td.render_user_message(task)
    assert "topic: cpp" in msg and "- a" in msg and "tickets: 1" in msg
    assert "1. situation: asking how to apply; style: short" in msg
    other = td.Task("other", (td.Spec("other", "citizenship", "asking how to apply", "plain"),), ())
    assert "1. topic: citizenship; situation:" in td.render_user_message(other)


def test_run_tasks_assigns_ids_and_carries_the_scenario(settings):
    tasks = td.plan_tasks(settings, TITLES)[:3]
    client = FakeDrafter()
    run = td.run_tasks(tasks, system="SYS", settings=settings, client=client)
    assert run.calls == 3 and not run.failures and not run.shortfalls
    n = sum(len(t.specs) for t in tasks)
    prefix = settings.triage_eval.id_prefix
    assert [c.id for c in run.candidates] == [f"{prefix}-{i:03d}" for i in range(1, n + 1)]
    first = run.candidates[0]
    assert (first.topic, first.situation, first.style) == (
        tasks[0].specs[0].program,
        tasks[0].specs[0].situation,
        tasks[0].specs[0].style,
    )
    again = td.run_tasks(tasks, system="SYS", settings=settings, client=client)
    assert again.calls == 0 and again.cache_hits == 3 and len(client.requests) == 3
    assert again.candidates == run.candidates


def test_shortfall_failure_and_cap(settings):
    tasks = td.plan_tasks(settings, TITLES)[:1]
    short = td.run_tasks(tasks, system="S", settings=settings, client=FakeDrafter(shortfall=1))
    assert len(short.candidates) == len(tasks[0].specs) - 1 and "asked for" in short.shortfalls[0]
    bad = SimpleNamespace(to_dict=lambda: {"stop_reason": "refusal", "content": []})
    client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: bad))
    other = settings.model_copy(deep=True)
    other.triage_eval.draft_seed += 1  # different requests, so no cached reply is replayed
    refused = td.run_tasks(
        td.plan_tasks(other, TITLES)[:1], system="S", settings=other, client=client
    )
    assert refused.candidates == [] and "refusal" in refused.failures[0]
    fresh = FakeDrafter()
    capped = td.run_tasks(
        td.plan_tasks(settings, TITLES)[5:8],
        system="S2",
        settings=settings,
        client=fresh,
        cap_cad=0,
    )
    assert fresh.requests == [] and "spend cap" in capped.failures[0]


def test_near_duplicates_are_dropped_against_earlier_sets_and_each_other(tmp_path):
    def ticket(tid, subject, body):
        return td.TicketCandidate(
            id=tid, subject=subject, body=body, style="plain", topic="ei", situation="x"
        )

    text = "my payment stopped last week and I need to know when it restarts please"
    earlier = tmp_path / "triage_set_v1.jsonl"
    earlier.write_text(
        json.dumps({"id": "tc-001", "subject": "Payment", "body": text}) + "\n", encoding="utf-8"
    )
    reference = td.load_reference([earlier, tmp_path / "missing.jsonl"])
    assert list(reference) == ["triage_set_v1:tc-001"]
    candidates = [
        ticket("t2-001", "Payment", text + " thanks"),  # repeats the earlier ticket
        ticket("t2-002", "Hello", "I lost my passport in Lisbon and fly home in ten days"),
        ticket("t2-003", "Hello!", "I lost my passport in Lisbon and fly home in ten days"),
        ticket("t2-004", "SIN", "How do I apply for a social insurance number as a student"),
    ]
    kept, dropped = td.drop_near_duplicates(candidates, reference, 0.6)
    assert [c.id for c in kept] == ["t2-002", "t2-004"]
    assert dropped == {"t2-001": "triage_set_v1:tc-001", "t2-003": "t2-002"}
    assert td.drop_near_duplicates(candidates, {}, 1.01)[1] == {}


def test_candidates_roundtrip(settings):
    run = td.run_tasks(
        td.plan_tasks(settings, TITLES)[:2], system="S", settings=settings, client=FakeDrafter()
    )
    td.write_candidates(settings.triage_candidates_path, run.candidates)
    assert td.load_candidates(settings.triage_candidates_path) == run.candidates
    assert td.load_candidates(settings.triage_candidates_path.with_name("none.jsonl")) == []


# --- labelling -------------------------------------------------------------------


def cand(i, style="plain"):
    return td.TicketCandidate(
        id=f"tc-{i:03d}", subject=f"s{i}", body=f"body {i}", style=style, topic="ei", situation="x"
    )


def label_reply(queue, priority):
    text = json.dumps(
        {
            "rationale": "r",
            "queue": queue,
            "priority": priority,
            "queue_confidence": "high",
            "priority_confidence": "high",
        }
    )
    return {
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": 400, "output_tokens": 80},
    }


class FakeLabeller:
    """Labels ticket ``i`` from a table keyed by body; records the models it was called with."""

    def __init__(self, table, refuse=()):
        self.table, self.refuse, self.models = table, set(refuse), []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **request):
        self.models.append(request["model"])
        body = request["messages"][0]["content"].split("<body>")[1].split("</body>")[0]
        if body in self.refuse:
            reply = {"stop_reason": "refusal", "content": []}
        else:
            reply = label_reply(*self.table[body])
        return SimpleNamespace(to_dict=lambda: reply)


def test_split_is_deterministic_and_balanced(settings):
    ids = [f"tc-{i:04d}" for i in range(2000)]
    splits = [tl.split_of(i, settings) for i in ids]
    assert splits == [tl.split_of(i, settings) for i in ids]
    assert 0.45 < splits.count("test") / len(ids) < 0.55
    salted = settings.model_copy(deep=True)
    salted.triage_eval.split_salt = "other"
    assert [tl.split_of(i, salted) for i in ids] != splits


def test_labeller_uses_the_label_model_without_touching_the_original(settings):
    lab = tl.labeller_settings(settings)
    assert lab.triage.model == settings.triage_eval.label_model != settings.triage.model
    assert lab.triage.max_tokens == settings.triage_eval.label_max_tokens
    assert settings.triage.model == "claude-haiku-5-5" and settings.triage.max_tokens == 400


def test_silver_labels_use_sonnet_and_cache(settings):
    cands = [cand(1), cand(2), cand(3)]
    table = {"body 1": ("ei", "urgent"), "body 2": ("cpp", "low"), "body 3": ("sin", "high")}
    client = FakeLabeller(table, refuse={"body 3"})
    silver, notes = tl.label_silver(cands, settings, client=client)
    assert notes == [] and set(client.models) == {"claude-sonnet-5-5"}
    assert [(s.id, s.status, s.queue, s.priority) for s in silver] == [
        ("tc-001", "ok", "ei", "urgent"),
        ("tc-002", "ok", "cpp", "low"),
        ("tc-003", "refusal", None, None),
    ]
    again, _ = tl.label_silver(cands, settings, client=client)
    assert len(client.models) == 3 and sum(s.cost_cad for s in again) == 0


def test_silver_cap_stops_before_the_call(settings):
    client = FakeLabeller({"body 1": ("ei", "low")})
    silver, notes = tl.label_silver([cand(1)], settings, client=client, cap_cad=0)
    assert silver == [] and client.models == [] and "tc-001" in notes[0]


def silver_pool(settings, n=400):
    """n silver labels spread evenly over the four priorities."""
    return [
        tl.SilverLabel(
            id=f"tc-{i:03d}",
            status="ok",
            queue="ei",
            priority=PRIORITIES[i % 4],
            rationale="r",
            model="m",
        )
        for i in range(n)
    ]


def test_gold_sample_is_stratified_by_priority_and_split(settings):
    cands = [cand(i) for i in range(400)]
    sample = tl.gold_sample(cands, silver_pool(settings), settings)
    assert sample.shortfalls == []
    by_priority = Counter(PRIORITIES[int(i[3:]) % 4] for i in sample.ids)
    assert by_priority == Counter(settings.triage_eval.gold_sample)
    urgent = [i for i in sample.ids if PRIORITIES[int(i[3:]) % 4] == "urgent"]
    assert Counter(tl.split_of(i, settings) for i in urgent) == {"test": 8, "dev": 7}
    assert len(set(sample.ids)) == len(sample.ids) == 50
    assert sample == tl.gold_sample(cands, silver_pool(settings), settings)


def test_gold_sample_reports_shortfalls_and_skips_failed_silver(settings):
    silver = silver_pool(settings, n=20)
    silver[0] = silver[0].model_copy(update={"status": "refusal", "priority": None, "queue": None})
    sample = tl.gold_sample([cand(i) for i in range(20)], silver, settings)
    assert sample.shortfalls and "tc-000" not in sample.ids
    assert len(sample.ids) < 50


def test_build_set_prefers_gold_and_records_the_source(settings):
    cands = [cand(1), cand(2), cand(3), cand(4)]
    silver = [
        tl.SilverLabel(
            id="tc-001", status="ok", queue="ei", priority="low", rationale="r", model="m"
        ),
        tl.SilverLabel(
            id="tc-002", status="ok", queue="cpp", priority="high", rationale="r", model="m"
        ),
        tl.SilverLabel(
            id="tc-003", status="refusal", queue=None, priority=None, rationale=None, model="m"
        ),
    ]
    gold = [
        tl.GoldLabel(id="tc-001", queue="oas", priority="urgent"),
        tl.GoldLabel(id="tc-003", queue="sin", priority="normal", labelled_by="model-agreed:a,b"),
    ]
    items = tl.build_set(cands, silver, gold, settings)
    by = {i["id"]: i for i in items}
    assert set(by) == {"tc-001", "tc-002", "tc-003"}  # tc-004 has no label at all
    assert (by["tc-001"]["queue"], by["tc-001"]["priority"], by["tc-001"]["label_source"]) == (
        "oas",
        "urgent",
        "gold",
    )
    assert (by["tc-001"]["silver_queue"], by["tc-001"]["silver_priority"]) == ("ei", "low")
    assert by["tc-002"]["label_source"] == "silver" and by["tc-002"]["gold_queue"] is None
    assert by["tc-003"]["label_source"] == "gold" and by["tc-003"]["silver_queue"] is None
    assert [by[k]["labelled_by"] for k in ("tc-001", "tc-002", "tc-003")] == [
        "human",
        None,
        "model-agreed:a,b",
    ]
    assert all(i["split"] == tl.split_of(i["id"], settings) for i in items)
    assert all(i["queue"] in QUEUES and i["priority"] in PRIORITIES for i in items)


def test_agreement_counts_matches_on_the_gold_sample():
    silver = [
        tl.SilverLabel(id=f"t{i}", status="ok", queue=q, priority=p, rationale="r", model="m")
        for i, (q, p) in enumerate([("ei", "low"), ("ei", "high"), ("cpp", "low")])
    ]
    gold = [
        tl.GoldLabel(id="t0", queue="ei", priority="low"),
        tl.GoldLabel(id="t1", queue="ei", priority="urgent"),
        tl.GoldLabel(id="t2", queue="oas", priority="low"),
    ]
    assert tl.agreement(silver, gold) == {"n": 3, "queue": 2, "priority": 2, "both": 1}


# --- blind review ----------------------------------------------------------------


def test_parse_decisions():
    text = "# note\ntc-001 ei urgent\ntc-002 skip\n\ntc-003 CPP High\nbad line\ntc-004 tax urgent\n"
    decisions, problems = tr.parse_decisions(text)
    assert decisions == {
        "tc-001": tr.Decision("label", "ei", "urgent"),
        "tc-002": tr.Decision("skip"),
        "tc-003": tr.Decision("label", "cpp", "high"),
    }
    assert len(problems) == 2 and "line 6" in problems[0] and "line 7" in problems[1]


def test_a_trailing_question_mark_marks_an_unsure_label():
    text = "tc-001 ei high ?\ntc-002 cpp low\ntc-003 skip ?\ntc-004 oas low maybe\n"
    decisions, problems = tr.parse_decisions(text)
    assert decisions["tc-001"] == tr.Decision("label", "ei", "high", unsure=True)
    assert decisions["tc-002"].unsure is False
    assert set(decisions) == {"tc-001", "tc-002"} and len(problems) == 2


def test_unsure_travels_from_the_decision_to_the_set(settings):
    cands = [cand(1), cand(2)]
    tr.apply_decisions(
        "tc-001 ei high ?\ntc-002 cpp low\n", cands, settings, labelled_by="model-session:x"
    )
    gold = tl.load_jsonl(settings.triage_gold_path, tl.GoldLabel)
    assert [(g.id, g.unsure) for g in gold] == [("tc-001", True), ("tc-002", False)]
    items = {i["id"]: i for i in tl.build_set(cands, [], gold, settings)}
    assert items["tc-001"]["unsure"] is True and items["tc-002"]["unsure"] is False
    assert items["tc-001"]["label_source"] == "gold" and items["tc-001"]["silver_queue"] is None


def test_parse_decisions_repeat_last_wins_and_warns():
    decisions, problems = tr.parse_decisions("tc-001 ei urgent\ntc-001 skip\n")
    assert decisions["tc-001"].action == "skip" and "twice" in problems[0]


def test_apply_records_gold_and_skips_and_is_idempotent(settings):
    cands = [cand(1), cand(2), cand(3)]
    text = "tc-001 oas urgent\ntc-002 skip\ntc-999 ei low\n"
    report = tr.apply_decisions(text, cands, settings)
    assert report.labelled == ["tc-001"] and report.skipped == ["tc-002"]
    assert any("no such ticket" in p for p in report.problems)
    gold = tl.load_jsonl(settings.triage_gold_path, tl.GoldLabel)
    assert gold == [tl.GoldLabel(id="tc-001", queue="oas", priority="urgent")]
    again = tr.apply_decisions(text, cands, settings)
    assert again.unchanged == ["tc-001", "tc-002"] and not again.labelled
    assert len(tl.load_jsonl(settings.triage_gold_path, tl.GoldLabel)) == 1


def test_apply_records_who_made_the_decisions(settings):
    cands = [cand(1), cand(2)]
    tr.apply_decisions("tc-001 oas urgent\n", cands, settings)
    tr.apply_decisions("tc-002 ei low\n", cands, settings, labelled_by="model-session:claude-x")
    gold = tl.load_jsonl(settings.triage_gold_path, tl.GoldLabel)
    assert [(g.id, g.labelled_by) for g in gold] == [
        ("tc-001", "human"),
        ("tc-002", "model-session:claude-x"),
    ]


def test_show_prints_only_the_ticket(settings, monkeypatch, capsys):
    cands = [cand(i) for i in range(400)]
    td.write_candidates(settings.triage_candidates_path, cands)
    tl.write_jsonl(settings.triage_silver_path, silver_pool(settings))
    monkeypatch.setattr(tr, "get_settings", lambda: settings)
    assert tr.main(["--show"]) == 0
    out = capsys.readouterr().out
    assert out.count("=== tc-") == 50 and "Subject: s" in out and "body " in out
    for hidden in ("silver", "rationale", "urgent", "priority", "topic", "situation"):
        assert hidden not in out.lower()
    assert (
        tr.main(["--apply", str(_write(settings, "tc-001 ei low\n")), "--labelled-by", "m:x"]) == 0
    )
    assert tl.load_jsonl(settings.triage_gold_path, tl.GoldLabel)[0].labelled_by == "m:x"


def test_show_all_needs_no_silver_labels(settings, monkeypatch, capsys):
    cands = [cand(i) for i in range(5)]
    td.write_candidates(settings.triage_candidates_path, cands)
    monkeypatch.setattr(tr, "get_settings", lambda: settings)
    assert tr.main(["--show"]) == 2  # the sample is chosen from silver labels
    capsys.readouterr()
    tr.apply_decisions("tc-000 ei low\n", cands, settings)
    assert tr.main(["--show", "--all"]) == 0
    out = capsys.readouterr().out
    assert out.count("=== tc-") == 4 and "=== tc-000 ===" not in out  # decided ones are skipped
    path = _write(settings, "tc-001 cpp high ?\n")
    assert tr.main(["--apply", str(path), "--labelled-by", "m:x"]) == 0
    assert "labels on record: 2 of 5 tickets" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        tr.main(["--all"])


def test_the_labeller_refuses_to_call_anything_but_haiku(settings, monkeypatch, capsys):
    td.write_candidates(settings.triage_candidates_path, [cand(1)])
    monkeypatch.setattr(tl, "get_settings", lambda: settings)
    assert settings.triage_eval.label_model == "claude-sonnet-5-5"
    assert tl.main(["--yes", "--cap", "99"]) == 2
    out = capsys.readouterr().out
    assert "Haiku only" in out and not settings.triage_silver_path.exists()


def _write(settings, text):
    path = settings.triage_gold_path.with_name("decisions.txt")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_apply_reports_a_conflict(settings):
    cands = [cand(1)]
    tr.apply_decisions("tc-001 skip\n", cands, settings)
    report = tr.apply_decisions("tc-001 ei low\n", cands, settings)
    assert any("already skip" in p for p in report.problems) and not report.labelled


def test_page_is_blind(settings):
    c = td.TicketCandidate(
        id="tc-001",
        subject="",
        body="<b>x</b> & more",
        style="injection",
        topic="passport",
        situation="a secret situation",
    )
    page = tr.render_page([c])
    card = page.split('<section class="card"')[1].split("</section>")[0]
    for leak in ("injection", "zzz-topic", "a secret situation", "silver", "rationale", "draft"):
        assert leak not in card
    assert "&lt;b&gt;x&lt;/b&gt;" in page and "(no subject)" in page
    assert page.count('<option value="">choose</option>') == 2  # nothing preselected
    assert "selected" not in card
    assert "Skip" in page and "Copy decisions" in page and "Queue and priority rules" in page


def test_pending_sample_excludes_decided_tickets(settings):
    cands = [cand(i) for i in range(400)]
    silver = silver_pool(settings)
    queue, shortfalls = tr.pending_sample(cands, silver, settings)
    assert len(queue) == 50 and shortfalls == []
    tr.apply_decisions(f"{queue[0].id} skip\n{queue[1].id} ei low\n", cands, settings)
    again, _ = tr.pending_sample(cands, silver, settings)
    assert len(again) == 48 and queue[0].id not in {c.id for c in again}


def test_terminal_decision_accepts_short_priorities():
    assert tr.terminal_decision("ei h") == "ei high"
    assert tr.terminal_decision("  Passport  URGENT ") == "passport urgent"
    assert tr.terminal_decision("skip") == "skip"
    for bad in ("", "ei", "tax high", "ei soon", "ei high now", "skip it"):
        assert tr.terminal_decision(bad) is None


def test_terminal_review_records_each_answer_and_resumes(settings):
    cands = [cand(i) for i in range(400)]
    silver = silver_pool(settings)
    queue, _ = tr.pending_sample(cands, silver, settings)
    shown: list[str] = []
    answers = iter(["ei h", "nonsense", "skip", "other l", "quit", "never read"])
    report = tr.review_in_terminal(
        cands, silver, settings, read=lambda _: next(answers), write=shown.append
    )
    assert report.labelled == [queue[0].id, queue[2].id] and report.skipped == [queue[1].id]
    gold = tl.load_jsonl(settings.triage_gold_path, tl.GoldLabel)
    assert [(g.id, g.queue, g.priority, g.labelled_by) for g in gold] == [
        (queue[0].id, "ei", "high", "human"),
        (queue[2].id, "other", "low", "human"),
    ]
    text = "\n".join(shown)
    assert queue[0].body in text and "expected '<queue> <priority>'" in text
    for hidden in ("silver", "rationale", "topic", "situation"):  # blind: nothing but the ticket
        assert hidden not in text.split("[1/50]")[1].lower()
    again = tr.pending_sample(cands, silver, settings)[0]
    assert len(again) == 47 and again[0].id == queue[3].id


def test_terminal_review_stops_at_end_of_input(settings):
    cands = [cand(i) for i in range(400)]

    def eof(_prompt):
        raise EOFError

    report = tr.review_in_terminal(cands, silver_pool(settings), settings, read=eof, write=print)
    assert not report.labelled and not report.skipped


def test_status_shows_agreement_only_after_labelling(settings):
    cands = [cand(i) for i in range(400)]
    silver = silver_pool(settings)
    assert not any(
        "silver vs your labels" in line for line in tr.status_lines(cands, silver, settings)
    )
    queue, _ = tr.pending_sample(cands, silver, settings)
    silver_by = {s.id: s for s in silver}
    first = silver_by[queue[0].id]
    tr.apply_decisions(f"{first.id} {first.queue} {first.priority}\n", cands, settings)
    lines = tr.status_lines(cands, silver, settings)
    assert "labelled 1" in lines[0] and "queue 1, priority 1, both 1" in lines[-1]
