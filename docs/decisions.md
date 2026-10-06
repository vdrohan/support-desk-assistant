# Decisions

Short architecture decision records. Newest decisions go at the bottom. If a decision changes, add a new entry that supersedes the old one instead of editing it.

## 1. Qdrant as the vector database

- **Decision:** Qdrant, run as a container and pinned to a specific image version in `compose.yaml`.
- **Reason:** It's a purpose-built vector DB with payload filtering (needed later to scope retrieval by product or ticket category), hybrid search, and a clean Python client. It runs as one small container on low-power hardware.
- **Rejected:**
  - *Chroma:* easy to start with, but fewer filtering and ops features. Its embedded mode also blurs the line between app and DB.
  - *pgvector:* strong if Postgres is already in the stack, but it isn't. Adding Postgres only for vectors means more to run for less vector-specific tooling.

## 2. Voyage AI for embeddings

- **Decision:** Voyage AI embedding models through its API.
- **Reason:** Strong retrieval quality on benchmarks, separate query and document input types, and it's the embedding provider Anthropic recommends alongside Claude.
- **Rejected:**
  - *OpenAI embeddings:* a good alternative, but they add a second large vendor for no clear quality gain.
  - *Local sentence-transformers:* see decision 4.

## 3. Claude for generation

- **Decision:** Anthropic Claude models through the Messages API. Model names are set in config.
- **Reason:** Good at grounded answers with citations and at following instructions for structured output (needed for triage). Prompt caching cuts token cost for repeated system prompts and context.
- **Rejected:**
  - *Other hosted LLMs:* possible. Keeping model names in config means generation can be re-evaluated against the eval set later.

## 4. Cloud APIs only, no local models

- **Decision:** All embedding and generation goes through hosted APIs.
- **Reason:** The deploy host is a 2017 MacBook Air (no usable GPU, little RAM). Local models would be slow and lower quality, and hard to evaluate fairly. Cloud APIs keep the host's job to orchestration and Qdrant.
- **Rejected:**
  - *Ollama or local models:* not viable on the target hardware.
  - *Hybrid (local embeddings, cloud generation):* runs into the same hardware limit.
- **Cost:** Needs network access and API keys, and costs money per call, so token efficiency is an explicit project goal.

## 5. SQLite for ticket data (Phase 2)

- **Decision:** SQLite file for ticket and triage data once the triage layer exists.
- **Reason:** No server to run, a single file that's easy to back up, and plenty for one-node, portfolio-scale volume. Standard SQL keeps a later move to Postgres cheap.
- **Rejected:**
  - *Postgres:* another container and more operational work with no benefit at this scale.
  - *Storing tickets as Qdrant payloads:* mixes relational and vector concerns, and querying is awkward.

## 6. Docker Compose with ports bound to 127.0.0.1

- **Decision:** All services run through `compose.yaml`, and every published port binds to `127.0.0.1`.
- **Reason:** Docker's port publishing goes around host firewalls like ufw. Binding to `0.0.0.0` would expose Qdrant (which has no auth by default) to the whole LAN. The host is reached only over Tailscale SSH, so localhost binding plus SSH tunnelling is enough.
- **Rejected:**
  - *Binding to 0.0.0.0 and relying on the firewall:* Docker goes around it.
  - *Kubernetes or k3s:* too heavy for one node.

## 7. Corpus: Government of Canada citizen-services pages

- **Decision:** English canada.ca pages for 5 mixed citizen-service programs: Employment Insurance, Canada Pension Plan, Old Age Security, Social Insurance Number and passports. About 30 pages each (depth 2 within each program's URL prefix). Seeds and limits are in `config.py`. `data/manifest.jsonl` is committed and defines the corpus for reproduction; the raw and processed text are gitignored and rebuilt by the crawler.
- **Reason:** Real support-desk material: eligibility rules, how-to-apply steps, payment amounts and timelines. Programs overlap in vocabulary (EI and CPP, CPP and OAS), which makes retrieval non-trivial and gives triage realistic categories. It's public, stable, consistently structured (`<main>`, `dcterms.modified`), and canada.ca's terms allow non-commercial reproduction with attribution. Every processed file and future answer carries the page title and URL.
- **Rejected:**
  - *Synthetic or LLM-generated docs:* no ground truth, so evals would measure the generator, not retrieval.
  - *Vendor help centres (SaaS docs):* terms often forbid scraping or reuse.
  - *A single program:* too little category spread for triage.

## 8. Crawler parsing: protego for robots.txt, BeautifulSoup + markdownify for content

- **Decision:** Parse robots.txt with `protego`. Select `<main>` and strip boilerplate with BeautifulSoup (lxml), then convert to Markdown with `markdownify`. Link targets are dropped and only their text is kept.
- **Reason:**
  - canada.ca's robots.txt uses `*` wildcards (e.g. `/en/*/search.html`). `urllib.robotparser` only does literal prefix matching, so it silently ignores those rules. protego implements the full Google/RFC 9309 matching.
  - We know exactly where the content is (`<main>`), so explicit selectors are predictable and testable. markdownify keeps tables and lists.
  - Dropping link URLs saves tokens per chunk; the page URL is recorded once per document.
- **Rejected:**
  - *urllib.robotparser:* wrong on wildcard rules.
  - *A hand-written wildcard check on top of it:* parsing code we'd have to own.
  - *trafilatura:* heuristic content detection can drop tables and alert boxes.
  - *html2text:* GPL-3.0 in an MIT repo.
