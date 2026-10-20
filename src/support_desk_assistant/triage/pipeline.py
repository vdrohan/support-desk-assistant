"""ticket text -> Claude (queue, priority) -> parsed result. Cached like the answer pipeline."""

import time
from dataclasses import dataclass, field
from typing import Any
from xml.sax.saxutils import escape

from pydantic import ValidationError

from support_desk_assistant.answer.cache import ResponseCache, cache_key
from support_desk_assistant.answer.cost import Usage, cost_usd, usd_to_cad
from support_desk_assistant.answer.pipeline import (
    MessagesClient,
    SystemPromptNotReady,
    load_system_prompt,
    make_anthropic_client,
    usage_from_raw,
)
from support_desk_assistant.config import Settings
from support_desk_assistant.triage.schema import TriageResult, output_schema

STATUS_OK = "ok"
STATUS_REFUSAL = "refusal"
STATUS_MAX_TOKENS = "max_tokens"
STATUS_INVALID = "invalid_output"

__all__ = ["SystemPromptNotReady", "TriageOutcome", "triage_ticket"]


@dataclass
class TriageOutcome:
    status: str
    result: TriageResult | None  # None unless status == "ok"
    model: str
    usage: Usage = field(default_factory=Usage)
    cost_cad: float = 0.0
    cache_hit: bool = False
    latency_ms: int = 0


def render_user_message(subject: str, body: str) -> str:
    """Escaped, so ticket text cannot close the <ticket> tag and pose as instructions."""
    return f"<ticket>\n<subject>{escape(subject)}</subject>\n<body>{escape(body)}</body>\n</ticket>"


def build_request(system: str, user_message: str, settings: Settings) -> dict[str, Any]:
    cfg = settings.triage
    profile = settings.answer.models[cfg.model]
    return {
        "model": cfg.model,
        "max_tokens": cfg.max_tokens,
        "system": [{"type": "text", "text": system}],
        "messages": [{"role": "user", "content": user_message}],
        "thinking": {"type": profile.thinking},
        "output_config": {
            "effort": profile.effort,
            "format": {"type": "json_schema", "schema": output_schema()},
        },
    }


def request_key(system: str, user_message: str, settings: Settings) -> str:
    cfg = settings.triage
    profile = settings.answer.models[cfg.model]
    return cache_key(
        {
            "kind": "triage",
            "schema_version": cfg.schema_version,
            "model": cfg.model,
            "system": system,
            "user": user_message,
            "max_tokens": cfg.max_tokens,
            "effort": profile.effort,
            "thinking": profile.thinking,
        }
    )


def parse_response(raw: dict[str, Any]) -> tuple[TriageResult | None, str]:
    """Raw API response (as cached) -> (result, status). Never raises."""
    stop = raw.get("stop_reason")
    if stop == "refusal":
        return None, STATUS_REFUSAL
    if stop == "max_tokens":
        return None, STATUS_MAX_TOKENS
    text = "".join(b.get("text", "") for b in raw.get("content", []) if b.get("type") == "text")
    try:
        return TriageResult.model_validate_json(text), STATUS_OK
    except ValidationError:
        return None, STATUS_INVALID


def triage_ticket(
    subject: str, body: str, *, settings: Settings, client: MessagesClient | None = None
) -> TriageOutcome:
    """API/network errors propagate and are not cached."""
    started = time.perf_counter()
    system = load_system_prompt(settings.triage.system_prompt_path)
    user = render_user_message(subject, body)
    key = request_key(system, user, settings)
    cache = ResponseCache(settings.triage_cache_dir)
    raw = cache.get(key)
    usage, cache_hit = Usage(), raw is not None
    if raw is None:
        client = client or make_anthropic_client(settings)
        message = client.messages.create(**build_request(system, user, settings))
        raw = message.to_dict()
        cache.put(key, raw)
        usage = usage_from_raw(raw)
    result, status = parse_response(raw)
    usd = cost_usd(usage, settings.triage.model, settings.answer)
    return TriageOutcome(
        status=status,
        result=result,
        model=settings.triage.model,
        usage=usage,
        cost_cad=usd_to_cad(usd, settings.answer),
        cache_hit=cache_hit,
        latency_ms=round((time.perf_counter() - started) * 1000),
    )
