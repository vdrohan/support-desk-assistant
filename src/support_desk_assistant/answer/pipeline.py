"""question -> retrieval (top-k) -> gate -> Claude -> validate -> log.

Split in two so the CLI's ``--smoke`` can price a run before spending anything:
``prepare`` does retrieval, the gate and the cache lookup (no Anthropic call);
``execute`` makes the API call if one is needed, validates and logs.
"""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from xml.sax.saxutils import quoteattr

import anthropic
from pydantic import ValidationError
from qdrant_client import QdrantClient

from support_desk_assistant.answer.cache import ResponseCache, cache_key
from support_desk_assistant.answer.cost import Usage, cost_usd, usd_to_cad
from support_desk_assistant.answer.schema import AnswerResult, output_schema
from support_desk_assistant.answer.validate import ValidationResult, validate_answer
from support_desk_assistant.config import Settings
from support_desk_assistant.ingest.embed import Embedder
from support_desk_assistant.retrieve import Hit, search

GATE_PASS = "pass"
GATE_LOW_SCORE = "abstain_low_score"

# Response status. "ok" means a schema-valid result came back; the rest are abstentions
# the pipeline made itself because generation didn't complete.
STATUS_OK = "ok"
STATUS_GATE = "gate"
STATUS_REFUSAL = "refusal"
STATUS_MAX_TOKENS = "max_tokens"
STATUS_INVALID = "invalid_output"

# Rough upper bound for pre-spend estimates (Haiku/Sonnet 5.5 tokenizers run ~30% above
# the old ~4 chars/token), so the estimate errs high.
CHARS_PER_TOKEN_ESTIMATE = 3.0


class SystemPromptNotReady(RuntimeError):
    """prompts/answer_system.md is missing or still only the TODO header."""


class MessagesClient(Protocol):
    @property
    def messages(self) -> Any: ...


def load_system_prompt(path: Path) -> str:
    try:
        text = path.read_text()
    except FileNotFoundError as exc:
        raise SystemPromptNotReady(f"{path} not found") from exc
    lines = [line for line in text.splitlines() if line.strip()]
    if all(line.lstrip().lower().startswith("# todo") for line in lines):
        raise SystemPromptNotReady(f"{path} is still just a TODO header; write the prompt first")
    return text


def load_manifest_dates(path: Path) -> dict[str, dict[str, str | None]]:
    """url -> {crawled_at, date_modified}. A missing manifest or URL yields nulls, not errors."""
    dates: dict[str, dict[str, str | None]] = {}
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return dates
    for line in lines:
        if not line.strip():
            continue
        row = json.loads(line)
        dates[row["url"]] = {
            "crawled_at": row.get("crawled_at"),
            "date_modified": row.get("date_modified"),
        }
    return dates


def render_user_message(question: str, hits: list[Hit], dates: dict[str, dict]) -> str:
    """Chunks first, question last. Each chunk carries its ID, title, URL and dates."""
    blocks = []
    for hit in hits:
        d = dates.get(hit.url, {})
        attrs = {
            "chunk_id": hit.chunk_id,
            "title": hit.title,
            "section": " > ".join(hit.heading_path),
            "url": hit.url,
            "crawled_at": d.get("crawled_at") or "null",
            "date_modified": d.get("date_modified") or "null",
        }
        head = " ".join(f"{k}={quoteattr(v)}" for k, v in attrs.items())
        blocks.append(f"<chunk {head}>\n{hit.text.strip()}\n</chunk>")
    return "<chunks>\n" + "\n".join(blocks) + f"\n</chunks>\n\n<question>\n{question}\n</question>"


@dataclass
class Prepared:
    question: str
    hits: list[Hit]
    gate: str
    model: str
    system: str = ""
    user_message: str = ""
    key: str = ""
    cached: dict[str, Any] | None = None

    @property
    def retrieved_ids(self) -> list[str]:
        return [h.chunk_id for h in self.hits]

    @property
    def will_call_api(self) -> bool:
        return self.gate == GATE_PASS and self.cached is None


@dataclass
class Outcome:
    question: str
    result: AnswerResult
    hits: list[Hit]
    gate: str
    model: str
    status: str
    validation: ValidationResult | None  # None: validate_answer not implemented yet
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0
    cost_cad: float = 0.0
    cache_hit: bool = False
    api_called: bool = False
    latency_ms: int = 0


def gate_abstention() -> AnswerResult:
    return AnswerResult(
        answer="",
        citations=[],
        abstained=True,
        abstain_reason="The retrieved documents don't match the question closely enough.",
    )


def _failed(status: str) -> AnswerResult:
    return AnswerResult(
        answer="",
        citations=[],
        abstained=True,
        abstain_reason=f"Answer generation did not complete ({status}).",
    )


def parse_response(raw: dict[str, Any]) -> tuple[AnswerResult, str]:
    """Raw API response (as stored in the cache) -> (result, status). Never raises."""
    stop = raw.get("stop_reason")
    if stop == "refusal":
        return _failed(STATUS_REFUSAL), STATUS_REFUSAL
    if stop == "max_tokens":
        return _failed(STATUS_MAX_TOKENS), STATUS_MAX_TOKENS
    text = "".join(b.get("text", "") for b in raw.get("content", []) if b.get("type") == "text")
    try:
        return AnswerResult.model_validate_json(text), STATUS_OK
    except ValidationError:
        return _failed(STATUS_INVALID), STATUS_INVALID


def usage_from_raw(raw: dict[str, Any]) -> Usage:
    u = raw.get("usage") or {}
    return Usage(
        input_tokens=u.get("input_tokens") or 0,
        output_tokens=u.get("output_tokens") or 0,
        cache_creation_input_tokens=u.get("cache_creation_input_tokens") or 0,
        cache_read_input_tokens=u.get("cache_read_input_tokens") or 0,
    )


def make_anthropic_client(settings: Settings) -> anthropic.Anthropic:
    if settings.anthropic_api_key is None:
        raise SystemExit("ANTHROPIC_API_KEY is not set (see .env.example)")
    return anthropic.Anthropic(
        api_key=settings.anthropic_api_key.get_secret_value(),
        max_retries=settings.answer.max_retries,
        timeout=settings.answer.timeout_s,
    )


def _key_inputs(settings: Settings, model: str, system: str, user_message: str) -> dict[str, Any]:
    profile = settings.answer.models[model]
    return {
        "model": model,
        "system": system,
        "user": user_message,
        "schema_version": settings.answer.schema_version,
        "max_tokens": profile.max_tokens,
        "effort": profile.effort,
        "thinking": profile.thinking,
    }


def prepare(
    question: str,
    *,
    settings: Settings,
    embedder: Embedder,
    qdrant: QdrantClient,
    program: str | None = None,
    k: int | None = None,
    model: str | None = None,
) -> Prepared:
    model = model or settings.answer.model
    if model not in settings.answer.models:
        raise ValueError(f"no profile for model {model!r} in settings.answer.models")
    hits = search(
        question, settings=settings, embedder=embedder, qdrant=qdrant, program=program, k=k
    )
    if not hits or hits[0].score < settings.answer.min_score:
        return Prepared(question, hits, GATE_LOW_SCORE, model)

    system = load_system_prompt(settings.answer.system_prompt_path)
    user_message = render_user_message(question, hits, load_manifest_dates(settings.manifest_path))
    key = cache_key(_key_inputs(settings, model, system, user_message))
    cached = ResponseCache(settings.answer_cache_dir).get(key)
    return Prepared(question, hits, GATE_PASS, model, system, user_message, key, cached)


def estimate_cost_usd(prepared: Prepared, settings: Settings) -> float:
    """Upper-bound cost of the API call ``prepared`` would make (0 if none is needed)."""
    if not prepared.will_call_api:
        return 0.0
    chars = len(prepared.system) + len(prepared.user_message)
    usage = Usage(
        input_tokens=int(chars / CHARS_PER_TOKEN_ESTIMATE),
        output_tokens=settings.answer.models[prepared.model].max_tokens,
    )
    return cost_usd(usage, prepared.model, settings.answer)


def build_request(prepared: Prepared, settings: Settings) -> dict[str, Any]:
    cfg = settings.answer
    profile = cfg.models[prepared.model]
    system_block: dict[str, Any] = {"type": "text", "text": prepared.system}
    if cfg.prompt_caching:
        system_block["cache_control"] = {"type": "ephemeral"}
    return {
        "model": prepared.model,
        "max_tokens": profile.max_tokens,
        "system": [system_block],
        "messages": [{"role": "user", "content": prepared.user_message}],
        "thinking": {"type": profile.thinking},
        "output_config": {
            "effort": profile.effort,
            "format": {"type": "json_schema", "schema": output_schema()},
        },
    }


def _validate(
    result: AnswerResult, ids: list[str], validator: Callable[..., ValidationResult]
) -> ValidationResult | None:
    try:
        return validator(result, ids)
    except NotImplementedError:
        return None


def log_line(outcome: Outcome, settings: Settings) -> dict[str, Any]:
    v = outcome.validation
    return {
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
        "question": outcome.question,
        "retrieved": [{"chunk_id": h.chunk_id, "score": round(h.score, 4)} for h in outcome.hits],
        "gate": outcome.gate,
        "model": outcome.model,
        "api_called": outcome.api_called,
        "status": outcome.status,
        "input_tokens": outcome.usage.input_tokens,
        "output_tokens": outcome.usage.output_tokens,
        "prompt_cache_write_tokens": outcome.usage.cache_creation_input_tokens,
        "prompt_cache_read_tokens": outcome.usage.cache_read_input_tokens,
        "latency_ms": outcome.latency_ms,
        "cache_hit": outcome.cache_hit,
        "cost_usd": round(outcome.cost_usd, 6),
        "cost_cad": round(outcome.cost_cad, 6),
        "validation": {
            "implemented": v is not None,
            "ok": None if v is None else v.ok,
            "failed_rules": [] if v is None else list(v.failed_rules),
        },
    }


def execute(
    prepared: Prepared,
    *,
    settings: Settings,
    client: MessagesClient | None = None,
    validator: Callable[..., ValidationResult] = validate_answer,
) -> Outcome:
    """Finish a ``Prepared`` request: call Claude if needed, validate, log.

    API/network errors propagate; nothing is cached or logged for them.
    """
    cfg = settings.answer
    started = time.perf_counter()
    usage = Usage()
    cache_hit = api_called = False

    if prepared.gate != GATE_PASS:
        result, status = gate_abstention(), STATUS_GATE
    else:
        if prepared.cached is not None:
            raw, cache_hit = prepared.cached, True
        else:
            client = client or make_anthropic_client(settings)
            message = client.messages.create(**build_request(prepared, settings))
            raw, api_called = message.to_dict(), True
            ResponseCache(settings.answer_cache_dir).put(prepared.key, raw)
            usage = usage_from_raw(raw)
        result, status = parse_response(raw)

    usd = cost_usd(usage, prepared.model, cfg)
    outcome = Outcome(
        question=prepared.question,
        result=result,
        hits=prepared.hits,
        gate=prepared.gate,
        model=prepared.model,
        status=status,
        validation=_validate(result, prepared.retrieved_ids, validator),
        usage=usage,
        cost_usd=usd,
        cost_cad=usd_to_cad(usd, cfg),
        cache_hit=cache_hit,
        api_called=api_called,
        latency_ms=round((time.perf_counter() - started) * 1000),
    )
    path = settings.request_log_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(log_line(outcome, settings), ensure_ascii=False) + "\n")
    return outcome
