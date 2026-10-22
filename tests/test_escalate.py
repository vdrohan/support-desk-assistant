"""Escalation rules: pure function of the triage result and EscalationSettings."""

import pytest

from support_desk_assistant.config import EscalationSettings
from support_desk_assistant.triage import escalate as es
from support_desk_assistant.triage.schema import TriageResult

# The rules before tuning (urgent on, medium floors, no second opinion): what most tests pin.
RULES = EscalationSettings(
    on_other_queue=True,
    on_urgent=True,
    min_queue_confidence="medium",
    min_priority_confidence="medium",
    on_disagreement=False,
)


def result(queue="ei", priority="normal", qc="high", pc="high"):
    return TriageResult(
        rationale="r", queue=queue, priority=priority, queue_confidence=qc, priority_confidence=pc
    )


def test_a_failed_triage_always_goes_to_a_person():
    off = EscalationSettings(on_other_queue=False, on_urgent=False)
    assert es.escalate(None, off) == es.Escalation((es.REASON_FAILED,))
    assert es.escalate(None, off).needs_review


def test_a_confident_ordinary_ticket_is_left_alone():
    out = es.escalate(result(), RULES)
    assert out.reasons == () and not out.needs_review


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"queue": "other"}, es.REASON_OTHER),
        ({"priority": "urgent"}, es.REASON_URGENT),
        ({"qc": "low"}, es.REASON_QUEUE),
        ({"pc": "low"}, es.REASON_PRIORITY),
    ],
)
def test_each_rule_adds_its_reason(kwargs, reason):
    assert es.escalate(result(**kwargs), RULES).reasons == (reason,)


def test_reasons_accumulate_in_a_fixed_order():
    out = es.escalate(result("other", "urgent", "low", "low"), RULES)
    assert out.reasons == (es.REASON_OTHER, es.REASON_URGENT, es.REASON_QUEUE, es.REASON_PRIORITY)
    assert set(out.reasons) <= set(es.REASONS)


def test_switches_turn_rules_off():
    cfg = EscalationSettings(on_other_queue=False, on_urgent=False)
    assert es.escalate(result("other", "urgent"), cfg).reasons == ()


def test_confidence_floor_is_strict():
    medium = result(qc="medium", pc="medium")
    assert es.escalate(medium, RULES).reasons == ()  # floor "medium": only low
    high_floor = EscalationSettings(min_queue_confidence="high", min_priority_confidence="high")
    assert es.escalate(medium, high_floor).reasons == (es.REASON_QUEUE, es.REASON_PRIORITY)
    low_floor = EscalationSettings(min_queue_confidence="low", min_priority_confidence="low")
    assert es.escalate(result(qc="low", pc="low"), low_floor).reasons == ()  # floor "low": none


def test_a_disagreeing_second_pass_escalates_only_when_the_rule_is_on():
    first = result()
    second_same = result()
    second_other = result(priority="high")
    assert es.agrees(first, second_same) and not es.agrees(first, second_other)
    assert not es.agrees(first, None)  # a second pass that produced nothing does not agree
    off, on = RULES, RULES.model_copy(update={"on_disagreement": True})
    assert es.escalate(first, off, second_agrees=False).reasons == ()
    assert es.escalate(first, on, second_agrees=False).reasons == (es.REASON_DISAGREE,)
    assert es.escalate(first, on, second_agrees=True).reasons == ()
    assert es.escalate(first, on, second_agrees=None).reasons == ()  # no second pass was run
    assert es.REASON_DISAGREE in es.REASONS


def test_second_opinion_settings_turn_thinking_on_without_touching_the_original():
    from support_desk_assistant.config import Settings

    base = Settings()
    second = es.second_opinion_settings(base)
    assert second.triage.model == base.triage.model == "claude-haiku-5-5"  # Haiku only
    assert (second.triage.thinking, second.triage.effort) == ("adaptive", "medium")
    assert second.triage.max_tokens == base.escalation.second_opinion_max_tokens
    assert base.triage.thinking is None and base.triage.max_tokens == 400


def test_the_shipped_policy_is_the_one_frozen_after_tuning_on_dev():
    cfg = EscalationSettings()
    assert (cfg.on_other_queue, cfg.on_urgent, cfg.on_disagreement) == (True, False, True)
    assert (cfg.min_queue_confidence, cfg.min_priority_confidence) == ("high", "low")
    # An urgent, confident, agreed-upon ticket is not "doubtful": it tops the queue instead.
    assert es.escalate(result(priority="urgent"), cfg, second_agrees=True).reasons == ()
    assert es.escalate(result(qc="medium"), cfg).reasons == (es.REASON_QUEUE,)
    assert es.escalate(result(pc="low"), cfg).reasons == ()  # the priority floor is off
    assert es.escalate(result(), cfg, second_agrees=False).reasons == (es.REASON_DISAGREE,)
