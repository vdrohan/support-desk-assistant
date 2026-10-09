"""Semantic checks on an answer that the JSON schema can't express."""

from collections.abc import Collection
from dataclasses import dataclass

from support_desk_assistant.answer.schema import AnswerResult

# Rule names, as they appear in ``ValidationResult.failed_rules`` and in the request log.
CITATION_NOT_RETRIEVED = "citation_not_retrieved"
NO_CITATIONS = "no_citations"
ABSTAINED_WITH_ANSWER = "abstained_with_answer"
ABSTAINED_WITH_CITATIONS = "abstained_with_citations"


@dataclass(frozen=True)
class ValidationResult:
    failed_rules: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failed_rules


def validate_answer(result: AnswerResult, retrieved_ids: Collection[str]) -> ValidationResult:
    """Check ``result`` against the rules and report every one that fails (not just the first).

    Rules:
    - ``citation_not_retrieved``: every cited ID is in ``retrieved_ids``.
    - ``no_citations``: a non-abstained answer has at least one citation.
    - ``abstained_with_answer``: an abstained result has an empty ``answer``.
    - ``abstained_with_citations``: an abstained result has no citations.

    Returns a ``ValidationResult`` whose ``failed_rules`` lists the names above (empty when
    everything passes); ``ok`` is True only when nothing failed.
    """
    raise NotImplementedError
