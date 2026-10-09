"""Pipeline tests: no network, no key. Retrieval is stubbed, Anthropic is a fake client."""

import json

import anthropic
import httpx
import pytest

from fakes import FakeAnthropic, make_message
from support_desk_assistant.answer import pipeline
from support_desk_assistant.answer.cli import format_outcome, run_smoke
from support_desk_assistant.answer.pipeline import (
    SystemPromptNotReady,
    estimate_cost_usd,
    execute,
    prepare,
)
from support_desk_assistant.answer.validate import ValidationResult
from support_desk_assistant.config import AnswerSettings, ModelProfile, Settings
from support_desk_assistant.retrieve import Hit

URL1 = "https://www.canada.ca/en/services/benefits/ei/eligibility.html"
URL2 = "https://www.canada.ca/en/services/benefits/ei/apply.html"
GOOD = json.dumps(
    {
        "answer": "420 to 700 hours.",
        "citations": ["id-1"],
        "abstained": False,
        "abstain_reason": None,
    }
)


def hit(chunk_id="id-1", url=URL1, score=0.6):
    return Hit(
        score=score,
        program="ei",
        title="Eligibility",
        heading_path=["Hours"],
        url=url,
        text="You need between 420 and 700 hours.",
        chunk_id=chunk_id,
    )


@pytest.fixture
def hits():
    return [hit("id-1", URL1, 0.6), hit("id-2", URL2, 0.5)]


@pytest.fixture
def settings(tmp_path):
    (tmp_path / "prompt.md").write_text("You answer from the chunks only.\n")
    (tmp_path / "manifest.jsonl").write_text(
        json.dumps(
            {"url": URL1, "crawled_at": "2026-10-06T00:00:00Z", "date_modified": "2026-09-29"}
        )
        + "\n"
    )
    return Settings(
        data_dir=tmp_path,
        logs_dir=tmp_path / "logs",
        answer=AnswerSettings(system_prompt_path=tmp_path / "prompt.md", min_score=0.3),
    )


@pytest.fixture(autouse=True)
def stub_search(monkeypatch, hits):
    state = {"hits": hits}
    monkeypatch.setattr(pipeline, "search", lambda *a, **k: state["hits"])
    return state


def passing(result, ids):
    return ValidationResult()


def run(settings, client, question="Hours for EI?", model=None, validator=passing):
    prepared = prepare(question, settings=settings, embedder=None, qdrant=None, model=model)
    return execute(prepared, settings=settings, client=client, validator=validator)


def log_lines(settings):
    return [json.loads(x) for x in settings.request_log_path.read_text().splitlines()]


# --- gate ------------------------------------------------------------------------


def test_low_top_score_abstains_without_calling_claude(settings, stub_search):
    stub_search["hits"] = [hit(score=0.2)]
    client = FakeAnthropic()  # any call would raise IndexError
    out = run(settings, client)
    assert client.calls == []
    assert out.result.abstained and out.result.citations == [] and out.result.answer == ""
    assert (out.gate, out.status, out.cost_usd, out.cost_cad) == ("abstain_low_score", "gate", 0, 0)
    line = log_lines(settings)[0]
    assert line["gate"] == "abstain_low_score" and line["api_called"] is False


def test_gate_abstains_on_empty_retrieval_and_needs_no_system_prompt(settings, stub_search):
    stub_search["hits"] = []
    settings.answer.system_prompt_path.unlink()
    assert run(settings, FakeAnthropic()).result.abstained


def test_score_exactly_at_threshold_passes(settings, stub_search):
    stub_search["hits"] = [hit(score=0.3)]
    assert run(settings, FakeAnthropic(make_message(GOOD))).gate == "pass"


# --- the request -------------------------------------------------------------------


def test_request_shape_for_haiku(settings):
    client = FakeAnthropic(make_message(GOOD))
    run(settings, client)
    (call,) = client.calls
    assert call["model"] == "claude-haiku-5-5"
    assert call["max_tokens"] == 500
    assert call["thinking"] == {"type": "disabled"}
    assert call["output_config"]["effort"] == "low"
    fmt = call["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert set(fmt["schema"]["required"]) == {"answer", "citations", "abstained", "abstain_reason"}
    for banned in ("temperature", "top_p", "top_k"):
        assert banned not in call
    assert call["system"] == [{"type": "text", "text": "You answer from the chunks only.\n"}]


def test_chunks_carry_id_title_url_and_both_dates(settings):
    client = FakeAnthropic(make_message(GOOD))
    run(settings, client)
    text = client.calls[0]["messages"][0]["content"]
    assert 'chunk_id="id-1"' in text and 'title="Eligibility"' in text and f'url="{URL1}"' in text
    assert 'crawled_at="2026-10-06T00:00:00Z"' in text and 'date_modified="2026-09-29"' in text
    assert text.rstrip().endswith("</question>") and "Hours for EI?" in text


def test_url_missing_from_manifest_gives_null_dates(settings):
    client = FakeAnthropic(make_message(GOOD))
    run(settings, client)
    second = client.calls[0]["messages"][0]["content"].split("<chunk ")[2]
    assert 'crawled_at="null"' in second and 'date_modified="null"' in second


def test_missing_manifest_file_does_not_crash(settings):
    settings.manifest_path.unlink()
    assert run(settings, FakeAnthropic(make_message(GOOD))).status == "ok"


def test_sonnet_profile_thinks_and_has_headroom(settings):
    client = FakeAnthropic(make_message(GOOD))
    run(settings, client, model="claude-sonnet-5-5")
    call = client.calls[0]
    assert call["model"] == "claude-sonnet-5-5" and call["thinking"] == {"type": "adaptive"}
    assert call["max_tokens"] > 500


def test_prompt_caching_flag_defaults_off_and_adds_cache_control_when_on(settings):
    off = FakeAnthropic(make_message(GOOD))
    run(settings, off)
    assert "cache_control" not in off.calls[0]["system"][0]

    settings.answer.prompt_caching = True
    on = FakeAnthropic(make_message(GOOD))
    run(settings, on, question="A different question?")
    assert on.calls[0]["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_unknown_model_is_rejected_before_any_work(settings):
    with pytest.raises(ValueError):
        prepare("q", settings=settings, embedder=None, qdrant=None, model="claude-nope")


def test_todo_only_system_prompt_blocks_the_call(settings):
    settings.answer.system_prompt_path.write_text("# TODO: answer system prompt\n")
    with pytest.raises(SystemPromptNotReady):
        prepare("q", settings=settings, embedder=None, qdrant=None)


# --- result, cost and log -----------------------------------------------------------


def test_happy_path_result_cost_and_log(settings):
    out = run(settings, FakeAnthropic(make_message(GOOD, input_tokens=1000, output_tokens=100)))
    assert out.result.citations == ["id-1"] and out.status == "ok" and out.api_called
    assert out.cost_usd == pytest.approx(0.00015) and out.cost_cad == pytest.approx(0.00015 * 1.38)
    (line,) = log_lines(settings)
    assert line["question"] == "Hours for EI?" and line["model"] == "claude-haiku-5-5"
    assert line["retrieved"] == [
        {"chunk_id": "id-1", "score": 0.6},
        {"chunk_id": "id-2", "score": 0.5},
    ]
    assert (line["input_tokens"], line["output_tokens"], line["cache_hit"]) == (1000, 100, False)
    assert line["cost_usd"] == 0.00015 and line["cost_cad"] > line["cost_usd"]
    assert line["validation"] == {"implemented": True, "ok": True, "failed_rules": []}
    assert isinstance(line["latency_ms"], int) and line["timestamp"].endswith("+00:00")


def test_failed_validation_is_logged(settings):
    out = run(
        settings,
        FakeAnthropic(make_message(GOOD)),
        validator=lambda r, ids: ValidationResult(("no_citations",)),
    )
    assert not out.validation.ok
    assert log_lines(settings)[0]["validation"]["failed_rules"] == ["no_citations"]


def test_validator_receives_retrieved_ids(settings):
    seen = []
    run(
        settings,
        FakeAnthropic(make_message(GOOD)),
        validator=lambda r, ids: seen.append(ids) or ValidationResult(),
    )
    assert seen == [["id-1", "id-2"]]


def test_unimplemented_validator_does_not_lose_the_response(settings):
    def todo(result, ids):
        raise NotImplementedError

    out = run(settings, FakeAnthropic(make_message(GOOD)), validator=todo)
    assert out.validation is None and out.result.citations == ["id-1"]
    assert log_lines(settings)[0]["validation"]["implemented"] is False
    assert "SKIPPED" in format_outcome(out)


@pytest.mark.parametrize(
    ("message", "status"),
    [
        (make_message("", stop_reason="refusal"), "refusal"),
        (make_message('{"answer": "cut off', stop_reason="max_tokens"), "max_tokens"),
        (make_message("not json"), "invalid_output"),
        (make_message('{"answer": "x"}'), "invalid_output"),
    ],
)
def test_incomplete_generation_becomes_an_explained_abstention(settings, message, status):
    out = run(settings, FakeAnthropic(message))
    assert out.status == status
    assert out.result.abstained and out.result.citations == [] and out.result.answer == ""
    assert status in out.result.abstain_reason
    assert out.cost_usd > 0  # tokens were still spent


# --- response cache ------------------------------------------------------------------


def test_second_identical_request_is_a_free_cache_hit(settings):
    client = FakeAnthropic(make_message(GOOD))
    run(settings, client)
    calls = []
    out = run(settings, client, validator=lambda r, ids: calls.append(1) or ValidationResult())
    assert len(client.calls) == 1
    assert out.cache_hit and not out.api_called and out.cost_usd == 0 and out.cost_cad == 0
    assert out.result.citations == ["id-1"]
    assert calls == [1]  # validator re-ran on the cached response
    first, second = log_lines(settings)
    assert (first["cache_hit"], second["cache_hit"]) == (False, True)
    assert second["cost_usd"] == 0 and second["input_tokens"] == 0


@pytest.mark.parametrize(
    "message",
    [make_message("", stop_reason="refusal"), make_message("{", stop_reason="max_tokens")],
)
def test_refusals_and_max_tokens_are_cached_too(settings, message):
    client = FakeAnthropic(message)
    first = run(settings, client)
    second = run(settings, client)
    assert len(client.calls) == 1 and second.cache_hit and second.status == first.status


def test_cached_response_is_reparsed_so_validator_changes_apply(settings):
    client = FakeAnthropic(make_message(GOOD))
    run(settings, client)
    out = run(settings, client, validator=lambda r, ids: ValidationResult(("no_citations",)))
    assert out.cache_hit and out.validation.failed_rules == ("no_citations",)


def test_api_errors_are_not_cached_or_logged(settings):
    error = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))
    client = FakeAnthropic(error, make_message(GOOD))
    with pytest.raises(anthropic.APIConnectionError):
        run(settings, client)
    assert not settings.answer_cache_dir.exists() and not settings.request_log_path.exists()
    assert run(settings, client).api_called  # retry goes to the API, not a cache entry


def _key(settings):
    return prepare("Hours for EI?", settings=settings, embedder=None, qdrant=None).key


def test_cache_key_is_stable_and_sensitive_to_every_input(settings, tmp_path, stub_search):
    base = _key(settings)
    assert _key(settings) == base

    def changed(**profile):
        old = settings.answer.models["claude-haiku-5-5"]
        settings.answer.models["claude-haiku-5-5"] = ModelProfile(**{**old.model_dump(), **profile})
        key = _key(settings)
        settings.answer.models["claude-haiku-5-5"] = old
        return key

    keys = {
        changed(max_tokens=501),
        changed(effort="medium"),
        changed(thinking="adaptive"),
    }
    settings.answer.schema_version = 2
    keys.add(_key(settings))
    settings.answer.schema_version = 1
    (tmp_path / "prompt.md").write_text("A different system prompt.\n")
    keys.add(_key(settings))
    (tmp_path / "prompt.md").write_text("You answer from the chunks only.\n")
    stub_search["hits"] = [hit("id-9")]
    keys.add(_key(settings))
    # edited chunk text under the same ID must change the key too
    stub_search["hits"] = [Hit(**{**hit().__dict__, "text": "edited"}), hit("id-2", URL2, 0.5)]
    keys.add(_key(settings))
    assert base not in keys and len(keys) == 7
    assert prepare("Another?", settings=settings, embedder=None, qdrant=None).key not in keys | {
        base
    }
    sonnet = prepare(
        "Hours for EI?", settings=settings, embedder=None, qdrant=None, model="claude-sonnet-5-5"
    )
    assert sonnet.key not in keys | {base}


def test_prompt_caching_flag_does_not_change_the_cache_key(settings):
    base = _key(settings)
    settings.answer.prompt_caching = True
    assert _key(settings) == base


# --- CLI output and smoke ------------------------------------------------------------


def test_format_outcome_numbers_citations_with_urls_and_shows_cad(settings):
    reply = json.dumps(
        {
            "answer": "420 to 700 hours.",
            "citations": ["id-2", "id-1", "ghost"],
            "abstained": False,
            "abstain_reason": None,
        }
    )
    text = format_outcome(run(settings, FakeAnthropic(make_message(reply))))
    assert "420 to 700 hours." in text
    assert f"[1] Eligibility - {URL2}" in text and f"[2] Eligibility - {URL1}" in text
    assert "[3] ghost (NOT in retrieved set)" in text
    assert "Cost: CA$0.0002" in text


def test_format_outcome_shows_abstain_reason_and_failed_rules(settings, stub_search):
    stub_search["hits"] = [hit(score=0.1)]
    text = format_outcome(
        run(settings, FakeAnthropic(), validator=lambda r, ids: ValidationResult(("x",)))
    )
    assert "ABSTAINED" in text and "Reason:" in text and "Validation FAILED: x" in text
    assert "gate: no API call" in text


def test_estimate_is_zero_for_gate_and_cached_and_bounded_otherwise(settings):
    p = prepare("Hours for EI?", settings=settings, embedder=None, qdrant=None)
    est = estimate_cost_usd(p, settings)
    assert 0 < est < 0.001
    execute(p, settings=settings, client=FakeAnthropic(make_message(GOOD)))
    cached = prepare("Hours for EI?", settings=settings, embedder=None, qdrant=None)
    assert estimate_cost_usd(cached, settings) == 0


def test_smoke_declined_makes_no_calls(settings, monkeypatch, capsys):
    client = FakeAnthropic()
    monkeypatch.setattr(pipeline, "make_anthropic_client", lambda s: client)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert run_smoke(settings, None, None, None) == 1
    out = capsys.readouterr().out
    assert "Estimated cost" in out and "Cancelled" in out
    assert client.calls == [] and not settings.request_log_path.exists()


def test_smoke_confirmed_runs_three_questions_and_prints_total(settings, monkeypatch, capsys):
    client = FakeAnthropic(*[make_message(GOOD)] * 3)
    monkeypatch.setattr(pipeline, "make_anthropic_client", lambda s: client)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    assert run_smoke(settings, None, None, None) == 0
    out = capsys.readouterr().out
    assert len(client.calls) == 3 and "Actual total cost: CA$0.0006" in out
