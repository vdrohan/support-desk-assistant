"""When a triaged ticket needs a person (docs/decisions.md #29).

A pure function of the triage result and ``EscalationSettings``: no model call, no I/O. Each rule
that fires adds a reason; any reason means ``needs_review``.
"""

from dataclasses import dataclass

from support_desk_assistant.config import EscalationSettings, Settings
from support_desk_assistant.triage.schema import CONFIDENCES, TriageResult

REASON_FAILED = "triage_failed"
REASON_OTHER = "no_program_queue"
REASON_URGENT = "urgent"
REASON_QUEUE = "low_queue_confidence"
REASON_PRIORITY = "low_priority_confidence"
REASON_DISAGREE = "second_opinion_differs"

REASONS: tuple[str, ...] = (
    REASON_FAILED,
    REASON_OTHER,
    REASON_URGENT,
    REASON_QUEUE,
    REASON_PRIORITY,
    REASON_DISAGREE,
)


@dataclass(frozen=True)
class Escalation:
    reasons: tuple[str, ...] = ()

    @property
    def needs_review(self) -> bool:
        return bool(self.reasons)


def below(level: str, floor: str) -> bool:
    """True if confidence ``level`` is strictly lower than ``floor``."""
    return CONFIDENCES.index(level) < CONFIDENCES.index(floor)


def second_opinion_settings(settings: Settings) -> Settings:
    """The settings for the second pass: the same model, thinking on (decisions.md #29)."""
    cfg = settings.escalation
    clone = settings.model_copy(deep=True)
    clone.triage.thinking = cfg.second_opinion_thinking
    clone.triage.effort = cfg.second_opinion_effort
    clone.triage.max_tokens = cfg.second_opinion_max_tokens
    return clone


def agrees(first: TriageResult, second: TriageResult | None) -> bool:
    """Same queue and priority; a second pass that produced nothing does not agree."""
    return second is not None and (second.queue, second.priority) == (first.queue, first.priority)


def escalate(
    result: TriageResult | None, cfg: EscalationSettings, second_agrees: bool | None = None
) -> Escalation:
    """``result`` is None when triage produced nothing usable (refusal, max_tokens, invalid
    output): that always goes to a person. ``second_agrees`` is None when no second pass was
    run; the disagreement rule needs a pass that disagreed (or failed)."""
    if result is None:
        return Escalation((REASON_FAILED,))
    reasons: list[str] = []
    if cfg.on_other_queue and result.queue == "other":
        reasons.append(REASON_OTHER)
    if cfg.on_urgent and result.priority == "urgent":
        reasons.append(REASON_URGENT)
    if below(result.queue_confidence, cfg.min_queue_confidence):
        reasons.append(REASON_QUEUE)
    if below(result.priority_confidence, cfg.min_priority_confidence):
        reasons.append(REASON_PRIORITY)
    if cfg.on_disagreement and second_agrees is False:
        reasons.append(REASON_DISAGREE)
    return Escalation(tuple(reasons))
