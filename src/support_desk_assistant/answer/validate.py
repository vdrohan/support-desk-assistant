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
    failed: list[str] = []
    allowed = set(retrieved_ids)

    if any(c not in allowed for c in result.citations):
        failed.append(CITATION_NOT_RETRIEVED)

    if result.abstained:
        if result.answer.strip():
            failed.append(ABSTAINED_WITH_ANSWER)
        if result.citations:
            failed.append(ABSTAINED_WITH_CITATIONS)
    else:
        if not result.citations:
            failed.append(NO_CITATIONS)

    return ValidationResult(failed_rules=tuple(failed))
