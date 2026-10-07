import pytest
from qdrant_client import QdrantClient

from fakes import DIM, FakeVoyage, word_count
from support_desk_assistant.config import ChunkSettings, EmbedSettings, IndexSettings, Settings
from support_desk_assistant.ingest.chunk import chunk_page
from support_desk_assistant.ingest.embed import Embedder
from support_desk_assistant.ingest.index import (
    CollectionMismatchError,
    MassDeleteError,
    ensure_collection,
    program_filter,
    sync,
)
from support_desk_assistant.retrieve import search

CHUNK_CFG = ChunkSettings(target_tokens=40, max_tokens=80, min_tokens=5)
INDEX_CFG = IndexSettings(collection="test_v1")

TOPICS = {
    "ei": [
        "regular benefits job loss",
        "sickness benefits illness",
        "maternity parental leave",
        "apply online application",
        "reporting biweekly reports",
        "payment deposit timing",
    ],
    "sin": [
        "apply social insurance number",
        "documents birth certificate",
        "confidentiality protect",
        "temporary residents expiry",
        "update record name change",
        "lost card replacement",
    ],
}


def page(program: str, topics: list[str]) -> str:
    sections = "\n\n".join(f"## {t.title()}\n\n" + f"{t} " * 3 for t in topics)
    return f"# {program.upper()} guide\n\nOverview of {program}.\n\n{sections}"


def make_chunks(program: str, topics: list[str] | None = None, edit: tuple[str, str] = ("", "")):
    markdown = page(program, topics or TOPICS[program])
    if edit[0]:
        markdown = markdown.replace(*edit)
    return chunk_page(
        markdown,
        url=f"https://www.canada.ca/en/{program}.html",
        program=program,
        date_modified=None,
        cfg=CHUNK_CFG,
        count=word_count,
    )


@pytest.fixture
def qdrant():
    client = QdrantClient(":memory:")
    yield client
    client.close()


@pytest.fixture
def client():
    return FakeVoyage()


@pytest.fixture
def embedder(client):
    return Embedder(client, EmbedSettings(output_dimension=DIM))


def count(qdrant, programs):
    return qdrant.count("test_v1", count_filter=program_filter(programs), exact=True).count


def test_first_sync_creates_collection_and_payload(qdrant, embedder):
    chunks = make_chunks("ei")
    report = sync(qdrant, chunks, embedder, INDEX_CFG, ["ei"])
    assert report.created_collection
    assert report.added == len(chunks) and report.deleted == 0
    assert report.point_count == report.chunk_count == len(chunks)

    params = qdrant.get_collection("test_v1").config.params.vectors
    assert params.size == DIM and params.distance.value == "Cosine"
    point = qdrant.retrieve("test_v1", [chunks[1].id], with_payload=True)[0]
    assert set(point.payload) == {
        "text", "title", "url", "program", "date_modified",
        "heading_path", "chunk_index", "content_hash",
    }  # fmt: skip
    assert point.payload["heading_path"] == chunks[1].heading_path


def test_rerun_is_a_no_op(qdrant, embedder, client):
    chunks = make_chunks("ei")
    sync(qdrant, chunks, embedder, INDEX_CFG, ["ei"])
    calls = len(client.calls)
    report = sync(qdrant, chunks, embedder, INDEX_CFG, ["ei"])
    assert (report.added, report.updated, report.deleted) == (0, 0, 0)
    assert report.unchanged == len(chunks)
    assert len(client.calls) == calls  # nothing embedded


def test_changed_chunk_updates_exactly_one(qdrant, embedder, client):
    sync(qdrant, make_chunks("ei"), embedder, INDEX_CFG, ["ei"])
    body_edit = ("timing payment", "timing CHANGED payment")  # body only, heading unchanged
    report = sync(qdrant, make_chunks("ei", edit=body_edit), embedder, INDEX_CFG, ["ei"])
    assert (report.added, report.updated, report.deleted) == (0, 1, 0)
    assert len(client.calls[-1]["texts"]) == 1


def test_removed_chunk_is_deleted(qdrant, embedder):
    sync(qdrant, make_chunks("ei"), embedder, INDEX_CFG, ["ei"])
    report = sync(qdrant, make_chunks("ei", TOPICS["ei"][:-1]), embedder, INDEX_CFG, ["ei"])
    assert report.deleted == 1
    assert report.point_count == report.chunk_count


def test_sync_scoped_to_program_never_touches_others(qdrant, embedder):
    ei, sin = make_chunks("ei"), make_chunks("sin")
    sync(qdrant, ei + sin, embedder, INDEX_CFG, ["ei", "sin"])
    before_sin = count(qdrant, ["sin"])

    # --program ei with a shrunken EI corpus: only EI points change
    report = sync(qdrant, make_chunks("ei", TOPICS["ei"][:-1]), embedder, INDEX_CFG, ["ei"])
    assert report.deleted == 1
    assert report.point_count == count(qdrant, ["ei"]) == len(ei) - 1
    assert count(qdrant, ["sin"]) == before_sin


def test_sync_rejects_chunks_outside_scope(qdrant, embedder):
    with pytest.raises(ValueError, match="outside the sync scope"):
        sync(qdrant, make_chunks("sin"), embedder, INDEX_CFG, ["ei"])


def test_mass_delete_guard(qdrant, embedder):
    sync(qdrant, make_chunks("ei"), embedder, INDEX_CFG, ["ei"])
    before = count(qdrant, ["ei"])
    shrunk = make_chunks("ei", TOPICS["ei"][:2])  # would delete 4 of 6 points

    with pytest.raises(MassDeleteError, match="--allow-mass-delete"):
        sync(qdrant, shrunk, embedder, INDEX_CFG, ["ei"])
    assert count(qdrant, ["ei"]) == before  # aborted before any write

    report = sync(qdrant, shrunk, embedder, INDEX_CFG, ["ei"], allow_mass_delete=True)
    assert report.deleted == 4 and report.point_count == len(shrunk)


def test_dimension_mismatch_raises(qdrant):
    ensure_collection(qdrant, "test_v1", DIM)
    with pytest.raises(CollectionMismatchError, match="Bump"):
        ensure_collection(qdrant, "test_v1", 1024)


def test_search_with_and_without_program_filter(qdrant, embedder):
    sync(qdrant, make_chunks("ei") + make_chunks("sin"), embedder, INDEX_CFG, ["ei", "sin"])
    settings = Settings(_env_file=None, index=INDEX_CFG)

    hits = search("lost card replacement", settings=settings, embedder=embedder, qdrant=qdrant, k=3)
    assert hits[0].program == "sin" and hits[0].heading_path == ["Lost Card Replacement"]

    hits = search("lost card", settings=settings, embedder=embedder, qdrant=qdrant, program="ei")
    assert hits and {h.program for h in hits} == {"ei"}
