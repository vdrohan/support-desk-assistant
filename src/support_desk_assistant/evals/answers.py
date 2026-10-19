"""Answer eval: score generated answers on the v1 answer set, per category.

Key facts with numbers are decided for free by matching numbers and content words;
everything else goes to a Haiku judge (one call per item, only for those facts). Gate,
validator and citation checks are deterministic. Category tables come first, each row
with n; the overall (micro and macro) and breakdown sections follow, every rate with n
and a 95% Wilson interval, never blending answerable and abstention items. See
docs/decisions.md #20 (eval set, per-category reporting), #21 (scoring, runner) and #23
(provenance, hard cap, blind review and judge calibration).

Fact decisions, item scoring and aggregation are pure functions (no I/O). Anthropic is
called only by a ``--yes`` run (answers via the pipeline, then the judge) and by
``--calibrate --yes`` (and only for judge verdicts not already cached). Completed
responses are cached (answers under ``data/cache/answers``, judge verdicts under
``settings.eval_cache_dir``), so a re-run resumes and costs nothing.

The default is a DRY RUN: it retrieves every selected question (Voyage query embeddings
for questions not in the embedding cache, so it needs the Qdrant tunnel), prices the run
as an upper bound in CAD and sends nothing to Anthropic. A ``--yes`` run is refused if
that bound passes the cap, and stops before any item whose bound would take the actual
spend past it. Each run writes ``answers_<collection>_<model>_<run_id>`` as ``.jsonl``
(one row per question), ``.summary.json`` (meta with provenance, metrics) and ``.md``.
Full runs from a clean tree go to ``data/eval/results/``; partial runs (``--limit``,
``--category``, spend-cap stop) and dirty-tree runs (unless ``--allow-dirty``) go to
``data/eval/runs/``. ``--export`` writes a blind hand-review sheet (offline);
``--import-labels`` records its filled-in labels; ``--calibrate`` measures judge vs human
agreement on them.

    uv run sda-eval-answers                       # dry run: plan and cost estimate
    uv run sda-eval-answers --limit 2 --yes       # first 2 items per category
    uv run sda-eval-answers --category confusable --category multi_page --yes
    uv run sda-eval-answers --yes [--cap 0.50] [--model X] [-k 5] [--dataset PATH]
    uv run sda-eval-answers --yes --allow-dirty   # full run to results/ from a dirty tree
    uv run sda-eval-answers --export [RESULTS] [--reveal]   # blind sheet; --reveal: not blind
    uv run sda-eval-answers --import-labels SHEET [--from RESULTS]
    uv run sda-eval-answers --calibrate [FILE] [--yes] [--cap CAD]   # dry run if any judge
                                                  # call is uncached; cached is free

RESULTS is a ``.summary.json`` or ``.jsonl`` results file (default: the newest).

Exit codes: 0 ok or dry run; 1 stopped by the spend cap, an Anthropic API error, or
label import problems/conflicts; 2 refused (estimate over the cap), a setup error or an
unreadable file.
"""

import argparse
import json
import random
import re
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import anthropic
import httpx
import voyageai.error
from anthropic import transform_schema
from pydantic import BaseModel, ConfigDict, ValidationError
from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import ApiException

from support_desk_assistant.answer.cache import ResponseCache, cache_key
from support_desk_assistant.answer.cost import Usage, cost_usd, usd_to_cad
from support_desk_assistant.answer.pipeline import (
    CHARS_PER_TOKEN_ESTIMATE,
    GATE_PASS,
    STATUS_GATE,
    STATUS_OK,
    MessagesClient,
    Outcome,
    Prepared,
    SystemPromptNotReady,
    estimate_cost_usd,
    execute,
    load_system_prompt,
    make_anthropic_client,
    parse_response,
    prepare,
    usage_from_raw,
)
from support_desk_assistant.answer.schema import AnswerResult
from support_desk_assistant.answer.validate import ValidationResult, validate_answer
from support_desk_assistant.config import EvalSettings, Settings, get_settings
from support_desk_assistant.evals import breakdown, provenance
from support_desk_assistant.evals.draft_questions import (
    CATEGORIES,
    UNANSWERABLE,
    Candidate,
    Category,
    load_candidates,
)
from support_desk_assistant.evals.retrieval import answer_hit, normalize
from support_desk_assistant.ingest.embed import Embedder, EmbeddingCache, make_voyage_client
from support_desk_assistant.retrieve import Hit

Method = Literal["none", "numbers", "judge"]
AbstainedBy = Literal["gate", "model"]
Attribution = Literal["retrieval_page", "retrieval_chunk", "generation"]
ATTRIBUTIONS: tuple[Attribution, ...] = ("retrieval_page", "retrieval_chunk", "generation")
METHODS: tuple[Method, ...] = ("numbers", "judge", "none")

# Report groups, in report order: (key, heading, categories).
GROUPS: tuple[tuple[str, str, tuple[Category, ...]], ...] = (
    ("single_page", "Single page (baseline)", ("single_page",)),
    ("hard", "Hard categories", ("multi_page", "confusable", "time_sensitive")),
    ("abstention", "Abstention", ("unanswerable_out_of_scope", "unanswerable_in_domain")),
)
# Any cited gold page is enough, except multi_page, which needs every gold page.
ALL_GOLD_CATEGORIES = frozenset({"multi_page"})


class PendingJudgeError(RuntimeError):
    """A pass/fail was read while some facts still wait for the judge."""


# --- Fact decisions --------------------------------------------------------------

# Function words that never carry a fact's meaning. Words that state a condition
# ("maximum", "minimum", "more", "less", "than", "within", "every", "only", "after")
# are deliberately NOT here: they are what tells "maximum of $729" from "minimum is $729".
# Words shorter than ``fact_content_min_chars`` are dropped before this is consulted.
STOPWORDS = frozenset(
    {
        "also", "been", "being", "could", "does", "doing", "done", "from", "have",
        "having", "here", "into", "just", "percent", "shall", "should", "that",
        "their", "theirs", "them", "then", "there", "these", "they", "this", "those",
        "what", "when", "where", "which", "while", "whom", "whose", "will", "with",
        "would", "your", "yours", "yourself",
    }
)  # fmt: skip

_NUMBER_WORDS = {
    w: str(i)
    for i, w in enumerate(
        ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"), 1
    )
}
_NUMBER_WORD_RE = re.compile(r"\b(" + "|".join(_NUMBER_WORDS) + r")\b")
# Times ("8:30", "5:00") are one token; otherwise money/plain numbers with optional
# thousands commas and decimals, then an optional percent or ordinal suffix. A leading
# "$" is kept in the surface form only; the canonical form drops it.
_NUMBER_RE = re.compile(
    r"\b(?P<hour>\d{1,2}):(?P<minute>\d{2})\b"
    r"|(?:\$\s?)?(?P<num>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)"
    r"(?P<suffix>\s?%|\s?per ?cent\b|(?:st|nd|rd|th)\b)?"
)
_WORD_RE = re.compile(r"[a-z]+")


@dataclass(frozen=True)
class NumberToken:
    canonical: str  # "1507.65", "2%", "64", "8:30"
    surface: str  # as written in the text, for reasons


def _canonical_number(digits: str) -> str:
    value = Decimal(digits.replace(",", "")).normalize()
    return format(value, "f")  # "2.0" -> "2", "1,000" -> "1000" (not "1E+3")


def numeric_tokens(text: str, *, number_words: bool = False) -> list[NumberToken]:
    """Numbers in already-normalized ``text``, canonicalised: "$1,507.65" == "1507.65",
    "2.0%" == "2%" (but "5%" != "5"), "64th" == "64", "5:00" == "5". With
    ``number_words`` (answers only), "one".."ten" count as digits."""
    if number_words:
        text = _NUMBER_WORD_RE.sub(lambda m: _NUMBER_WORDS[m.group(1)], text)
    tokens = []
    for m in _NUMBER_RE.finditer(text):
        if m.group("hour"):
            minute = m.group("minute")
            hour = str(int(m.group("hour")))
            canonical = hour if minute == "00" else f"{hour}:{minute}"
        else:
            canonical = _canonical_number(m.group("num"))
            suffix = (m.group("suffix") or "").strip()
            if suffix == "%" or suffix.startswith("per"):
                canonical += "%"
        tokens.append(NumberToken(canonical, m.group(0).strip()))
    return tokens


def _fold(word: str) -> str:
    return word.removesuffix("s")


def content_words(fact: str, settings: EvalSettings) -> list[str]:
    """Plural-folded alphabetic words of a normalized fact that carry meaning."""
    words = [
        w
        for w in _WORD_RE.findall(fact)
        if len(w) >= settings.fact_content_min_chars and w not in STOPWORDS
    ]
    return list(dict.fromkeys(_fold(w) for w in words))


def answer_words(answer: str) -> frozenset[str]:
    return frozenset(_fold(w) for w in _WORD_RE.findall(answer))


@dataclass(frozen=True)
class FactDecision:
    fact: str
    method: Method
    present: bool | None  # None: waiting for the judge
    reason: str
    judge_error: bool = False

    @property
    def pending(self) -> bool:
        return self.present is None


def decide_fact(
    fact: str, answer: str, *, abstained_or_failed: bool, settings: EvalSettings
) -> FactDecision:
    """Decide a key fact without the judge where that is safe (decisions.md #21).

    Returns ``method="judge", present=None`` for facts the judge must decide.
    """
    norm_answer = normalize(answer)
    if abstained_or_failed or not norm_answer:
        return FactDecision(fact, "none", False, "no answer (abstained or failed)")

    norm_fact = normalize(fact)
    fact_numbers = numeric_tokens(norm_fact)
    if not fact_numbers:
        return FactDecision(fact, "judge", None, "no number")

    have = {t.canonical for t in numeric_tokens(norm_answer, number_words=True)}
    missing = [t.surface for t in fact_numbers if t.canonical not in have]
    if missing:
        return FactDecision(fact, "numbers", False, "missing: " + ", ".join(missing))

    if len(norm_fact.split()) > settings.atomic_fact_max_words:
        return FactDecision(
            fact, "judge", None, f"longer than {settings.atomic_fact_max_words} words"
        )
    words = answer_words(norm_answer)
    absent = [w for w in content_words(norm_fact, settings) if w not in words]
    if absent:
        return FactDecision(fact, "judge", None, "content word missing: " + ", ".join(absent))
    return FactDecision(fact, "numbers", True, "numbers and content words present")


def decide_facts(
    facts: Iterable[str], answer: str, *, abstained_or_failed: bool, settings: EvalSettings
) -> tuple[FactDecision, ...]:
    return tuple(
        decide_fact(f, answer, abstained_or_failed=abstained_or_failed, settings=settings)
        for f in facts
    )


def pending_facts(decisions: Sequence[FactDecision]) -> list[str]:
    """The facts the judge must decide, in the order its verdicts must come back."""
    return [d.fact for d in decisions if d.pending]


@dataclass(frozen=True)
class JudgeVerdict:
    present: bool
    reason: str


@dataclass(frozen=True)
class JudgeError:
    """Refusal, max_tokens, invalid JSON or wrong verdict indices: no verdict usable."""

    reason: str


def apply_judge_verdicts(
    decisions: Sequence[FactDecision], verdicts: Sequence[JudgeVerdict] | JudgeError
) -> tuple[FactDecision, ...]:
    """Fill pending facts. ``verdicts[i]`` belongs to the i-th pending fact (the judge's
    fact number i+1). A ``JudgeError`` marks every pending fact absent with judge_error."""
    pending = [i for i, d in enumerate(decisions) if d.pending]
    if not isinstance(verdicts, JudgeError) and len(verdicts) != len(pending):
        raise ValueError(f"{len(verdicts)} verdicts for {len(pending)} pending facts")
    out = list(decisions)
    for n, i in enumerate(pending):
        if isinstance(verdicts, JudgeError):
            out[i] = replace(
                out[i], present=False, reason=f"judge error: {verdicts.reason}", judge_error=True
            )
        else:
            out[i] = replace(out[i], present=verdicts[n].present, reason=verdicts[n].reason)
    return tuple(out)


# --- Item scoring ----------------------------------------------------------------


@dataclass(frozen=True)
class ItemScore:
    """Offline per-item metrics. ``facts_fraction``, ``all_facts``, ``passed`` and
    ``attribution`` raise ``PendingJudgeError`` until every judge fact is resolved
    (``with_judge_verdicts``). Fields that don't apply to a category are None."""

    id: str
    category: Category
    question: str
    answerable: bool
    gate: str
    status: str
    abstained: bool
    abstained_by: AbstainedBy | None
    validator_ok: bool
    cited_urls: tuple[str, ...]
    gold_cited: bool | None
    decoy_cited: bool | None  # confusable only
    page_hit: bool | None
    fact_in_context: bool | None
    facts: tuple[FactDecision, ...]
    answered_confidently: bool | None  # unanswerable only

    @property
    def pending_judge(self) -> bool:
        return any(d.pending for d in self.facts)

    def _resolved(self) -> None:
        if self.pending_judge:
            raise PendingJudgeError(f"{self.id}: judge facts not resolved yet")

    @property
    def facts_fraction(self) -> float | None:
        self._resolved()
        if not self.answerable:
            return None
        return sum(1 for d in self.facts if d.present) / len(self.facts)

    @property
    def all_facts(self) -> bool | None:
        self._resolved()
        return all(d.present for d in self.facts) if self.answerable else None

    @property
    def passed(self) -> bool:
        self._resolved()
        if not self.answerable:
            # A refusal or max_tokens stop is the pipeline's own fallback abstention, not
            # the model declining; counting it would flatter the abstention score.
            return self.abstained and self.validator_ok and self.status in (STATUS_OK, STATUS_GATE)
        return (
            not self.abstained
            and self.status == STATUS_OK
            and self.validator_ok
            and bool(self.all_facts)
            and bool(self.gold_cited)
            and self.decoy_cited is not True
        )

    @property
    def attribution(self) -> Attribution | None:
        if not self.answerable or self.passed:
            return None
        if not self.page_hit:
            return "retrieval_page"
        if not self.fact_in_context:
            return "retrieval_chunk"
        return "generation"

    def with_judge_verdicts(self, verdicts: Sequence[JudgeVerdict] | JudgeError) -> "ItemScore":
        return replace(self, facts=apply_judge_verdicts(self.facts, verdicts))

    def to_dict(self) -> dict[str, Any]:
        """Every scored field, for the result row. Raises while judge facts are pending."""
        row = asdict(self)
        row["cited_urls"] = list(self.cited_urls)
        row["facts"] = [asdict(d) for d in self.facts]
        row |= {
            "facts_fraction": self.facts_fraction,
            "all_facts": self.all_facts,
            "passed": self.passed,
            "attribution": self.attribution,
        }
        return row


def cited_urls(citations: Iterable[str], hits: Sequence[Hit]) -> tuple[str, ...]:
    """URLs of retrieved chunks the answer cites, in citation order, deduplicated.
    Citations of chunks that weren't retrieved are the validator's business."""
    url_of = {h.chunk_id: h.url for h in hits}
    return tuple(dict.fromkeys(url_of[c] for c in citations if c in url_of))


def score_item(
    candidate: Candidate,
    *,
    result: AnswerResult,
    hits: Sequence[Hit],
    gate: str,
    status: str,
    validation: ValidationResult,
    k: int,
    settings: EvalSettings,
) -> ItemScore:
    """Score one answered item (decisions.md #21). Pure; judge facts stay pending."""
    abstained = result.abstained
    abstained_by: AbstainedBy | None = None
    if abstained:
        abstained_by = "gate" if gate != GATE_PASS else "model"
    cited = cited_urls(result.citations, hits)
    common = {
        "id": candidate.id,
        "category": candidate.category,
        "question": candidate.question,
        "answerable": candidate.answerable,
        "gate": gate,
        "status": status,
        "abstained": abstained,
        "abstained_by": abstained_by,
        "validator_ok": validation.ok,
        "cited_urls": cited,
    }
    if not candidate.answerable:
        return ItemScore(
            **common,
            gold_cited=None,
            decoy_cited=None,
            page_hit=None,
            fact_in_context=None,
            facts=(),
            answered_confidently=not abstained,
        )
    if not candidate.key_facts:
        raise ValueError(f"{candidate.id}: answerable item without key_facts")

    gold = set(candidate.gold_urls)
    retrieved = {h.url for h in hits[:k]}
    every_gold = candidate.category in ALL_GOLD_CATEGORIES
    has = all if every_gold else any
    decoy_cited = None
    if candidate.category == "confusable":
        decoys = set(candidate.source_urls) - gold
        decoy_cited = any(u in decoys for u in cited)
    return ItemScore(
        **common,
        gold_cited=has(u in cited for u in gold),
        decoy_cited=decoy_cited,
        page_hit=has(u in retrieved for u in gold),
        fact_in_context=answer_hit(hits, candidate.key_facts, candidate.gold_urls, k),
        facts=decide_facts(
            candidate.key_facts,
            result.answer,
            abstained_or_failed=abstained or status != STATUS_OK,
            settings=settings,
        ),
        answered_confidently=None,
    )


# --- Judge -----------------------------------------------------------------------


class JudgeItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fact_index: int  # 1-based, as numbered in the user message
    # reason before present: the model writes in schema order, so it reasons before it
    # commits to a verdict (decision 25).
    reason: str
    present: bool


class JudgeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdicts: list[JudgeItem]


def judge_schema() -> dict[str, Any]:
    return transform_schema(JudgeResult)


def load_judge_prompt(settings: Settings) -> str:
    """Raises ``SystemPromptNotReady`` if the prompt is missing or only a TODO header."""
    return load_system_prompt(settings.eval.judge_prompt_path)


def render_judge_message(question: str, answer: str, facts: Sequence[str]) -> str:
    """Question, answer and the facts numbered from 1. No chunks, no citations: the
    judge rates the answer text alone."""
    numbered = "\n".join(f"{i}. {fact}" for i, fact in enumerate(facts, 1))
    return (
        f"<question>\n{question}\n</question>\n"
        f"<answer>\n{answer}\n</answer>\n"
        f"<facts>\n{numbered}\n</facts>"
    )


def build_judge_request(system: str, user_message: str, settings: Settings) -> dict[str, Any]:
    model = settings.eval.judge_model
    profile = settings.answer.models[model]
    system_block: dict[str, Any] = {"type": "text", "text": system}
    if settings.answer.prompt_caching:
        # Expected no-op: the judge prompt is below the minimum cacheable length.
        system_block["cache_control"] = {"type": "ephemeral"}
    return {
        "model": model,
        "max_tokens": settings.eval.judge_max_tokens,
        "system": [system_block],
        "messages": [{"role": "user", "content": user_message}],
        "thinking": {"type": profile.thinking},
        "output_config": {
            "effort": profile.effort,
            "format": {"type": "json_schema", "schema": judge_schema()},
        },
    }


def judge_cache_key(system: str, user_message: str, settings: Settings) -> str:
    model = settings.eval.judge_model
    profile = settings.answer.models[model]
    return cache_key(
        {
            "kind": "eval_judge",
            "schema_version": settings.eval.judge_schema_version,
            "model": model,
            "system": system,
            "user": user_message,
            "max_tokens": settings.eval.judge_max_tokens,
            "effort": profile.effort,
            "thinking": profile.thinking,
        }
    )


def parse_judge(raw: dict[str, Any], n_facts: int) -> list[JudgeVerdict] | JudgeError:
    """Raw API response -> one verdict per fact, in fact order. Never raises: anything
    but exactly one verdict for each of 1..n_facts is a ``JudgeError``."""
    stop = raw.get("stop_reason")
    if stop in ("refusal", "max_tokens"):
        return JudgeError(stop)
    text = "".join(b.get("text", "") for b in raw.get("content", []) if b.get("type") == "text")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return JudgeError("invalid JSON")
    try:
        result = JudgeResult.model_validate(data)
    except ValidationError:
        return JudgeError("output does not match the schema")
    indices = [v.fact_index for v in result.verdicts]
    expected = range(1, n_facts + 1)
    out_of_range = sorted({i for i in indices if i not in expected})
    if out_of_range:
        return JudgeError(f"fact_index out of range: {out_of_range}")
    duplicates = sorted({i for i in indices if indices.count(i) > 1})
    if duplicates:
        return JudgeError(f"duplicate fact_index: {duplicates}")
    missing = [i for i in expected if i not in indices]
    if missing:
        return JudgeError(f"missing fact_index: {missing}")
    by_index = {v.fact_index: v for v in result.verdicts}
    return [JudgeVerdict(by_index[i].present, by_index[i].reason) for i in expected]


@dataclass(frozen=True)
class JudgeRun:
    decisions: tuple[FactDecision, ...]  # with the judge's verdicts applied
    calls: int = 0  # API calls made (0 or 1)
    cache_hit: bool = False
    usage: Usage = field(default_factory=Usage)  # of the API call; zero on a cache hit
    cost_usd: float = 0.0
    cost_cad: float = 0.0
    error: str | None = None  # JudgeError reason, if the judge's output was unusable
    # Usage stored in the response (fresh or cached) and its CAD as if nothing was cached.
    usage_list: Usage = field(default_factory=Usage)
    cost_cad_list: float = 0.0
    cache_key: str | None = None  # None when no judge call was needed


def run_judge(
    question: str,
    answer: str,
    decisions: Sequence[FactDecision],
    *,
    settings: Settings,
    system: str,
    client: MessagesClient | None = None,
) -> JudgeRun:
    """Decide the pending facts with one judge call (or its cached response). No pending
    facts -> no call. API/network errors propagate and are not cached."""
    pending = pending_facts(decisions)
    if not pending:
        return JudgeRun(tuple(decisions))
    user = render_judge_message(question, answer, pending)
    key = judge_cache_key(system, user, settings)
    cache = ResponseCache(settings.eval_cache_dir)
    raw = cache.get(key)
    run = JudgeRun(tuple(decisions), cache_hit=raw is not None, cache_key=key)
    if raw is None:
        client = client or make_anthropic_client(settings)
        message = client.messages.create(**build_judge_request(system, user, settings))
        raw = message.to_dict()
        cache.put(key, raw)
        usage = usage_from_raw(raw)
        usd = cost_usd(usage, settings.eval.judge_model, settings.answer)
        run = replace(
            run, calls=1, usage=usage, cost_usd=usd, cost_cad=usd_to_cad(usd, settings.answer)
        )
    usage_list = usage_from_raw(raw)
    usd_list = cost_usd(usage_list, settings.eval.judge_model, settings.answer)
    verdicts = parse_judge(raw, len(pending))
    return replace(
        run,
        decisions=apply_judge_verdicts(decisions, verdicts),
        error=verdicts.reason if isinstance(verdicts, JudgeError) else None,
        usage_list=usage_list,
        cost_cad_list=usd_to_cad(usd_list, settings.answer),
    )


def _judge_call_usd(input_chars: int, extra_input_tokens: int, settings: Settings) -> float:
    usage = Usage(
        input_tokens=int(input_chars / CHARS_PER_TOKEN_ESTIMATE) + extra_input_tokens,
        output_tokens=settings.eval.judge_max_tokens,
    )
    return cost_usd(usage, settings.eval.judge_model, settings.answer)


def estimate_judge_call(
    question: str,
    answer: str | None,
    decisions: Sequence[FactDecision] | None,
    system: str,
    settings: Settings,
    cache: ResponseCache,
    *,
    facts: Sequence[str] = (),
    answer_model: str | None = None,
) -> tuple[int, float]:
    """(judge calls needed, upper-bound USD) for one item; output at the max_tokens ceiling.

    Answer and decisions known (cached answer): exact, 0 calls if nothing is pending or
    the judge response is cached. Answer unknown (``answer``/``decisions`` None): one
    call over every fact in ``facts``, the answer bounded by ``answer_model``'s max_tokens
    (default ``settings.answer.model``). Whether an unanswered item needs the judge at
    all is the caller's call."""
    if answer is not None and decisions is not None:
        pending = pending_facts(decisions)
        if not pending:
            return 0, 0.0
        user = render_judge_message(question, answer, pending)
        if cache.get(judge_cache_key(system, user, settings)) is not None:
            return 0, 0.0
        return 1, _judge_call_usd(len(system) + len(user), 0, settings)
    model = answer_model or settings.answer.model
    user = render_judge_message(question, "", facts)
    answer_tokens = settings.answer.models[model].max_tokens
    return 1, _judge_call_usd(len(system) + len(user), answer_tokens, settings)


# --- Aggregation -----------------------------------------------------------------


def _rate(flags: Sequence[bool]) -> float | None:
    return round(sum(1 for f in flags if f) / len(flags), 3) if flags else None


def _mean(values: Sequence[float]) -> float | None:
    return round(sum(values) / len(values), 3) if values else None


def _category_metrics(
    category: Category, items: Sequence[ItemScore], costs_cad: Mapping[str, float] | None
) -> dict[str, Any]:
    out: dict[str, Any] = {"n": len(items)}
    if category in UNANSWERABLE:
        out |= {
            "correct_abstention_rate": _rate([s.passed for s in items]),
            "abstained_by_gate": sum(1 for s in items if s.abstained_by == "gate"),
            "abstained_by_model": sum(1 for s in items if s.abstained_by == "model"),
            "answered_confidently": sum(1 for s in items if s.answered_confidently),
            "validator_ok_rate": _rate([s.validator_ok for s in items]),
        }
    else:
        out |= {
            "pass_rate": _rate([s.passed for s in items]),
            "all_facts_rate": _rate([bool(s.all_facts) for s in items]),
            "mean_facts_fraction": _mean([s.facts_fraction or 0.0 for s in items]),
            "validator_ok_rate": _rate([s.validator_ok for s in items]),
            "gold_cited_rate": _rate([bool(s.gold_cited) for s in items]),
            "decoy_cited_rate": (
                _rate([bool(s.decoy_cited) for s in items]) if category == "confusable" else None
            ),
            # Same rule as breakdown._intentional: refusal/max_tokens fallbacks are generation
            # failures, not false abstentions (decisions.md #23).
            "false_abstention_rate": _rate(
                [s.abstained and s.status in (STATUS_OK, STATUS_GATE) for s in items]
            ),
            "attribution": {a: sum(1 for s in items if s.attribution == a) for a in ATTRIBUTIONS},
        }
    facts = [d for s in items for d in s.facts]
    out["fact_methods"] = {m: sum(1 for d in facts if d.method == m) for m in METHODS}
    out["judge_errors"] = sum(1 for d in facts if d.judge_error)
    out["cost_cad"] = (
        round(sum(costs_cad.get(s.id, 0.0) for s in items), 6) if costs_cad is not None else None
    )
    return out


def aggregate(
    items: Sequence[ItemScore], costs_cad: Mapping[str, float] | None = None
) -> dict[str, Any]:
    """Per-category metrics, every category present (n=0 if empty), no overall number.
    ``costs_cad`` maps item id -> CAD spent on it; omitted, costs are None."""
    if any(s.pending_judge for s in items):
        raise PendingJudgeError("aggregate needs every judge fact resolved")
    return {
        "groups": {key: list(cats) for key, _, cats in GROUPS},
        "categories": {
            c: _category_metrics(c, [s for s in items if s.category == c], costs_cad)
            for c in CATEGORIES
        },
    }


# --- Markdown --------------------------------------------------------------------

DASH = "-"


def _fmt_rate(value: float | None) -> str:
    return DASH if value is None else f"{value:.2f}"


def _fmt_cost(value: float | None) -> str:
    return DASH if value is None else f"{value:.4f}"


def _fmt_count(value: int, n: int) -> str:
    return str(value) if n else DASH


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    return [
        "| " + " | ".join(header) + " |",
        "|" + "---|" * len(header),
        *("| " + " | ".join(r) + " |" for r in rows),
    ]


def _answerable_row(category: str, m: dict[str, Any]) -> list[str]:
    n, attribution = m["n"], m["attribution"]
    return [
        category,
        str(n),
        _fmt_rate(m["pass_rate"]),
        _fmt_rate(m["all_facts_rate"]),
        _fmt_rate(m["mean_facts_fraction"]),
        _fmt_rate(m["validator_ok_rate"]),
        _fmt_rate(m["gold_cited_rate"]),
        _fmt_rate(m["decoy_cited_rate"]),
        _fmt_rate(m["false_abstention_rate"]),
        *(_fmt_count(attribution[a], n) for a in ATTRIBUTIONS),
        _fmt_cost(m["cost_cad"]),
    ]


def _abstention_row(category: str, m: dict[str, Any]) -> list[str]:
    n = m["n"]
    return [
        category,
        str(n),
        _fmt_rate(m["correct_abstention_rate"]),
        _fmt_count(m["abstained_by_gate"], n),
        _fmt_count(m["abstained_by_model"], n),
        _fmt_count(m["answered_confidently"], n),
        _fmt_rate(m["validator_ok_rate"]),
        _fmt_cost(m["cost_cad"]),
    ]


ANSWERABLE_HEADER = [
    "category",
    "n",
    "pass",
    "all facts",
    "mean facts",
    "validator ok",
    "gold cited",
    "decoy cited",
    "false abstention",
    "fail: retrieval page",
    "fail: retrieval chunk",
    "fail: generation",
    "cost CA$",
]
ABSTENTION_HEADER = [
    "category",
    "n",
    "correct abstention",
    "abstained by gate",
    "abstained by model",
    "answered confidently (fail)",
    "validator ok",
    "cost CA$",
]


def _header(meta: dict[str, Any]) -> list[str]:
    dirty = " (dirty)" if meta.get("git_dirty") else ""
    partial = " · PARTIAL" if meta.get("partial") else ""
    return [
        f"# Answer eval: {meta.get('collection', '?')}, {meta.get('answer_model', '?')}",
        "",
        f"run {meta.get('run_id', '?')} · "
        f"{meta.get('created_at', '?')} · git {meta.get('git_sha', '?')}{dirty} · "
        f"{meta.get('dataset', '?')} · k={meta.get('k', '?')} · "
        f"judge {meta.get('judge_model', '?')}{partial}",
        "",
    ]


def _failure_lines(row: dict[str, Any]) -> list[str]:
    if not row["answerable"]:
        if row["answered_confidently"]:
            why = "answered confidently"
        elif row["status"] not in (STATUS_OK, STATUS_GATE):
            why = f"generation did not complete ({row['status']})"
        else:
            why = "validator failed"
        return [f"- **{row['id']}** ({row['category']}, {why}): {row['question']}"]
    why = [row["attribution"]]
    if row["abstained"]:
        why.append(f"abstained by {row['abstained_by']}")
    if not row["validator_ok"]:
        why.append("validator failed")
    if not row["abstained"] and not row["gold_cited"]:
        why.append("gold not cited")
    if row["decoy_cited"]:
        why.append("decoy cited")
    lines = [f"- **{row['id']}** ({row['category']}, {', '.join(why)}): {row['question']}"]
    for d in row["facts"]:
        if not d["present"]:
            error = " [judge error]" if d["judge_error"] else ""
            lines.append(f"  - missing ({d['method']}{error}): {d['fact']}")
    return lines


SUMMARY_KEYS = ("overall", "by_program", "abstention", "citation", "attribution")
TOKEN_KINDS = (
    ("input_tokens", "input"),
    ("output_tokens", "output"),
    ("cache_creation_input_tokens", "cache write"),
    ("cache_read_input_tokens", "cache read"),
)


def _cost_lines(meta: Mapping[str, Any]) -> list[str]:
    """Tokens by kind, spent this run vs list cost, calls and cache hits. Parts the meta
    lacks are left out."""
    out = ["", "## Cost and tokens", ""]
    tokens = meta.get("tokens")
    if tokens:
        out += [
            "Tokens used by this run (cache hits and gate abstentions use none):",
            "",
            *_table(
                ["part", *(label for _, label in TOKEN_KINDS)],
                [
                    [part, *(str(tokens[part][kind]) for kind, _ in TOKEN_KINDS)]
                    for part in ("answer", "judge")
                ],
            ),
            "",
        ]
    cost, listed = meta.get("cost_cad") or {}, meta.get("cost_cad_list") or {}
    if cost and listed:
        out += [
            "Spent this run vs list cost (what the Anthropic responses cost when made, as if "
            "nothing was cached):",
            "",
            *_table(
                ["part", "spent CA$", "list CA$"],
                [
                    ["answers", _fmt_cost(cost["answers"]), _fmt_cost(listed["answers"])],
                    ["judge", _fmt_cost(cost["judge"]), _fmt_cost(listed["judge"])],
                    ["voyage", _fmt_cost(cost["voyage"]), DASH],
                    ["sum", _fmt_cost(cost["total"]), _fmt_cost(listed["total"])],
                ],
            ),
            "",
        ]
    counts = meta.get("counts")
    if counts:
        out.append(
            f"Answer calls: {counts['answer_calls']} ({counts['answer_cache_hits']} cache "
            f"hits, {counts['gate_abstentions']} gate abstentions). Judge calls: "
            f"{counts['judge_calls']} ({counts['judge_cache_hits']} cache hits, "
            f"{counts['judge_errors']} judge errors)."
        )
    return out if len(out) > 3 else []


def render_markdown(report: dict[str, Any]) -> str:
    """``report``: {"metrics": aggregate(...) [+ breakdown.summarize(...)], "items":
    [row, ...], optional "meta"}. Category tables (every row with n), then the overall
    and breakdown sections when the metrics have them, then cost when meta has it."""
    all_metrics = report["metrics"]
    metrics = all_metrics["categories"]
    meta = report.get("meta")
    out = _header(meta) if meta else []
    out += [
        "Category tables first (decisions.md #20), then overall: micro, with macro beside "
        "it; answerable and abstention are never blended. Every row carries n; every rate "
        "in the overall and breakdown sections also carries a 95% Wilson interval. Decoy "
        "cited applies to confusable items only.",
    ]
    for key, heading, cats in GROUPS:
        if key == "abstention":
            table = _table(ABSTENTION_HEADER, [_abstention_row(c, metrics[c]) for c in cats])
        else:
            table = _table(ANSWERABLE_HEADER, [_answerable_row(c, metrics[c]) for c in cats])
        out += ["", f"## {heading}", "", *table]

    if all(k in all_metrics for k in SUMMARY_KEYS):
        out += ["", *breakdown.render_summary_markdown(all_metrics)]

    out += ["", "## Fact decisions", ""]
    rows = []
    for c in CATEGORIES:
        m = metrics[c]
        rows.append(
            [c, str(m["n"]), *(str(m["fact_methods"][x]) for x in METHODS), str(m["judge_errors"])]
        )
    out += _table(["category", "n", "numbers", "judge", "none", "judge errors"], rows)

    out += ["", "## Failures", ""]
    failures = [r for r in report["items"] if not r["passed"]]
    if not failures:
        out.append("None.")
    for row in failures:
        out += _failure_lines(row)
    if meta:
        out += _cost_lines(meta)
    return "\n".join(out) + "\n"


# --- Plan (the dry run) ----------------------------------------------------------


def select_items(
    candidates: Sequence[Candidate],
    categories: Sequence[str] | None = None,
    limit: int | None = None,
) -> list[Candidate]:
    """File order kept. ``limit`` is per category (the first N of each in file order)."""
    taken: Counter[str] = Counter()
    out = []
    for c in candidates:
        if categories and c.category not in categories:
            continue
        if limit is not None and taken[c.category] >= limit:
            continue
        taken[c.category] += 1
        out.append(c)
    return out


def may_need_judge(fact: str, settings: EvalSettings) -> bool:
    """Whether some answer could send this fact to the judge. Only a short fact made of
    numbers alone ("$1,507.65") is always decided by the number check."""
    norm = normalize(fact)
    return not (
        numeric_tokens(norm)
        and len(norm.split()) <= settings.atomic_fact_max_words
        and not content_words(norm, settings)
    )


def voyage_cost_cad(tokens: int, settings: Settings) -> float:
    return usd_to_cad(tokens * settings.embed.usd_per_mtok / 1_000_000, settings.answer)


@dataclass(frozen=True)
class PlanItem:
    candidate: Candidate
    prepared: Prepared
    answer_usd: float  # upper bound; 0 for a gate abstention or a cached answer
    judge_calls: int
    judge_usd: float
    judge_exact: bool  # the answer is known (cached or gated), so judge_calls is exact


@dataclass(frozen=True)
class VoyageUsage:
    queries: int
    cached: int
    embedded: int
    tokens: int
    cad: float


@dataclass(frozen=True)
class Plan:
    items: tuple[PlanItem, ...]
    voyage: VoyageUsage
    answer_cad: float  # upper bound
    judge_cad: float  # upper bound

    @property
    def answer_calls(self) -> int:
        return sum(1 for p in self.items if p.prepared.will_call_api)

    @property
    def answer_cache_hits(self) -> int:
        return sum(
            1 for p in self.items if p.prepared.gate == GATE_PASS and p.prepared.cached is not None
        )

    @property
    def gate_abstentions(self) -> int:
        return sum(1 for p in self.items if p.prepared.gate != GATE_PASS)

    @property
    def judge_calls(self) -> int:
        return sum(p.judge_calls for p in self.items)

    @property
    def anthropic_cad(self) -> float:
        return self.answer_cad + self.judge_cad

    @property
    def total_cad(self) -> float:
        return self.anthropic_cad + self.voyage.cad


def _plan_judge(
    c: Candidate,
    p: Prepared,
    *,
    settings: Settings,
    k: int,
    judge_system: str,
    cache: ResponseCache,
) -> tuple[int, float, bool]:
    """(judge calls, upper-bound USD, exact) for one prepared item."""
    if not c.answerable or p.gate != GATE_PASS:
        return 0, 0.0, True  # no facts, or every fact "none": never judged
    if p.cached is not None:
        result, status = parse_response(p.cached)
        known = score_item(
            c,
            result=result,
            hits=p.hits,
            gate=p.gate,
            status=status,
            validation=validate_answer(result, p.retrieved_ids),
            k=k,
            settings=settings.eval,
        )
        calls, usd = estimate_judge_call(
            c.question, result.answer, known.facts, judge_system, settings, cache
        )
        return calls, usd, True
    facts = [f for f in c.key_facts if may_need_judge(f, settings.eval)]
    if not facts:
        return 0, 0.0, False
    calls, usd = estimate_judge_call(
        c.question, None, None, judge_system, settings, cache, facts=facts, answer_model=p.model
    )
    return calls, usd, False


def make_plan(
    candidates: Sequence[Candidate],
    *,
    settings: Settings,
    embedder: Embedder,
    qdrant: QdrantClient,
    k: int,
    model: str,
    judge_system: str,
) -> Plan:
    """Retrieve, gate and look up caches for every item (Voyage query embeddings for
    uncached questions; no Anthropic call), then price the run as an upper bound."""
    judge_cache = ResponseCache(settings.eval_cache_dir)
    before = replace(embedder.stats)
    items = []
    for c in candidates:
        p = prepare(
            c.question, settings=settings, embedder=embedder, qdrant=qdrant, k=k, model=model
        )
        calls, judge_usd, exact = _plan_judge(
            c, p, settings=settings, k=k, judge_system=judge_system, cache=judge_cache
        )
        items.append(PlanItem(c, p, estimate_cost_usd(p, settings), calls, judge_usd, exact))
    after = embedder.stats
    tokens = after.tokens_billed - before.tokens_billed
    voyage = VoyageUsage(
        queries=len(candidates),
        cached=after.cache_hits - before.cache_hits,
        embedded=after.api_calls - before.api_calls,
        tokens=tokens,
        cad=voyage_cost_cad(tokens, settings),
    )
    return Plan(
        tuple(items),
        voyage,
        answer_cad=usd_to_cad(sum(p.answer_usd for p in items), settings.answer),
        judge_cad=usd_to_cad(sum(p.judge_usd for p in items), settings.answer),
    )


def plan_lines(
    plan: Plan,
    *,
    settings: Settings,
    dataset: str,
    total_items: int,
    k: int,
    model: str,
    cap_cad: float,
) -> list[str]:
    per_category = Counter(p.candidate.category for p in plan.items)
    exact = sum(p.judge_calls for p in plan.items if p.judge_exact)
    v = plan.voyage
    lines = [
        f"Answer eval plan: {dataset}, {len(plan.items)} of {total_items} items, "
        f"collection {settings.index.collection}, k={k}, model {model}, "
        f"judge {settings.eval.judge_model}",
        "",
        "Items per category:",
        *(f"  {c:<28}{per_category[c]:>3}" for c in CATEGORIES),
        "",
        f"Answers: {plan.answer_calls} API call(s), {plan.answer_cache_hits} cached, "
        f"{plan.gate_abstentions} gate abstention(s) (free); <= CA${plan.answer_cad:.4f}",
        f"Judge: {plan.judge_calls} API call(s) ({exact} exact for known answers, "
        f"{plan.judge_calls - exact} assumed for answers not known yet); "
        f"<= CA${plan.judge_cad:.4f}",
        f"Voyage: the dry run makes query-embedding calls for questions not in the embedding "
        f"cache and needs the Qdrant tunnel (QDRANT_URL): {v.queries} query embeddings "
        f"({v.cached} cached, {v.embedded} embedded now), {v.tokens} tokens billed, "
        f"CA${v.cad:.6f} (price per MTok from config; placeholder price until verified)",
        f"Estimate: <= CA${plan.total_cad:.4f} (Anthropic upper bound CA${plan.anthropic_cad:.4f}"
        f" + Voyage CA${v.cad:.6f}) vs cap CA${cap_cad:.2f}. Upper bound: input estimated "
        "from characters, output at the max_tokens ceiling.",
    ]
    return lines


# --- Runner ----------------------------------------------------------------------


class _LazyClient:
    """The Anthropic client, built on first use: a run served entirely from caches (and
    every dry run) needs no API key."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: MessagesClient | None = None

    @property
    def messages(self) -> Any:
        if self._client is None:
            self._client = make_anthropic_client(self._settings)
        return self._client.messages


@dataclass(frozen=True)
class ItemRun:
    candidate: Candidate
    prepared: Prepared
    score: ItemScore  # judge facts resolved
    result: AnswerResult
    validation: ValidationResult
    answer_cad: float
    answer_api_called: bool
    answer_cache_hit: bool
    judge: JudgeRun
    answer_usage: Usage = field(default_factory=Usage)  # this invocation; zero on a hit
    answer_cad_list: float = 0.0  # as if nothing was cached; 0 for a gate abstention

    @property
    def cost_cad(self) -> float:
        return self.answer_cad + self.judge.cost_cad

    @property
    def cost_cad_list(self) -> float:
        return self.answer_cad_list + self.judge.cost_cad_list

    def row(self) -> dict[str, Any]:
        """The result row: every scored field plus what a reviewer needs to read it.
        ``tokens`` is actual usage of this invocation (zeros for cache hits and gate
        abstentions); ``cost_cad_list`` is CAD as if no response had been cached."""
        s = self.score.to_dict()
        return {
            "id": s["id"],
            "category": s["category"],
            "question": s["question"],
            "answerable": s["answerable"],
            "gold_urls": list(self.candidate.gold_urls),
            "retrieved": [
                {"chunk_id": h.chunk_id, "url": h.url, "score": round(h.score, 4)}
                for h in self.prepared.hits
            ],
            "gate": s["gate"],
            "status": s["status"],
            "abstained": s["abstained"],
            "abstain_reason": self.result.abstain_reason,
            "abstained_by": s["abstained_by"],
            "answer": self.result.answer,
            "citations": list(self.result.citations),
            "cited_urls": s["cited_urls"],
            "validator": {
                "ok": self.validation.ok,
                "failed_rules": list(self.validation.failed_rules),
            },
            "validator_ok": s["validator_ok"],
            "facts": s["facts"],
            "facts_fraction": s["facts_fraction"],
            "all_facts": s["all_facts"],
            "gold_cited": s["gold_cited"],
            "decoy_cited": s["decoy_cited"],
            "page_hit": s["page_hit"],
            "fact_in_context": s["fact_in_context"],
            "attribution": s["attribution"],
            "passed": s["passed"],
            "answered_confidently": s["answered_confidently"],
            "cache_hit": self.answer_cache_hit,
            "judge_cache_hit": self.judge.cache_hit,
            "judge_error": self.judge.error,
            "cost_cad": {
                "answer": round(self.answer_cad, 6),
                "judge": round(self.judge.cost_cad, 6),
            },
            "cost_cad_list": {
                "answer": round(self.answer_cad_list, 6),
                "judge": round(self.judge.cost_cad_list, 6),
            },
            "tokens": {"answer": asdict(self.answer_usage), "judge": asdict(self.judge.usage)},
            "answer_cache_key": self.prepared.key if self.prepared.gate == GATE_PASS else None,
            "judge_cache_key": self.judge.cache_key,
            "notes": self.candidate.notes,
        }


@dataclass
class RunResult:
    items: list[ItemRun] = field(default_factory=list)
    voyage_cad: float = 0.0
    stopped_by_cap: bool = False

    @property
    def answer_cad(self) -> float:
        return sum(i.answer_cad for i in self.items)

    @property
    def judge_cad(self) -> float:
        return sum(i.judge.cost_cad for i in self.items)

    @property
    def total_cad(self) -> float:
        return self.voyage_cad + self.answer_cad + self.judge_cad

    @property
    def answer_cad_list(self) -> float:
        return sum(i.answer_cad_list for i in self.items)

    @property
    def judge_cad_list(self) -> float:
        return sum(i.judge.cost_cad_list for i in self.items)

    def tokens(self) -> dict[str, dict[str, int]]:
        """Actual token usage of this invocation by kind, for answers and the judge."""
        kinds = list(asdict(Usage()))
        out = {"answer": dict.fromkeys(kinds, 0), "judge": dict.fromkeys(kinds, 0)}
        for i in self.items:
            for part, usage in (("answer", i.answer_usage), ("judge", i.judge.usage)):
                for kind, n in asdict(usage).items():
                    out[part][kind] += n
        return out

    def counts(self) -> dict[str, int]:
        items = self.items
        return {
            "items": len(items),
            "answer_calls": sum(1 for i in items if i.answer_api_called),
            "answer_cache_hits": sum(1 for i in items if i.answer_cache_hit),
            "judge_calls": sum(i.judge.calls for i in items),
            "judge_cache_hits": sum(1 for i in items if i.judge.cache_hit),
            # Facts marked absent because the judge's output was unusable.
            "judge_errors": sum(1 for i in items for d in i.score.facts if d.judge_error),
            "gate_abstentions": sum(1 for i in items if i.prepared.gate != GATE_PASS),
        }


def run_plan(
    plan: Plan,
    *,
    settings: Settings,
    k: int,
    judge_system: str,
    client: MessagesClient,
    cap_cad: float,
) -> RunResult:
    """Answer, score and judge each planned item in order. Before each item (the first
    included), stop if the actual spend so far (Voyage for the plan + answers + judge)
    plus that item's upper bound (``answer_usd + judge_usd`` from the plan) would pass
    ``cap_cad``; the cap is never crossed by a planned bound. Anthropic API errors
    propagate; completed calls are already cached."""
    run = RunResult(voyage_cad=plan.voyage.cad)
    for p in plan.items:
        if run.total_cad + usd_to_cad(p.answer_usd + p.judge_usd, settings.answer) > cap_cad:
            run.stopped_by_cap = True
            break
        outcome = execute(p.prepared, settings=settings, client=client)
        validation = outcome.validation
        if validation is None:  # validate_answer not implemented: never a pass
            validation = ValidationResult(("validator_not_implemented",))
        score = score_item(
            p.candidate,
            result=outcome.result,
            hits=outcome.hits,
            gate=outcome.gate,
            status=outcome.status,
            validation=validation,
            k=k,
            settings=settings.eval,
        )
        judge = run_judge(
            p.candidate.question,
            outcome.result.answer,
            score.facts,
            settings=settings,
            system=judge_system,
            client=client,
        )
        run.items.append(
            ItemRun(
                candidate=p.candidate,
                prepared=p.prepared,
                score=replace(score, facts=judge.decisions),
                result=outcome.result,
                validation=validation,
                answer_cad=outcome.cost_cad,
                answer_api_called=outcome.api_called,
                answer_cache_hit=outcome.cache_hit,
                judge=judge,
                answer_usage=outcome.usage,
                answer_cad_list=_answer_cad_list(p.prepared, outcome, settings),
            )
        )
    return run


def _answer_cad_list(prepared: Prepared, outcome: Outcome, settings: Settings) -> float:
    """CAD of the answer call as if nothing was cached: from the usage stored in the
    response (the cached one on a hit); 0 for a gate abstention."""
    if prepared.gate != GATE_PASS:
        return 0.0
    usage = usage_from_raw(prepared.cached) if outcome.cache_hit else outcome.usage
    return usd_to_cad(cost_usd(usage, prepared.model, settings.answer), settings.answer)


# --- Report ----------------------------------------------------------------------

RESULT_PREFIX = "answers"
ROWS_SUFFIX = ".jsonl"
SUMMARY_SUFFIX = ".summary.json"
# Bump when a change to the fact decision or the item rules can change a score.
SCORER_VERSION = 1
REASON_PARTIAL = "partial"
REASON_DIRTY = "dirty tree"


def _filename_part(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name)


def scorer_config(cfg: EvalSettings | None = None) -> dict[str, Any]:
    """Everything that decides a fact without the judge, for the provenance block.
    ``cfg`` defaults to the configured ``EvalSettings``."""
    cfg = cfg or get_settings().eval
    return {
        "scorer_version": SCORER_VERSION,
        "atomic_fact_max_words": cfg.atomic_fact_max_words,
        "fact_content_min_chars": cfg.fact_content_min_chars,
        "stopwords": sorted(STOPWORDS),
        "number_words": dict(_NUMBER_WORDS),
    }


def results_dir_reason(*, partial: bool, git: Mapping[str, Any], allow_dirty: bool) -> str | None:
    """Why a run goes to runs/ instead of the committed results/ (None: results/). A
    partial run never goes to results/; nor does a dirty tree, unless ``allow_dirty``."""
    if partial:
        return REASON_PARTIAL
    if provenance.dirty_blocks_results(git) and not allow_dirty:
        return REASON_DIRTY
    return None


def build_report(
    run: RunResult,
    *,
    settings: Settings,
    dataset_path: Path,
    k: int,
    model: str,
    filters: dict[str, Any],
    partial: bool,
    run_id: str,
    points_count: int | None,
    allow_dirty: bool = False,
) -> dict[str, Any]:
    """{"meta", "metrics", "items"}. The flat meta keys are derived from the provenance
    block (``meta["provenance"]``); ``cost_cad`` is what this invocation spent,
    ``cost_cad_list`` what the Anthropic responses cost when they were made."""
    prov = provenance.collect_provenance(
        settings,
        dataset_path=dataset_path,
        answer_model=model,
        k=k,
        scorer_config=scorer_config(settings.eval),
        points_count=points_count,
        run_id=run_id,
    )
    rows = [i.row() for i in run.items]
    answers_list, judge_list = run.answer_cad_list, run.judge_cad_list
    meta = {
        "run_id": run_id,
        "created_at": prov["created_at"],
        "git_sha": prov["git"]["sha"],
        "git_dirty": prov["git"]["dirty"],
        "dataset": prov["dataset"]["name"],
        "dataset_sha256": prov["dataset"]["sha256"],
        "collection": prov["corpus"]["collection"],
        "k": k,
        "answer_model": model,
        "judge_model": settings.eval.judge_model,
        "answer_prompt_sha256": prov["prompts"]["answer_sha256"],
        "judge_prompt_sha256": prov["prompts"]["judge_sha256"],
        "schema_version": prov["schema_version"],
        "judge_schema_version": prov["judge_schema_version"],
        "min_score": prov["min_score"],
        "prices_as_of": prov["prices_as_of"],
        "usd_cad": prov["usd_cad"],
        "embed_model": prov["corpus"]["embed_model"],
        "filters": filters,
        "partial": partial,
        "stopped_by_cap": run.stopped_by_cap,
        "results_dir_reason": results_dir_reason(
            partial=partial, git=prov["git"], allow_dirty=allow_dirty
        ),
        "counts": run.counts(),
        "tokens": run.tokens(),
        "cost_cad": {
            "answers": round(run.answer_cad, 6),
            "judge": round(run.judge_cad, 6),
            "voyage": round(run.voyage_cad, 6),
            "total": round(run.total_cad, 6),
        },
        "cost_cad_list": {
            "answers": round(answers_list, 6),
            "judge": round(judge_list, 6),
            "total": round(answers_list + judge_list, 6),
        },
        "provenance": prov,
    }
    metrics = {
        **aggregate(
            [i.score for i in run.items], costs_cad={i.score.id: i.cost_cad for i in run.items}
        ),
        **breakdown.summarize(
            rows,
            program_of=breakdown.load_program_map(settings.manifest_path),
            z=settings.eval.ci_z,
        ),
    }
    return {"meta": meta, "metrics": metrics, "items": rows}


def report_dir(report: dict[str, Any], settings: Settings) -> Path:
    """results/ (committed) only for a full run from a clean tree (or ``--allow-dirty``);
    anything else goes to runs/, and ``meta["results_dir_reason"]`` says why."""
    meta = report["meta"]
    if meta.get("partial") or meta.get("results_dir_reason"):
        return settings.runs_dir
    return settings.eval_results_dir


def report_stem(meta: Mapping[str, Any]) -> str:
    """``answers_<collection>_<model>_<run_id>``: runs on the same day never collide."""
    return "_".join(
        [
            RESULT_PREFIX,
            _filename_part(meta["collection"]),
            _filename_part(meta["answer_model"]),
            _filename_part(meta["run_id"]),
        ]
    )


def write_report(report: dict[str, Any], directory: Path) -> tuple[Path, Path, Path]:
    """Three files: ``<stem>.jsonl`` (one result row per line), ``<stem>.summary.json``
    ({"meta", "metrics"}) and ``<stem>.md``. Returns (rows, summary, markdown) paths."""
    stem = report_stem(report["meta"])
    directory.mkdir(parents=True, exist_ok=True)
    rows_path = directory / f"{stem}{ROWS_SUFFIX}"
    summary_path = directory / f"{stem}{SUMMARY_SUFFIX}"
    md_path = directory / f"{stem}.md"
    rows_path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in report["items"]),
        encoding="utf-8",
    )
    summary = {"meta": report["meta"], "metrics": report["metrics"]}
    summary_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return rows_path, summary_path, md_path


def _report_files(path: Path) -> tuple[Path, Path, str]:
    """(summary path, rows path, stem) for either file of a written report."""
    path = Path(path)
    for suffix in (SUMMARY_SUFFIX, ROWS_SUFFIX):
        if path.name.endswith(suffix):
            stem = path.name[: -len(suffix)]
            return (
                path.with_name(stem + SUMMARY_SUFFIX),
                path.with_name(stem + ROWS_SUFFIX),
                stem,
            )
    raise ValueError(f"{path}: expected a {SUMMARY_SUFFIX} or {ROWS_SUFFIX} results file")


def load_report(path: Path) -> dict[str, Any]:
    """{"meta", "metrics", "items"} from either the ``.summary.json`` or the ``.jsonl``
    of a report (the sibling file is read too). Raises OSError or ValueError."""
    summary_path, rows_path, _ = _report_files(path)
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if not isinstance(summary, dict):
        raise ValueError(f"{summary_path}: not a JSON object")
    items = [
        json.loads(line)
        for line in rows_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return {"meta": summary.get("meta", {}), "metrics": summary.get("metrics", {}), "items": items}


# --- Hand-review export ----------------------------------------------------------

Entry = tuple[dict[str, Any], dict[str, Any]]  # (item row, fact decision)


def newest_results_file(settings: Settings) -> Path | None:
    """The newest answers_*.summary.json in results/, else the newest in runs/."""
    for directory in (settings.eval_results_dir, settings.runs_dir):
        files = sorted(
            directory.glob(f"{RESULT_PREFIX}_*{SUMMARY_SUFFIX}"),
            key=lambda p: (p.stat().st_mtime, p.name),
        )
        if files:
            return files[-1]
    return None


def _spread(entries: Sequence[Entry], n: int, rng: random.Random) -> list[Entry]:
    """Up to ``n`` entries, round-robin across categories, shuffled within each."""
    by_category: dict[str, list[Entry]] = {c: [] for c in CATEGORIES}
    for e in entries:
        by_category[e[0]["category"]].append(e)
    for group in by_category.values():
        rng.shuffle(group)
    out: list[Entry] = []
    while len(out) < n and any(by_category.values()):
        for c in CATEGORIES:
            if by_category[c] and len(out) < n:
                out.append(by_category[c].pop(0))
    return out


def _by_report_order(entries: list[Entry]) -> list[Entry]:
    order = {c: i for i, c in enumerate(CATEGORIES)}
    return sorted(entries, key=lambda e: (order[e[0]["category"]], e[0]["id"], e[1]["fact"]))


def pick_judged_facts(rows: Sequence[dict[str, Any]], n: int, rng: random.Random) -> list[Entry]:
    """Up to ``n`` judge-decided facts, half present and half absent where the pools allow
    (a short pool is topped up from the other). Judge errors carry no verdict: skipped."""
    judged = [
        (r, d) for r in rows for d in r["facts"] if d["method"] == "judge" and not d["judge_error"]
    ]
    present = [e for e in judged if e[1]["present"]]
    absent = [e for e in judged if not e[1]["present"]]
    n_present = min(len(present), n // 2)
    n_absent = min(len(absent), n - n_present)
    n_present = min(len(present), n - n_absent)
    return _by_report_order(_spread(present, n_present, rng) + _spread(absent, n_absent, rng))


def pick_gate_facts(rows: Sequence[dict[str, Any]], n: int, rng: random.Random) -> list[Entry]:
    """Up to ``n`` facts the number check marked present (possible wrong-context hits)."""
    gate = [(r, d) for r in rows for d in r["facts"] if d["method"] == "numbers" and d["present"]]
    return _by_report_order(_spread(gate, n, rng))


def _yes_no(flag: bool) -> str:
    return "present" if flag else "absent"


def _fact_lines(
    heading: str, row: dict[str, Any], d: dict[str, Any], verdict: str | None
) -> list[str]:
    """One fact block. ``verdict`` None (the blind sheet) leaves out the machine-verdict
    line; ``calibrate.parse_sheet`` depends on every other line's format."""
    lines = [
        "",
        f"### {heading}. {row['id']} ({row['category']})",
        "",
        f"- Question: {row['question']}",
        f"- Answer: {row['answer']}",
        f"- Fact: {d['fact']}",
    ]
    if verdict is not None:
        lines.append(f"- {verdict}: {_yes_no(d['present'])}. {d['reason']}")
    lines.append("- Your verdict: ___")
    return lines


def _in_domain_lines(i: int, row: dict[str, Any]) -> list[str]:
    if row["abstained"]:
        handled = f"abstained, by {row['abstained_by']}"
        said = f"- Abstain reason: {row['abstain_reason']}"
    else:
        handled = "answered (not abstained)"
        said = f"- Answer: {row['answer']}"
    urls: dict[str, float] = {}
    for h in row["retrieved"]:
        urls.setdefault(h["url"], h["score"])
    lines = [
        "",
        f"### C{i}. {row['id']}",
        "",
        f"- Question: {row['question']}",
        f"- Handled: {handled} ({'pass' if row['passed'] else 'FAIL'})",
        said,
        "- Top retrieved:",
        *(f"  - {score:.3f} {url}" for url, score in urls.items()),
    ]
    if not urls:
        lines[-1] += " none"
    lines.append(f"- Notes: {row.get('notes') or '-'}")
    return lines


def render_judge_review(
    report: dict[str, Any], source: str, settings: Settings, *, reveal: bool = False
) -> str:
    """Hand-review sheet (decisions.md #21). Seeded: the same results file always gives
    the same sheet. Blind unless ``reveal``: the judge and number-check verdict lines are
    left out (fact selection is still balanced on the hidden verdict), so the labels can
    go to ``--import-labels`` for judge calibration."""
    cfg = settings.eval
    rng = random.Random(cfg.judge_review_seed)
    rows = report["items"]
    meta = report.get("meta", {})
    judged = pick_judged_facts(rows, cfg.judge_review_n, rng)
    gate = pick_gate_facts(rows, cfg.judge_review_gate_n, rng)
    in_domain = [r for r in rows if r["category"] == "unanswerable_in_domain"]

    out = [
        "# Judge review",
        "",
        f"Source: {source}",
        f"Answer model: {meta.get('answer_model', '?')}",
        f"Judge: {meta.get('judge_model', '?')}",
        f"Seed: {cfg.judge_review_seed}",
        "",
        "Write present or absent after each 'Your verdict'.",
        "",
        *(
            [
                "Machine verdicts are shown (--reveal): labels from this sheet are not blind; "
                "do not record them with --import-labels."
            ]
            if reveal
            else [
                "This sheet is blind: the judge and number-check verdicts are hidden. When it "
                "is filled in, record the labels with "
                "`sda-eval-answers --import-labels <this file>`."
            ]
        ),
        "",
        f"## A. Judged facts (n={len(judged)})",
        "",
        "Decided by the LLM judge. Balanced between present and absent where possible.",
    ]
    for i, (row, d) in enumerate(judged, 1):
        out += _fact_lines(f"A{i}", row, d, "Judge" if reveal else None)
    if not judged:
        out += ["", "None in this run."]

    out += [
        "",
        f"## B. Numbers-gate facts (n={len(gate)})",
        "",
        "NOT judged by the LLM: decided by number matching, not by the LLM.",
        *(
            [
                "Marked present because the fact's numbers and content words appear in the answer.",
                "Check that the number is used in the same context as the fact.",
            ]
            if reveal
            else ["Check whether the answer states the fact, with any number in the same context."]
        ),
    ]
    for i, (row, d) in enumerate(gate, 1):
        out += _fact_lines(f"B{i}", row, d, "Number check (not the LLM)" if reveal else None)
    if not gate:
        out += ["", "None in this run."]

    out += [
        "",
        f"## C. In-domain unanswerables (n={len(in_domain)})",
        "",
        "Every unanswerable_in_domain item: how each was handled.",
    ]
    for i, row in enumerate(in_domain, 1):
        out += _in_domain_lines(i, row)
    if not in_domain:
        out += ["", "None in this run."]
    return "\n".join(out) + "\n"


def _no_results_error(settings: Settings) -> None:
    _err(
        f"no {RESULT_PREFIX}_*{SUMMARY_SUFFIX} in {settings.eval_results_dir} or "
        f"{settings.runs_dir}; run the eval first or pass a file"
    )


def export_review(settings: Settings, results_file: Path | None, *, reveal: bool = False) -> int:
    path = results_file or newest_results_file(settings)
    if path is None:
        _no_results_error(settings)
        return 2
    try:
        report = load_report(path)
    except (OSError, ValueError) as exc:
        _err(f"cannot read {path}: {exc}")
        return 2
    out = settings.judge_review_path
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet = render_judge_review(report, path.name, settings, reveal=reveal)
    out.write_text(sheet, encoding="utf-8")
    print(f"wrote {out} (from {path}){'' if reveal else ', blind'}")
    return 0


# --- CLI -------------------------------------------------------------------------

QDRANT_ERRORS = (ApiException, httpx.TransportError, OSError)


class SetupError(RuntimeError):
    """Missing key or unreachable service: nothing was spent."""


def connect(settings: Settings) -> tuple[Embedder, QdrantClient, EmbeddingCache]:
    """Embedder (with the query-embedding cache) and Qdrant, wired like the retrieval eval."""
    if settings.voyage_api_key is None:
        raise SetupError("VOYAGE_API_KEY is not set (see .env.example)")
    cache = EmbeddingCache(settings.embedding_cache_path)
    embedder = Embedder(make_voyage_client(settings), settings.embed, cache)
    qdrant = QdrantClient(url=settings.qdrant_url, timeout=int(settings.index.timeout_s))
    return embedder, qdrant, cache


def _positive_int(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return n


def _err(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)


def run_eval(
    settings: Settings,
    *,
    dataset: Path,
    categories: list[str] | None,
    limit: int | None,
    model: str,
    k: int,
    cap_cad: float,
    yes: bool,
    allow_dirty: bool = False,
) -> int:
    try:
        candidates = load_candidates(dataset)
    except (OSError, ValidationError) as exc:
        _err(f"cannot load {dataset}: {exc}")
        return 2
    selected = select_items(candidates, categories, limit)
    if not selected:
        _err("no items selected")
        return 2
    no_facts = [c.id for c in selected if c.answerable and not c.key_facts]
    if no_facts:
        _err(f"answerable items without key_facts: {', '.join(no_facts)}")
        return 2
    try:
        load_system_prompt(settings.answer.system_prompt_path)
        judge_system = load_judge_prompt(settings)
    except SystemPromptNotReady as exc:
        _err(str(exc))
        return 2

    collection = settings.index.collection
    try:
        embedder, qdrant, embed_cache = connect(settings)
    except SetupError as exc:
        _err(str(exc))
        return 2
    try:
        try:
            if not qdrant.collection_exists(collection):
                _err(f"collection {collection!r} not found in Qdrant ({settings.qdrant_url})")
                return 2
        except QDRANT_ERRORS as exc:
            _err(
                f"cannot reach Qdrant at {settings.qdrant_url} ({exc}). Is the tunnel up? "
                "Nothing was spent."
            )
            return 2
        try:
            plan = make_plan(
                selected,
                settings=settings,
                embedder=embedder,
                qdrant=qdrant,
                k=k,
                model=model,
                judge_system=judge_system,
            )
        except (*QDRANT_ERRORS, voyageai.error.VoyageError) as exc:
            _err(
                f"retrieval failed ({type(exc).__name__}: {exc}). Nothing was sent to "
                "Anthropic; query embeddings already made are cached."
            )
            return 2
        except SystemPromptNotReady as exc:
            _err(str(exc))
            return 2

        for line in plan_lines(
            plan,
            settings=settings,
            dataset=dataset.name,
            total_items=len(candidates),
            k=k,
            model=model,
            cap_cad=cap_cad,
        ):
            print(line)
        over = plan.total_cad > cap_cad
        if over:
            print(
                f"Over the cap: the estimate CA${plan.total_cad:.4f} exceeds CA${cap_cad:.2f}. "
                "Narrow the run with --limit N (per category) or --category C, or raise --cap."
            )
        if not yes:
            print("dry run: nothing sent to Anthropic; add --yes to run")
            return 0
        if over:
            print("Refused: nothing sent to Anthropic.")
            return 2

        try:
            points_count = qdrant.count(collection_name=collection, exact=True).count
        except QDRANT_ERRORS:
            points_count = None
        run_id = provenance.new_run_id()
        try:
            run = run_plan(
                plan,
                settings=settings,
                k=k,
                judge_system=judge_system,
                client=_LazyClient(settings),
                cap_cad=cap_cad,
            )
        except anthropic.APIError as exc:
            _err(
                f"Anthropic API error: {exc}\nCompleted calls are cached; a re-run resumes "
                "from them. No result file written."
            )
            return 1
    finally:
        embed_cache.close()

    partial = run.stopped_by_cap or len(run.items) < len(candidates)
    report = build_report(
        run,
        settings=settings,
        dataset_path=dataset,
        k=k,
        model=model,
        filters={"limit": limit, "categories": categories},
        partial=partial,
        run_id=run_id,
        points_count=points_count,
        allow_dirty=allow_dirty,
    )
    directory = report_dir(report, settings)
    paths = write_report(report, directory)
    print()
    print(render_markdown(report))
    meta = report["meta"]
    counts, cost = meta["counts"], meta["cost_cad"]
    print(
        f"Anthropic: {counts['answer_calls']} answer call(s) "
        f"({counts['answer_cache_hits']} cached), "
        f"{counts['judge_calls']} judge call(s) ({counts['judge_cache_hits']} cached), "
        f"{counts['judge_errors']} judge error(s)."
    )
    print(
        f"Actual cost: CA${cost['total']:.4f} (answers CA${cost['answers']:.4f}, "
        f"judge CA${cost['judge']:.4f}, Voyage CA${cost['voyage']:.6f}); list cost "
        f"CA${meta['cost_cad_list']['total']:.4f} as if nothing was cached"
    )
    if run.stopped_by_cap:
        print(
            f"Stopped by the spend cap (CA${cap_cad:.2f}) after {len(run.items)} of "
            f"{len(plan.items)} items: the next item's upper bound would pass the cap. "
            "Completed calls are cached; a re-run resumes."
        )
    reason = meta["results_dir_reason"]
    if reason == REASON_DIRTY:
        print(
            f"Written to {directory}, not {settings.eval_results_dir}: the working tree is "
            "dirty (uncommitted changes), so the result can't be tied to a commit. Commit "
            "and re-run (cached calls are free), or pass --allow-dirty."
        )
    elif reason == REASON_PARTIAL:
        print(f"Written to {directory}, not {settings.eval_results_dir}: partial run.")
    elif meta["git_dirty"]:
        print("Dirty working tree, written to results/ because of --allow-dirty.")
    print(f"wrote {', '.join(str(p) for p in paths[:-1])} and {paths[-1]}")
    return 1 if run.stopped_by_cap else 0


def import_labels_cli(settings: Settings, sheet: Path, results_file: Path | None) -> int:
    """``--import-labels``: filled blind sheet + its results file -> calibration records.
    Exit 0 clean, 1 with problems or conflicts, 2 if a file can't be read."""
    from support_desk_assistant.evals import calibrate

    try:
        text = sheet.read_text(encoding="utf-8")
    except OSError as exc:
        _err(f"cannot read {sheet}: {exc}")
        return 2
    path = results_file or newest_results_file(settings)
    if path is None:
        _no_results_error(settings)
        return 2
    try:
        report = load_report(path)
    except (OSError, ValueError) as exc:
        _err(f"cannot read {path}: {exc}")
        return 2
    source_run = report["meta"].get("run_id") or _report_files(path)[2]
    result = calibrate.import_labels(
        text, report["items"], source_run=source_run, labelled_at=date.today().isoformat()
    )
    for problem in result.problems:
        print(f"problem: {problem}", file=sys.stderr)
    try:
        appended = calibrate.append_records(settings.calibration_path, result.records)
    except (OSError, ValueError) as exc:
        _err(f"cannot update {settings.calibration_path}: {exc}")
        return 2
    for record_id in appended.conflicts:
        print(
            f"conflict: {record_id} already has a different human label in "
            f"{settings.calibration_path}; not written (fix by hand)",
            file=sys.stderr,
        )
    print(
        f"{sheet} (results {path.name}, run {source_run}) -> {settings.calibration_path}: "
        f"{len(appended.added)} added, {len(appended.duplicates)} duplicate(s), "
        f"{len(appended.conflicts)} conflict(s), {result.unlabelled} unlabelled, "
        f"{len(result.problems)} problem(s)"
    )
    return 1 if result.problems or appended.conflicts else 0


def calibrate_cli(settings: Settings, records_file: Path, *, yes: bool, cap_cad: float) -> int:
    """``--calibrate``: replay the judge on the labelled facts and report agreement with
    the human labels. Writes json + md into runs/.

    Dry-run-first like the main runner (decisions #21, #23): cached judge verdicts are
    free and always replayed, but if any judge call is a cache miss nothing is sent to
    Anthropic without ``--yes``; with ``--yes`` the run is refused if the upper bound of
    the missing calls passes ``cap_cad`` and stops before any call that would."""
    from support_desk_assistant.evals import calibrate

    try:
        records = calibrate.load_records(records_file)
    except (OSError, ValueError) as exc:
        _err(f"cannot read {records_file}: {exc}")
        return 2
    if not records:
        _err(
            f"no calibration records in {records_file}. Label a sheet from --export and "
            "record it with --import-labels first."
        )
        return 2
    try:
        judge_system = load_judge_prompt(settings)
    except SystemPromptNotReady as exc:
        _err(str(exc))
        return 2
    plan = calibrate.plan_calibration(records, settings=settings, system=judge_system)
    print(
        f"Calibration plan: {len(records)} record(s), {plan.groups} judge call group(s): "
        f"{plan.cached} cached, {plan.misses} need an API call"
        + (f" (<= CA${plan.upper_bound_cad:.4f} vs cap CA${cap_cad:.2f})" if plan.misses else "")
    )
    if plan.misses and not yes:
        print(
            f"dry run: {plan.misses} judge call(s) would be made; nothing sent to Anthropic. "
            "Add --yes to run (cached verdicts alone are free and need no --yes)."
        )
        return 0
    if plan.upper_bound_cad > cap_cad:
        print(
            f"Refused: the estimate CA${plan.upper_bound_cad:.4f} exceeds the cap "
            f"CA${cap_cad:.2f}. Raise --cap, or label fewer facts. Nothing sent to Anthropic."
        )
        return 2
    run_id = provenance.new_run_id()
    try:
        report = calibrate.run_calibration(
            records,
            settings=settings,
            system=judge_system,
            client=_LazyClient(settings),
            cap_cad=cap_cad,
        )
    except anthropic.APIError as exc:
        _err(
            f"Anthropic API error: {exc}\nCompleted calls are cached; a re-run resumes "
            "from them. No calibration file written."
        )
        return 1
    md = calibrate.render_calibration_markdown(report)
    print(md)
    out = {
        "run_id": run_id,
        "calibration_file": records_file.name,
        "calibration_sha256": provenance.file_sha256(records_file),
        "judge_model": settings.eval.judge_model,
        "judge_prompt_sha256": provenance.file_sha256(settings.eval.judge_prompt_path),
        "git": provenance.git_state(),
        "report": report,
    }
    directory = settings.runs_dir
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / f"judge_calibration_{run_id}.json"
    md_path = directory / f"judge_calibration_{run_id}.md"
    json_path.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    md_path.write_text(md, encoding="utf-8")
    print(
        f"Judge: {report['calls']} call(s), {report['cache_hits']} cache hit(s), "
        f"cost CA${report['cost_cad']:.4f}"
    )
    print(f"wrote {json_path} and {md_path}")
    print(f"{directory} is gitignored: keep a result worth keeping with `git add -f` on both.")
    if report["stopped_by_cap"]:
        print(
            f"Stopped by the spend cap (CA${cap_cad:.2f}): {report['n_unreplayed']} record(s) "
            "not replayed. Completed calls are cached; a re-run resumes."
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(
        prog="sda-eval-answers",
        description=__doc__.split("\n\n")[0],
        epilog="Default is a dry run: nothing is sent to Anthropic without --yes.",
    )
    parser.add_argument("--yes", action="store_true", help="run for real (calls Anthropic)")
    parser.add_argument(
        "--limit", type=_positive_int, metavar="N", help="at most N items per category (file order)"
    )
    parser.add_argument(
        "--category",
        action="append",
        choices=CATEGORIES,
        metavar="C",
        help="restrict to category C (repeatable): " + ", ".join(CATEGORIES),
    )
    parser.add_argument("--model", help=f"answer model (default {settings.answer.model})")
    parser.add_argument(
        "-k",
        type=_positive_int,
        metavar="K",
        help=f"chunks retrieved (default {settings.retrieval.top_k})",
    )
    parser.add_argument(
        "--dataset", type=Path, metavar="PATH", help=f"eval set (default {settings.eval_set_path})"
    )
    parser.add_argument(
        "--cap",
        type=float,
        metavar="CAD",
        help=f"spend cap in CAD (default {settings.eval.spend_cap_cad})",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="write a full run from a dirty working tree to results/ (default: runs/)",
    )
    parser.add_argument(
        "--export",
        nargs="?",
        const="",
        metavar="RESULTS",
        help="write the blind judge hand-review sheet from a results file (.summary.json or "
        ".jsonl; offline; default: newest)",
    )
    parser.add_argument(
        "--reveal",
        action="store_true",
        help="with --export: show the machine verdicts (the sheet is then not blind)",
    )
    parser.add_argument(
        "--import-labels",
        type=Path,
        metavar="SHEET",
        help=f"record a filled blind sheet's labels in {settings.calibration_path} (offline)",
    )
    parser.add_argument(
        "--from",
        dest="from_results",
        type=Path,
        metavar="RESULTS",
        help="with --import-labels: the results file the sheet came from (default: newest)",
    )
    parser.add_argument(
        "--calibrate",
        nargs="?",
        const="",
        metavar="FILE",
        help="judge vs human agreement on the labelled facts (default "
        f"{settings.calibration_path}); cached judge verdicts are free, any uncached call "
        "needs --yes and respects --cap",
    )
    args = parser.parse_args(argv)

    run_options = {
        "--yes": args.yes,
        "--limit": args.limit,
        "--category": args.category,
        "--model": args.model,
        "-k": args.k,
        "--dataset": args.dataset,
        "--cap": args.cap,
        "--allow-dirty": args.allow_dirty,
    }
    # Run options each offline/side mode may still take: --calibrate's judge calls need
    # --yes and --cap; --export and --import-labels take none.
    mode_options = {"--calibrate": {"--yes", "--cap"}}
    modes = [
        flag
        for flag, value in (
            ("--export", args.export),
            ("--import-labels", args.import_labels),
            ("--calibrate", args.calibrate),
        )
        if value is not None
    ]
    if len(modes) > 1:
        parser.error(f"{' and '.join(modes)} are mutually exclusive")
    if modes:
        allowed = mode_options.get(modes[0], set())
        given = [f for f, v in run_options.items() if v not in (None, False) and f not in allowed]
        if given:
            parser.error(f"{modes[0]} does not take {', '.join(given)}")
        if args.cap is not None and args.cap <= 0:
            parser.error("--cap must be positive")
    if args.reveal and args.export is None:
        parser.error("--reveal is valid only with --export")
    if args.from_results is not None and args.import_labels is None:
        parser.error("--from is valid only with --import-labels")
    if args.export is not None:
        return export_review(
            settings, Path(args.export) if args.export else None, reveal=args.reveal
        )
    if args.import_labels is not None:
        return import_labels_cli(settings, args.import_labels, args.from_results)
    if args.calibrate is not None:
        return calibrate_cli(
            settings,
            Path(args.calibrate) if args.calibrate else settings.calibration_path,
            yes=args.yes,
            cap_cad=settings.eval.spend_cap_cad if args.cap is None else args.cap,
        )

    model = args.model or settings.answer.model
    for name in (model, settings.eval.judge_model):
        if name not in settings.answer.models:
            parser.error(f"no profile for {name!r} in settings.answer.models")
    if args.cap is not None and args.cap <= 0:
        parser.error("--cap must be positive")
    return run_eval(
        settings,
        dataset=args.dataset or settings.eval_set_path,
        categories=args.category,
        limit=args.limit,
        model=model,
        k=args.k or settings.retrieval.top_k,
        cap_cad=settings.eval.spend_cap_cad if args.cap is None else args.cap,
        yes=args.yes,
        allow_dirty=args.allow_dirty,
    )


if __name__ == "__main__":
    raise SystemExit(main())
