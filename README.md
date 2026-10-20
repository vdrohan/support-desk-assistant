# support-desk-assistant

A retrieval-augmented Q&A and ticket triage assistant for support desks, with an evaluation harness that measures its accuracy.

> **Status: Phase 1 in progress.** Crawl, chunk, embed, index, vector search and cited answers from the CLI (`sda ask`, with citation validation) work. Retrieval has an eval baseline, and the answer eval set v1 is in place. The answer scoring runner (`sda-eval-answers`, with per-program, abstention and citation breakdowns and judge calibration) has a baseline run scored with judge v2 (answerable pass 0.84, abstention recall 0.44). The judge has been calibrated on 50 labels written by Claude, not a person (40/40 agreement, in-sample, only one absent fact), so a human review is still pending. The HTTP API doesn't exist yet. See [Roadmap](#roadmap).

## Problem

Support agents spend much of their time finding answers that already exist in docs, past tickets and runbooks, and routing tickets to the right queue by hand. This project aims to:

1. **Answer questions** from a knowledge base, with citations to the source passages.
2. **Triage incoming tickets:** category, priority and suggested routing.
3. **Escalate** when confidence is low, instead of guessing.

And to **measure** all of this with a versioned eval set, so accuracy claims are backed by numbers.

## Architecture (planned)

```
             ┌──────────────┐  embed (Voyage AI)  ┌───────────┐
 docs ──────▶│   ingest     │────────────────────▶│  Qdrant   │
             │ chunk+embed  │       upsert        │ (vectors) │
             └──────────────┘                     └─────┬─────┘
                                                        │ top_k
 question ──▶┌──────────────┐  retrieve ◀───────────────┘
             │ FastAPI api  │
             │              │──▶ Claude: grounded answer with citations
             └──────┬───────┘
                    │ Phase 2+
                    ▼
             triage + escalation ──▶ SQLite (tickets)

 evals: fixed question set ──▶ retrieval and answer metrics
```

| Component | Choice | Why |
|---|---|---|
| API | FastAPI | Typed, async, auto OpenAPI docs |
| Vector DB | Qdrant (Docker, pinned) | Filtering, hybrid search, light to run |
| Embeddings | Voyage AI | Retrieval quality; pairs with Claude |
| Generation | Claude | Grounded answers, structured output, prompt caching |
| Ticket store | SQLite (Phase 2) | No server to run at this scale |

All AI runs through cloud APIs; there are no local models. Reasoning and rejected alternatives: [docs/decisions.md](docs/decisions.md).

**Deployment:** developed on macOS, pushed to GitHub. The deploy host, a small Ubuntu Server box reachable only over Tailscale, pulls the repo and runs it with Docker Compose. Every container port binds to `127.0.0.1`.

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 0 | Repo scaffolding: tooling, Compose with Qdrant, decision log | ✅ done |
| 1 | RAG core: corpus crawler ✅, chunk + embed + index (`sda-ingest`) ✅, vector search (`sda-search`) ✅, answers with citations (`sda ask`) ✅, eval sets in `data/eval/` ✅, retrieval eval (`sda-eval-retrieval`) ✅, answer scoring runner and judge calibration (`sda-eval-answers`) ✅ (baseline run 2 done with judge v2; judge calibrated only on Claude-written labels, human review and absent-fact labels pending), then `/ask` endpoint | 🚧 in progress |
| 2 | Triage layer: ticket classification and priority, SQLite storage, triage eval | planned |
| 3 | Escalation logic for low-confidence cases, plus a write-up with eval results | planned |

A retrieval baseline is in [`data/eval/results/`](data/eval/results/). The answer eval baseline is there too: run 1 (answer cap, decisions #24) and run 2 (judge v2, decisions #25), which supersedes it.

## Corpus

English Government of Canada pages from canada.ca for 5 citizen-service programs: Employment Insurance, Canada Pension Plan, Old Age Security, Social Insurance Number and passports. That's up to 30 pages per program, crawled within each program's URL prefix. Source: Government of Canada, canada.ca, reproduced for non-commercial use with attribution. Each page's title and URL are kept in the manifest and in every processed file. Why this corpus: [decisions #7](docs/decisions.md#7-corpus-government-of-canada-citizen-services-pages).

## Repository layout

```
src/support_desk_assistant/
  config.py      # single source of config values (pydantic-settings)
  api/           # FastAPI app
  ingest/        # crawl, chunk, embed, index (sda-ingest)
  retrieve.py    # vector search (sda-search)
  evals/         # eval harness
docs/            # decisions and, later, the write-up
compose.yaml     # Qdrant
data/
  manifest.jsonl # committed corpus definition: url, title, program, dates, hash
  raw/           # cached HTML (gitignored)
  processed/     # extracted Markdown (gitignored)
  eval/          # versioned eval datasets, judge calibration labels, committed results
tests/           # pytest, offline (no live network)
```

## Getting started

Prerequisites: [uv](https://docs.astral.sh/uv/) and, to run Qdrant, Docker.

```bash
git clone https://github.com/vdrohan/support-desk-assistant.git
cd support-desk-assistant
uv sync                              # Python 3.12, dev tools, editable package
uv run pre-commit install            # ruff and gitleaks on every commit
cp .env.example .env                 # then add your ANTHROPIC_API_KEY and VOYAGE_API_KEY
docker compose up -d qdrant          # Qdrant on 127.0.0.1:6333
```

Checks:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

### Build the corpus

```bash
uv run python -m support_desk_assistant.ingest.crawl              # all 5 programs
uv run python -m support_desk_assistant.ingest.crawl --program ei --refresh
```

The crawler respects robots.txt, sends at most 1 request per second with a descriptive User-Agent, and caches raw HTML so re-runs don't download again. It writes `data/manifest.jsonl`.

### Index and search

With Qdrant reachable at `QDRANT_URL` (default `http://localhost:6333`, e.g. through an SSH tunnel) and `VOYAGE_API_KEY` in `.env`:

```bash
uv run sda-ingest --dry-run          # chunk only: counts and token distribution, no API calls
uv run sda-ingest                    # chunk -> embed (cached) -> sync to Qdrant
uv run sda-search "What documents do I need to apply for a SIN?" --program sin
```

Re-runs are idempotent: unchanged chunks aren't re-embedded or re-written.

### Evaluate retrieval

```bash
uv run sda-eval-retrieval --validate   # check the dataset against the corpus, no API calls
uv run sda-eval-retrieval              # writes data/eval/results/<collection>_<date>.{json,md}
```

### Evaluate answers

```bash
uv run sda-eval-answers                   # dry run: plan and upper-bound cost, nothing sent to Claude
uv run sda-eval-answers --limit 2 --yes   # first 2 items per category, written to data/eval/runs/
uv run sda-eval-answers --yes             # full run from a clean tree: data/eval/results/
uv run sda-eval-answers --export          # offline blind hand-review sheet (--reveal shows the verdicts)
uv run sda-eval-answers --import-labels data/eval/judge_review.md   # filled sheet -> judge_calibration.jsonl
uv run sda-eval-answers --calibrate       # judge vs human agreement; cached verdicts are free, an uncached call is a dry run
uv run sda-eval-answers --calibrate --yes # make the uncached judge calls (costs money, capped by --cap)
```

Even the dry run needs `VOYAGE_API_KEY` and the Qdrant tunnel (it embeds and retrieves every question). Runs with `--yes` call Claude and cost money; `EvalSettings.spend_cap_cad` (override with `--cap`) is checked before the run and before each item. Each run writes `answers_<collection>_<model>_<run_id>.{jsonl,summary.json,md}`. Partial runs, and full runs from a dirty working tree, go to the gitignored `data/eval/runs/`; `--allow-dirty` writes the latter to `results/`. Scoring rules: [decisions #21](docs/decisions.md#21-answer-scoring-number-gate-plus-judge-per-category-report-capped-dry-run-first-runner); breakdowns, provenance and calibration: [decisions #23](docs/decisions.md#23-answer-eval-runner-overall-rows-abstention-and-citation-metrics-provenance-hard-cap-blind-judge-calibration).

There's no API to run yet.

## Disclaimer

This is an independent portfolio and research project. It is not affiliated with or endorsed by the Government of Canada, and its answers are not legal, financial or benefits advice. Answers can be wrong or out of date: check the official canada.ca page. The corpus text belongs to the Government of Canada (see [Corpus](#corpus)); the code is MIT-licensed.

## License

[MIT](LICENSE)
