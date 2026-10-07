"""Offline stand-ins: a word-count tokenizer and a fake Voyage client."""

import re
import zlib
from types import SimpleNamespace

DIM = 32


def word_count(text: str) -> int:
    return len(text.split())


def bag_of_words(text: str, dim: int = DIM) -> list[float]:
    vector = [0.0] * dim
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        vector[zlib.crc32(word.encode()) % dim] += 1.0
    vector[0] += 0.01  # never all-zero (cosine)
    return vector


class FakeVoyage:
    """Records calls; optionally raises the queued exceptions first."""

    def __init__(self, errors=(), dim: int = DIM):
        self.calls: list[dict] = []
        self.errors = list(errors)
        self.dim = dim

    def embed(self, texts, model=None, input_type=None, truncation=True, **kwargs):
        if self.errors:
            raise self.errors.pop(0)
        self.calls.append(
            {"texts": list(texts), "model": model, "input_type": input_type, **kwargs}
        )
        return SimpleNamespace(
            embeddings=[bag_of_words(t, self.dim) for t in texts],
            total_tokens=sum(word_count(t) for t in texts),
        )
