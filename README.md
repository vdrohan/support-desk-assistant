# support-desk-assistant

A retrieval-augmented Q&A and ticket triage assistant for support desks, with an evaluation harness that measures its accuracy.

> **Status: early scaffolding.** The repo has project tooling, a Qdrant container definition and an empty package layout. There's no application code yet. See [Roadmap](#roadmap).

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
| 1 | RAG core: ingest pipeline, `/ask` endpoint with citations, small eval set in `data/eval/`, `sda-ingest` and `sda-evals` commands | ⏳ next |
| 2 | Triage layer: ticket classification and priority, SQLite storage, triage eval | planned |
| 3 | Escalation logic for low-confidence cases, plus a write-up with eval results | planned |

Eval results will be published here once Phase 1 has a baseline. None exist yet.

## Repository layout

```
src/support_desk_assistant/
  config.py      # single source of config values (filled in Phase 1)
  api/           # FastAPI app
  ingest/        # chunk, embed, upsert
  evals/         # eval harness
docs/            # decisions and, later, the write-up
compose.yaml     # Qdrant
data/eval/       # eval datasets (planned, Phase 1)
tests/           # pytest (added with the first code)
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
uv run pytest                        # exits 5 (no tests collected) until Phase 1
```

There's no API to run yet. Phase 1 will add the run command here.

## License

[MIT](LICENSE)
