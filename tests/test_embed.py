import logging

import pytest
import voyageai.error

from fakes import FakeVoyage
from support_desk_assistant.config import EmbedSettings, Settings
from support_desk_assistant.ingest.embed import (
    Embedder,
    EmbeddingCache,
    cache_key,
    make_voyage_client,
)

CFG = EmbedSettings(output_dimension=32, batch_size=2)
TEXTS = ["alpha one", "beta two", "gamma three", "delta four", "epsilon five"]


def embedder(tmp_path, client=None, cfg=CFG, sleeps=None):
    cache = EmbeddingCache(tmp_path / "cache" / "embeddings.sqlite")
    return Embedder(
        client or FakeVoyage(), cfg, cache, sleep=(sleeps if sleeps is not None else []).append
    )


def test_batches_and_document_input_type(tmp_path):
    client = FakeVoyage()
    vectors = embedder(tmp_path, client).embed_documents(TEXTS)
    assert len(vectors) == 5 and len(vectors[0]) == 32
    assert [len(c["texts"]) for c in client.calls] == [2, 2, 1]
    assert {c["input_type"] for c in client.calls} == {"document"}
    assert {c["model"] for c in client.calls} == {"voyage-4"}
    assert {c["output_dimension"] for c in client.calls} == {32}


def test_cache_hits_skip_api_and_bill_nothing(tmp_path):
    first = embedder(tmp_path)
    vectors = first.embed_documents(TEXTS)
    assert first.stats.tokens_billed == 10

    client = FakeVoyage()
    second = embedder(tmp_path, client)
    again = second.embed_documents(TEXTS)
    assert client.calls == []
    assert second.stats.cache_hits == 5 and second.stats.tokens_billed == 0
    assert again == [pytest.approx(v) for v in vectors]


def test_only_changed_text_is_embedded(tmp_path):
    embedder(tmp_path).embed_documents(TEXTS)
    client = FakeVoyage()
    e = embedder(tmp_path, client)
    e.embed_documents([*TEXTS[:4], "epsilon CHANGED"])
    assert [c["texts"] for c in client.calls] == [["epsilon CHANGED"]]
    assert e.stats.cache_hits == 4


def test_duplicate_texts_embedded_once(tmp_path):
    client = FakeVoyage()
    out = embedder(tmp_path, client).embed_documents(["same", "same"])
    assert sum(len(c["texts"]) for c in client.calls) == 1
    assert out[0] == out[1]


def test_cache_key_covers_model_dimension_and_input_type():
    base = cache_key("t", CFG, "document")
    assert base != cache_key("t", CFG, "query")
    assert base != cache_key("t", CFG.model_copy(update={"model": "voyage-4-lite"}), "document")
    assert base != cache_key("t", CFG.model_copy(update={"output_dimension": 1024}), "document")


@pytest.mark.parametrize(
    "error",
    [
        voyageai.error.RateLimitError("429"),
        voyageai.error.ServerError("500"),
        voyageai.error.ServiceUnavailableError("503"),
        voyageai.error.APIConnectionError("reset"),
    ],
)
def test_retries_with_backoff(tmp_path, error):
    sleeps: list[float] = []
    client = FakeVoyage(errors=[error, error])
    e = embedder(tmp_path, client, sleeps=sleeps)
    assert len(e.embed_documents(["x"])) == 1
    assert len(sleeps) == 2 and sleeps[1] > sleeps[0]


def test_non_retryable_error_raises_immediately(tmp_path):
    sleeps: list[float] = []
    client = FakeVoyage(errors=[voyageai.error.AuthenticationError("bad key")])
    with pytest.raises(voyageai.error.AuthenticationError):
        embedder(tmp_path, client, sleeps=sleeps).embed_documents(["x"])
    assert sleeps == []


def test_gives_up_after_max_retries(tmp_path):
    cfg = CFG.model_copy(update={"max_retries": 2})
    client = FakeVoyage(errors=[voyageai.error.RateLimitError("429")] * 5)
    with pytest.raises(voyageai.error.RateLimitError):
        embedder(tmp_path, client, cfg=cfg).embed_documents(["x"])


def test_query_uses_query_input_type_and_cache(tmp_path):
    client = FakeVoyage()
    e = embedder(tmp_path, client)
    first = e.embed_query("when is my payment")
    assert e.embed_query("when is my payment") == pytest.approx(first)
    assert [c["input_type"] for c in client.calls] == ["query"]  # second call from cache
    e.embed_documents(["when is my payment"])  # same text as a document: separate key
    assert [c["input_type"] for c in client.calls] == ["query", "document"]


def test_query_without_cache_calls_api_each_time():
    client = FakeVoyage()
    e = Embedder(client, CFG)
    e.embed_query("q")
    e.embed_query("q")
    assert len(client.calls) == 2


def test_api_key_never_logged_or_repr(tmp_path, caplog):
    secret = "pa-test-SECRET-value"
    settings = Settings(_env_file=None, voyage_api_key=secret)
    assert secret not in repr(settings) and secret not in str(settings.voyage_api_key)
    client = make_voyage_client(settings)
    assert client.api_key == secret

    caplog.set_level(logging.DEBUG)
    flaky = FakeVoyage(errors=[voyageai.error.RateLimitError("429")])
    Embedder(flaky, CFG, sleep=lambda s: None).embed_documents(["x"])
    assert secret not in caplog.text


def test_missing_api_key_fails_clearly(monkeypatch):
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    with pytest.raises(SystemExit, match="VOYAGE_API_KEY is not set"):
        make_voyage_client(Settings(_env_file=None))
