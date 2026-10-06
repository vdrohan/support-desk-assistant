"""Single source of truth for runtime configuration.

All tunable values (crawl limits, chunk size, top_k, model names, collection
names) live here. Other modules import from here; they never hardcode these
values or read the environment directly.

Any field can be overridden with an environment variable prefixed ``SDA_``;
nested fields use ``__``, e.g. ``SDA_CRAWL__MAX_DEPTH=1``.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel
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


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SDA_", env_nested_delimiter="__")

    data_dir: Path = Path("data")
    crawl: CrawlSettings = CrawlSettings()

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def processed_dir(self) -> Path:
        return self.data_dir / "processed"

    @property
    def manifest_path(self) -> Path:
        return self.data_dir / "manifest.jsonl"


@lru_cache
def get_settings() -> Settings:
    return Settings()
