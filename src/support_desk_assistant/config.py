"""Single source of truth for runtime configuration.

All tunable values (crawl limits, chunk size, top_k, model names, collection
names) live here. Other modules import from here; they never hardcode these
values or read the environment directly.

Any field can be overridden with an environment variable prefixed ``SDA_``;
nested fields use ``__``, e.g. ``SDA_CRAWL__MAX_DEPTH=1``. ``.env`` is loaded too.
``VOYAGE_API_KEY`` and ``QDRANT_URL`` keep their conventional, unprefixed names.

Changing the embedding model, dimension or chunking changes the vectors: bump
``IndexSettings.collection`` (docs/decisions.md #11).
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class CrawlSettings(BaseModel):
    """Corpus crawl of English canada.ca pages (see docs/decisions.md #7)."""

    seeds: dict[str, str] = {
        "ei": "https://www.canada.ca/en/services/benefits/ei.html",
        "cpp": "https://www.canada.ca/en/services/benefits/publicpensions/cpp.html",
        "oas": "https://www.canada.ca/en/services/benefits/publicpensions/old-age-security.html",
        "sin": "https://www.canada.ca/en/employment-social-development/services/sin.html",
        "passport": (
            "https://www.canada.ca/en/immigration-refugees-citizenship/services/"
            "canadian-passports.html"
        ),
    }
    max_pages_per_program: int = 30
    max_depth: int = 2
    request_interval_s: float = 1.0
    max_retries: int = 3
    backoff_base_s: float = 2.0
    max_backoff_s: float = 60.0
    timeout_s: float = 30.0
    user_agent: str = (
        "support-desk-assistant/0.1 (personal non-commercial research project; "
        "+https://github.com/vdrohan/support-desk-assistant)"
    )


class ChunkSettings(BaseModel):
    """Heading-aware chunking (docs/decisions.md #9). Sizes in voyage tokenizer tokens,
    measured on the embedded text (heading-path prefix included)."""

    target_tokens: int = 350
    max_tokens: int = 600
    min_tokens: int = 80
    # Exact repeats of blocks at least this long are dropped within a page (#9).
    dedup_min_tokens: int = 8


class EmbedSettings(BaseModel):
    """Voyage embeddings (docs/decisions.md #10)."""

    model: str = "voyage-4"
    output_dimension: int = 1024
    output_dtype: str = "float"
    document_input_type: str = "document"
    query_input_type: str = "query"
    # 128 texts x 600 tokens max = 77k tokens, well under voyage-4's 320k/request.
    batch_size: int = 128
    max_retries: int = 5
    backoff_base_s: float = 2.0
    max_backoff_s: float = 60.0
    timeout_s: float = 60.0
    # UNVERIFIED placeholder: no Voyage price is recorded in the repo; check against
    # Voyage's price list before trusting cost estimates (same spirit as ``usd_cad``).
    usd_per_mtok: float = 0.06


class IndexSettings(BaseModel):
    """Qdrant collection (docs/decisions.md #11)."""

    collection: str = "canada_docs_v1"
    upsert_batch_size: int = 128
    max_delete_fraction: float = 0.2
    timeout_s: float = 30.0


class RetrievalSettings(BaseModel):
    top_k: int = 5


class ApiSettings(BaseModel):
    """HTTP API (docs/decisions.md #27). Binds to loopback by default; there is no auth."""

    host: str = "127.0.0.1"
    port: int = 8000
    max_question_chars: int = 1000
    max_k: int = 20


class EvalSettings(BaseModel):
    """Retrieval eval (docs/decisions.md #12). Datasets are versioned files under
    ``data/eval/``; never edit one that has published results, add ``_v2``."""

    retrieval_dataset: str = "retrieval_v1.jsonl"
    k: int = 5

    # Answer-level eval set (docs/decisions.md #19). ``candidates`` is drafted by Haiku,
    # reviewed by hand, and accepted items land in the versioned ``eval_set``.
    candidates_file: str = "candidates.jsonl"
    eval_set_file: str = "eval_set_v1.jsonl"
    review_state_file: str = "review_state.json"
    review_html_file: str = "review.html"
    # Must be a key of ``AnswerSettings.models`` (prices and request profile come from there).
    draft_model: str = "claude-haiku-5-5"
    draft_max_tokens: int = 1500
    draft_prompt_path: Path = Path("prompts/eval_draft.md")
    # Candidates per category: 40 in all, so the human cut still leaves a set worth
    # having. Keys are the category names in ``evals/draft_questions.py``.
    draft_mix: dict[str, int] = {
        "single_page": 18,
        "multi_page": 7,
        "confusable": 6,
        "unanswerable_out_of_scope": 3,
        "unanswerable_in_domain": 4,
        "time_sensitive": 2,
    }
    draft_seed: int = 20261012
    # Pages outside this length are never sampled: too thin to ask about, or too long to
    # send whole (child-passport.html alone is ~40k words).
    draft_min_page_words: int = 150
    draft_max_page_words: int = 3000
    # Word-set Jaccard at or above this flags two questions as near-duplicates.
    duplicate_jaccard: float = 0.7
    # Lines of context (blank-line blocks) the review helper shows around a key fact.
    review_context_blocks: int = 1
    review_excerpt_chars: int = 1800

    # Answer scoring runner (docs/decisions.md #20, #21).
    # Must be a key of ``AnswerSettings.models`` (prices and request profile come from there).
    judge_model: str = "claude-haiku-5-5"
    judge_max_tokens: int = 400
    judge_prompt_path: Path = Path("prompts/eval_judge.md")
    # Bump when the judge prompt or schema changes (part of the judge cache key).
    judge_schema_version: int = 2  # 2: reason before present (decisions.md #25)
    # Facts this short (in words) are checked by string matching, not the judge.
    atomic_fact_max_words: int = 6
    # Content words shorter than this are ignored when matching a fact.
    fact_content_min_chars: int = 4
    # A run stops before its estimated spend passes this.
    spend_cap_cad: float = 0.25
    # Hand-check export of judge verdicts (gitignored working file).
    judge_review_file: str = "judge_review.md"
    judge_review_n: int = 40
    judge_review_gate_n: int = 10
    judge_review_seed: int = 20261013
    # Human vs judge verdicts on hand-reviewed facts (one JSON object per line).
    calibration_file: str = "judge_calibration.jsonl"
    # Fewer calibrated facts than this: agreement numbers are reported as provisional.
    calibration_min_n: int = 30
    # z for the Wilson confidence intervals in reports (1.96 = 95%).
    ci_z: float = 1.96


class ModelProfile(BaseModel):
    """Per-model request settings and prices (docs/decisions.md #16).

    ``thinking="disabled"`` is rejected by Sonnet 5.5, so its profile uses ``adaptive``.
    Haiku 5.5 and Sonnet 5.5 reject non-default ``temperature``/``top_p``/``top_k``,
    so none are sent. Thinking tokens are billed as (and counted in) output tokens.
    """

    max_tokens: int
    thinking: Literal["disabled", "adaptive"]
    effort: Literal["low", "medium", "high", "xhigh", "max"]
    input_usd_per_mtok: float
    output_usd_per_mtok: float


class AnswerSettings(BaseModel):
    """Answer generation (docs/decisions.md #15-#18)."""

    model: str = "claude-haiku-5-5"
    # One-time comparison run only; not like-for-like with ``model`` (it thinks).
    comparison_model: str = "claude-sonnet-5-5"
    models: dict[str, ModelProfile] = {
        "claude-haiku-5-5": ModelProfile(
            max_tokens=1000,  # 500 truncated 8 of 58 baseline answers (decisions.md #24)
            thinking="disabled",
            effort="low",
            input_usd_per_mtok=0.10,  # prompts <= 100K tokens; $0.50/$2.50 beyond
            output_usd_per_mtok=0.50,
        ),
        "claude-sonnet-5-5": ModelProfile(
            max_tokens=4000,  # headroom so thinking can't truncate the JSON
            thinking="adaptive",
            effort="low",
            input_usd_per_mtok=2.00,
            output_usd_per_mtok=10.00,
        ),
    }
    # Multipliers on the input price (Anthropic prompt caching).
    cache_read_multiplier: float = 0.1
    cache_write_multiplier: float = 1.25
    prices_as_of: str = "2026-10-06"
    usd_cad: float = 1.42  # USD->CAD, early Oct 2026 (supplied by the project owner)
    # Gate: the top retrieval score below this abstains without calling Claude.
    # Placeholder; tune with the eval (score_analysis in the retrieval eval).
    min_score: float = 0.30
    # Anthropic prompt caching (cache_control on the system prompt). Off by default:
    # sporadic CLI use pays the write premium with no reads. Turn on for eval runs.
    prompt_caching: bool = False
    system_prompt_path: Path = Path("prompts/answer_system.md")
    # Bump when AnswerResult or the prompt layout changes (part of the response-cache key).
    schema_version: int = 2  # 2: abstained first (decisions.md #26)
    max_retries: int = 2
    timeout_s: float = 60.0
    smoke_questions: list[str] = [
        "Can I get EI if I quit my job?",
        "How big is the cut if I take CPP at 60?",
        "How do I apply for Ontario Works?",
    ]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SDA_", env_nested_delimiter="__", env_file=".env", extra="ignore"
    )

    data_dir: Path = Path("data")
    voyage_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("VOYAGE_API_KEY", "voyage_api_key")
    )
    anthropic_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("ANTHROPIC_API_KEY", "anthropic_api_key")
    )
    logs_dir: Path = Path("logs")
    qdrant_url: str = Field(
        default="http://localhost:6333", validation_alias=AliasChoices("QDRANT_URL", "qdrant_url")
    )
    crawl: CrawlSettings = CrawlSettings()
    chunk: ChunkSettings = ChunkSettings()
    embed: EmbedSettings = EmbedSettings()
    index: IndexSettings = IndexSettings()
    retrieval: RetrievalSettings = RetrievalSettings()
    eval: EvalSettings = EvalSettings()
    answer: AnswerSettings = AnswerSettings()
    api: ApiSettings = ApiSettings()

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def processed_dir(self) -> Path:
        return self.data_dir / "processed"

    @property
    def manifest_path(self) -> Path:
        return self.data_dir / "manifest.jsonl"

    @property
    def chunks_path(self) -> Path:
        return self.data_dir / "chunks.jsonl"

    @property
    def eval_dir(self) -> Path:
        return self.data_dir / "eval"

    @property
    def eval_results_dir(self) -> Path:
        return self.eval_dir / "results"

    @property
    def candidates_path(self) -> Path:
        return self.eval_dir / self.eval.candidates_file

    @property
    def eval_set_path(self) -> Path:
        return self.eval_dir / self.eval.eval_set_file

    @property
    def review_state_path(self) -> Path:
        return self.eval_dir / self.eval.review_state_file

    @property
    def review_html_path(self) -> Path:
        return self.eval_dir / self.eval.review_html_file

    @property
    def runs_dir(self) -> Path:
        return self.eval_dir / "runs"

    @property
    def judge_review_path(self) -> Path:
        return self.eval_dir / self.eval.judge_review_file

    @property
    def calibration_path(self) -> Path:
        return self.eval_dir / self.eval.calibration_file

    @property
    def eval_cache_dir(self) -> Path:
        return self.data_dir / "cache" / "evals"

    @property
    def request_log_path(self) -> Path:
        return self.logs_dir / "requests.jsonl"

    @property
    def answer_cache_dir(self) -> Path:
        return self.data_dir / "cache" / "answers"

    @property
    def embedding_cache_path(self) -> Path:
        return self.data_dir / "cache" / "embeddings.sqlite"


@lru_cache
def get_settings() -> Settings:
    return Settings()
