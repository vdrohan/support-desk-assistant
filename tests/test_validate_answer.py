"""Rules for validate_answer. These fail with NotImplementedError until it is written."""

from support_desk_assistant.answer.schema import AnswerResult
from support_desk_assistant.answer.validate import (
    ABSTAINED_WITH_ANSWER,
    ABSTAINED_WITH_CITATIONS,
    CITATION_NOT_RETRIEVED,
    NO_CITATIONS,
    validate_answer,
)

IDS = {"a", "b", "c"}


def result(**kw) -> AnswerResult:
    base = {"answer": "Yes.", "citations": ["a"], "abstained": False, "abstain_reason": None}
    return AnswerResult(**(base | kw))


def abstention(**kw) -> AnswerResult:
    base = {"answer": "", "citations": [], "abstained": True, "abstain_reason": "not covered"}
    return AnswerResult(**(base | kw))


def failed(r: AnswerResult, ids=IDS) -> set[str]:
    return set(validate_answer(r, ids).failed_rules)


def test_good_answer_passes():
    v = validate_answer(result(citations=["a", "b"]), IDS)
    assert v.ok and v.failed_rules == ()


def test_good_abstention_passes():
    assert validate_answer(abstention(), IDS).ok


def test_cited_id_not_retrieved_fails():
    assert failed(result(citations=["a", "zzz"])) == {CITATION_NOT_RETRIEVED}


def test_citing_anything_when_nothing_retrieved_fails():
    assert CITATION_NOT_RETRIEVED in failed(result(citations=["a"]), ids=set())


def test_answer_without_citations_fails():
    v = validate_answer(result(citations=[]), IDS)
    assert not v.ok
    assert set(v.failed_rules) == {NO_CITATIONS}


def test_abstention_with_answer_text_fails():
    assert failed(abstention(answer="Probably yes")) == {ABSTAINED_WITH_ANSWER}


def test_abstention_with_citations_fails():
    assert failed(abstention(citations=["a"])) == {ABSTAINED_WITH_CITATIONS}


def test_reports_every_failed_rule_not_just_the_first():
    r = abstention(answer="Probably yes", citations=["a", "zzz"])
    assert failed(r) == {ABSTAINED_WITH_ANSWER, ABSTAINED_WITH_CITATIONS, CITATION_NOT_RETRIEVED}
