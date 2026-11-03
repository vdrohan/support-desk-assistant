"""scripts/check_commit_msg.py: the commit subject rule (decisions #32)."""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_commit_msg.py"
_spec = importlib.util.spec_from_file_location("check_commit_msg", SCRIPT)
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)


@pytest.mark.parametrize(
    "subject",
    [
        "feat: add ticket override endpoint",
        "fix(api): refuse to start without a key",
        "eval: record triage test-split run (queue 0.90, urgent recall 0.95)",
        "chore(deps): bump astral-sh/setup-uv from 10.2.0 to 10.3.3",
        "Merge pull request #4: Phase 4 and README first-screen rewrite",
        'Revert "feat: add ticket override endpoint"',
        "fixup! feat: add ticket override endpoint",
        "feat: " + "x" * 66,  # exactly 72 characters
    ],
)
def test_accepts_good_subjects(subject):
    assert check.subject_problems(subject) == []


@pytest.mark.parametrize(
    "subject",
    [
        "Record first full answer-eval run: results, impact log",
        "feat add ticket override endpoint",
        "Feat: add ticket override endpoint",
        "feat:add ticket override endpoint",
        "feat: add ticket override endpoint.",
        "feat: " + "x" * 67,  # 73 characters
        "wip",
        "",
    ],
)
def test_rejects_bad_subjects(subject):
    assert check.subject_problems(subject)


def test_first_subject_skips_comment_lines():
    message = "# Please enter the message\n# lines starting with '#' are ignored\nfix: x\n\nbody"
    assert check.first_subject(message) == "fix: x"
    assert check.first_subject("# only a comment\n") == ""


def test_main_reads_a_message_file(tmp_path, capsys):
    good = tmp_path / "good.txt"
    good.write_text("feat: add thing\n\nA body line.\n", encoding="utf-8")
    assert check.main([str(good)]) == 0

    bad = tmp_path / "bad.txt"
    bad.write_text("Add a thing.\n", encoding="utf-8")
    assert check.main([str(bad)]) == 1
    err = capsys.readouterr().err
    assert "'Add a thing.'" in err and "must start with 'type: summary'" in err


def test_main_checks_a_subject_argument(capsys):
    assert check.main(["--subject", "docs: update the README"]) == 0
    assert check.main(["--subject", "Update the README"]) == 1
    assert "Commit subject not accepted" in capsys.readouterr().err


def test_main_needs_exactly_one_source():
    with pytest.raises(SystemExit) as exc:
        check.main([])
    assert exc.value.code == 2
    with pytest.raises(SystemExit):
        check.main(["--subject", "feat: x", "message.txt"])
