# CLAUDE.md

RAG Q&A plus ticket triage assistant. FastAPI, Qdrant, Voyage AI embeddings, Claude generation.
Background: [README.md](README.md). Why things are the way they are: [docs/decisions.md](docs/decisions.md).

## Layout

- `src/support_desk_assistant/`: the installable package (uv_build, src layout)
  - `config.py`: the **only** place for config values (pydantic-settings; override with `SDA_*` env vars)
  - `api/`: FastAPI app (`app.py`: `create_app`, `POST /ask`, `GET /health`; `__main__.py` is `sda-api`)
  - `ingest/`: `crawl.py` → `chunk.py` → `embed.py` → `index.py` (Qdrant sync); `__main__.py` is `sda-ingest`
  - `triage/`: `schema.py` (queue, priority, confidence, `TriageResult`), `pipeline.py` (`triage_ticket`), `escalate.py` (`escalate`: `needs_review` and reasons), `store.py` (SQLite `TicketStore`, migrates old databases on open); routes in `api/tickets.py`
  - `retrieve.py`: `search()` and the `sda-search` CLI
  - `evals/`: eval harness (named `evals`, not `eval`, so it doesn't shadow the builtin); `retrieval.py` is `sda-eval-retrieval`
    - `draft_questions.py`: `sda-eval-draft`, Haiku drafts candidate questions
    - `review.py`: `sda-eval-review`, offline accept/reject; `--export` writes a phone HTML page, `--apply` records its decisions
    - `answers.py`: `sda-eval-answers`, scores generated answers (number check + Haiku judge), per-category report
    - `stats.py`: Wilson interval, confusion counts, Cohen's kappa
    - `breakdown.py`: overall, by program, abstention, citation and failure-attribution rows
    - `provenance.py`: reproducibility block recorded with every answer-eval run
    - `triage_draft.py`: `sda-eval-triage-draft`, Haiku drafts tickets from scenarios (never from labels), drops near-duplicates of earlier sets
    - `triage_label.py`: `sda-eval-triage-label`, Sonnet silver labels, gold sampling, `--build` the set with a dev/test split
    - `triage_review.py`: `sda-eval-triage-review`, offline blind gold labelling (`--terminal`, or a page and `--apply`)
    - `triage.py`: `sda-eval-triage`, scores the classifier on `data/eval/triage_set_v2.jsonl` (dev/test, gold view, baselines; test is scored once). v1 is frozen with its results
    - `escalation.py`: `sda-eval-escalation`, scores the escalation policy (capture, residual error, urgent unreached, sweep, targets); dev runs stay local, test is scored once
    - `calibrate.py`: blind-sheet label import and judge vs human agreement
- `tests/`: pytest; fixtures in `tests/fixtures/`. No live network in tests: use `httpx.MockTransport`, `tests/fakes.py` (fake Voyage) and `QdrantClient(":memory:")`
- `data/manifest.jsonl`: committed corpus definition. Gitignored: `data/raw/`, `data/processed/`, `data/chunks.jsonl`, `data/cache/` (embedding cache)
- `data/eval/`: versioned eval datasets (`retrieval_v1.jsonl`, `eval_set_v1.jsonl` = accepted answer eval set, `judge_calibration.jsonl` = human labels for the judge) and `results/` (committed). Gitignored: `candidates.jsonl`, `review_state.json`, `review.html`, `judge_review.md`, `runs/`
- `compose.yaml`: Qdrant (and later the API)

## Commands

```bash
uv sync                          # install deps and the package (editable) into .venv
uv run ruff check .              # lint
uv run ruff format .             # format
uv run pytest                    # tests
uv run python -m support_desk_assistant.ingest.crawl [--program ei] [--refresh]
uv run sda-ingest [--program ei] [--dry-run]   # chunk -> embed -> index (needs the Qdrant tunnel)
uv run sda-search "question" [--program ei] [-k 5]
uv run sda-eval-triage-draft [--yes] [--force]   # Claude API, costs money; dry run by default
uv run sda-eval-triage-label --build   # merge labels into the set. (--yes would call Sonnet and refuses: Haiku only)
uv run sda-eval-triage-review [--status | --terminal | --show [--all] | --export | --apply FILE [--labelled-by NAME]]   # offline
uv run sda-eval-triage [--yes] [--limit N]   # dry run by default; --yes calls Claude (capped)
uv run sda-eval-escalation [--split dev|test] [--second-opinion] [--yes]   # dry run by default; replays cached responses, an uncached item costs a Haiku call (the shipped policy needs --second-opinion)
uv run sda-api                   # serve POST /ask on 127.0.0.1:8000 (needs the Qdrant tunnel and both API keys)
uv run sda-eval-retrieval [--validate] [--collection X] [-k 5]
uv run sda-eval-draft [--dry-run] [--yes] [--force] [--seed N] [--out FILE]   # Claude API, costs money (mock in tests)
uv run sda-eval-review [--status | --export | --apply FILE] [--category C]   # offline
uv run sda-eval-answers [--yes] [--limit N] [--category C] [--cap CAD] [--allow-dirty]   # dry run by default; --yes calls Claude (costs money; spend cap in config); needs Qdrant tunnel + Voyage even for the dry run
uv run sda-eval-answers --export [RESULTS] [--reveal] | --import-labels SHEET [--from RESULTS] | --calibrate [FILE] [--yes] [--cap CAD]   # blind sheet and label import are offline; --calibrate replays cached judge verdicts free, any uncached call is a dry run without --yes (costs money with it, capped)
uv run pre-commit run --all-files
uv add <pkg> / uv add --dev <pkg>
docker compose up -d qdrant      # on the deploy host
```

Python is pinned to 3.12 (`.python-version`). Always go through `uv run`; don't use the system python.

## Rules

- **Secrets:** never read, print, create or commit `.env`. Keys come from `.env`; the variable names are in `.env.example`. If a new key is needed, add its name to `.env.example` and nothing else.
- **Docker:** every published port binds to `127.0.0.1` (`"127.0.0.1:PORT:PORT"`). Never use a bare `"PORT:PORT"`. Pin image versions.
- **Config:** chunk size, top_k, model names, collection names and similar values live only in `src/support_desk_assistant/config.py`. Never hardcode them elsewhere.
- **Before committing:** run `uv run ruff check .` and `uv run pytest`; both must pass.
- **Dependencies:** add them with `uv add`, and commit `uv.lock`. Keep ruff's version in `pyproject.toml` the same as its rev in `.pre-commit-config.yaml`.
- **Evals:** any retrieval change (chunking, embeddings, collection) gets a before/after `sda-eval-retrieval` run on the same dataset version. Don't edit a dataset that has published results; fix only genuine labelling errors (explain in `notes`) or create `retrieval_v2`.
- **Index versions:** changing the embedding model, dimension, dtype or chunking means bumping `IndexSettings.collection` (`canada_docs_v2`, ...). Never pass `--allow-mass-delete` without understanding why the guard tripped.
- **Tokens:** API calls cost money. Keep prompts lean, use prompt caching for stable system prompts, and don't call live APIs in unit tests (mock them).
- **API models:** paid API calls use Haiku only (owner's rule, 2026-10-22). Never call Sonnet or Opus through the API, not even as a probe. Work that needs a stronger reader (labelling eval tickets) is done by Claude in the working session and recorded as `model-session`, never as human. `triage_eval.label_model` defaults to Sonnet: do not run `sda-eval-triage-label --yes`.
- **Decisions:** when a significant choice is made or changed, add an entry to `docs/decisions.md`.
- **Crawling:** keep it polite (robots.txt, ≥1 s between requests, descriptive User-Agent). Every processed page keeps its source URL for attribution (canada.ca terms).
- **Scope:** don't build ahead of the current phase in the README roadmap.
