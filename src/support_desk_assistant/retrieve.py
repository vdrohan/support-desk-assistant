"""Vector search over the indexed corpus (no LLM).

uv run sda-search "How do I apply for EI?" [--program ei] [-k 5]
"""

import argparse
from collections.abc import Sequence
from dataclasses import dataclass

from qdrant_client import QdrantClient

from support_desk_assistant.config import Settings, get_settings
from support_desk_assistant.ingest.embed import Embedder, make_voyage_client
from support_desk_assistant.ingest.index import program_filter


@dataclass(frozen=True)
class Hit:
    score: float
    program: str
    title: str
    heading_path: list[str]
    url: str
    text: str


def search(
    query: str,
    *,
    settings: Settings,
    embedder: Embedder,
    qdrant: QdrantClient,
    program: str | None = None,
    programs: Sequence[str] | None = None,
    k: int | None = None,
    collection: str | None = None,
) -> list[Hit]:
    """Embed ``query`` (input_type=query) and return the top-k chunks, optionally
    restricted to one ``program`` or any of ``programs``."""
    wanted = list(programs or ([program] if program else []))
    response = qdrant.query_points(
        collection or settings.index.collection,
        query=embedder.embed_query(query),
        query_filter=program_filter(wanted) if wanted else None,
        limit=k or settings.retrieval.top_k,
        with_payload=True,
    )
    return [
        Hit(
            score=point.score,
            program=point.payload["program"],
            title=point.payload["title"],
            heading_path=point.payload["heading_path"],
            url=point.payload["url"],
            text=point.payload["text"],
        )
        for point in response.points
    ]


def format_hit(rank: int, hit: Hit, preview_chars: int = 200) -> str:
    path = " > ".join([hit.title, *hit.heading_path])
    preview = " ".join(hit.text.split())[:preview_chars]
    return f"{rank}. {hit.score:.3f} [{hit.program}] {path}\n   {hit.url}\n   {preview}"


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(prog="sda-search", description="vector search smoke test")
    parser.add_argument("question")
    parser.add_argument("--program", choices=sorted(settings.crawl.seeds))
    parser.add_argument("-k", type=int, default=settings.retrieval.top_k)
    args = parser.parse_args(argv)

    embedder = Embedder(make_voyage_client(settings), settings.embed)
    qdrant = QdrantClient(url=settings.qdrant_url, timeout=int(settings.index.timeout_s))
    hits = search(
        args.question,
        settings=settings,
        embedder=embedder,
        qdrant=qdrant,
        program=args.program,
        k=args.k,
    )
    for rank, hit in enumerate(hits, 1):
        print(format_hit(rank, hit))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
