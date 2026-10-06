# CLAUDE.md

RAG Q&A plus ticket triage assistant. FastAPI, Qdrant, Voyage AI embeddings, Claude generation.
Background: [README.md](README.md). Why things are the way they are: [docs/decisions.md](docs/decisions.md).

## Layout

- `src/support_desk_assistant/`: the installable package (uv_build, src layout)
  - `config.py`: the **only** place for config values (pydantic-settings; override with `SDA_*` env vars)
  - `api/`: FastAPI app
  - `ingest/`: `crawl.py` (canada.ca corpus); chunk, embed and upsert come next
  - `evals/`: eval harness (named `evals`, not `eval`, so it doesn't shadow the builtin)
- `tests/`: pytest; fixtures in `tests/fixtures/`. No live network in tests (use `httpx.MockTransport`)
- `data/manifest.jsonl`: committed corpus definition. `data/raw/` and `data/processed/` are gitignored and rebuilt by the crawler
- `data/eval/`: eval datasets (planned for Phase 1)
- `compose.yaml`: Qdrant (and later the API)

## Commands

```bash
uv sync                          # install deps and the package (editable) into .venv
uv run ruff check .              # lint
uv run ruff format .             # format
uv run pytest                    # tests
uv run python -m support_desk_assistant.ingest.crawl [--program ei] [--refresh]
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
- **Tokens:** API calls cost money. Keep prompts lean, use prompt caching for stable system prompts, and don't call live APIs in unit tests (mock them).
- **Decisions:** when a significant choice is made or changed, add an entry to `docs/decisions.md`.
- **Crawling:** keep it polite (robots.txt, ≥1 s between requests, descriptive User-Agent). Every processed page keeps its source URL for attribution (canada.ca terms).
- **Scope:** don't build ahead of the current phase in the README roadmap.
