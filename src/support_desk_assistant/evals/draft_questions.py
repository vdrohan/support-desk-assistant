"""Draft candidate eval questions from the processed corpus with Haiku.

Writes ``data/eval/candidates.jsonl`` only. Candidates are reviewed by hand with
``sda-eval-review``; accepted ones land in ``eval_set_v1.jsonl`` (docs/decisions.md #19).

    uv run sda-eval-draft --dry-run        # show the sampled pages, no API calls
    uv run sda-eval-draft [--yes] [--force]

The drafter never decides what is true: every key fact is checked verbatim against the
gold pages, and anything that fails is flagged for the reviewer, not silently dropped.
"""

import argparse
import json
import math
import random
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol
from xml.sax.saxutils import quoteattr

import anthropic
from anthropic import transform_schema
from pydantic import BaseModel, ConfigDict, ValidationError

from support_desk_assistant.answer.cache import ResponseCache, cache_key
from support_desk_assistant.answer.cost import Usage, cost_usd, usd_to_cad
from support_desk_assistant.answer.pipeline import (
    CHARS_PER_TOKEN_ESTIMATE,
    STATUS_INVALID,
    STATUS_MAX_TOKENS,
    STATUS_OK,
    STATUS_REFUSAL,
    SystemPromptNotReady,
    load_system_prompt,
    make_anthropic_client,
    usage_from_raw,
)
from support_desk_assistant.config import Settings, get_settings
from support_desk_assistant.evals.retrieval import normalize
from support_desk_assistant.ingest.crawl import read_manifest, slug_for

Category = Literal[
    "single_page",
    "multi_page",
    "confusable",
    "unanswerable_out_of_scope",
    "unanswerable_in_domain",
    "time_sensitive",
]
# Also the output order of candidates.jsonl.
CATEGORIES: tuple[Category, ...] = (
    "single_page",
    "multi_page",
    "confusable",
    "unanswerable_out_of_scope",
    "unanswerable_in_domain",
    "time_sensitive",
)
UNANSWERABLE = frozenset({"unanswerable_out_of_scope", "unanswerable_in_domain"})

# Part of the response-cache key: bump when DraftResponse or the user-message layout changes.
DRAFT_SCHEMA_VERSION = 1

MONEY_RE = re.compile(r"\$\s?\d")
WORD_RE = re.compile(r"[a-z0-9']+")
HEADER_PREFIXES = ("Source:", "Date modified:")
# The crawler leaves this where a page loaded a value by JavaScript (decisions #13).
PLACEHOLDER_MARKER = "Error loading"
MAX_KEY_FACTS = 3
# Per program, how many of the most similar related-page pairs multi_page may draw from.
MULTI_PAGE_PAIRS_PER_PROGRAM = 4


# --- Data ------------------------------------------------------------------------


class Candidate(BaseModel):
    """One drafted (later: accepted) eval question. Shape rules live in
    ``check_candidate`` so a malformed draft is reported to the reviewer, not lost."""

    model_config = ConfigDict(extra="forbid")

    id: str
    question: str
    category: Category
    answerable: bool
    key_facts: list[str]
    gold_urls: list[str]
    # Pages the question was drafted from (provenance; the scorer ignores it).
    source_urls: list[str]
    notes: str = ""


class DraftedQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str
    key_facts: list[str]
    gold_urls: list[str]
    notes: str


class DraftResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    questions: list[DraftedQuestion]


def draft_schema() -> dict[str, Any]:
    return transform_schema(DraftResponse)


@dataclass(frozen=True)
class Page:
    url: str
    title: str
    program: str
    date_modified: str | None
    crawled_at: str | None
    text: str  # body, without the Source/Date header
    words: int
    norm: str  # normalize(text), for verbatim key-fact checks


def strip_header(markdown: str) -> str:
    lines = markdown.splitlines()
    start = 0
    while start < len(lines) and (
        not lines[start].strip() or lines[start].startswith(HEADER_PREFIXES)
    ):
        start += 1
    return "\n".join(lines[start:]).strip()


def load_pages(settings: Settings) -> dict[str, Page]:
    """Every manifest page that has a processed file, in manifest order."""
    pages: dict[str, Page] = {}
    for record in read_manifest(settings.manifest_path):
        path = settings.processed_dir / record["program"] / f"{slug_for(record['url'])}.md"
        if not path.exists():
            continue
        text = strip_header(path.read_text(encoding="utf-8"))
        pages[record["url"]] = Page(
            url=record["url"],
            title=record["title"],
            program=record["program"],
            date_modified=record.get("date_modified"),
            crawled_at=record.get("crawled_at"),
            text=text,
            words=len(text.split()),
            norm=normalize(text),
        )
    return pages


def eligible_pages(pages: Iterable[Page], settings: Settings) -> list[Page]:
    cfg = settings.eval
    return [
        p
        for p in pages
        if cfg.draft_min_page_words <= p.words <= cfg.draft_max_page_words
        and PLACEHOLDER_MARKER not in p.text
    ]


# --- Sampling --------------------------------------------------------------------


@dataclass(frozen=True)
class Task:
    """One API call: ``count`` questions of one category from ``pages``."""

    category: Category
    count: int
    pages: tuple[Page, ...] = ()


def _directory(url: str) -> str:
    return url.rsplit("/", 1)[0]


def related(a: Page, b: Page) -> bool:
    """Same program and either siblings or parent/child (a page and its sub-pages)."""
    if a.program != b.program or a.url == b.url:
        return False
    stem_a, stem_b = a.url.removesuffix(".html"), b.url.removesuffix(".html")
    return (
        _directory(a.url) == _directory(b.url)
        or b.url.startswith(stem_a + "/")
        or a.url.startswith(stem_b + "/")
    )


def tokens(text: str) -> list[str]:
    return [w for w in WORD_RE.findall(normalize(text)) if len(w) > 2]


def tfidf_vectors(pages: Sequence[Page]) -> dict[str, dict[str, float]]:
    """Sparse, L2-normalised TF-IDF per page URL (pure Python; ~130 pages)."""
    counts = {p.url: Counter(tokens(p.text)) for p in pages}
    df: Counter[str] = Counter()
    for c in counts.values():
        df.update(c.keys())
    n = len(pages)
    vectors: dict[str, dict[str, float]] = {}
    for url, c in counts.items():
        vec = {t: (1 + math.log(f)) * math.log(n / df[t]) for t, f in c.items() if df[t] < n}
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        vectors[url] = {t: v / norm for t, v in vec.items()}
    return vectors


def cosine(a: Mapping[str, float], b: Mapping[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(t, 0.0) for t, v in a.items())


def _round_robin[T](by_program: Mapping[str, list[T]], n: int, rng: random.Random) -> list[T]:
    """Take ``n`` items cycling through programs (random order, random within each)."""
    order = sorted(by_program)
    rng.shuffle(order)
    queues = {}
    for program in order:
        items = list(by_program[program])
        rng.shuffle(items)
        queues[program] = items
    out: list[T] = []
    while len(out) < n and any(queues.values()):
        for program in order:
            if queues[program] and len(out) < n:
                out.append(queues[program].pop())
    return out


def _group(pages: Iterable[Page]) -> dict[str, list[Page]]:
    grouped: dict[str, list[Page]] = defaultdict(list)
    for p in sorted(pages, key=lambda p: p.url):
        grouped[p.program].append(p)
    return grouped


def plan_tasks(
    pages: Sequence[Page], mix: Mapping[str, int], seed: int, settings: Settings
) -> list[Task]:
    """Deterministic for a given corpus, mix and seed. Scarce categories pick first and
    pages are not reused across tasks unless a pool runs out."""
    unknown = set(mix) - set(CATEGORIES)
    if unknown:
        raise ValueError(f"unknown categories in draft_mix: {sorted(unknown)}")
    rng = random.Random(seed)
    pool = sorted(eligible_pages(pages, settings), key=lambda p: p.url)
    used: set[str] = set()
    tasks: dict[Category, list[Task]] = {c: [] for c in CATEGORIES}

    def take_pages(n: int, keep: Callable[[Page], bool] = lambda p: True) -> list[Page]:
        fresh = [p for p in pool if p.url not in used and keep(p)]
        picked = _round_robin(_group(fresh), n, rng)
        if len(picked) < n:  # pool exhausted: reuse pages rather than under-deliver
            reuse = [p for p in pool if p.url in used and keep(p) and p not in picked]
            picked += _round_robin(_group(reuse), n - len(picked), rng)
        used.update(p.url for p in picked)
        return picked

    vectors = tfidf_vectors(pool)

    n = mix.get("confusable", 0)
    if n:
        scored = sorted(
            (
                (cosine(vectors[a.url], vectors[b.url]), a.url, b.url, a, b)
                for i, a in enumerate(pool)
                for b in pool[i + 1 :]
                if a.program != b.program
            ),
            reverse=True,
        )
        window = [(a, b) for _, _, _, a, b in scored[: max(4 * n, 12)]]
        rng.shuffle(window)
        for a, b in window:
            if len(tasks["confusable"]) == n:
                break
            if a.url in used or b.url in used:
                continue
            used.update((a.url, b.url))
            tasks["confusable"].append(Task("confusable", 1, (a, b)))

    n = mix.get("multi_page", 0)
    if n:
        # Related pages (siblings or parent/child), keeping each program's closest pairs
        # by content so the question has something real to span.
        free = [p for p in pool if p.url not in used]
        by_program: dict[str, list[tuple[float, Page, Page]]] = defaultdict(list)
        for i, a in enumerate(free):
            for b in free[i + 1 :]:
                if related(a, b):
                    by_program[a.program].append((cosine(vectors[a.url], vectors[b.url]), a, b))
        closest = {
            program: [
                (a, b)
                for _, a, b in sorted(rows, key=lambda r: -r[0])[:MULTI_PAGE_PAIRS_PER_PROGRAM]
            ]
            for program, rows in by_program.items()
        }
        for a, b in _round_robin(closest, 4 * n, rng):
            if len(tasks["multi_page"]) == n:
                break
            if a.url in used or b.url in used:
                continue
            used.update((a.url, b.url))
            tasks["multi_page"].append(Task("multi_page", 1, (a, b)))

    n = mix.get("time_sensitive", 0)
    for page in take_pages(n, keep=lambda p: bool(MONEY_RE.search(p.text))):
        tasks["time_sensitive"].append(Task("time_sensitive", 1, (page,)))

    for page in take_pages(mix.get("unanswerable_in_domain", 0)):
        tasks["unanswerable_in_domain"].append(Task("unanswerable_in_domain", 1, (page,)))

    for page in take_pages(mix.get("single_page", 0)):
        tasks["single_page"].append(Task("single_page", 1, (page,)))

    n = mix.get("unanswerable_out_of_scope", 0)
    if n:
        tasks["unanswerable_out_of_scope"].append(Task("unanswerable_out_of_scope", n))

    return [t for c in CATEGORIES for t in tasks[c]]


# --- Prompting -------------------------------------------------------------------


def render_user_message(task: Task, all_pages: Iterable[Page]) -> str:
    parts = [f"<category>{task.category}</category>", f"<count>{task.count}</count>"]
    if task.category == "unanswerable_out_of_scope":
        titles = sorted({f"{p.program}: {p.title}" for p in all_pages})
        parts.append("<titles>\n" + "\n".join(titles) + "\n</titles>")
    else:
        blocks = []
        for p in task.pages:
            attrs = {
                "url": p.url,
                "title": p.title,
                "program": p.program,
                "date_modified": p.date_modified or "null",
            }
            head = " ".join(f"{k}={quoteattr(v)}" for k, v in attrs.items())
            blocks.append(f"<page {head}>\n{p.text}\n</page>")
        parts.append("<pages>\n" + "\n".join(blocks) + "\n</pages>")
    return "\n".join(parts)


def build_request(system: str, user_message: str, settings: Settings) -> dict[str, Any]:
    profile = settings.answer.models[settings.eval.draft_model]
    return {
        "model": settings.eval.draft_model,
        "max_tokens": settings.eval.draft_max_tokens,
        "system": [{"type": "text", "text": system}],
        "messages": [{"role": "user", "content": user_message}],
        "thinking": {"type": profile.thinking},
        "output_config": {
            "effort": profile.effort,
            "format": {"type": "json_schema", "schema": draft_schema()},
        },
    }


def request_key(system: str, user_message: str, settings: Settings) -> str:
    profile = settings.answer.models[settings.eval.draft_model]
    return cache_key(
        {
            "kind": "eval_draft",
            "schema_version": DRAFT_SCHEMA_VERSION,
            "model": settings.eval.draft_model,
            "system": system,
            "user": user_message,
            "max_tokens": settings.eval.draft_max_tokens,
            "effort": profile.effort,
            "thinking": profile.thinking,
        }
    )


def parse_response(raw: dict[str, Any]) -> tuple[DraftResponse | None, str]:
    """Raw API response -> (parsed, status). Never raises."""
    stop = raw.get("stop_reason")
    if stop == "refusal":
        return None, STATUS_REFUSAL
    if stop == "max_tokens":
        return None, STATUS_MAX_TOKENS
    text = "".join(b.get("text", "") for b in raw.get("content", []) if b.get("type") == "text")
    try:
        return DraftResponse.model_validate_json(text), STATUS_OK
    except ValidationError:
        return None, STATUS_INVALID


# --- Candidates ------------------------------------------------------------------


def assemble(task: Task, drafted: DraftedQuestion, cand_id: str) -> Candidate:
    source_urls = [p.url for p in task.pages]
    if task.category in ("single_page", "time_sensitive"):
        gold = source_urls[:1]
    elif task.category == "multi_page":
        gold = list(source_urls)
    elif task.category == "confusable":
        gold = list(drafted.gold_urls)  # the model chooses; check_candidate verifies
    else:
        gold = []
    notes = drafted.notes.strip()
    if task.category == "time_sensitive" and task.pages:
        p = task.pages[0]
        notes = (
            f"{notes} Figures as of page date_modified {p.date_modified} (crawled {p.crawled_at})."
        )
    return Candidate(
        id=cand_id,
        question=drafted.question.strip(),
        category=task.category,
        answerable=task.category not in UNANSWERABLE,
        key_facts=[f.strip() for f in drafted.key_facts],
        gold_urls=gold,
        source_urls=source_urls,
        notes=notes.strip(),
    )


def check_candidate(c: Candidate, pages: Mapping[str, Page]) -> list[str]:
    """Hard problems: the candidate must not be accepted until these are fixed."""
    problems: list[str] = []
    if len(c.question.split()) < 3:
        problems.append("question is too short")
    if c.answerable == (c.category in UNANSWERABLE):
        problems.append(f"answerable={c.answerable} contradicts category {c.category}")
    for url in c.source_urls:
        if url not in pages:
            problems.append(f"source_url not in the corpus: {url}")

    if c.category in UNANSWERABLE:
        if c.key_facts:
            problems.append("unanswerable items must have no key_facts")
        if c.gold_urls:
            problems.append("unanswerable items must have no gold_urls")
        if c.category == "unanswerable_out_of_scope" and c.source_urls:
            problems.append("out-of-scope items must have no source_urls")
        if c.category == "unanswerable_in_domain" and not c.source_urls:
            problems.append("in-domain items need the source page they were drafted from")
        return problems

    if not 1 <= len(c.key_facts) <= MAX_KEY_FACTS:
        problems.append(f"needs 1-{MAX_KEY_FACTS} key_facts, has {len(c.key_facts)}")
    if not c.gold_urls:
        problems.append("answerable items need gold_urls")
    expected = {"single_page": 1, "time_sensitive": 1}.get(c.category)
    if expected and len(c.gold_urls) != expected:
        problems.append(f"{c.category} needs exactly {expected} gold_url")
    if c.category == "multi_page" and len(c.gold_urls) < 2:
        problems.append("multi_page needs at least 2 gold_urls")
    if c.category == "confusable" and len(c.source_urls) < 2:
        problems.append("confusable needs at least 2 source_urls (the decoy is one of them)")
    for url in c.gold_urls:
        if url not in pages:
            problems.append(f"gold_url not in the corpus: {url}")
        if c.source_urls and url not in c.source_urls:
            problems.append(f"gold_url not among the source pages: {url}")

    gold_norms = {u: pages[u].norm for u in c.gold_urls if u in pages}
    for fact in c.key_facts:
        needle = normalize(fact)
        if not needle or not any(needle in text for text in gold_norms.values()):
            problems.append(f"key fact not verbatim on any gold page: {fact!r}")
    if c.category == "multi_page":
        for url, text in gold_norms.items():
            if not any(normalize(f) in text for f in c.key_facts):
                problems.append(f"no key fact comes from gold page {url}")
    return problems


def _word_set(question: str) -> frozenset[str]:
    return frozenset(WORD_RE.findall(normalize(question)))


def jaccard(a: str, b: str) -> float:
    wa, wb = _word_set(a), _word_set(b)
    return len(wa & wb) / len(wa | wb) if wa | wb else 0.0


def near_duplicates(
    questions: Mapping[str, str], others: Mapping[str, str], threshold: float
) -> dict[str, list[str]]:
    """id -> ids (from ``questions`` itself or ``others``) it is a near-duplicate of."""
    found: dict[str, list[str]] = {}
    for qid, text in questions.items():
        hits = [
            oid
            for oid, other in {**questions, **others}.items()
            if oid != qid and jaccard(text, other) >= threshold
        ]
        if hits:
            found[qid] = sorted(hits)
    return found


def load_candidates(path: Path) -> list[Candidate]:
    return [
        Candidate.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def dump_candidate(c: Candidate) -> str:
    return json.dumps(c.model_dump(), ensure_ascii=False)


def write_candidates(path: Path, candidates: Sequence[Candidate]) -> None:
    order = {c: i for i, c in enumerate(CATEGORIES)}
    ordered = sorted(candidates, key=lambda c: (order[c.category], c.id))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(dump_candidate(c) + "\n" for c in ordered), encoding="utf-8")


def existing_questions(settings: Settings) -> dict[str, str]:
    """Questions already in the retrieval dataset and the answer eval set, for dedup."""
    out: dict[str, str] = {}
    for path in (
        settings.eval_dir / settings.eval.retrieval_dataset,
        settings.eval_set_path,
    ):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                out[f"{path.name}:{row['id']}"] = row["question"]
    return out


# --- Running ---------------------------------------------------------------------


class MessagesClient(Protocol):
    @property
    def messages(self) -> Any: ...


@dataclass
class DraftRun:
    candidates: list[Candidate] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)  # "<category>: <status>"
    shortfalls: list[str] = field(default_factory=list)
    calls: int = 0
    cache_hits: int = 0
    usage: Usage = field(default_factory=Usage)


def _add(a: Usage, b: Usage) -> Usage:
    return Usage(
        a.input_tokens + b.input_tokens,
        a.output_tokens + b.output_tokens,
        a.cache_creation_input_tokens + b.cache_creation_input_tokens,
        a.cache_read_input_tokens + b.cache_read_input_tokens,
    )


def estimate(
    tasks: Sequence[Task], all_pages: Sequence[Page], system: str, settings: Settings
) -> tuple[int, float]:
    """(API calls needed, upper-bound USD): uncached tasks only, input from characters,
    output at the max_tokens ceiling."""
    cache = ResponseCache(settings.eval_cache_dir)
    calls, usd = 0, 0.0
    for task in tasks:
        user = render_user_message(task, all_pages)
        if cache.get(request_key(system, user, settings)) is not None:
            continue
        calls += 1
        usage = Usage(
            input_tokens=int((len(system) + len(user)) / CHARS_PER_TOKEN_ESTIMATE),
            output_tokens=settings.eval.draft_max_tokens,
        )
        usd += cost_usd(usage, settings.eval.draft_model, settings.answer)
    return calls, usd


def run_tasks(
    tasks: Sequence[Task],
    all_pages: Sequence[Page],
    *,
    system: str,
    settings: Settings,
    client: MessagesClient | None = None,
) -> DraftRun:
    """Call (or replay from cache) each task. API/network errors propagate; the calls
    that completed are already cached, so a re-run resumes without re-spending."""
    cache = ResponseCache(settings.eval_cache_dir)
    run = DraftRun()
    per_category: Counter[str] = Counter()
    for task in tasks:
        user = render_user_message(task, all_pages)
        key = request_key(system, user, settings)
        raw = cache.get(key)
        if raw is not None:
            run.cache_hits += 1
        else:
            client = client or make_anthropic_client(settings)
            message = client.messages.create(**build_request(system, user, settings))
            raw = message.to_dict()
            cache.put(key, raw)
            run.calls += 1
            run.usage = _add(run.usage, usage_from_raw(raw))
        parsed, status = parse_response(raw)
        if parsed is None:
            run.failures.append(f"{task.category}: {status}")
            continue
        if len(parsed.questions) < task.count:
            run.shortfalls.append(
                f"{task.category}: asked for {task.count}, got {len(parsed.questions)}"
            )
        for drafted in parsed.questions[: task.count]:
            per_category[task.category] += 1
            cand_id = f"{task.category}-{per_category[task.category]:02d}"
            run.candidates.append(assemble(task, drafted, cand_id))
    return run


# --- CLI -------------------------------------------------------------------------


def describe_task(task: Task) -> str:
    if not task.pages:
        return f"{task.category} x{task.count}: (titles of the whole corpus)"
    pages = " + ".join(f"[{p.program}] {p.title} ({p.words}w)" for p in task.pages)
    return f"{task.category}: {pages}"


def summarize(
    run: DraftRun, pages: Mapping[str, Page], settings: Settings, mix: Mapping[str, int]
) -> list[str]:
    lines = ["", "Candidates by category (drafted / target):"]
    drafted = Counter(c.category for c in run.candidates)
    for category in CATEGORIES:
        lines.append(f"  {category:<28}{drafted[category]:>3} / {mix.get(category, 0)}")
    lines.append(f"  {'total':<28}{len(run.candidates):>3} / {sum(mix.values())}")
    flagged = {c.id: check_candidate(c, pages) for c in run.candidates}
    flagged = {cid: p for cid, p in flagged.items() if p}
    dupes = near_duplicates(
        {c.id: c.question for c in run.candidates},
        existing_questions(settings),
        settings.eval.duplicate_jaccard,
    )
    lines.append(
        f"\nAuto-checks: {len(flagged)} candidate(s) with problems, {len(dupes)} near-duplicate(s)"
    )
    for cid, problems in flagged.items():
        lines.append(f"  {cid}: " + "; ".join(problems))
    for cid, others in dupes.items():
        lines.append(f"  {cid}: near-duplicate of {', '.join(others)}")
    for item in run.failures + run.shortfalls:
        lines.append(f"  WARNING {item}")
    return lines


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(
        prog="sda-eval-draft", description="draft candidate eval questions (Haiku)"
    )
    parser.add_argument("--seed", type=int, default=settings.eval.draft_seed)
    parser.add_argument("--out", type=Path, default=settings.candidates_path)
    parser.add_argument("--dry-run", action="store_true", help="show the sampled pages; no API")
    parser.add_argument("--yes", action="store_true", help="skip the spend confirmation")
    parser.add_argument("--force", action="store_true", help="overwrite an existing --out file")
    args = parser.parse_args(argv)

    if settings.eval.draft_model not in settings.answer.models:
        parser.error(f"no profile for {settings.eval.draft_model!r} in settings.answer.models")
    pages = load_pages(settings)
    if not pages:
        print(f"error: no processed pages under {settings.processed_dir} (run the crawler)")
        return 2
    mix = settings.eval.draft_mix
    tasks = plan_tasks(list(pages.values()), mix, args.seed, settings)

    print(f"{len(tasks)} drafting calls over {len(pages)} corpus pages (seed {args.seed}):")
    for task in tasks:
        print(f"  - {describe_task(task)}")
    if args.dry_run:
        return 0
    if args.out.exists() and not args.force:
        print(f"error: {args.out} exists (review decisions refer to its ids); use --force")
        return 2

    try:
        system = load_system_prompt(settings.eval.draft_prompt_path)
    except SystemPromptNotReady as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    all_pages = list(pages.values())
    calls, usd = estimate(tasks, all_pages, system, settings)
    cad = usd_to_cad(usd, settings.answer)
    print(
        f"\n{calls} API call(s) to {settings.eval.draft_model}, {len(tasks) - calls} cached. "
        f"Estimated cost: <= CA${cad:.4f} (CA${settings.answer.usd_cad:.2f}/US$)"
    )
    if calls and not args.yes and input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
        print("Cancelled; nothing spent.")
        return 1

    try:
        run = run_tasks(tasks, all_pages, system=system, settings=settings)
    except anthropic.APIError as exc:
        print(
            f"Anthropic API error: {exc}\nCompleted calls are cached; re-run to resume.",
            file=sys.stderr,
        )
        return 1
    write_candidates(args.out, run.candidates)
    actual = usd_to_cad(
        cost_usd(run.usage, settings.eval.draft_model, settings.answer), settings.answer
    )
    for line in summarize(run, pages, settings, mix):
        print(line)
    print(
        f"\n{run.calls} API call(s), {run.cache_hits} from cache. Actual cost: CA${actual:.4f}"
        f"\nwrote {args.out}. Next: uv run sda-eval-review"
    )
    return 0 if not run.failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
