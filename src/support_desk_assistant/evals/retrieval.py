"""Retrieval eval: page hit@1/@k, MRR@k and answer hit@k on a versioned question set.

Every question runs twice: unfiltered (realistic) and filtered to its expected
programs (oracle routing). Out-of-corpus questions are not scored for hits; their
top-1 scores are compared with answerable questions' to see whether a refusal
threshold is possible. See docs/decisions.md #12.

    uv run sda-eval-retrieval [--collection canada_docs_v1] [-k 5] [--validate]
"""

import argparse
import hashlib
import json
import re
import statistics
import subprocess
import unicodedata
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from support_desk_assistant.config import Settings, get_settings
from support_desk_assistant.ingest.crawl import read_manifest, slug_for
from support_desk_assistant.retrieve import Hit

Category = Literal[
    "direct", "colloquial", "known_issue", "ambiguous", "out_of_corpus", "corpus_gap"
]
UNANSWERABLE = frozenset({"out_of_corpus", "corpus_gap"})
Source = Literal["human", "generated"]
Mode = Literal["unfiltered", "oracle"]
MODES: tuple[Mode, ...] = ("unfiltered", "oracle")
SearchFn = Callable[[str, list[str] | None, int], list[Hit]]


# --- Dataset ---------------------------------------------------------------------


class EvalItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    question: str
    category: Category
    source: Source
    expected_programs: list[str]
    expected_urls: list[str]
    key_facts: list[str]
    notes: str = ""

    @property
    def answerable(self) -> bool:
        return self.category not in UNANSWERABLE

    @model_validator(mode="after")
    def _shape(self) -> "EvalItem":
        if self.category == "out_of_corpus":
            if self.expected_programs or self.expected_urls or self.key_facts:
                raise ValueError("out_of_corpus items must have no programs, urls or key_facts")
        elif self.category == "corpus_gap":
            # In scope for a program, but the corpus doesn't contain the answer
            # (docs/decisions.md #13): programs yes, urls/facts no.
            if self.expected_urls or self.key_facts or not self.expected_programs:
                raise ValueError("corpus_gap items need expected_programs and no urls/key_facts")
        elif not (self.expected_programs and self.expected_urls and self.key_facts):
            raise ValueError("answerable items need expected_programs, expected_urls, key_facts")
        if self.category == "ambiguous" and len(self.expected_programs) < 2:
            raise ValueError("ambiguous items need at least 2 expected_programs")
        return self


class DatasetError(Exception):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("\n".join(errors))
        self.errors = errors


def load_dataset(path: Path) -> list[EvalItem]:
    items, errors = [], []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            items.append(EvalItem.model_validate_json(line))
        except ValidationError as exc:
            errors.append(f"{path.name}:{lineno}: {exc.errors()[0]['msg']}")
    if errors:
        raise DatasetError(errors)
    return items


_CHAR_MAP = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-"})


def normalize(text: str) -> str:
    """Case, whitespace, curly quotes/dashes and Markdown emphasis don't matter."""
    text = unicodedata.normalize("NFKC", text).translate(_CHAR_MAP)
    text = re.sub(r"\*\*|__", "", text)
    return re.sub(r"\s+", " ", text).strip().lower()


@dataclass
class ValidationResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unreachable_facts: dict[str, list[str]] = field(default_factory=dict)  # id -> facts

    @property
    def ok(self) -> bool:
        return not self.errors


def _load_chunk_texts(path: Path) -> dict[str, list[str]] | None:
    if not path.exists():
        return None
    by_url: dict[str, list[str]] = defaultdict(list)
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        by_url[row["url"]].append(normalize(row["embed_text"]))
    return by_url


def validate_dataset(items: Sequence[EvalItem], settings: Settings) -> ValidationResult:
    """Hard errors: duplicate ids, unknown urls/programs, key facts missing from the
    expected pages. Warning: a fact on the page but in no chunk of it (unreachable)."""
    result = ValidationResult()
    manifest = {r["url"]: r for r in read_manifest(settings.manifest_path)}
    chunks = _load_chunk_texts(settings.chunks_path)
    if chunks is None:
        result.warnings.append(f"{settings.chunks_path} missing: chunk reachability not checked")
    pages: dict[str, str] = {}

    def page_text(url: str) -> str:
        if url not in pages:
            path = settings.processed_dir / manifest[url]["program"] / f"{slug_for(url)}.md"
            pages[url] = normalize(path.read_text(encoding="utf-8")) if path.exists() else ""
        return pages[url]

    seen: set[str] = set()
    for item in items:
        if item.id in seen:
            result.errors.append(f"{item.id}: duplicate id")
        seen.add(item.id)
        unknown_programs = set(item.expected_programs) - set(settings.crawl.seeds)
        if unknown_programs:
            result.errors.append(f"{item.id}: unknown programs {sorted(unknown_programs)}")
        missing = [u for u in item.expected_urls if u not in manifest]
        for url in missing:
            result.errors.append(f"{item.id}: expected_url not in manifest: {url}")
        urls = [u for u in item.expected_urls if u in manifest]
        url_programs = {manifest[u]["program"] for u in urls}
        if urls and not missing and url_programs != set(item.expected_programs):
            result.errors.append(
                f"{item.id}: expected_programs {sorted(item.expected_programs)} "
                f"!= programs of expected_urls {sorted(url_programs)}"
            )
        for fact in item.key_facts:
            needle = normalize(fact)
            if not any(needle in page_text(u) for u in urls):
                result.errors.append(
                    f"{item.id}: key fact not verbatim in any expected page: {fact!r}"
                )
            elif chunks is not None and not any(
                needle in text for u in urls for text in chunks.get(u, [])
            ):
                result.unreachable_facts.setdefault(item.id, []).append(fact)
    for item_id, facts in result.unreachable_facts.items():
        result.warnings.append(f"{item_id}: on the page but in no chunk (unreachable): {facts}")
    return result


# --- Scoring ---------------------------------------------------------------------


@dataclass
class ItemResult:
    id: str
    question: str
    category: str
    source: str
    program_group: str
    mode: str
    answerable: bool
    rank: int | None  # 1-based rank of the first chunk from an expected page, within k
    answer_hit: bool | None
    top1_score: float | None
    top1_url: str | None
    top1_path: str | None
    expected_urls: list[str]


def first_relevant_rank(hits: Sequence[Hit], expected_urls: Iterable[str], k: int) -> int | None:
    expected = set(expected_urls)
    return next((rank for rank, hit in enumerate(hits[:k], 1) if hit.url in expected), None)


def retrieved_text(hit: Hit) -> str:
    """What retrieval hands to the reader: title, heading path, then the chunk body."""
    return " > ".join([hit.title, *hit.heading_path]) + "\n\n" + hit.text


def answer_hit(
    hits: Sequence[Hit], key_facts: Iterable[str], expected_urls: Iterable[str], k: int
) -> bool:
    """A key fact in a top-k chunk *from an expected page*. Short facts ("up to 26
    weeks") also occur on other benefits' pages; those don't count."""
    facts = [normalize(f) for f in key_facts]
    expected = set(expected_urls)
    return any(
        f in normalize(retrieved_text(hit))
        for hit in hits[:k]
        if hit.url in expected
        for f in facts
    )


def hit_rate(ranks: Sequence[int | None], cutoff: int) -> float:
    return sum(1 for r in ranks if r is not None and r <= cutoff) / len(ranks) if ranks else 0.0


def mean_reciprocal_rank(ranks: Sequence[int | None]) -> float:
    return sum(1 / r for r in ranks if r) / len(ranks) if ranks else 0.0


def score_item(item: EvalItem, hits: Sequence[Hit], k: int, mode: Mode) -> ItemResult:
    top = hits[0] if hits else None
    return ItemResult(
        id=item.id,
        question=item.question,
        category=item.category,
        source=item.source,
        program_group="+".join(sorted(item.expected_programs)) or "none",
        mode=mode,
        answerable=item.answerable,
        rank=first_relevant_rank(hits, item.expected_urls, k) if item.answerable else None,
        answer_hit=(
            answer_hit(hits, item.key_facts, item.expected_urls, k) if item.answerable else None
        ),
        top1_score=round(top.score, 4) if top else None,
        top1_url=top.url if top else None,
        top1_path=" > ".join([top.title, *top.heading_path]) if top else None,
        expected_urls=list(item.expected_urls),
    )


def run_eval(items: Sequence[EvalItem], search_fn: SearchFn, k: int) -> list[ItemResult]:
    """Unfiltered for every item; oracle (filtered to expected programs) for answerable ones."""
    results = []
    for item in items:
        results.append(score_item(item, search_fn(item.question, None, k), k, "unfiltered"))
        if item.answerable:
            hits = search_fn(item.question, list(item.expected_programs), k)
            results.append(score_item(item, hits, k, "oracle"))
    return results


# --- Aggregation -----------------------------------------------------------------


def metrics(results: Sequence[ItemResult], k: int) -> dict[str, float | int]:
    scored = [r for r in results if r.answerable]
    ranks = [r.rank for r in scored]
    out: dict[str, float | int] = {"n": len(scored)}
    if scored:
        out |= {
            "page_hit@1": round(hit_rate(ranks, 1), 3),
            f"page_hit@{k}": round(hit_rate(ranks, k), 3),
            f"mrr@{k}": round(mean_reciprocal_rank(ranks), 3),
            f"answer_hit@{k}": round(sum(1 for r in scored if r.answer_hit) / len(scored), 3),
        }
    return out


def breakdowns(results: Sequence[ItemResult], k: int) -> dict:
    out: dict = {}
    for mode in MODES:
        rows = [r for r in results if r.mode == mode]
        out[mode] = {"overall": metrics(rows, k)}
        for dim in ("program_group", "category", "source"):
            groups: dict[str, list[ItemResult]] = defaultdict(list)
            for r in rows:
                groups[getattr(r, dim)].append(r)
            out[mode][dim] = {g: metrics(rs, k) for g, rs in sorted(groups.items())}
    return out


def _dist(values: Sequence[float]) -> dict:
    if not values:
        return {"n": 0}
    ordered = sorted(values)
    p10 = ordered[int(0.1 * (len(ordered) - 1))]  # nearest rank, rounded down
    return {
        "n": len(ordered),
        "min": round(ordered[0], 3),
        "p10": round(p10, 3),
        "median": round(statistics.median(ordered), 3),
        "max": round(ordered[-1], 3),
    }


def score_analysis(results: Sequence[ItemResult]) -> dict:
    """Top-1 scores (unfiltered) by kind, and what a refusal threshold just above the
    best out-of-corpus score would cost in wrongly refused answerable questions."""
    unfiltered = [r for r in results if r.mode == "unfiltered" and r.top1_score is not None]
    kinds = {
        "answerable": [
            r.top1_score for r in unfiltered if r.answerable and r.category != "ambiguous"
        ],
        "ambiguous": [r.top1_score for r in unfiltered if r.category == "ambiguous"],
        "out_of_corpus": [r.top1_score for r in unfiltered if r.category == "out_of_corpus"],
    }
    out = {name: _dist(scores) for name, scores in kinds.items()}
    out["out_of_corpus_scores"] = {
        r.id: r.top1_score for r in unfiltered if r.category == "out_of_corpus"
    }
    # Reported on their own: in scope, so a refusal threshold shouldn't be tuned on them.
    out["corpus_gap"] = {
        r.id: {"question": r.question, "top1_score": r.top1_score, "top1_url": r.top1_url}
        for r in unfiltered
        if r.category == "corpus_gap"
    }
    if kinds["out_of_corpus"] and kinds["answerable"]:
        threshold = max(kinds["out_of_corpus"])
        refused = sum(1 for s in kinds["answerable"] if s <= threshold)
        out["threshold_at_max_ooc"] = {
            "threshold": round(threshold, 3),
            "answerable_refused": refused,
            "answerable_n": len(kinds["answerable"]),
            "answerable_refused_rate": round(refused / len(kinds["answerable"]), 3),
        }
    return out


def misses(results: Sequence[ItemResult], k: int) -> list[dict]:
    out = []
    for r in results:
        if not r.answerable:
            continue
        page_miss, answer_miss = r.rank is None, not r.answer_hit
        if page_miss or answer_miss:
            out.append(
                {
                    "id": r.id,
                    "mode": r.mode,
                    "question": r.question,
                    "page_miss": page_miss,
                    "answer_miss": answer_miss,
                    "rank": r.rank,
                    "expected_urls": r.expected_urls,
                    "top1": {"score": r.top1_score, "url": r.top1_url, "path": r.top1_path},
                }
            )
    return out


# --- Report ----------------------------------------------------------------------


def _git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def build_report(
    results: Sequence[ItemResult],
    *,
    k: int,
    collection: str,
    dataset_path: Path,
    settings: Settings,
    validation: ValidationResult,
) -> dict:
    return {
        "meta": {
            "created_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "git_sha": _git("rev-parse", "--short", "HEAD"),
            "git_dirty": bool(_git("status", "--porcelain")),
            "collection": collection,
            "k": k,
            "dataset": dataset_path.name,
            "dataset_sha256": hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
            "embed": settings.embed.model_dump(
                include={"model", "output_dimension", "output_dtype"}
            ),
            "chunk": settings.chunk.model_dump(),
            "questions": len({r.id for r in results}),
            "unreachable_facts": validation.unreachable_facts,
        },
        "metrics": breakdowns(results, k),
        "scores": score_analysis(results),
        "misses": misses(results, k),
        "items": [asdict(r) for r in results],
    }


def _table(rows: dict[str, dict], k: int, label: str) -> list[str]:
    cols = ["page_hit@1", f"page_hit@{k}", f"mrr@{k}", f"answer_hit@{k}"]
    lines = [f"| {label} | n | " + " | ".join(cols) + " |", "|---" * (len(cols) + 2) + "|"]
    for name, m in rows.items():
        values = [f"{m[c]:.2f}" if c in m else "–" for c in cols]
        lines.append(f"| {name} | {m['n']} | " + " | ".join(values) + " |")
    return lines


def render_markdown(report: dict) -> str:
    meta, k = report["meta"], report["meta"]["k"]
    m = report["metrics"]
    dirty = " (dirty)" if meta["git_dirty"] else ""
    out = [
        f"# Retrieval eval: {meta['collection']}, k={k}",
        "",
        f"{meta['created_at']} · git {meta['git_sha']}{dirty} · {meta['dataset']} "
        f"({meta['questions']} questions, sha256 {meta['dataset_sha256'][:12]}) · "
        f"{meta['embed']['model']} {meta['embed']['output_dimension']}d",
        "",
        "n = answerable questions in the group (out_of_corpus and corpus_gap items have no "
        "hit metrics). Answer hit = a key fact in a top-k chunk from an expected page.",
        "",
        "## Overall",
        "",
        *_table({mode: m[mode]["overall"] for mode in MODES}, k, "mode"),
    ]
    for dim, label in (
        ("program_group", "program"),
        ("category", "category"),
        ("source", "source"),
    ):
        for mode in MODES:
            out += ["", f"## By {label} ({mode})", "", *_table(m[mode][dim], k, label)]

    s = report["scores"]
    out += ["", "## Top-1 scores (unfiltered)", "", "| kind | n | min | p10 | median | max |"]
    out.append("|---|---|---|---|---|---|")
    for kind in ("answerable", "ambiguous", "out_of_corpus"):
        d = s[kind]
        vals = [f"{d[c]:.3f}" if c in d else "–" for c in ("min", "p10", "median", "max")]
        out.append(f"| {kind} | {d['n']} | " + " | ".join(vals) + " |")
    if t := s.get("threshold_at_max_ooc"):
        out += [
            "",
            f"Refusal threshold at the best out-of-corpus score ({t['threshold']:.3f}) would "
            f"refuse {t['answerable_refused']} of {t['answerable_n']} answerable questions "
            f"({t['answerable_refused_rate']:.0%}).",
        ]
    if s.get("corpus_gap"):
        out += ["", "## Corpus gaps (in scope, answer not in corpus; no hit metrics)", ""]
        out += [
            f"- {i}: {g['question']} → top-1 {g['top1_score']:.3f} {g['top1_url']}"
            for i, g in s["corpus_gap"].items()
        ]
    if meta["unreachable_facts"]:
        out += ["", "## Key facts unreachable by chunking", ""]
        out += [f"- {i}: {facts}" for i, facts in meta["unreachable_facts"].items()]

    out += ["", "## Misses", ""]
    if not report["misses"]:
        out.append("None.")
    for miss in report["misses"]:
        kind = " + ".join(
            name
            for name, flag in (("page", miss["page_miss"]), ("answer", miss["answer_miss"]))
            if flag
        )
        top = miss["top1"]
        out += [
            f"- **{miss['id']}** ({miss['mode']}, {kind} miss"
            + (f", expected page at #{miss['rank']}" if miss["rank"] else "")
            + f"): {miss['question']}",
            f"  - expected: {', '.join(miss['expected_urls'])}",
            f"  - got #1: {top['score']:.3f} {top['url']} ({top['path']})",
        ]
    return "\n".join(out) + "\n"


def write_report(report: dict, results_dir: Path) -> tuple[Path, Path]:
    results_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{report['meta']['collection']}_{report['meta']['created_at'][:10]}"
    json_path, md_path = results_dir / f"{stem}.json", results_dir / f"{stem}.md"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, md_path


def review_table(items: Sequence[EvalItem]) -> str:
    """Compact listing for freezing the dataset before the first run."""
    lines = []
    for it in items:
        programs = ",".join(it.expected_programs) or "–"
        lines.append(f"{it.id:<14} {it.source:<9} {it.category:<13} {programs:<12} {it.question}")
        for fact in it.key_facts:
            lines.append(f"{'':<50} · {fact}")
    return "\n".join(lines)


# --- CLI -------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(
        prog="sda-eval-retrieval", description=__doc__.split("\n\n")[0]
    )
    parser.add_argument("--collection", default=settings.index.collection)
    parser.add_argument("-k", type=int, default=settings.eval.k)
    parser.add_argument(
        "--dataset", type=Path, default=settings.eval_dir / settings.eval.retrieval_dataset
    )
    parser.add_argument(
        "--validate", action="store_true", help="validate and print the dataset; no API calls"
    )
    args = parser.parse_args(argv)

    try:
        items = load_dataset(args.dataset)
    except DatasetError as exc:
        print("dataset schema errors:\n" + "\n".join(exc.errors))
        return 1
    validation = validate_dataset(items, settings)
    for warning in validation.warnings:
        print(f"WARNING {warning}")
    if not validation.ok:
        print("dataset validation FAILED:\n" + "\n".join(validation.errors))
        return 1
    print(f"dataset OK: {len(items)} questions, {len(validation.warnings)} warnings")
    if args.validate:
        print(review_table(items))
        return 0

    # Imported here so --validate needs no API key, Qdrant or network.
    from qdrant_client import QdrantClient

    from support_desk_assistant.ingest.embed import Embedder, EmbeddingCache, make_voyage_client
    from support_desk_assistant.retrieve import search

    cache = EmbeddingCache(settings.embedding_cache_path)
    embedder = Embedder(make_voyage_client(settings), settings.embed, cache)
    qdrant = QdrantClient(url=settings.qdrant_url, timeout=int(settings.index.timeout_s))

    def search_fn(question: str, programs: list[str] | None, k: int) -> list[Hit]:
        return search(
            question,
            settings=settings,
            embedder=embedder,
            qdrant=qdrant,
            programs=programs,
            k=k,
            collection=args.collection,
        )

    try:
        results = run_eval(items, search_fn, args.k)
    finally:
        cache.close()
    report = build_report(
        results,
        k=args.k,
        collection=args.collection,
        dataset_path=args.dataset,
        settings=settings,
        validation=validation,
    )
    json_path, md_path = write_report(report, settings.eval_results_dir)
    print(render_markdown(report))
    print(
        f"query embeddings: {embedder.stats.cache_hits} cached, "
        f"{embedder.stats.tokens_billed} tokens billed"
    )
    print(f"wrote {json_path} and {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
