"""Review drafted candidates next to the page text that supports them.

    uv run sda-eval-review [--category single_page] [--status]
    uv run sda-eval-review --export            # data/eval/review.html, for a phone
    uv run sda-eval-review --apply decisions.txt

Each candidate is shown with the key facts highlighted in the gold page's text. Accepted
items are appended to ``data/eval/eval_set_v1.jsonl`` with a final ``ev-NNN`` id; rejects
and edits are remembered in ``data/eval/review_state.json`` so a session can be resumed.
An accept is refused while ``check_candidate`` reports problems, so a key fact that isn't
on the gold page can't reach the eval set. To undo an accept, delete the line from the
eval set by hand. No API calls, no network (docs/decisions.md #19).

The phone path: ``--export`` writes one offline HTML page of the pending candidates with
Accept / Reject buttons; its "Copy decisions" button yields ``<candidate id> a|r`` lines,
which ``--apply`` feeds through the same ``accept`` / ``reject`` the terminal uses (same
grounding check, same state file). ``--apply`` is idempotent: running it again changes
nothing, and a decision already on record is never overwritten.
"""

import argparse
import hashlib
import json
import math
import os
import re
import shlex
import subprocess
import sys
import tempfile
import textwrap
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from html import escape
from pathlib import Path

from pydantic import ValidationError

from support_desk_assistant.config import Settings, get_settings
from support_desk_assistant.evals.draft_questions import (
    CATEGORIES,
    UNANSWERABLE,
    Candidate,
    Page,
    check_candidate,
    dump_candidate,
    load_candidates,
    load_pages,
    near_duplicates,
    tokens,
)
from support_desk_assistant.evals.retrieval import normalize
from support_desk_assistant.evals.review_html import (
    CardView,
    ExcerptView,
    MatchView,
    render_review_page,
)

BLOCK_SPLIT = re.compile(r"\n\s*\n")
PROMPT = "[a]ccept  [e]dit  [r]eject  [s]kip  [f]ull page  [q]uit > "


# --- State -----------------------------------------------------------------------


@dataclass
class ReviewState:
    # candidate id -> {"action": "accept" | "reject", "eval_id": str | None}
    decisions: dict[str, dict] = field(default_factory=dict)
    # candidate id -> the edited candidate (so an edit survives quitting before accept)
    edits: dict[str, dict] = field(default_factory=dict)


def load_state(path: Path) -> ReviewState:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return ReviewState()
    return ReviewState(decisions=raw.get("decisions", {}), edits=raw.get("edits", {}))


def save_state(path: Path, state: ReviewState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"decisions": state.decisions, "edits": state.edits}, indent=1) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def apply_edits(candidates: Sequence[Candidate], state: ReviewState) -> list[Candidate]:
    out = []
    for c in candidates:
        edited = state.edits.get(c.id)
        out.append(Candidate.model_validate({**edited, "id": c.id}) if edited else c)
    return out


def load_eval_set(path: Path) -> list[Candidate]:
    return load_candidates(path) if path.exists() else []


def next_eval_id(existing_ids: Iterable[str]) -> str:
    numbers = [int(m.group(1)) for i in existing_ids if (m := re.fullmatch(r"ev-(\d+)", i))]
    return f"ev-{max(numbers, default=0) + 1:03d}"


def pending(candidates: Sequence[Candidate], state: ReviewState) -> list[Candidate]:
    return [c for c in candidates if c.id not in state.decisions]


# --- Display ---------------------------------------------------------------------


def fact_pattern(fact: str) -> re.Pattern[str] | None:
    """Whitespace-flexible, case-insensitive matcher for a key fact (None if it's blank)."""
    words = fact.split()
    if not words:
        return None
    return re.compile(r"\s+".join(re.escape(w) for w in words), re.IGNORECASE)


def highlight(text: str, facts: Iterable[str], color: bool) -> str:
    """Mark each key fact where it appears (whitespace-flexible, case-insensitive)."""
    on, off = ("\033[1;43m", "\033[0m") if color else ("»", "«")
    for fact in facts:
        pattern = fact_pattern(fact)
        if pattern is None:
            continue
        text = pattern.sub(lambda m: f"{on}{m.group(0)}{off}", text)
    return text


def highlight_html(text: str, facts: Iterable[str]) -> str:
    """``text`` HTML-escaped, with each key fact wrapped in ``<mark>``. Matches are found
    on the raw text and escaped piece by piece, so a fact can't land inside an entity."""
    spans = sorted(
        m.span() for f in facts if (p := fact_pattern(f)) is not None for m in p.finditer(text)
    )
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    out, pos = [], 0
    for start, end in merged:
        out += [escape(text[pos:start]), "<mark>", escape(text[start:end]), "</mark>"]
        pos = end
    out.append(escape(text[pos:]))
    return "".join(out)


def relevant_excerpt(text: str, facts: Sequence[str], *, context: int, fallback_chars: int) -> str:
    """The blank-line blocks that contain a key fact, plus ``context`` neighbours each
    side. With no fact on the page (unanswerable items) the head of the page."""
    blocks = [b.strip() for b in BLOCK_SPLIT.split(text) if b.strip()]
    needles = [normalize(f) for f in facts if f.strip()]
    hit = {i for i, b in enumerate(blocks) if any(n in normalize(b) for n in needles)}
    if not hit:
        head = text[:fallback_chars]
        return head + ("\n[...]" if len(text) > fallback_chars else "")
    keep = sorted(
        {j for i in hit for j in range(i - context, i + context + 1) if 0 <= j < len(blocks)}
    )
    parts: list[str] = []
    for n, i in enumerate(keep):
        if n and i != keep[n - 1] + 1:
            parts.append("[...]")
        parts.append(blocks[i])
    if keep[0] > 0:
        parts.insert(0, "[...]")
    if keep[-1] < len(blocks) - 1:
        parts.append("[...]")
    return "\n\n".join(parts)


class KeywordIndex:
    """Offline answer to 'is this answered somewhere else in the corpus?': pages ranked
    by the summed IDF of the question's words they contain."""

    def __init__(self, pages: Iterable[Page]) -> None:
        self.pages = list(pages)
        self.token_sets = {p.url: set(tokens(p.text)) for p in self.pages}
        df: Counter[str] = Counter()
        for s in self.token_sets.values():
            df.update(s)
        n = len(self.pages) or 1
        self.idf = {t: math.log(1 + n / c) for t, c in df.items()}

    def matches(
        self, question: str, *, exclude: Iterable[str] = (), n: int = 3
    ) -> list[tuple[Page, float, str]]:
        """(page, score, best block) for the top ``n`` pages."""
        query = {t for t in tokens(question) if t in self.idf}
        skip = set(exclude)
        scored = []
        for p in self.pages:
            if p.url in skip:
                continue
            score = sum(self.idf[t] for t in query & self.token_sets[p.url])
            if score:
                scored.append((score, p))
        scored.sort(key=lambda sp: (-sp[0], sp[1].url))
        out = []
        for score, p in scored[:n]:
            blocks = [b.strip() for b in BLOCK_SPLIT.split(p.text) if b.strip()]
            best = max(
                blocks,
                key=lambda b: sum(self.idf.get(t, 0.0) for t in query & set(tokens(b))),
                default="",
            )
            out.append((p, score, best))
        return out


def _wrap(text: str, indent: str = "  ", width: int = 100) -> str:
    return "\n".join(
        textwrap.fill(line, width=width, initial_indent=indent, subsequent_indent=indent)
        if line.strip()
        else ""
        for line in text.splitlines()
    )


@dataclass
class ReviewContext:
    settings: Settings
    pages: Mapping[str, Page]
    index: KeywordIndex
    eval_path: Path
    state_path: Path
    state: ReviewState
    color: bool = False


def supporting_pages(c: Candidate, ctx: ReviewContext) -> list[tuple[str, Page]]:
    """(role, page) for the gold pages, then the other source pages; URLs not in the corpus
    are skipped."""
    urls = [*c.gold_urls, *(u for u in c.source_urls if u not in c.gold_urls)]
    return [
        ("GOLD" if u in c.gold_urls else "SOURCE (not gold)", ctx.pages[u])
        for u in urls
        if u in ctx.pages
    ]


def render_card(
    c: Candidate,
    position: tuple[int, int],
    ctx: ReviewContext,
    *,
    full: bool = False,
    duplicates: Sequence[str] = (),
) -> str:
    cfg = ctx.settings.eval
    problems = check_candidate(c, ctx.pages)
    lines = [
        "",
        "=" * 100,
        f"[{position[0]}/{position[1]}] {c.id} · {c.category} · "
        f"{'answerable' if c.answerable else 'should abstain'}",
        "",
        f"Q: {c.question}",
    ]
    if c.key_facts:
        lines.append("Key facts:")
        lines += [f"  {n}. {f}" for n, f in enumerate(c.key_facts, 1)]
    if c.gold_urls:
        lines.append("Gold: " + ", ".join(c.gold_urls))
    if c.notes:
        lines.append(f"Notes: {c.notes}")
    lines.append(
        "Auto-check: OK"
        if not problems
        else "Auto-check PROBLEMS (accept is blocked):\n" + "\n".join(f"  ! {p}" for p in problems)
    )
    if duplicates:
        lines.append("Near-duplicate of: " + ", ".join(duplicates))

    shown: list[str] = []
    for role, page in supporting_pages(c, ctx):
        shown.append(page.url)
        lines += [
            "",
            f"--- {role}: [{page.program}] {page.title} · modified {page.date_modified}",
            f"    {page.url}",
        ]
        body = (
            page.text
            if full
            else relevant_excerpt(
                page.text,
                c.key_facts,
                context=cfg.review_context_blocks,
                fallback_chars=cfg.review_excerpt_chars,
            )
        )
        lines.append(_wrap(highlight(body, c.key_facts, ctx.color)))

    if c.category in UNANSWERABLE:
        lines += ["", "--- Closest pages elsewhere in the corpus (is it answered anywhere?)"]
        found = ctx.index.matches(c.question, exclude=shown, n=3)
        for page, score, block in found:
            lines += [f"  [{page.program}] {page.title} (score {score:.1f})", f"    {page.url}"]
            lines.append(_wrap(block[:400], indent="    "))
        if not found:
            lines.append("  (no keyword overlap)")
    return "\n".join(lines)


# --- Phone page ------------------------------------------------------------------


def build_card_view(c: Candidate, ctx: ReviewContext) -> CardView:
    cfg = ctx.settings.eval
    pages = supporting_pages(c, ctx)
    excerpts = [
        ExcerptView(
            role=role,
            program=page.program,
            title=page.title,
            url=page.url,
            date_modified=page.date_modified,
            body_html=highlight_html(
                relevant_excerpt(
                    page.text,
                    c.key_facts,
                    context=cfg.review_context_blocks,
                    fallback_chars=cfg.review_excerpt_chars,
                ),
                c.key_facts,
            ),
        )
        for role, page in pages
    ]
    matches = []
    if c.category in UNANSWERABLE:
        found = ctx.index.matches(c.question, exclude=[p.url for _, p in pages], n=3)
        matches = [MatchView(p.program, p.title, p.url, score, b[:400]) for p, score, b in found]
    fingerprint = hashlib.sha256(
        json.dumps([c.question, c.key_facts, c.gold_urls], ensure_ascii=False).encode()
    ).hexdigest()[:10]
    return CardView(
        id=c.id,
        question=c.question,
        category=c.category,
        answerable=c.answerable,
        key_facts=c.key_facts,
        notes=c.notes,
        fingerprint=fingerprint,
        problems=check_candidate(c, ctx.pages),
        excerpts=excerpts,
        matches=matches,
    )


def export_html(queue: Sequence[Candidate], ctx: ReviewContext, path: Path) -> None:
    page = render_review_page([build_card_view(c, ctx) for c in queue])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(page, encoding="utf-8")
    tmp.replace(path)


# --- Decisions -------------------------------------------------------------------


def edit_in_editor(c: Candidate) -> Candidate:
    """Open the candidate's JSON in $VISUAL/$EDITOR; the id can't be changed."""
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", prefix=f"{c.id}-", delete=False, encoding="utf-8"
    ) as f:
        json.dump(c.model_dump(), f, indent=2, ensure_ascii=False)
        path = Path(f.name)
    try:
        subprocess.run([*shlex.split(editor), str(path)], check=True)
        edited = Candidate.model_validate_json(path.read_text(encoding="utf-8"))
    finally:
        path.unlink(missing_ok=True)
    return edited.model_copy(update={"id": c.id})


def accept(c: Candidate, ctx: ReviewContext) -> str:
    """Append to the eval set under a new ev-NNN id and record the decision."""
    problems = check_candidate(c, ctx.pages)
    if problems:
        raise ValueError("; ".join(problems))
    existing = [i.id for i in load_eval_set(ctx.eval_path)]
    eval_id = next_eval_id(existing)
    ctx.eval_path.parent.mkdir(parents=True, exist_ok=True)
    with ctx.eval_path.open("a", encoding="utf-8") as f:
        f.write(dump_candidate(c.model_copy(update={"id": eval_id})) + "\n")
    ctx.state.decisions[c.id] = {"action": "accept", "eval_id": eval_id}
    save_state(ctx.state_path, ctx.state)
    return eval_id


def reject(c: Candidate, ctx: ReviewContext) -> None:
    ctx.state.decisions[c.id] = {"action": "reject", "eval_id": None}
    save_state(ctx.state_path, ctx.state)


ACTIONS = {"a": "accept", "accept": "accept", "r": "reject", "reject": "reject"}


def parse_decisions(text: str) -> tuple[dict[str, str], list[str]]:
    """``<candidate id> a|r`` lines (also ``accept``/``reject``) to {id: action}, plus a
    list of problems. Blank lines and ``#`` comments are ignored. A malformed line is
    reported, not fatal. If an id appears twice the last line wins."""
    decisions: dict[str, str] = {}
    problems: list[str] = []
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        action = ACTIONS.get(parts[1].lower()) if len(parts) == 2 else None
        if action is None:
            problems.append(f"line {number}: expected '<id> a|r', got {line!r}")
            continue
        if decisions.get(parts[0], action) != action:
            problems.append(f"line {number}: {parts[0]} appears twice; using {action}")
        decisions[parts[0]] = action
    return decisions, problems


@dataclass
class ApplyReport:
    accepted: list[tuple[str, str]] = field(default_factory=list)  # (candidate id, eval id)
    rejected: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)  # already on record, same decision
    problems: list[str] = field(default_factory=list)


def apply_decisions(text: str, candidates: Sequence[Candidate], ctx: ReviewContext) -> ApplyReport:
    """Record each decision in ``text`` the way the terminal session would. Safe to run
    again: a decision already in the state file is left alone, and an accept whose
    question is already in the eval set (state file lost, interrupted run) is recorded
    against that item instead of being appended a second time."""
    decisions, problems = parse_decisions(text)
    report = ApplyReport(problems=problems)
    by_id = {c.id: c for c in candidates}
    for cid, action in decisions.items():
        recorded = ctx.state.decisions.get(cid)
        if recorded is not None:
            if recorded["action"] == action:
                report.unchanged.append(cid)
            else:
                report.problems.append(
                    f"{cid}: already recorded as {recorded['action']}, left unchanged "
                    f"(file says {action})"
                )
            continue
        candidate = by_id.get(cid)
        if candidate is None:
            report.problems.append(f"{cid}: no such candidate")
            continue
        if action == "reject":
            reject(candidate, ctx)
            report.rejected.append(cid)
            continue
        wanted = normalize(candidate.question)
        twin = next(
            (i for i in load_eval_set(ctx.eval_path) if normalize(i.question) == wanted), None
        )
        if twin is not None:
            ctx.state.decisions[cid] = {"action": "accept", "eval_id": twin.id}
            save_state(ctx.state_path, ctx.state)
            report.unchanged.append(cid)
            continue
        try:
            report.accepted.append((cid, accept(candidate, ctx)))
        except ValueError as exc:
            report.problems.append(f"{cid}: cannot accept: {exc}")
    return report


def review_loop(
    queue: Sequence[Candidate],
    ctx: ReviewContext,
    *,
    input_fn: Callable[[str], str] | None = None,
    print_fn: Callable[[str], None] = print,
    edit_fn: Callable[[Candidate], Candidate] = edit_in_editor,
) -> dict[str, int]:
    """Walk ``queue`` once. Returns counts of accepted / rejected / skipped."""
    read = input_fn or input
    counts = {"accepted": 0, "rejected": 0, "skipped": 0}
    threshold = ctx.settings.eval.duplicate_jaccard
    for number, original in enumerate(queue, 1):
        c, full = original, False
        while True:
            known = {i.id: i.question for i in load_eval_set(ctx.eval_path)}
            dupes = near_duplicates({c.id: c.question}, known, threshold).get(c.id, [])
            print_fn(render_card(c, (number, len(queue)), ctx, full=full, duplicates=dupes))
            try:
                choice = read(PROMPT).strip().lower()[:1]
            except EOFError:
                choice = "q"
            if choice == "a":
                try:
                    eval_id = accept(c, ctx)
                except ValueError as exc:
                    print_fn(f"cannot accept: {exc}")
                    continue
                print_fn(f"accepted as {eval_id}")
                counts["accepted"] += 1
            elif choice == "r":
                reject(c, ctx)
                counts["rejected"] += 1
            elif choice == "s":
                counts["skipped"] += 1
            elif choice == "f":
                full = True
                continue
            elif choice == "e":
                try:
                    c = edit_fn(c)
                except (ValidationError, subprocess.CalledProcessError, OSError) as exc:
                    print_fn(f"edit discarded: {exc}")
                else:
                    ctx.state.edits[c.id] = c.model_dump()
                    save_state(ctx.state_path, ctx.state)
                continue
            elif choice == "q":
                return counts
            else:
                print_fn("a, e, r, s, f or q")
                continue
            break
    return counts


def status_lines(
    candidates: Sequence[Candidate],
    state: ReviewState,
    eval_set: Sequence[Candidate],
    mix: Mapping[str, int],
) -> list[str]:
    by_id = {c.id: c for c in candidates}
    left = Counter(c.category for c in pending(candidates, state))
    rejected = Counter(
        by_id[i].category
        for i, d in state.decisions.items()
        if d["action"] == "reject" and i in by_id
    )
    accepted = Counter(c.category for c in eval_set)
    lines = [f"{'category':<28}{'accepted':>9}{'rejected':>9}{'pending':>8}{'target':>8}"]
    for cat in CATEGORIES:
        lines.append(
            f"{cat:<28}{accepted[cat]:>9}{rejected[cat]:>9}{left[cat]:>8}{mix.get(cat, 0):>8}"
        )
    lines.append(
        f"{'total':<28}{sum(accepted.values()):>9}{sum(rejected.values()):>9}"
        f"{sum(left.values()):>8}{sum(mix.values()):>8}"
    )
    return lines


# --- CLI -------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(
        prog="sda-eval-review", description="accept, edit or reject drafted eval questions"
    )
    parser.add_argument("--candidates", type=Path, default=settings.candidates_path)
    parser.add_argument("--eval-set", type=Path, default=settings.eval_set_path)
    parser.add_argument("--state", type=Path, default=settings.review_state_path)
    parser.add_argument("--category", choices=CATEGORIES, help="review one category only")
    parser.add_argument("--html", type=Path, default=settings.review_html_path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--status", action="store_true", help="show progress and exit")
    mode.add_argument(
        "--export",
        action="store_true",
        help="write the pending candidates to one offline HTML page (--html) and exit",
    )
    mode.add_argument(
        "--apply",
        type=Path,
        metavar="FILE",
        help="record '<candidate id> a|r' lines from FILE (from the page's Copy button)",
    )
    parser.add_argument("--no-color", action="store_true")
    args = parser.parse_args(argv)

    if not args.candidates.exists():
        print(f"error: {args.candidates} not found; run sda-eval-draft first")
        return 2
    state = load_state(args.state)
    candidates = apply_edits(load_candidates(args.candidates), state)
    eval_set = load_eval_set(args.eval_set)
    if args.status:
        print("\n".join(status_lines(candidates, state, eval_set, settings.eval.draft_mix)))
        return 0

    if args.apply and not args.apply.exists():
        print(f"error: {args.apply} not found")
        return 2

    pages = load_pages(settings)
    ctx = ReviewContext(
        settings=settings,
        pages=pages,
        index=KeywordIndex(pages.values()),
        eval_path=args.eval_set,
        state_path=args.state,
        state=state,
        color=sys.stdout.isatty() and not args.no_color,
    )
    queue = [
        c for c in pending(candidates, state) if not args.category or c.category == args.category
    ]
    if args.export:
        if not queue:
            print("nothing pending to export")
            return 0
        export_html(queue, ctx, args.html)
        print(f"wrote {len(queue)} candidate(s) to {args.html}")
        return 0
    if args.apply:
        # utf-8-sig: a BOM from a phone editor or share sheet must not eat the first id
        report = apply_decisions(args.apply.read_text(encoding="utf-8-sig"), candidates, ctx)
        print(
            f"accepted {len(report.accepted)}, rejected {len(report.rejected)}, "
            f"already recorded {len(report.unchanged)}, problems {len(report.problems)}"
        )
        for cid, eval_id in report.accepted:
            print(f"  {cid} -> {eval_id}")
        for problem in report.problems:
            print(f"  ! {problem}")
        print(
            "\n".join(
                status_lines(
                    candidates, ctx.state, load_eval_set(args.eval_set), settings.eval.draft_mix
                )
            )
        )
        return 1 if report.problems else 0
    print(f"{len(queue)} candidate(s) to review; {len(eval_set)} already in {args.eval_set.name}")
    counts = review_loop(queue, ctx)
    print(
        f"\naccepted {counts['accepted']}, rejected {counts['rejected']}, "
        f"skipped {counts['skipped']}"
    )
    print(
        "\n".join(
            status_lines(
                candidates, ctx.state, load_eval_set(args.eval_set), settings.eval.draft_mix
            )
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
