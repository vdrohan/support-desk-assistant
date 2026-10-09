"""uv run sda ask "question" [--program ei] [-k 5] [--model X]
uv run sda ask --smoke        # 3 fixed questions against the real API, with confirmation
"""

import argparse
import sys

import anthropic
from qdrant_client import QdrantClient

from support_desk_assistant.answer.pipeline import (
    Outcome,
    Prepared,
    SystemPromptNotReady,
    estimate_cost_usd,
    execute,
    prepare,
)
from support_desk_assistant.config import Settings, get_settings
from support_desk_assistant.ingest.embed import Embedder, make_voyage_client


def format_outcome(outcome: Outcome) -> str:
    by_id = {h.chunk_id: h for h in outcome.hits}
    lines = []
    if outcome.result.abstained:
        lines.append("ABSTAINED")
        if outcome.result.abstain_reason:
            lines.append(f"Reason: {outcome.result.abstain_reason}")
    else:
        lines.append(outcome.result.answer)
    if outcome.result.citations:
        lines.append("\nCitations:")
        for n, cid in enumerate(outcome.result.citations, 1):
            hit = by_id.get(cid)
            lines.append(
                f"  [{n}] {hit.title} - {hit.url}"
                if hit
                else f"  [{n}] {cid} (NOT in retrieved set)"
            )
    v = outcome.validation
    if v is None:
        lines.append("\nValidation: SKIPPED (validate_answer is not implemented yet)")
    elif not v.ok:
        lines.append(f"\nValidation FAILED: {', '.join(v.failed_rules)}")
    if outcome.status not in ("ok", "gate"):
        lines.append(f"Generation status: {outcome.status}")
    note = " (response cache hit)" if outcome.cache_hit else ""
    if outcome.gate != "pass":
        note = " (gate: no API call)"
    lines.append(f"\nCost: CA${outcome.cost_cad:.4f}{note}")
    return "\n".join(lines)


def run_smoke(
    settings: Settings, embedder: Embedder, qdrant: QdrantClient, model: str | None
) -> int:
    questions = settings.answer.smoke_questions
    prepared: list[Prepared] = [
        prepare(q, settings=settings, embedder=embedder, qdrant=qdrant, model=model)
        for q in questions
    ]
    calls = sum(p.will_call_api for p in prepared)
    est_usd = sum(estimate_cost_usd(p, settings) for p in prepared)
    est_cad = est_usd * settings.answer.usd_cad
    print(f"Smoke test: {len(questions)} questions, model {prepared[0].model}")
    for p in prepared:
        how = "API call" if p.will_call_api else ("gate abstain" if p.gate != "pass" else "cached")
        print(f"  - {p.question}  [{how}]")
    print(
        f"{calls} API call(s). Estimated cost: <= CA${est_cad:.4f} "
        f"(upper bound: input estimated from characters, output at the max_tokens ceiling)"
    )
    if calls and input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
        print("Cancelled; nothing spent.")
        return 1

    total_cad = 0.0
    for p in prepared:
        outcome = execute(p, settings=settings)
        print(f"\n=== {p.question}\n{format_outcome(outcome)}")
        total_cad += outcome.cost_cad
    print(f"\nActual total cost: CA${total_cad:.4f}")
    return 0


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(prog="sda")
    sub = parser.add_subparsers(dest="command", required=True)
    ask = sub.add_parser("ask", help="answer a question from the indexed corpus")
    ask.add_argument("question", nargs="?")
    ask.add_argument("--program", choices=sorted(settings.crawl.seeds))
    ask.add_argument("-k", type=int, default=settings.retrieval.top_k)
    ask.add_argument(
        "--model",
        help=f"override the model (e.g. the comparison model {settings.answer.comparison_model})",
    )
    ask.add_argument("--smoke", action="store_true", help="run the fixed smoke questions")
    args = parser.parse_args(argv)
    if args.smoke == bool(args.question):
        parser.error("give a question, or --smoke (not both)")

    embedder = Embedder(make_voyage_client(settings), settings.embed)
    qdrant = QdrantClient(url=settings.qdrant_url, timeout=int(settings.index.timeout_s))
    try:
        if args.smoke:
            return run_smoke(settings, embedder, qdrant, args.model)
        prepared = prepare(
            args.question,
            settings=settings,
            embedder=embedder,
            qdrant=qdrant,
            program=args.program,
            k=args.k,
            model=args.model,
        )
        print(format_outcome(execute(prepared, settings=settings)))
    except SystemPromptNotReady as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except anthropic.APIError as exc:
        print(f"Anthropic API error (nothing cached or logged): {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
