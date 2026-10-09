"""Token usage -> USD and CAD, from the per-model prices in config."""

from dataclasses import dataclass

from support_desk_assistant.config import AnswerSettings


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0  # uncached input
    output_tokens: int = 0  # includes thinking tokens
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


def cost_usd(usage: Usage, model: str, cfg: AnswerSettings) -> float:
    profile = cfg.models[model]
    in_price = profile.input_usd_per_mtok
    total = (
        usage.input_tokens * in_price
        + usage.cache_creation_input_tokens * in_price * cfg.cache_write_multiplier
        + usage.cache_read_input_tokens * in_price * cfg.cache_read_multiplier
        + usage.output_tokens * profile.output_usd_per_mtok
    )
    return total / 1_000_000


def usd_to_cad(usd: float, cfg: AnswerSettings) -> float:
    return usd * cfg.usd_cad
