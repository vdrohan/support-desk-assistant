"""Idempotent sync of chunks into a versioned Qdrant collection.

Point IDs are the deterministic chunk IDs. A sync is scoped to the programs
being processed: it compares stored ``content_hash`` values with the current
chunks, embeds and upserts only new or changed chunks, and deletes points whose
chunks no longer exist. Other programs' points are never touched. A sync that
would delete more than ``max_delete_fraction`` of the points in scope aborts
unless explicitly allowed. See docs/decisions.md #11.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from qdrant_client import QdrantClient, models

from support_desk_assistant.config import IndexSettings
from support_desk_assistant.ingest.chunk import Chunk
from support_desk_assistant.ingest.embed import Embedder

logger = logging.getLogger(__name__)


class MassDeleteError(RuntimeError):
    pass


class CollectionMismatchError(RuntimeError):
    pass


def program_filter(programs: Sequence[str]) -> models.Filter:
    return models.Filter(
        must=[models.FieldCondition(key="program", match=models.MatchAny(any=list(programs)))]
    )


def ensure_collection(client: QdrantClient, name: str, dimension: int) -> bool:
    """Create the collection (cosine) with a keyword index on ``program``.
    Returns True if it was created. Raises if it exists with another shape."""
    if client.collection_exists(name):
        params = client.get_collection(name).config.params.vectors
        if params.size != dimension or params.distance != models.Distance.COSINE:
            raise CollectionMismatchError(
                f"collection {name!r} has size={params.size}, distance={params.distance}; "
                f"vectors are size={dimension}, cosine. Bump IndexSettings.collection."
            )
        return False
    client.create_collection(
        name, vectors_config=models.VectorParams(size=dimension, distance=models.Distance.COSINE)
    )
    client.create_payload_index(name, "program", field_schema=models.PayloadSchemaType.KEYWORD)
    return True


def existing_hashes(client: QdrantClient, name: str, programs: Sequence[str]) -> dict[str, str]:
    """Point ID -> stored content_hash, for points in the given programs."""
    if not client.collection_exists(name):
        return {}
    found: dict[str, str] = {}
    offset = None
    while True:
        points, offset = client.scroll(
            name,
            scroll_filter=program_filter(programs),
            with_payload=["content_hash"],
            with_vectors=False,
            limit=256,
            offset=offset,
        )
        found.update({str(p.id): p.payload.get("content_hash", "") for p in points})
        if offset is None:
            return found


def payload(chunk: Chunk) -> dict:
    return {
        "text": chunk.text,
        "title": chunk.title,
        "url": chunk.url,
        "program": chunk.program,
        "date_modified": chunk.date_modified,
        "heading_path": chunk.heading_path,
        "chunk_index": chunk.chunk_index,
        "content_hash": chunk.content_hash,
    }


@dataclass
class SyncReport:
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    created_collection: bool = False
    point_count: int = 0
    chunk_count: int = 0

    def lines(self) -> list[str]:
        status = "OK" if self.point_count == self.chunk_count else "MISMATCH"
        return [
            f"index: {self.added} added, {self.updated} updated, {self.unchanged} unchanged, "
            f"{self.deleted} deleted"
            + (" (collection created)" if self.created_collection else ""),
            f"points in scope: {self.point_count} vs chunks: {self.chunk_count} -> {status}",
        ]


def sync(
    client: QdrantClient,
    chunks: Sequence[Chunk],
    embedder: Embedder,
    cfg: IndexSettings,
    programs: Sequence[str],
    allow_mass_delete: bool = False,
) -> SyncReport:
    stray = {c.program for c in chunks} - set(programs)
    if stray:
        raise ValueError(f"chunks for programs outside the sync scope: {sorted(stray)}")

    report = SyncReport(chunk_count=len(chunks))
    existing = existing_hashes(client, cfg.collection, programs)
    current = {c.id for c in chunks}
    to_write = [c for c in chunks if existing.get(c.id) != c.content_hash]
    to_delete = sorted(set(existing) - current)
    report.added = sum(1 for c in to_write if c.id not in existing)
    report.updated = len(to_write) - report.added
    report.unchanged = len(chunks) - len(to_write)

    if (
        existing
        and len(to_delete) / len(existing) > cfg.max_delete_fraction
        and not allow_mass_delete
    ):
        raise MassDeleteError(
            f"sync would delete {len(to_delete)} of {len(existing)} points for "
            f"{', '.join(programs)} (> {cfg.max_delete_fraction:.0%}). "
            "Check the corpus, or pass --allow-mass-delete."
        )

    if to_write:
        vectors = embedder.embed_documents([c.embed_text for c in to_write])
        report.created_collection = ensure_collection(client, cfg.collection, len(vectors[0]))
        for start in range(0, len(to_write), cfg.upsert_batch_size):
            batch = range(start, min(start + cfg.upsert_batch_size, len(to_write)))
            client.upsert(
                cfg.collection,
                points=[
                    models.PointStruct(
                        id=to_write[i].id, vector=vectors[i], payload=payload(to_write[i])
                    )
                    for i in batch
                ],
                wait=True,
            )
    if to_delete:
        client.delete(
            cfg.collection, points_selector=models.PointIdsList(points=to_delete), wait=True
        )
    report.deleted = len(to_delete)

    if client.collection_exists(cfg.collection):
        report.point_count = client.count(
            cfg.collection, count_filter=program_filter(programs), exact=True
        ).count
    return report
