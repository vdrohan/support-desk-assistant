import hashlib
import json
import re
from datetime import UTC, datetime, timedelta, timezone

import pytest

from support_desk_assistant.config import Settings
from support_desk_assistant.evals import provenance
from support_desk_assistant.evals.provenance import (
    collect_provenance,
    config_hash,
    dirty_blocks_results,
    file_sha256,
    git_state,
    new_run_id,
)

FIXED = datetime(2026, 10, 16, 9, 5, 7, 123456, tzinfo=UTC)


def fake_git(responses: dict[tuple[str, ...], str]):
    def _git(*args: str) -> str:
        return responses.get(args, "")

    return _git


CLEAN = {("rev-parse", "--short", "HEAD"): "abc1234"}


def dirty(untracked: list[str], diff: str = "diff --git a/x b/x\n+1") -> dict:
    status = "\n".join([" M src/x.py", *[f"?? {p}" for p in untracked]])
    return {**CLEAN, ("status", "--porcelain"): status, ("diff", "HEAD"): diff}


def test_run_id_format_fixed_now():
    assert new_run_id(FIXED) == "20261016T090507Z"
    other_tz = FIXED.astimezone(timezone(timedelta(hours=-7)))
    assert new_run_id(other_tz) == "20261016T090507Z"
    assert re.fullmatch(r"\d{8}T\d{6}Z", new_run_id())


def test_config_hash_stable_and_sensitive():
    a = {"x": 1, "y": {"b": [1, 2], "a": {"p", "q", "r"}}}
    b = {"y": {"a": {"r", "q", "p"}, "b": [1, 2]}, "x": 1}
    assert config_hash(a) == config_hash(b)
    assert config_hash(frozenset({3, 1, 2})) == config_hash({2, 3, 1})
    assert config_hash(a) != config_hash({**a, "x": 2})
    assert config_hash([1, 2]) != config_hash([2, 1])


def test_file_sha256(tmp_path):
    p = tmp_path / "f.txt"
    p.write_bytes(b"hello")
    assert file_sha256(p) == hashlib.sha256(b"hello").hexdigest()
    assert file_sha256(tmp_path / "missing.txt") is None


def test_git_state_clean(monkeypatch):
    monkeypatch.setattr(provenance, "git_output", fake_git(CLEAN))
    state = git_state()
    assert state == {"sha": "abc1234", "dirty": False, "diff_sha256": None, "untracked": []}
    assert not dirty_blocks_results(state)


def test_git_state_dirty(monkeypatch):
    monkeypatch.setattr(provenance, "git_output", fake_git(dirty(["b.txt", "a.txt"])))
    state = git_state()
    assert state["sha"] == "abc1234"
    assert state["dirty"] is True
    assert state["untracked"] == ["a.txt", "b.txt"]
    assert re.fullmatch(r"[0-9a-f]{64}", state["diff_sha256"])
    assert dirty_blocks_results(state)


def test_git_state_failure(monkeypatch):
    monkeypatch.setattr(provenance, "git_output", fake_git({}))
    state = git_state()
    assert state["sha"] == "unknown"
    assert state["dirty"] is False
    assert state["diff_sha256"] is None


def test_diff_hash_changes_with_untracked_and_diff(monkeypatch):
    def state_for(responses):
        monkeypatch.setattr(provenance, "git_output", fake_git(responses))
        return git_state()

    base = state_for(dirty(["a.txt"]))["diff_sha256"]
    assert state_for(dirty(["a.txt"]))["diff_sha256"] == base
    assert state_for(dirty(["a.txt", "new.txt"]))["diff_sha256"] != base
    assert state_for(dirty(["a.txt"], diff="other"))["diff_sha256"] != base


def test_untracked_list_capped(monkeypatch):
    names = [f"f{i:03}.txt" for i in range(80)]
    monkeypatch.setattr(provenance, "git_output", fake_git(dirty(names)))
    assert len(git_state()["untracked"]) == provenance.MAX_UNTRACKED


FAKE_ANTHROPIC = "sk-ant-FAKE-provenance-test-key"
FAKE_VOYAGE = "pa-FAKE-voyage-provenance-key"


@pytest.fixture
def settings(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "manifest.jsonl").write_text('{"url": "u"}\n')
    answer_prompt = tmp_path / "answer.md"
    answer_prompt.write_text("answer prompt")
    judge_prompt = tmp_path / "judge.md"
    judge_prompt.write_text("judge prompt")
    base = Settings(
        _env_file=None,
        data_dir=data,
        anthropic_api_key=FAKE_ANTHROPIC,
        voyage_api_key=FAKE_VOYAGE,
    )
    return base.model_copy(
        update={
            "answer": base.answer.model_copy(update={"system_prompt_path": answer_prompt}),
            "eval": base.eval.model_copy(update={"judge_prompt_path": judge_prompt}),
        }
    )


def sha(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_collect_provenance(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(provenance, "git_output", fake_git(dirty(["x.txt"])))
    dataset = tmp_path / "eval_set_v1.jsonl"
    dataset.write_text('{"id": "q1"}\n')
    scorer = {"atomic_fact_max_words": 6, "categories": {"b", "a"}}
    model = settings.answer.model
    result = collect_provenance(
        settings,
        dataset_path=dataset,
        answer_model=model,
        k=5,
        scorer_config=scorer,
        points_count=123,
        now=FIXED,
    )
    expected_keys = {
        "run_id", "created_at", "git", "dataset", "corpus", "prompts", "models", "k",
        "min_score", "prompt_caching", "schema_version", "judge_schema_version", "usd_cad",
        "prices_as_of", "scorer", "sdk",
    }  # fmt: skip
    assert expected_keys <= result.keys()
    assert result["run_id"] == "20261016T090507Z"
    assert result["created_at"] == "2026-10-16T09:05:07+00:00"
    assert result["git"]["dirty"] is True
    assert result["dataset"] == {"name": "eval_set_v1.jsonl", "sha256": sha(dataset)}
    corpus = result["corpus"]
    assert corpus["manifest_sha256"] == sha(settings.manifest_path)
    assert corpus["collection"] == settings.index.collection
    assert corpus["points_count"] == 123
    assert corpus["embed_model"] == settings.embed.model
    assert corpus["embed_dimension"] == settings.embed.output_dimension
    assert result["prompts"] == {
        "answer_sha256": sha(settings.answer.system_prompt_path),
        "judge_sha256": sha(settings.eval.judge_prompt_path),
    }
    profile = settings.answer.models[model]
    assert result["models"]["answer"] == {
        "id": model,
        "max_tokens": profile.max_tokens,
        "thinking": profile.thinking,
        "effort": profile.effort,
    }
    assert result["models"]["judge"]["id"] == settings.eval.judge_model
    assert result["scorer"]["sha256"] == config_hash(scorer)
    assert result["scorer"]["config"]["categories"] == ["a", "b"]
    assert set(result["sdk"]) == set(provenance.SDK_PACKAGES)

    dumped = json.dumps(result)
    assert FAKE_ANTHROPIC not in dumped
    assert FAKE_VOYAGE not in dumped


def test_collect_provenance_missing_files(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(provenance, "git_output", fake_git({}))
    settings.manifest_path.unlink()
    settings.answer.system_prompt_path.unlink()
    result = collect_provenance(
        settings,
        dataset_path=tmp_path / "nope.jsonl",
        answer_model="not-a-configured-model",
        k=3,
        scorer_config={},
        points_count=None,
        run_id="custom",
    )
    assert result["run_id"] == "custom"
    assert result["dataset"]["sha256"] is None
    assert result["corpus"]["manifest_sha256"] is None
    assert result["prompts"]["answer_sha256"] is None
    assert result["prompts"]["judge_sha256"] is not None
    assert result["models"]["answer"]["max_tokens"] is None
    assert result["git"]["sha"] == "unknown"
    json.dumps(result)
