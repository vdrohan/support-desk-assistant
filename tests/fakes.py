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


# --- Anthropic ---------------------------------------------------------------------


def make_message(
    text: str = "",
    *,
    stop_reason: str = "end_turn",
    input_tokens: int = 1000,
    output_tokens: int = 100,
    cache_creation: int = 0,
    cache_read: int = 0,
):
    """A real ``anthropic.types.Message`` (what the SDK returns), built offline."""
    from anthropic.types import Message, TextBlock, Usage

    return Message(
        id="msg_fake",
        type="message",
        role="assistant",
        model="claude-haiku-5-5",
        content=[TextBlock(type="text", text=text)] if text else [],
        stop_reason=stop_reason,
        usage=Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=cache_creation,
            cache_read_input_tokens=cache_read,
        ),
    )


class FakeAnthropic:
    """Stands in for ``anthropic.Anthropic``: returns queued messages (or raises queued
    exceptions) from ``messages.create`` and records the kwargs of every call."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response
