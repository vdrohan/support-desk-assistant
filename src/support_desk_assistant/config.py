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


class IndexSettings(BaseModel):
    """Qdrant collection (docs/decisions.md #11)."""

    collection: str = "canada_docs_v1"
    upsert_batch_size: int = 128
    max_delete_fraction: float = 0.2
    timeout_s: float = 30.0


class RetrievalSettings(BaseModel):
    top_k: int = 5


class EvalSettings(BaseModel):
    """Retrieval eval (docs/decisions.md #12). Datasets are versioned files under
    ``data/eval/``; never edit one that has published results, add ``_v2``."""

    retrieval_dataset: str = "retrieval_v1.jsonl"
    k: int = 5


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SDA_", env_nested_delimiter="__", env_file=".env", extra="ignore"
    )

    data_dir: Path = Path("data")
    voyage_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("VOYAGE_API_KEY", "voyage_api_key")
    )
    qdrant_url: str = Field(
        default="http://localhost:6333", validation_alias=AliasChoices("QDRANT_URL", "qdrant_url")
    )
    crawl: CrawlSettings = CrawlSettings()
    chunk: ChunkSettings = ChunkSettings()
    embed: EmbedSettings = EmbedSettings()
    index: IndexSettings = IndexSettings()
    retrieval: RetrievalSettings = RetrievalSettings()
    eval: EvalSettings = EvalSettings()

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
    def embedding_cache_path(self) -> Path:
        return self.data_dir / "cache" / "embeddings.sqlite"


@lru_cache
def get_settings() -> Settings:
    return Settings()
