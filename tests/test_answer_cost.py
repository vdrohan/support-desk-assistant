import pytest

from support_desk_assistant.answer.cost import Usage, cost_usd, usd_to_cad
from support_desk_assistant.config import AnswerSettings

CFG = AnswerSettings()


def test_haiku_plain_tokens():
    # 1000 in at $0.10/M + 100 out at $0.50/M
    assert cost_usd(Usage(1000, 100), "claude-haiku-5-5", CFG) == pytest.approx(0.00015)


def test_sonnet_prices():
    assert cost_usd(Usage(1000, 100), "claude-sonnet-5-5", CFG) == pytest.approx(0.003)


def test_prompt_cache_multipliers():
    usage = Usage(0, 0, cache_creation_input_tokens=1_000_000, cache_read_input_tokens=1_000_000)
    assert cost_usd(usage, "claude-sonnet-5-5", CFG) == pytest.approx(2.0 * 1.25 + 2.0 * 0.1)


def test_cad_uses_configured_rate():
    cfg = AnswerSettings(usd_cad=1.5)
    assert usd_to_cad(2.0, cfg) == 3.0


def test_unknown_model_is_an_error():
    with pytest.raises(KeyError):
        cost_usd(Usage(1, 1), "claude-nope", CFG)
