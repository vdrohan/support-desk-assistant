"""chunk -> embed -> index.

uv run python -m support_desk_assistant.ingest [--program ei] [--dry-run]
uv run sda-ingest ...
"""

import argparse
import logging

from qdrant_client import QdrantClient

from support_desk_assistant.config import get_settings
from support_desk_assistant.ingest.chunk import (
    ChunkReport,
    chunk_corpus,
    voyage_token_counter,
    write_chunks,
)
from support_desk_assistant.ingest.embed import Embedder, EmbeddingCache, make_voyage_client
from support_desk_assistant.ingest.index import MassDeleteError, sync


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(prog="sda-ingest", description="chunk -> embed -> index")
    parser.add_argument(
        "--program",
        action="append",
        choices=sorted(settings.crawl.seeds),
        help="repeatable; default all. Sync only touches these programs' points.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="chunk and report only; no API calls"
    )
    parser.add_argument(
        "--allow-mass-delete",
        action="store_true",
        help=f"allow deleting > {settings.index.max_delete_fraction:.0%} of points in scope",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    programs = args.program or sorted(settings.crawl.seeds)
    count = voyage_token_counter(settings.embed.model)
    dropped: dict[str, int] = {}
    chunks = chunk_corpus(settings, count, programs, dropped)
    write_chunks(settings.chunks_path, chunks)
    report = ChunkReport.build(chunks, settings.chunk, dropped)
    for line in report.lines():
        print(line)
    if args.dry_run:
        print(f"dry run: wrote {settings.chunks_path}; no embeddings or index changes")
        return 0

    cache = EmbeddingCache(settings.embedding_cache_path)
    embedder = Embedder(make_voyage_client(settings), settings.embed, cache)
    qdrant = QdrantClient(url=settings.qdrant_url, timeout=int(settings.index.timeout_s))
    try:
        result = sync(qdrant, chunks, embedder, settings.index, programs, args.allow_mass_delete)
    except MassDeleteError as exc:
        print(f"ABORTED: {exc}")
        return 2
    finally:
        cache.close()

    print(embedder.stats.line())
    for line in result.lines():
        print(line)
    return 0 if result.point_count == result.chunk_count else 1


if __name__ == "__main__":
    raise SystemExit(main())
