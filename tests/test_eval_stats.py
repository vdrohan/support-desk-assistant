import pytest

from support_desk_assistant.evals.stats import cohens_kappa, confusion, rate_ci, wilson_interval

Z = 1.96


def test_wilson_known_value():
    assert wilson_interval(9, 10, Z) == (0.596, 0.982)


def test_wilson_n_zero_is_none():
    assert wilson_interval(0, 0, Z) is None


def test_wilson_k_zero_starts_at_zero():
    lo, hi = wilson_interval(0, 10, Z)
    assert lo == 0.0
    assert hi == pytest.approx(0.278, abs=0.001)


def test_wilson_k_equals_n_ends_at_one():
    lo, hi = wilson_interval(10, 10, Z)
    assert hi == 1.0
    assert lo == pytest.approx(0.722, abs=0.001)


def test_wilson_rejects_k_out_of_range():
    with pytest.raises(ValueError):
        wilson_interval(11, 10, Z)


def test_rate_ci_shape():
    assert rate_ci(9, 10, Z) == {"k": 9, "n": 10, "rate": 0.9, "ci95": [0.596, 0.982]}
    assert rate_ci(0, 0, Z) == {"k": 0, "n": 0, "rate": None, "ci95": None}


def test_confusion_orientation():
    pairs = [
        (True, True),  # tp
        (False, True),  # machine says present, human absent: false present
        (False, True),
        (True, False),  # machine says absent, human present: false absent
        (False, False),  # tn
    ]
    assert confusion(pairs) == {"tp": 1, "fp": 2, "fn": 1, "tn": 1}


def test_kappa_perfect_agreement():
    assert cohens_kappa([(True, True), (False, False), (True, True)]) == 1.0


def test_kappa_known_value():
    # tp=20, fp=5, fn=10, tn=15: po=0.7, pe=0.6*0.5+0.4*0.5=0.5 -> kappa 0.4
    pairs = [(True, True)] * 20 + [(False, True)] * 5 + [(True, False)] * 10 + [(False, False)] * 15
    assert cohens_kappa(pairs) == 0.4


def test_kappa_total_disagreement_is_negative():
    assert cohens_kappa([(True, False), (False, True)]) == -1.0


def test_kappa_none_cases():
    assert cohens_kappa([]) is None
    # Both raters say present for everything: chance agreement 1.
    assert cohens_kappa([(True, True), (True, True)]) is None
