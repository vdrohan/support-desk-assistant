"""Guards on the committed answer eval set (reads repo files; no network)."""

import json
from pathlib import Path

import pytest

from support_desk_assistant.evals.retrieval import normalize

EVAL_SET = Path(__file__).resolve().parents[1] / "data" / "eval" / "eval_set_v1.jsonl"

# Left out on purpose (docs/decisions.md #20): no quotable answer, a broken gold page,
# and a question that needs a clarifying "which benefit?" first.
LEFT_OUT = (
    "How long does a passport take?",
    "What documents do I need to apply for a passport?",
    "When do I get paid?",
)


@pytest.fixture(scope="module")
def items():
    lines = EVAL_SET.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def test_left_out_questions_are_absent(items):
    questions = {normalize(i["question"]) for i in items}
    for q in LEFT_OUT:
        assert normalize(q) not in questions, q


def test_ids_are_unique(items):
    ids = [i["id"] for i in items]
    assert len(ids) == len(set(ids))


def test_answerable_items_have_key_facts_and_gold_urls(items):
    for i in items:
        if i["answerable"]:
            assert i["key_facts"] and i["gold_urls"], i["id"]


def test_unanswerable_items_have_neither(items):
    for i in items:
        if not i["answerable"]:
            assert not i["key_facts"] and not i["gold_urls"], i["id"]
