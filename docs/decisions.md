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

## 9. Chunking: heading-aware, with a context prefix and within-page dedup

- **Decision:**
  - Split each page at H2/H3. Inside a section, pack whole blank-line blocks (paragraphs, lists, tables) toward a target of about 350 tokens, with a hard max of 600.
  - A single block over the max is split by lines; tables repeat their header row.
  - Sections under 80 tokens are merged into a neighbour, keeping their heading as a Markdown line.
  - The embedded text is `<title> > <H2> > <H3>` followed by the body. The payload stores the body and the heading path separately.
  - Sizes are counted with voyage-4's own tokenizer.
  - Chunk IDs are `uuid5(url | heading path | ordinal within that path)`.
  - Exact repeats of blocks (8+ tokens) within a page are dropped, keeping the first occurrence.
- **Reason:**
  - canada.ca pages are already organised by question ("Who can apply", "How much you could get"), so sections are natural retrieval units, and the prefix carries their context into the vector.
  - Splitting at block boundaries keeps lists and tables readable.
  - Counting with the real tokenizer makes the hard max actually hard.
  - IDs that depend on position within a heading path mean an edit in one section doesn't change IDs elsewhere, so re-runs upsert instead of duplicating.
  - The median chunk is about 230 tokens, below the target, because headings are hard boundaries. That's accepted: single-topic chunks retrieve more precisely and cost less as LLM context.
  - **Dedup:** interactive "get your instructions" pages include every scenario's full text. `child-passport.html` repeated about 85% of its blocks (it went from 238 chunks to 36). Corpus-wide, 1,702 repeats were dropped on 29 pages, and passport went from 445 chunks to 204. Without dedup, a single page filled most of the top-k slots for passport queries.
- **Trade-off, to be validated by evals:** after dedup, later scenarios on `child-passport.html` (by mail, abroad, urgent, family-services worker) keep only their unique steps. The shared steps survive only under the first scenario, so a scenario-specific chunk can read as incomplete, and some leftover fragments are hard to follow. The Phase 1 eval set will include scenario-specific child-passport questions. If they show this matters, the fallback is a **structured parse of the instructions tool in the crawler**, producing one clean document per scenario.
- **Rejected:**
  - *Fixed-size windows with overlap:* they cut through lists and tables and lose the heading context.
  - *Packing sibling H3s up to the target:* fewer chunks, but more of them mix topics.
  - *A characters-÷-4 token estimate:* off by 15–25% on numeric text.
  - *Indexing duplicates and diversifying at query time:* it pays the cost on every query and skews evals.

## 10. Embeddings: voyage-4, 1024-dim float, asymmetric input_type, local cache

- **Decision:** `voyage-4` with `output_dimension=1024` and `float` dtype. Chunks are embedded with `input_type="document"` and queries with `input_type="query"`. Vectors are cached in `data/cache/embeddings.sqlite`, keyed by `sha256(model | dimension | dtype | input_type | text)`. Retries with backoff happen on 429, 5xx and connection errors.
- **Reason:**
  - Voyage trains the query and document input types as a pair: queries are short questions and documents are passages.
  - 1024 float is the model's default and best-quality setting. At about 735 points, storage doesn't matter.
  - Because the cache key covers every setting that changes the vector, unchanged chunks are never re-embedded, and a model or dimension change can't serve stale vectors.
  - The first full run cost 174k tokens. Re-runs with an unchanged corpus cost 0.
- **Rejected:**
  - *512 dimensions or int8:* smaller, but loses accuracy that isn't worth trading at this scale.
  - *A JSONL cache:* not atomic, and larger.
  - *The client's built-in `max_retries`:* our own loop is explicit, logs each retry without the key, and is testable with a fake client.

## 11. Qdrant: versioned collection, deterministic IDs, scoped sync with a mass-delete guard

- **Decision:**
  - Collection `canada_docs_v1`, cosine distance, vector size taken from the actual model output, and a keyword payload index on `program`.
  - Each sync is scoped to the programs being processed. It compares the stored `content_hash` values with the current chunks, embeds and upserts only new or changed chunks, and deletes chunks that no longer exist.
  - It aborts if it would delete more than 20% of the points in scope, unless `--allow-mass-delete` is passed.
  - Afterwards, it checks that the point count matches the chunk count.
- **Reason:**
  - Re-runs are idempotent: an unchanged corpus means 0 embeddings and 0 writes.
  - With `--program X`, other programs' points can't be touched.
  - The guard catches a broken crawl or a chunking bug before it empties the index.
  - **Any change to the model, dimension, dtype or chunking means a new collection version (`_v2`).** Old and new can then be evaluated side by side, and switching is just a config change.
- **Rejected:**
  - *Drop-and-rebuild on every run:* re-embeds everything and leaves a window with an empty index.
  - *Random point IDs:* duplicates on every re-run.
