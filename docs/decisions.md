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

## 12. Retrieval eval: versioned question set, page-hit and answer-hit, oracle routing

- **Decision:**
  - **Dataset:** `data/eval/retrieval_v1.jsonl` (committed) holds 37 questions: 10 written by a human, kept word for word including typos, and 27 generated and worded the way a citizen would ask, weighted by corpus size.
  - **Categories:** `direct`, `colloquial`, `known_issue`, `ambiguous`, `out_of_corpus`.
  - **Labels:** each question has `expected_urls` (any one counts) and `key_facts`, short phrases copied exactly from the page.
  - **Validator:** runs before every eval and fails on any key fact that isn't on an expected page. It warns when a fact is on the page but in no chunk.
  - **Runner (`sda-eval-retrieval`):** asks every question **unfiltered** and again **filtered to its expected programs** (the "oracle").
  - **Metrics:** page hit@1, page hit@k, MRR@k and answer hit@k, overall and by program, category and source, with *n* on every row.
  - **Out-of-corpus questions** get no hit metrics. Instead their top-1 scores are compared with those of answerable questions.
  - **Results:** written to `data/eval/results/<collection>_<date>.{json,md}`, with the git SHA, dataset hash and settings.
- **Why two hit metrics:**
  - **Page hit** asks: did we retrieve the right *document*? It is what citations and links depend on. But a long page (the child passport page has 36 chunks) can "hit" with a chunk that doesn't contain the answer.
  - **Answer hit** asks: is the answer *text* in what we retrieved? A key fact counts if it appears anywhere in the title, heading path or body of a top-k chunk, after normalizing case, whitespace, quotes and Markdown. That's exactly what the answer-writing model will see. It can also credit a different page that states the same fact.
  - When the two disagree, that is diagnostic: right page but wrong chunk points at chunking; right fact but wrong page points at duplicated content.
- **Why the oracle mode:** the gap between filtered and unfiltered results is the most that program routing (Phase 2 triage) could add to retrieval. It tells us whether routing is worth building before we build it.
- **Rules:**
  - The set is frozen before its first run.
  - After that, labels change only to fix genuine labelling errors, and each fix is explained in the item's `notes`.
  - New or reworded questions go into `retrieval_v2.jsonl`.
  - Any change to chunking, embeddings or the collection is judged by comparing result JSONs on the same dataset version.
- **Rejected:**
  - *LLM-judged relevance:* costs tokens on every run, isn't deterministic, and isn't needed before generation exists.
  - *Only page-level labels:* would hide the chunk-level failures that dedup and chunking can cause.
  - *Only generated questions:* they tend to echo the corpus wording, so human questions are tracked as a separate group.

## 13. Known corpus gaps

Found while labelling `retrieval_v1`. Each is a content problem, not a retrieval one, so it's tracked here rather than hidden in eval numbers. Affected eval items reference this entry in their `notes`.

| Gap | Effect | Likely fix |
|---|---|---|
| **Passport fees are loaded by JavaScript.** The static HTML has placeholders, so the corpus reads `[Error loading fee. Check the fee list.]` (135 times on `child-passport.html` alone). | No fee question is answerable. `gen-gap-01` is classed `corpus_gap`, with no hit metrics. | Add the passport fee-list page (or the JSON/fragment its script loads) to the crawl as a separate document, then replace the placeholders at extraction time or index the fee list on its own. Check robots.txt and the terms first. |
| **The service-standards "table" on `processing-times.html` is built from `div`s.** markdownify writes each cell as its own paragraph (`10Footnote *`, then `business days`). | Processing times are retrievable, but the numbers are split from their labels and footnote markers are mixed in (`human-02`, `gen-pp-01`). | Add an extraction rule for that layout: join each row's label, value and unit into one line (or rebuild a real table), and drop the `Footnote` markers. Re-extract from cache; no re-crawl needed. |
| **The adult passport document checklist sits behind a selector.** `new-adult-passport.html` is a "Where are you right now?" chooser, and the checklist lives on sub-pages the crawler never reached. | Document questions only partially answerable (`human-07`). | Add the chooser's target pages as extra seeds (or allow depth 3 for passport), within the same prefix and robots rules. |

When a fix lands, re-crawl or re-extract and bump the collection version. The affected items stay in `retrieval_v1` (labels change only if the fix creates a genuine labelling error), so the before/after effect shows up in the same dataset version.

## 14. CI: lint and tests on GitHub Actions; gitleaks stays in pre-commit only

- **Decision:** `.github/workflows/ci.yml` runs `ruff check`, `ruff format --check` and `pytest` on push to `main` and on pull requests, after `uv sync --locked`. The tests need no network, API keys or Qdrant, so there are no skip markers. Actions are pinned to full commit SHAs, with the version in a comment. `.github/dependabot.yml` bumps `github-actions` only, monthly, so the pins don't go stale.
- **Gitleaks is not in CI for now.** It runs in pre-commit, and the full history was scanned once when CI was added (7 commits, no findings).
  - The pre-commit hook already scans every commit before it lands, so a CI job would mostly repeat it.
  - It would add another third-party action to pin and track.
  - Pre-commit can be skipped with `--no-verify`. If the repo gets collaborators or goes public, add a CI scan then.

## 15. Answer step: native structured output, validated twice

- **Decision:** `client.messages.create(..., output_config={"format": {"type": "json_schema", ...}})`, with the schema derived from the pydantic `AnswerResult` (`answer`, `citations` as chunk IDs, `abstained`, `abstain_reason`). The response is parsed with the same model, then `validate_answer` checks the rules a schema can't express (every citation was retrieved, non-abstained answers cite, abstentions are empty).
- **Why native output:** the API constrains decoding to the schema, so there are no JSON parse failures to retry (each retry costs tokens).
  - Forced tool calls (`tool_choice` of `any`/`tool`) are not an option on Sonnet 5.5, and the comparison run needs the same code on both models.
  - The Anthropic `citations` feature is incompatible with `output_config.format`. It isn't needed: our citations are our own chunk IDs.
- **Sampling parameters:** none are sent. Haiku 5.5 and Sonnet 5.5 reject non-default `temperature`/`top_p`/`top_k` (400), so "low temperature" isn't available. Stability comes from the schema, the prompt and the response cache.
- **Incomplete generations** (`refusal`, `max_tokens`, output that fails the schema) become an abstention that says why. The log line has the `status`.
- **Chunk IDs:** `chunk_id` is `uuid5(url | heading path | ordinal)`, deterministic across reindexes for unchanged page structure. A section inserted before others on the same heading path shifts their ordinals, and the content under an ID can change. That is why the response-cache key hashes the rendered prompt (chunk text included), not just the IDs.

## 16. Haiku 5.5 as the answer model, Sonnet 5.5 as a one-time comparison

- **Decision:** `claude-haiku-5-5` ($0.10 / $0.50 per MTok, prompts up to 100K tokens) generates answers; `claude-sonnet-5-5` ($2 / $10) stays in config for a one-off comparison run (`sda ask --model`). Prices and the USD/CAD rate are in `AnswerSettings` (`prices_as_of`); the CAD rate is a placeholder.
- **Haiku runs with thinking disabled and effort `low`.** Thinking is on by default on 5.5 models and counts against `max_tokens` (500), so it could truncate the JSON.
- **The comparison is not like-for-like.** Sonnet 5.5 rejects `thinking: disabled`, so its profile runs adaptive thinking (effort `low`) with `max_tokens` 4000 so thinking can't truncate the JSON. Differences in quality or cost mix model size with thinking. Treat the run as indicative only.
- **Defaults:** top-k 5 and `max_tokens` 500 are in config.

## 17. Two-layer abstention

- **Layer 1, the gate:** if the top retrieval score is below `AnswerSettings.min_score`, abstain without calling Claude. It costs nothing and cannot hallucinate. `min_score` is a placeholder (0.30); tune it against the eval's out-of-corpus and answerable top-1 score distributions (`score_analysis`).
- **Layer 2, the model:** the system prompt tells Claude to set `abstained` when the retrieved chunks don't answer the question. This catches questions that retrieve something plausible but irrelevant, which no score threshold reliably separates.
- **Why both:** the gate alone misses on-topic-but-unanswered questions; the model alone spends tokens on obvious misses. `validate_answer` enforces that an abstention is empty and uncited.

## 18. Response cache and prompt caching

- **Response cache** (`data/cache/answers/`, gitignored): every completed API response is stored raw, including refusals and `max_tokens` stops. HTTP and network errors are never cached. The response is re-parsed and re-validated on every read, so changing `validate_answer` needs no re-spend. A hit makes no API call and logs cost 0.
- **Key:** sha256 of model, system prompt text, the rendered user message (question plus each chunk's ID, text and dates, in rank order), `max_tokens`, effort, thinking mode and the schema version. This is stricter than question + system prompt + model + chunk IDs: edited chunk text or a recrawl (new `crawled_at`) misses. Bump `AnswerSettings.schema_version` when `AnswerResult` or the prompt layout changes.
- **Anthropic prompt caching** (`cache_control` on the system prompt) is a separate, config flag `prompt_caching`, **off by default**: sporadic CLI use would pay the 1.25x write premium with no reads. Turn it on for eval runs (`SDA_ANSWER__PROMPT_CACHING=true`). Prompts under the model's minimum cacheable length won't cache either way. The log records cache read/write tokens separately from the local `cache_hit`.
- **Crawl dates:** the prompt gets `crawled_at` and `date_modified` from `data/manifest.jsonl`, joined by URL at answer time. A URL missing from the manifest gets `null`. This avoids a reindex.

## 19. Answer eval set: drafted by Haiku, accepted by hand, reviewable from a phone

- **Decision:** `sda-eval-draft` has Haiku write about 40 candidate questions (`data/eval/candidates.jsonl`, gitignored) across six categories (`EvalSettings.draft_mix`). A person accepts, edits or rejects each one; accepted items are appended to `data/eval/eval_set_v1.jsonl` under a final `ev-NNN` id. The drafter never decides what is true.
- **Grounding check:** every key fact must appear verbatim on a gold page (`check_candidate`). A failing candidate is flagged, not dropped, and **cannot be accepted** until it is edited.
- **Review is offline.** No API calls: the reviewer sees the key facts highlighted in the page text, and for unanswerable items the best keyword matches elsewhere in the corpus.
- **Two ways to review, one set of rules:**
  - *Terminal:* `sda-eval-review`.
  - *Phone:* `sda-eval-review --export` writes one self-contained HTML file (`data/eval/review.html`, gitignored, no external assets). Accept/Reject state is kept in `localStorage`, and "Copy decisions" yields `<candidate id> a|r` lines. `sda-eval-review --apply decisions.txt` replays them through the same `accept`/`reject` functions, so the same grounding check applies.
- **`--apply` is idempotent:**
  - A decision already in `review_state.json` is never changed (a conflicting line is reported, not applied).
  - An accept whose question is already in the eval set is recorded against that item instead of being appended again, so a lost state file or an interrupted run can't duplicate it.
  - A bad line or an unknown id is reported and the rest still apply; the exit code is 1 if anything was reported.
- **Stale phone decisions:** each card carries a fingerprint of its question, key facts and gold URLs. A decision stored in the browser for an older version of the same id is ignored.
- **Not done:** `--apply` lines carry no fingerprint, so a decisions file written against an older draft of the same ids would be applied to the new draft. Don't run `sda-eval-draft --force` between exporting and applying.
