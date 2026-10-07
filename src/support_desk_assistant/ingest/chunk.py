"""Heading-aware chunking of the processed canada.ca Markdown.

Each page is split into H2/H3 sections. Exact repeats of a block within a page
(interactive "get your instructions" tools ship every scenario's text) are
dropped, keeping the first occurrence. Tiny sections are merged into a
neighbour, and long sections are packed into chunks at blank-line block
boundaries, so lists and tables stay whole unless a single block is over the
hard max. Every chunk's embedded text is prefixed with
``<title> > <H2> > <H3>``. See docs/decisions.md #9.
"""

import hashlib
import json
import logging
import re
import statistics
import uuid
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from support_desk_assistant.config import ChunkSettings, Settings
from support_desk_assistant.ingest.crawl import read_manifest, slug_for

logger = logging.getLogger(__name__)

TokenCounter = Callable[[str], int]

HEADING_RE = re.compile(r"^(#{1,3}) (.+?)\s*$")
TABLE_SEPARATOR_RE = re.compile(r"^\|\s*:?-{3,}")
BLOCK_SPLIT_RE = re.compile(r"\n\s*\n")
CHUNK_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL, "https://github.com/vdrohan/support-desk-assistant"
)


def voyage_token_counter(model: str) -> TokenCounter:
    """Exact token counts with the model's tokenizer (downloaded once from Hugging Face)."""
    from tokenizers import Tokenizer

    tokenizer = Tokenizer.from_pretrained(f"voyageai/{model}")
    tokenizer.no_truncation()
    return lambda text: len(tokenizer.encode(text).ids)


@dataclass
class Section:
    path: tuple[str, ...]  # (title, H2?, H3?)
    blocks: list[str]


@dataclass(frozen=True)
class Chunk:
    id: str
    url: str
    title: str
    program: str
    date_modified: str | None
    heading_path: list[str]  # H2/H3 below the title
    chunk_index: int
    text: str  # body, stored in the payload
    embed_text: str  # prefix + body, what gets embedded
    content_hash: str  # sha256 of embed_text
    tokens: int  # of embed_text


# --- Parsing ---------------------------------------------------------------------


def parse_page(markdown: str) -> tuple[str, list[Section]]:
    """Return the page title and its H2/H3 sections (empty sections dropped)."""
    lines = markdown.splitlines()
    start = 0
    while start < len(lines) and (
        not lines[start].strip() or lines[start].startswith(("Source:", "Date modified:"))
    ):
        start += 1

    title, h2, h3 = "", None, None
    current: list[str] = []
    sections: list[Section] = []

    def flush() -> None:
        text = "\n".join(current).strip()
        blocks = [b.strip() for b in BLOCK_SPLIT_RE.split(text) if b.strip()]
        if blocks:
            path = tuple(part for part in (title, h2, h3) if part)
            sections.append(Section(path, blocks))
        current.clear()

    for line in lines[start:]:
        match = HEADING_RE.match(line)
        if not match:
            current.append(line)
            continue
        flush()
        level, text = len(match.group(1)), match.group(2).strip()
        if level == 1:
            title, h2, h3 = text, None, None
        elif level == 2:
            h2, h3 = text, None
        else:
            h3 = text
    flush()
    return title, sections


# --- Within-page dedup ----------------------------------------------------------


def drop_repeated_blocks(
    sections: list[Section], count: TokenCounter, min_tokens: int
) -> tuple[list[Section], int]:
    """Drop exact repeats of blocks with >= ``min_tokens`` tokens, keeping the first
    occurrence on the page. Sections left empty are removed. Returns the number dropped."""
    seen: set[str] = set()
    dropped = 0
    result: list[Section] = []
    for section in sections:
        kept = []
        for block in section.blocks:
            if block in seen and count(block) >= min_tokens:
                dropped += 1
                continue
            seen.add(block)
            kept.append(block)
        if kept:
            result.append(Section(section.path, kept))
    return result, dropped


# --- Merging tiny sections -------------------------------------------------------


def _heading_lines(path: tuple[str, ...], target: tuple[str, ...]) -> list[str]:
    """Markdown heading lines for the parts of ``path`` not shared with ``target``."""
    common = 0
    while common < min(len(path), len(target)) and path[common] == target[common]:
        common += 1
    return [f"{'#' * (depth + 1)} {path[depth]}" for depth in range(max(common, 1), len(path))]


def _merge_target(sections: list[Section], i: int) -> int | None:
    """Prefer the next, then previous, section under the same H2; else any neighbour."""
    same_h2 = sections[i].path[:2]
    for j in (i + 1, i - 1):
        if 0 <= j < len(sections) and sections[j].path[:2] == same_h2:
            return j
    for j in (i - 1, i + 1):
        if 0 <= j < len(sections):
            return j
    return None


def merge_small_sections(
    sections: list[Section], count: TokenCounter, min_tokens: int
) -> list[Section]:
    """Fold sections under ``min_tokens`` into a neighbour. The merged text keeps
    the small section's heading as a Markdown line; the target keeps its path."""
    result = [Section(s.path, list(s.blocks)) for s in sections]
    i = 0
    while i < len(result):
        section = result[i]
        target_index = _merge_target(result, i) if len(result) > 1 else None
        if target_index is None or count("\n\n".join(section.blocks)) >= min_tokens:
            i += 1
            continue
        target = result[target_index]
        moved = list(section.blocks)
        if headings := _heading_lines(section.path, target.path):
            moved[0] = "\n\n".join([*headings, moved[0]])
        target.blocks = moved + target.blocks if target_index > i else target.blocks + moved
        del result[i]
        if target_index < i:
            i -= 1  # re-check the grown target is not still tiny
    return result


# --- Packing ---------------------------------------------------------------------


def _split_words(text: str, count: TokenCounter, limit: int) -> list[str]:
    pieces, current = [], []
    for word in text.split(" "):
        if current and count(" ".join([*current, word])) > limit:
            pieces.append(" ".join(current))
            current = []
        current.append(word)
    if current:
        pieces.append(" ".join(current))
    return pieces


def split_oversized_block(block: str, count: TokenCounter, limit: int) -> list[str]:
    """Split one block over ``limit`` by lines. Tables repeat their header row."""
    lines = block.splitlines()
    is_table = len(lines) > 2 and lines[0].startswith("|") and TABLE_SEPARATOR_RE.match(lines[1])
    header = lines[:2] if is_table else []
    pieces: list[str] = []
    current: list[str] = []
    for line in lines[len(header) :]:
        if current and count("\n".join([*header, *current, line])) > limit:
            pieces.append("\n".join([*header, *current]))
            current = []
        current.append(line)
    if current:
        pieces.append("\n".join([*header, *current]))
    out: list[str] = []
    for piece in pieces:
        out += _split_words(piece, count, limit) if count(piece) > limit else [piece]
    return out


def pack_blocks(
    blocks: list[str], count: TokenCounter, target: int, max_tokens: int, min_tokens: int
) -> list[str]:
    """Greedy packing of whole blocks up to ``target``; a chunk may grow up to
    ``max_tokens`` while it is still under ``min_tokens``. A small tail is folded
    into the previous chunk when it fits."""
    units: list[tuple[str, int]] = []
    for block in blocks:
        tokens = count(block)
        if tokens <= max_tokens:
            units.append((block, tokens))
        else:
            units += [(p, count(p)) for p in split_oversized_block(block, count, max_tokens)]

    groups: list[list[tuple[str, int]]] = []
    current: list[tuple[str, int]] = []
    size = 0
    for text, tokens in units:
        if (
            current
            and size + tokens > target
            and (size >= min_tokens or size + tokens > max_tokens)
        ):
            groups.append(current)
            current, size = [], 0
        current.append((text, tokens))
        size += tokens
    if current:
        groups.append(current)

    if len(groups) > 1:
        tail, prev = sum(t for _, t in groups[-1]), sum(t for _, t in groups[-2])
        if tail < min_tokens and prev + tail <= max_tokens:
            last = groups.pop()
            groups[-1] += last

    chunks = ["\n\n".join(text for text, _ in group) for group in groups]
    # Block sums undercount the "\n\n" joins; re-split anything that slipped over.
    out: list[str] = []
    for chunk in chunks:
        out += [chunk] if count(chunk) <= max_tokens else _resplit(chunk, count, max_tokens)
    return out


def _resplit(chunk: str, count: TokenCounter, max_tokens: int) -> list[str]:
    blocks = BLOCK_SPLIT_RE.split(chunk)
    mid = max(1, len(blocks) // 2)
    halves = ["\n\n".join(blocks[:mid]), "\n\n".join(blocks[mid:])]
    out: list[str] = []
    for half in filter(None, halves):
        if count(half) <= max_tokens or len(BLOCK_SPLIT_RE.split(half)) == 1:
            out.append(half)
        else:
            out += _resplit(half, count, max_tokens)
    return out


# --- Chunks ----------------------------------------------------------------------


def chunk_id(url: str, path: tuple[str, ...], ordinal: int) -> str:
    """Deterministic: same page, heading path and position -> same ID."""
    return str(uuid.uuid5(CHUNK_NAMESPACE, f"{url}|{' > '.join(path)}|{ordinal}"))


def chunk_page(
    markdown: str,
    *,
    url: str,
    program: str,
    date_modified: str | None,
    cfg: ChunkSettings,
    count: TokenCounter,
    dropped: dict[str, int] | None = None,
) -> list[Chunk]:
    """Chunk one page. If ``dropped`` is given, records ``url -> repeated blocks dropped``."""
    title, sections = parse_page(markdown)
    sections, n_dropped = drop_repeated_blocks(sections, count, cfg.dedup_min_tokens)
    if dropped is not None and n_dropped:
        dropped[url] = n_dropped
    sections = merge_small_sections(sections, count, cfg.min_tokens)
    ordinals: Counter[tuple[str, ...]] = Counter()
    chunks: list[Chunk] = []
    for section in sections:
        prefix = " > ".join(section.path)
        overhead = count(f"{prefix}\n\n")
        bodies = pack_blocks(
            section.blocks,
            count,
            target=cfg.target_tokens - overhead,
            max_tokens=cfg.max_tokens - overhead,
            min_tokens=cfg.min_tokens,
        )
        for body in bodies:
            embed_text = f"{prefix}\n\n{body}"
            chunks.append(
                Chunk(
                    id=chunk_id(url, section.path, ordinals[section.path]),
                    url=url,
                    title=title,
                    program=program,
                    date_modified=date_modified,
                    heading_path=list(section.path[1:]),
                    chunk_index=len(chunks),
                    text=body,
                    embed_text=embed_text,
                    content_hash="sha256:" + hashlib.sha256(embed_text.encode()).hexdigest(),
                    tokens=count(embed_text),
                )
            )
            ordinals[section.path] += 1
    return chunks


def chunk_corpus(
    settings: Settings,
    count: TokenCounter,
    programs: Iterable[str] | None = None,
    dropped: dict[str, int] | None = None,
) -> list[Chunk]:
    """Chunk every manifest page (optionally only some programs), in manifest order."""
    wanted = set(programs) if programs else None
    chunks: list[Chunk] = []
    for record in read_manifest(settings.manifest_path):
        if wanted and record["program"] not in wanted:
            continue
        path = settings.processed_dir / record["program"] / f"{slug_for(record['url'])}.md"
        if not path.exists():
            logger.warning("missing processed file for %s (run the crawler)", record["url"])
            continue
        chunks += chunk_page(
            path.read_text(encoding="utf-8"),
            url=record["url"],
            program=record["program"],
            date_modified=record["date_modified"],
            cfg=settings.chunk,
            count=count,
            dropped=dropped,
        )
    ids = [c.id for c in chunks]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate chunk IDs; is a URL listed twice in the manifest?")
    return chunks


def write_chunks(path: Path, chunks: Iterable[Chunk]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(asdict(c), ensure_ascii=False) + "\n" for c in chunks))


@dataclass
class ChunkReport:
    per_program: dict[str, int] = field(default_factory=dict)
    token_min: int = 0
    token_median: float = 0
    token_max: int = 0
    over_max: list[str] = field(default_factory=list)
    under_min: int = 0
    dropped_blocks: dict[str, int] = field(default_factory=dict)  # url -> repeats dropped

    @classmethod
    def build(
        cls, chunks: list[Chunk], cfg: ChunkSettings, dropped: dict[str, int] | None = None
    ) -> "ChunkReport":
        tokens = [c.tokens for c in chunks] or [0]
        return cls(
            per_program=dict(sorted(Counter(c.program for c in chunks).items())),
            token_min=min(tokens),
            token_median=statistics.median(tokens),
            token_max=max(tokens),
            over_max=[c.id for c in chunks if c.tokens > cfg.max_tokens],
            under_min=sum(1 for c in chunks if c.tokens < cfg.min_tokens),
            dropped_blocks=dict(dropped or {}),
        )

    def lines(self, top: int = 10) -> list[str]:
        total = sum(self.per_program.values())
        out = [
            "chunks per program: "
            + ", ".join(f"{p}={n}" for p, n in self.per_program.items())
            + f" (total {total})",
            f"tokens: min {self.token_min}, median {self.token_median:g}, max {self.token_max}",
            f"over max: {len(self.over_max)}; under min: {self.under_min}",
            f"repeated blocks dropped: {sum(self.dropped_blocks.values())} "
            f"on {len(self.dropped_blocks)} pages",
        ]
        ranked = sorted(self.dropped_blocks.items(), key=lambda kv: (-kv[1], kv[0]))
        out += [f"  {n:5}  {url}" for url, n in ranked[:top]]
        return out
