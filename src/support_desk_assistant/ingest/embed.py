"""Voyage embeddings with batching, retries and a local SQLite cache.

The cache key covers model, dimension, dtype, input_type and the exact text, so
unchanged chunks are never re-embedded and a model or dimension change can't
serve stale vectors. See docs/decisions.md #10.
"""

import hashlib
import logging
import random
import sqlite3
import time
from array import array
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import voyageai
import voyageai.error

from support_desk_assistant.config import EmbedSettings, Settings

logger = logging.getLogger(__name__)

RETRYABLE = (
    voyageai.error.RateLimitError,
    voyageai.error.ServerError,
    voyageai.error.ServiceUnavailableError,
    voyageai.error.APIConnectionError,
    voyageai.error.Timeout,
    voyageai.error.TryAgain,
)


class EmbeddingClient(Protocol):
    """The subset of ``voyageai.Client`` we use (tests pass a fake)."""

    def embed(
        self,
        texts: list[str],
        model: str | None = None,
        input_type: str | None = None,
        truncation: bool = True,
        output_dtype: str | None = None,
        output_dimension: int | None = None,
    ): ...


def make_voyage_client(settings: Settings) -> voyageai.Client:
    if settings.voyage_api_key is None:
        raise SystemExit("VOYAGE_API_KEY is not set (see .env.example)")
    return voyageai.Client(
        api_key=settings.voyage_api_key.get_secret_value(),
        max_retries=0,  # retries are handled here, with backoff
        timeout=settings.embed.timeout_s,
    )


class EmbeddingCache:
    """key -> float32 vector, in one SQLite file."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS embeddings (key TEXT PRIMARY KEY, vector BLOB NOT NULL)"
        )

    def get_many(self, keys: Sequence[str]) -> dict[str, list[float]]:
        found: dict[str, list[float]] = {}
        for start in range(0, len(keys), 500):
            batch = list(keys[start : start + 500])
            marks = ",".join("?" * len(batch))
            rows = self.conn.execute(
                f"SELECT key, vector FROM embeddings WHERE key IN ({marks})", batch
            )
            for key, blob in rows:
                found[key] = array("f", blob).tolist()
        return found

    def put_many(self, items: dict[str, list[float]]) -> None:
        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO embeddings (key, vector) VALUES (?, ?)",
                [(k, array("f", v).tobytes()) for k, v in items.items()],
            )

    def close(self) -> None:
        self.conn.close()


def cache_key(text: str, cfg: EmbedSettings, input_type: str) -> str:
    raw = f"{cfg.model}|{cfg.output_dimension}|{cfg.output_dtype}|{input_type}|{text}"
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass
class EmbedStats:
    texts: int = 0
    cache_hits: int = 0
    api_calls: int = 0
    tokens_billed: int = 0

    def line(self) -> str:
        return (
            f"embeddings: {self.texts} texts, {self.cache_hits} cache hits, "
            f"{self.texts - self.cache_hits} embedded in {self.api_calls} API calls, "
            f"{self.tokens_billed} tokens billed"
        )


class Embedder:
    def __init__(
        self,
        client: EmbeddingClient,
        cfg: EmbedSettings,
        cache: EmbeddingCache | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client
        self.cfg = cfg
        self.cache = cache
        self.sleep = sleep
        self.stats = EmbedStats()

    def _call(self, texts: list[str], input_type: str) -> list[list[float]]:
        for attempt in range(self.cfg.max_retries + 1):
            try:
                result = self.client.embed(
                    texts,
                    model=self.cfg.model,
                    input_type=input_type,
                    output_dtype=self.cfg.output_dtype,
                    output_dimension=self.cfg.output_dimension,
                )
            except RETRYABLE as exc:
                if attempt == self.cfg.max_retries:
                    raise
                delay = min(
                    self.cfg.backoff_base_s * 2**attempt + random.uniform(0, 0.5),
                    self.cfg.max_backoff_s,
                )
                logger.warning(
                    "voyage %s; retry %d in %.1fs", type(exc).__name__, attempt + 1, delay
                )
                self.sleep(delay)
                continue
            self.stats.api_calls += 1
            self.stats.tokens_billed += result.total_tokens
            return [list(v) for v in result.embeddings]
        raise AssertionError("unreachable")

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed with input_type=document, serving unchanged texts from the cache."""
        input_type = self.cfg.document_input_type
        keys = [cache_key(t, self.cfg, input_type) for t in texts]
        vectors = self.cache.get_many(keys) if self.cache else {}
        self.stats.texts += len(texts)
        self.stats.cache_hits += sum(1 for k in keys if k in vectors)

        missing = list(dict.fromkeys(k for k in keys if k not in vectors))
        text_for = dict(zip(keys, texts, strict=True))
        for start in range(0, len(missing), self.cfg.batch_size):
            batch = missing[start : start + self.cfg.batch_size]
            new = dict(
                zip(batch, self._call([text_for[k] for k in batch], input_type), strict=True)
            )
            if self.cache:
                self.cache.put_many(new)  # per batch, so an interrupted run keeps progress
            vectors.update(new)
        return [vectors[k] for k in keys]

    def embed_query(self, text: str) -> list[float]:
        """Embed a search query with input_type=query (not cached)."""
        return self._call([text], self.cfg.query_input_type)[0]
