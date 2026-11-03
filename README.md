# support-desk-assistant

A retrieval-augmented Q&A and ticket triage assistant for support desks, with an evaluation harness that measures its accuracy. It answers questions from 129 Government of Canada pages with citations (or says it cannot), sorts support tickets by queue and priority, flags the ones a person should check, and reports every accuracy number with its sample size.

> **Status: Phases 0 to 4 done.** The API key and Docker image (Phase 4) are built and checked in CI; the API is not deployed anywhere, by choice. Phases and what was built: [Roadmap](#roadmap). Full results, limits and costs: [docs/writeup.md](docs/writeup.md).

## Results at a glance

| Part | Headline result | Read it with |
|---|---|---|
| Retrieval | The right page is in the top 5 for 0.91 of 34 questions | Small set; ambiguous and multi-program questions fail |
| Answers | 42 of 51 answerable questions pass (0.82); 7 of 7 unanswerable ones abstain | The judge was calibrated on labels written by Claude |
| Triage | Test splits, set v1 / set v2: queue 0.90 / 0.88, priority 0.80 / 0.81, urgent recall 19/20 and 28/29 | Tickets are model-drafted; most gold labels were agreed by models, not people |
| Escalation | Flags 40% of tickets and catches 24 of the 30 classifier errors; 1 of 29 truly urgent tickets unreached | Four targets fixed beforehand, all met, one of them by a single ticket |

Most sets have 50 to 100 items, so the 95% intervals in the reports (`data/eval/results/`) are wide. A full answer-eval run costs about CA$0.05, and paid API calls use Haiku only.

**Try it without any keys:** the tests are offline, so with [uv](https://docs.astral.sh/uv/) installed this runs the whole suite in seconds.

```bash
git clone https://github.com/vdrohan/support-desk-assistant.git && cd support-desk-assistant
uv sync && uv run pytest             # over 600 tests, no network, no API keys
```

## What it looks like

These come from the committed result files, not from a new run. The questions and tickets are from the eval sets.

**A cited answer** (eval item `ev-001`, answer shortened here with `[...]`):

```
Q: My dad had a bad accident and needs someone at home looking after him for a while. I had
   to stop working to do it. How soon should I apply for EI, and do I risk losing anything
   if I wait?

A: Apply for EI as soon as possible after you stop working or after your earnings are
   reduced. If you wait too long, you may lose benefits. [...] Amounts and deadlines can
   change, so check the official page for the current rules. The page was last modified
   on 2026-06-02.

Cited pages:
   EI caregiving benefits – How to apply    canada.ca/en/services/benefits/ei/caregiving/apply.html
   EI caregiving benefits – After you apply canada.ca/en/services/benefits/ei/caregiving/after-applying.html
   EI sickness benefits: Apply              canada.ca/en/services/benefits/ei/ei-sickness/apply.html
```

**An abstention** (`ev-054`): the documents do not cover it, so it does not guess.

```
Q: What's the minimum wage in Ontario right now?
abstained: true
reason: I can only answer questions about Employment Insurance, CPP, OAS, SIN and passports,
        and the documents provided do not include Ontario minimum wage information.
```

**A triaged ticket** (`tc-001`, a ticket drafted by Haiku for the eval; the classifier's answer matched the gold label):

```
Subject: Need EI document before job start
Body:    I start a new job on Monday and my employer needs proof of my most recent EI claim.
         Where can I get a copy of that document quickly? I only have a few days left.

queue: ei    priority: urgent
rationale: The ticket asks where to get a copy of an EI claim document that an employer
           needs before a job starts on Monday, with only a few days left. This is a
           time-bound deadline within 7 days, so it is urgent.
```

## Problem

Support agents spend much of their time finding answers that already exist in docs, past tickets and runbooks, and routing tickets to the right queue by hand. This project aims to:

1. **Answer questions** from a knowledge base, with citations to the source passages.
2. **Triage incoming tickets:** category, priority and suggested routing.
3. **Escalate** when confidence is low, instead of guessing.

And to **measure** all of this with a versioned eval set, so accuracy claims are backed by numbers.

## Architecture

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
                    │ tickets
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
| Ticket store | SQLite | No server to run at this scale |

All AI runs through cloud APIs; there are no local models. Reasoning and rejected alternatives: [docs/decisions.md](docs/decisions.md).

**Deployment:** developed on macOS, pushed to GitHub. Qdrant runs with Docker Compose on a small Ubuntu Server box reachable only over Tailscale, and the code reaches it through an SSH tunnel. The API itself is not deployed: this is a learning and portfolio project that nobody needs running day to day (decision 31). Every container port binds to `127.0.0.1`.

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 0 | Repo scaffolding: tooling, Compose with Qdrant, decision log | ✅ done |
| 1 | RAG core: corpus crawler ✅, chunk + embed + index (`sda-ingest`) ✅, vector search (`sda-search`) ✅, answers with citations (`sda ask`) ✅, eval sets in `data/eval/` ✅, retrieval eval (`sda-eval-retrieval`) ✅, answer scoring runner and judge calibration (`sda-eval-answers`) ✅ (baseline run 2 done with judge v2; judge calibrated only on Claude-written labels; a human review was skipped by choice), `/ask` endpoint (`sda-api`) ✅ | ✅ done |
| 2 | Triage layer: ticket classification (queue + priority) ✅, SQLite storage and `/tickets` API ✅, triage eval pipeline (scenario-drafted tickets, silver and blind gold labels, dev/test split, baselines) and `sda-eval-triage` runner ✅, gold sample of 50: 10 labelled by the owner, 40 where three blind model passes (Sonnet, Sonnet, Opus) agreed, not reviewed by a person (decision 28); first scores (Haiku, test split, run once): queue 0.90, priority 0.80, urgent recall 0.95 (19/20); gold labels mostly model-agreed, and a human review of those 40 was skipped by choice, see decision 28 | ✅ done |
| 3 | Escalation logic for low-confidence cases (a `needs_review` flag from rules, queue confidence and a Haiku second opinion; scored once on the v2 test split against targets fixed beforehand: all four met, 41/103 escalated, 24/30 errors caught, labels are one model's reading), plus the write-up with eval results ([docs/writeup.md](docs/writeup.md)) | ✅ done |
| 4 | Leave localhost: an API key on `/ask` and `/tickets` that fails closed ✅, a Docker image and a Compose service for the API ✅ (CI builds the image and checks it); the API is not deployed, by choice, because the project is not meant to run day to day (decision 31) | ✅ done (not deployed) |

A retrieval baseline is in [`data/eval/results/`](data/eval/results/). The answer eval baseline is there too: run 1 (answer cap, decisions #24) and run 2 (judge v2, decisions #25), which supersedes it. A reorder-only run (2a) and an independent audit of run 1 are in [docs/judge_audit_run1.md](docs/judge_audit_run1.md).

## Corpus

English Government of Canada pages from canada.ca for 5 citizen-service programs: Employment Insurance, Canada Pension Plan, Old Age Security, Social Insurance Number and passports. That's up to 30 pages per program, crawled within each program's URL prefix. Source: Government of Canada, canada.ca, reproduced for non-commercial use with attribution. Each page's title and URL are kept in the manifest and in every processed file. Why this corpus: [decisions #7](docs/decisions.md#7-corpus-government-of-canada-citizen-services-pages).

## Repository layout

```
src/support_desk_assistant/
  config.py      # single source of config values (pydantic-settings)
  api/           # FastAPI app and the API-key check
  ingest/        # crawl, chunk, embed, index (sda-ingest)
  retrieve.py    # vector search (sda-search)
  triage/        # ticket triage: schema, pipeline, escalation rules, SQLite store
  evals/         # eval harness (answers, retrieval, triage, escalation)
prompts/         # the instructions given to Claude (answers, judge, triage, drafting)
docs/            # decisions and the write-up
CHANGELOG.md     # what changed, by phase
scripts/         # dev tooling (the commit message check)
compose.yaml     # Qdrant and the API
Dockerfile       # the API image (built in CI, never deployed)
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
uv run pre-commit install            # ruff, gitleaks and the commit message check on every commit
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

### Triage tickets

```bash
uv run sda-eval-triage-draft --yes          # Haiku drafts 200 tickets from scenarios, never from labels (about CA$0.10); near-duplicates of v1 are dropped. Paid API calls use Haiku only
uv run sda-eval-triage-review --show --all  # prints every undecided ticket, blind; a reviewer writes '<id> <queue> <priority> [?]' lines
uv run sda-eval-triage-review --apply FILE --labelled-by model-session:claude-sonnet-5-5   # record them (v2 was labelled by Claude in the session; '?' = unsure)
uv run sda-eval-triage-review --terminal    # or: a person labels the stratified sample one ticket at a time (needs silver labels); quit and resume any time
uv run sda-eval-triage-label --build        # merge the labels into data/eval/triage_set_v2.jsonl with a dev/test split
uv run sda-eval-triage --split dev          # dry run; --yes scores the classifier on dev; --split test is scored once
uv run sda-eval-escalation --split dev --second-opinion   # dry run; --yes scores the escalation policy on dev (tuning); --split test is scored once
```

A ticket is triaged into one queue (`ei`, `cpp`, `oas`, `sin`, `passport`, `other`) and one priority (`urgent`, `high`, `normal`, `low`), each with a `low`/`medium`/`high` confidence; the rules are in [prompts/triage.md](prompts/triage.md). A ticket goes to a person (`needs_review`, with `escalation_reasons`) when triage failed, the queue is `other`, the model's queue confidence is below high, or a second Haiku pass with thinking on labels it differently; urgent tickets top the queue by priority instead ([decisions #29](docs/decisions.md#29-escalation-rules-plus-the-classifiers-own-confidence-scored-on-a-fresh-set-with-the-targets-fixed-first)). Tickets are free text and can contain personal information: the SQLite store (`data/tickets.sqlite`, gitignored) keeps them as typed and they are sent to the Anthropic API, so don't put real SINs or passport numbers in them.

A person can correct or confirm a triage with `POST /tickets/{id}/override` (`queue`, `priority`, `note`, all optional: send none to confirm). The model's own `queue` and `priority` stay as they were; the decision is stored beside them as `final_queue`, `final_priority`, `review_note` and `reviewed_at`, and `human_changed` says whether the person changed either label. `GET /tickets?needs_review=true&reviewed=false` is the to-do list of flagged tickets nobody has looked at ([decisions #30](docs/decisions.md#30-override-a-person-records-their-decision-beside-the-models-answer)).

### Run the API

```bash
python -c 'import secrets; print(secrets.token_urlsafe(32))'   # make a key and put it in .env as SDA_API_KEY
uv run sda-api                       # http://127.0.0.1:8000, needs Qdrant, VOYAGE_API_KEY, ANTHROPIC_API_KEY and SDA_API_KEY
curl -s localhost:8000/ask -H 'X-API-Key: <your key>' -H 'content-type: application/json' \
  -d '{"question": "How many weeks of EI sickness benefits can I get?", "program": "ei"}'
```

`POST /ask` takes `question` (required), `program` (one of the five) and `k`, and returns `abstained`, `answer`, `abstain_reason`, numbered `citations` (title and URL), `status`, `model`, `cost_cad`, `latency_ms` and `cache_hit`. An answer whose citations fail validation is returned as an abstention with status `validation_failed`. `POST /tickets` (`subject`, `body`) triages a ticket, stores it and returns its queue, priority and rationale; `GET /tickets/{id}` and `GET /tickets?queue=&priority=&needs_review=` read them back; a ticket also carries `queue_confidence`, `priority_confidence`, `needs_review` and `escalation_reasons`. `/ask` adds `escalate` and `escalate_reason` (set when there is no answer: the retrieval gate, an abstention, failed citation validation or a generation failure). `GET /health` is a liveness check and needs no key. Every other route needs the key in an `X-API-Key` header and answers 401 without it. The API refuses to start without `SDA_API_KEY` (at least 24 characters); `SDA_API__ALLOW_NO_KEY=true` serves without one for local development, on a loopback host only. It binds to `127.0.0.1` (`ApiSettings`). Why: [decisions #27](docs/decisions.md#27-ask-endpoint-same-pipeline-as-the-cli-validated-answers-only-no-auth-loopback-only) and [#31](docs/decisions.md#31-leaving-localhost-one-shared-api-key-that-fails-closed-and-a-docker-image-for-the-api).

To run the API as a container on a server, it is a Compose service next to Qdrant:

```bash
docker compose up -d --build api     # needs .env there with ANTHROPIC_API_KEY, VOYAGE_API_KEY and SDA_API_KEY
```

The image holds the code, the prompts and the corpus manifest and nothing else; keys come from `.env` when it starts. Port 8000 is published on the host's loopback only, so reach it through the SSH tunnel, as with Qdrant. Tickets live in the `api_db` volume: back that one up. CI builds the image on every pull request and checks that it refuses to start without a key. The Compose service itself has never been started anywhere: the API is not deployed (decision 31).

## What I learned

I built this to learn how to build and measure an AI tool, not to run one every day. Here's what stuck.

- The order of fields in the model's output matters. The model writes them top to bottom, so my answers explained first and only then said whether to abstain. It would write "the documents don't say" and then answer anyway. Rewording the prompt did nothing. Moving the abstain flag to the top took abstention from 4 of 9 to 7 of 9. My judge had the same bug.
- Set the pass mark before you run the test. I wrote the escalation targets down first and reported each one as met or not met. One was met by a single ticket, and I said so.
- A test set only counts once. After I'd read its results I couldn't tune the prompt on it and still trust the score, so I built a second set.
- Most of my labels came from models, not people. I wrote that in the write-up, because a score is only as good as the labels behind it.
- With 50 to 100 items, a score of 0.82 could really be anywhere from 0.70 to 0.90. So every number has its range next to it.
- Cost stayed small because I planned for it. Evals are dry runs by default, there's a spend cap, and a full answer eval costs about CA$0.05.

## Disclaimer

This is an independent portfolio and research project. It is not affiliated with or endorsed by the Government of Canada, and its answers are not legal, financial or benefits advice. Answers can be wrong or out of date: check the official canada.ca page. The corpus text belongs to the Government of Canada (see [Corpus](#corpus)); the code is MIT-licensed.

## License

[MIT](LICENSE)
