# Decisions

Short architecture decision records. Newest decisions go at the bottom. If a decision changes, add a new entry that supersedes the old one instead of editing it.

Numbering goes from 21 to 23; there is no decision 22.

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

## 20. Answer eval set v1: 58 accepted questions, per-category reporting, frozen after first results, hybrid scoring

- **Decision:** `data/eval/eval_set_v1.jsonl` holds 58 accepted questions, ids `ev-001` to `ev-058`, produced by the process in [decision 19](#19-answer-eval-set-drafted-by-haiku-accepted-by-hand-reviewable-from-a-phone).
- **Review outcome:**
  - The first candidate file had 46 items. During review 20 more were added and 6 reworked, so 66 were reviewed: 58 accepted, 8 rejected, 0 pending. Review ran offline in two rounds of `sda-eval-review --apply`.
  - *Rework:* junk key facts (bare table cells like "20", labels like "Benefit name", fragments like "either", stray `**`) were replaced with verbatim, concrete facts.
  - *Added:* 7 of the maintainer's raw questions with typos kept (the same 7 appear in both `retrieval_v1.jsonl` and the answer set because both came from `data/eval/human_questions.txt`, which lets a failed answer be traced to retrieval or generation), 4 new EI questions, 3 out-of-scope questions, 1 in-domain abstention question, and 5 replacements (below).
  - *Rejected:* `single_page-05` and `multi_page-04` (near-duplicates of other questions' key figures); `multi_page-02`, `-03`, `-07` (one page was enough; replaced by `single_page-19`, `-20`, `-21`); `multi_page-01` (assumed a rule for minors' ID that the pages don't state); `unanswerable_in_domain-01` and `-04` (the corpus answers them: EI "may lose benefits" after 4 weeks, and the SIN page says you need not give your SIN to rent; both became answerable `single_page` questions).
- **Final mix** (accepted vs `EvalSettings.draft_mix`):

  | Category | Accepted | Draft target |
  |---|---|---|
  | `single_page` | 32 | 18 |
  | `multi_page` | 8 | 7 |
  | `confusable` | 6 | 6 |
  | `unanswerable_out_of_scope` | 6 | 3 |
  | `unanswerable_in_domain` | 3 | 4 |
  | `time_sensitive` | 3 | 2 |
  | **Total** | **58** | **40** |

- **Why more than 40:** 40 was the drafter's target, not a goal for the set. The first file already had 46 items, and review added 20 more to fill gaps it found: raw citizen wording, EI coverage, out-of-scope abstention, and replacements for rejected `multi_page` items and reclassified in-domain items.
- **Report per category, never as one number:** `single_page` is 55% of the set (32/58), so a single headline score would be dominated by the easiest category. Four of six categories have 3 to 8 items, so every result carries *n*.
- **Freeze rule** (as in decision 12): once results are published, `eval_set_v1.jsonl` is immutable. Fix only genuine labelling errors, explained in the item's `notes`; anything else goes into `eval_set_v2.jsonl`. Any change to retrieval or generation is judged by comparing result files on the same dataset version.
- **Left out on purpose:**
  - "How long does a passport take?": the only answer is a bare table cell ("10"/"20") with no quotable sentence (decision 13, processing-times table).
  - "What documents do I need to apply for a passport?": the obvious gold page contains an "Error loading fee" placeholder (decision 13).
  - "When do I get paid?": ambiguous across programs. The right behaviour is to ask which benefit, so it is parked for the triage phase.
- **Scoring approach (design only, runner not built yet):** hybrid.
  - Key facts that are numbers or codes (e.g. "$1,507.65", "PPTC 203") are matched by normalized substring, reusing the retrieval eval's normalization (decision 12).
  - Sentence-style key facts are judged by a Haiku rubric, with the key facts as the rubric.
  - The citation validator (`validate_answer`, decision 15) and the abstention check (decision 17) stay deterministic and free.
  - Results are reported per category.
- **Not done:** the scoring runner is not built, no judge calls have been made, and no answer scores exist yet. The three questions above are not in v1. LLM-only judging of every fact was not chosen: it costs tokens on every run and isn't deterministic (same reasoning as decision 12).

## 21. Answer scoring: number gate plus judge, per-category report, capped dry-run-first runner

- **Decision:** `sda-eval-answers` (`src/support_desk_assistant/evals/answers.py`) answers every item of `eval_set_v1.jsonl` through the answer pipeline, scores it and reports per category. It refines the scoring rule of [decision 20](#20-answer-eval-set-v1-58-accepted-questions-per-category-reporting-frozen-after-first-results-hybrid-scoring).
- **Why decision 20's rule changed:** a literal "fact has a digit -> normalized substring" would fail paraphrased sentence facts. 23 of the 48 digit-containing key facts in v1 are 7 words or longer, e.g. `ev-023`'s "Expect a decision by mail within 28 days from when we receive the application", which a correct answer will rarely repeat word for word.
- **Fact decision**, after the normalization of [decision 12](#12-retrieval-eval-versioned-question-set-page-hit-and-answer-hit-oracle-routing), in this order:
  - Abstained, generation not `ok`, or empty answer: every fact absent, method `none`, no judge call.
  - A numeric token of the fact (money, number, percent, ordinal; `$` and thousands commas stripped, `2.0` = `2`, `5%` ≠ `5`; "one" to "ten" in the answer count as digits) missing from the answer: absent, method `numbers`, free.
  - All numbers present, the fact has at most `atomic_fact_max_words` (6) words, and every content word of the fact (alphabetic, at least `fact_content_min_chars` (4) characters, not a stopword, trailing "s" folded) is in the answer: present, method `numbers`, free. A fact with no content words (e.g. "$1,507.65") qualifies on its numbers alone.
  - Everything else goes to the judge: facts without numbers, long facts with their numbers present, and short facts with a content word missing.
  - The content-word guard is there for numbers in the wrong context: for the fact "maximum of $729 a week", an answer saying "the minimum is $729" lacks "maximum", so the fact goes to the judge instead of passing.
- **Judge:**
  - One call per item, covering only the facts that need it. It sees the question, the answer and the numbered facts; no chunks and no citations. Prompt: `prompts/eval_judge.md`.
  - Native structured output (`JudgeResult`: one `fact_index`, `present`, `reason` per fact), no sampling parameters, as in [decision 15](#15-answer-step-native-structured-output-validated-twice).
  - Model `EvalSettings.judge_model` (`claude-haiku-5-5`), the same family as the answer model. That is a known bias (a model may rate its own phrasing kindly), and the reason the hand-review export below exists.
  - Responses are cached in `data/cache/evals/` (gitignored), keyed like the answer cache plus `judge_schema_version`.
  - A refusal, a `max_tokens` stop, invalid JSON or a verdict set that is not exactly the requested fact numbers counts every judged fact of that item as absent and is reported as a judge error.
- **Item rules:**
  - *Answerable* passes when it did not abstain, generation is `ok`, `validate_answer` passes, every key fact is present, the gold page is cited (`multi_page`: every gold page) and no decoy page is cited (`confusable` only). `gold_cited` and `decoy_cited` are separate columns.
  - *Unanswerable* (both categories) passes when the `abstained` flag is set and the validator passes. Strict on the flag: a "the documents don't cover this" reply with `abstained=false` fails. A refusal or `max_tokens` fallback is not a correct abstention. Gate and model abstentions ([decision 17](#17-two-layer-abstention)) are counted separately.
  - *Attribution* of an answerable failure: `retrieval_page` (gold page not in the top k), `retrieval_chunk` (page retrieved but no key fact in a top-k chunk from a gold page, the answer-hit check of decision 12), otherwise `generation`.
- **Report:** one markdown table per group (single page; hard categories: `multi_page`, `confusable`, `time_sensitive`; abstention), one row per category with *n*, no overall number ([decision 20](#20-answer-eval-set-v1-58-accepted-questions-per-category-reporting-frozen-after-first-results-hybrid-scoring)). Below them: facts decided by method, judge errors, and a failures list. Full runs write `answers_<collection>_<model>_<date>.{json,md}` to `data/eval/results/`; partial runs (`--limit`, which is per category, `--category`, or a spend-cap stop) write the same pair to the gitignored `data/eval/runs/`.
- **Cost controls:**
  - The default is a dry run. Anthropic is called only with `--yes`.
  - Spend cap `EvalSettings.spend_cap_cad` (CA$0.25), overridable with `--cap`. Pre-flight, `--yes` is refused (exit 2) if the upper-bound estimate (input from characters, output at `max_tokens`) exceeds the cap. During the run, it stops after the item at which actual spend reaches the cap (exit 1), so the cap can be overshot by at most one item.
  - Answer responses ([decision 18](#18-response-cache-and-prompt-caching)) and judge responses are cached, so a re-run is free and a stopped run resumes.
  - The dry run is not offline: it retrieves every selected question, so it makes Voyage query-embedding calls for questions not in the embedding cache and needs the Qdrant tunnel. Its Voyage cost uses `EmbedSettings.usd_per_mtok`, an UNVERIFIED placeholder (0.06).
- **Hand review:** `sda-eval-answers --export [RESULTS.json]` (offline, seeded) writes `data/eval/judge_review.md` (gitignored) from the newest results file: (A) up to `judge_review_n` (10) judged facts, balanced present/absent; (B) up to `judge_review_gate_n` (4) facts the number check marked present; (C) every `unanswerable_in_domain` answer. Each fact has a blank "Your verdict" line.
- **Not done:**
  - No baseline run exists yet. Answer scores: Pending.
  - Whether time-sensitive answers state the date is not scored.
  - A short fact whose numbers and content words all appear in a wrong-context answer is still marked present without the judge. Export section B samples these.
  - `execute()` still appends eval calls to `logs/requests.jsonl`, mixed with `sda ask` traffic.
  - A same-day re-run overwrites that day's results file.
  - No `--collection` option: the runner uses the configured `IndexSettings.collection`.

## 23. Answer eval runner: overall rows, abstention and citation metrics, provenance, hard cap, blind judge calibration

- **Decision:** extend `sda-eval-answers` rather than add a second CLI. New modules: `evals/stats.py` (Wilson interval, confusion counts, Cohen's kappa), `evals/breakdown.py` (overall, by program, abstention, citation, failure attribution), `evals/provenance.py` (reproducibility block), `evals/calibrate.py` (judge vs human labels).
- **Supersedes:**
  - The "no overall number" rule of [decision 20](#20-answer-eval-set-v1-58-accepted-questions-per-category-reporting-frozen-after-first-results-hybrid-scoring) and [decision 21](#21-answer-scoring-number-gate-plus-judge-per-category-report-capped-dry-run-first-runner)'s report. Overall rows are now shown: answerable pass rate as micro (over items) with macro (mean of the answerable category rates) beside it, and abstention recall as a separate row. The answerable pass rate and abstention recall are never blended. Category tables stay first and remain the primary view; decision 20's reason for them (`single_page` is 32 of 58 items) stands.
  - From decision 21's **Not done**: the output format, the same-day overwrite and the soft cap (below). Decision 21's other points stand.
- **Metric definitions** (`breakdown.summarize`, every rate with k, n and a 95% Wilson interval):
  - *Abstention:* positive class = should abstain (the 9 unanswerable items). Recall = correct abstentions / 9. Precision = correct abstentions / intentional abstentions, where intentional means status gate or ok (gate or model abstention). Refusal and `max_tokens` fallbacks are not in the denominator; they are counted as generation failures in their own row. Also reported: by gate, by model, false abstentions on answerable items, missed.
  - *Citation validity:* `validate_answer` pass rate over non-abstained answers with status ok. Abstentions are excluded because an empty, uncited abstention passes trivially. Gold cited and mean citation precision (cited pages that are gold) are reported for the answerable ones.
  - *By program:* answerable items joined to programs through their gold URLs in `data/manifest.jsonl`. An item whose gold pages span programs counts under each (1 item in v1, so the program rows sum to 50 for 49 answerable items). Unanswerables have no gold pages and get one `n/a` row with their correct-abstention rate.
  - *Failure attribution:* shares of failed answerable items by `retrieval_page`, `retrieval_chunk`, `generation` (rules from decision 21).
  - *Why Wilson intervals:* n is small (9 unanswerables; programs 7 to 14 answerable items), and Wilson behaves at k = 0 and k = n where the normal approximation doesn't. z is `EvalSettings.ci_z` (1.96). Every row shows its real n.
- **Output files:** a run writes three files named `answers_<collection>_<model>_<run_id>`: `.jsonl` (one result row per item), `.summary.json` (meta and metrics) and `.md`. The run id is a UTC timestamp (`YYYYMMDDTHHMMSSZ`), so runs on the same day no longer overwrite each other.
- **Provenance** (`meta.provenance`): run id and time; git sha, dirty flag, a hash of the uncommitted diff plus untracked paths; dataset name and sha256; manifest sha256, collection, Qdrant points count, embed model and dimension; answer and judge prompt sha256; answer and judge model ids with their profiles (`max_tokens`, thinking, effort); k, `min_score`, prompt caching, schema versions, `usd_cad`, `prices_as_of`; the scorer config (scorer version, `atomic_fact_max_words`, `fact_content_min_chars`, stopwords, number words) and its sha256; SDK versions (anthropic, voyageai, qdrant-client, pydantic).
  - The corpus and index fingerprint is there because an answer score depends on what was retrieved: a recrawl or reindex changes it with no code change.
  - The scorer-config hash is there because the number gate decides facts without the judge: changing a stopword or a threshold can change a score, and two runs are only comparable if the hash matches.
- **Hard cap:** before each item, including the first, the run stops if the spend so far (Voyage for the plan, answers, judge) plus that item's upper bound would pass the cap. The cap is never crossed by a planned bound. A stopped run exits 1 and is partial. The pre-flight refusal (exit 2 if the whole-run estimate is over the cap) is unchanged.
- **Spent vs list cost:** the report gives tokens by kind (input, output, cache write, cache read) for answers and the judge, then CAD spent this run and list CAD (what the Anthropic responses cost when they were made, from the usage stored with the cached response). A cached re-run otherwise reports CA$0, which says nothing about what a fresh run costs.
- **Dirty tree:** a full run from a working tree with uncommitted changes goes to the gitignored `data/eval/runs/` (the summary's `results_dir_reason` says why), unless `--allow-dirty`. Partial runs (`--limit`, `--category`, a cap stop) always go to `runs/`. A committed result must be tied to a commit.
- **Calibration protocol:**
  - `--export` now writes a **blind** sheet: the judge and number-check verdict lines are left out (`--reveal` shows them, and such a sheet must not be imported). Section A has up to `judge_review_n` (40) judge-decided facts, balanced on the hidden verdict; section B up to `judge_review_gate_n` (10) facts the number gate marked present; section C every `unanswerable_in_domain` answer (not imported).
  - `--import-labels SHEET [--from RESULTS]` turns the filled sheet into records in `data/eval/judge_calibration.jsonl` (committed). Each record is self-contained: question, answer, the fact, and for judge facts the full list of facts sent in the original judge call with the fact's index, so the replay sends the exact production message. Duplicates are skipped; a label that conflicts with a stored one is reported and not written.
  - `--calibrate [FILE] [--yes] [--cap CAD]` replays the judge on the judge records and reports agreement with a Wilson interval, Cohen's kappa, the confusion matrix, false present (of human-absent; inflates scores) vs false absent (of human-present; deflates scores), per-category agreement and every disagreement with the judge's reason. Number-gate records are compared with the verdict stored at labelling time, no API call, in a separate section. Drift counts replayed judge verdicts that differ from the machine verdict stored at labelling time. Output goes to `runs/judge_calibration_<run_id>.{json,md}`. Dry run first, as in decision 21: cached verdicts are always replayed for free, but if any judge call is a cache miss nothing is sent without `--yes`; with `--yes` the run is refused (exit 2) if the upper bound of the missing calls passes the cap (`--cap`, default `spend_cap_cad`) and stops (exit 1) before any call that would pass it, while groups already cached are still replayed. A changed judge prompt or model therefore turns the next `--calibrate` into a dry run.
  - *Minimum n:* below `calibration_min_n` (30) labelled judge facts the report is marked provisional. With 10 labels, 9/10 agreement has a 95% interval of [0.60, 0.98]; at 27/30 it is [0.74, 0.97]. That is why `judge_review_n` went from 10 to 40 (and `judge_review_gate_n` from 4 to 10).
- **Dataset guard:** `tests/test_eval_dataset.py` pins that the three questions left out of v1 (decision 20) are absent.
- **Freeze reminder** (decision 20): do exploratory runs to `runs/`, fix genuine labelling errors found there, then commit and run the baseline from a clean tree. After that, v1 is frozen.
- **Rejected:**
  - A separate `sda-eval-run` CLI: it would duplicate most of the 2,186-line `answers.py` (plan, cache, cap, scoring).
  - Per-category tables only for the overall view: hides the total a reader asks for first. Micro only: dominated by `single_page`, so macro sits beside it.
  - Relaxed abstention (counting a "the documents don't cover this" reply with `abstained=false` as correct): the strict flag from decision 21 is kept, because the flag, not the wording, is what calling code can act on.
  - Showing the judge verdict to the labeller: anchoring would inflate agreement.
  - A Sonnet cross-judge now: costs a second judge on every run; deferred unless kappa comes out low.
  - A soft cap checked after each item: can overshoot by one item.
- **Not done:**
  - No baseline run exists, so there is no answer score, cost per run or judge agreement. Pending: baseline run.
  - `data/eval/judge_calibration.jsonl` has no labels yet (the file does not exist until the first `--import-labels`).
  - Judge non-determinism is untested: no `--fresh-judge` variance check and no `--compare A B` verdict-flip report.
  - The run id has one-second resolution.
  - Eval calls still append to `logs/requests.jsonl`.
  - Time-sensitive answers are not checked for stating the date.
  - The Haiku judge shares a model family with the Haiku answerer.
  - The Voyage price (`EmbedSettings.usd_per_mtok`) and `usd_cad` remain placeholders.

## 24. Haiku answer cap raised from 500 to 1000 tokens after the first full run

- **Decision:** `AnswerSettings.models["claude-haiku-5-5"].max_tokens` goes from 500 to 1000. The first full answer run (2026-10-19, run `20261019T133733Z`, committed as run 0) stays in `data/eval/results/` as a record; the re-run with the new cap is the baseline.
- **Reason:** 8 of the 58 answers stopped at `max_tokens` (3 `single_page`, 4 `multi_page`, 1 `confusable`), which is 8 of the 15 failed answerable items. The pipeline turns a truncated response into a fallback abstention (decision 15), so the scorer counted each as a generation failure. The cached raw responses show the answers themselves were complete and on topic; the cut fell in the trailing JSON (`abstained`, `abstain_reason`) after the answer text and four citation UUIDs. Successful answers had a median of 305 output tokens, a 90th percentile of 442 and a maximum of 496, so 500 left almost no headroom. A cap that clips the output format measures the cap, not the model or the prompt.
- **Why decision 20's freeze still holds:** the freeze is on the dataset (`eval_set_v1.jsonl`: questions, gold pages, key facts), which does not change. This changes a generation setting. The answer cache key includes `max_tokens` (decision 18), so all 58 answers are regenerated; output is billed on tokens produced, so the higher cap costs more only for answers that were being cut.
- **Not changed:** the prompt (its sha256 in the provenance stays comparable), the Sonnet profile (4000), retrieval, chunking and the index. The `max_tokens: 600` in `ChunkSettings` is the chunk size, a different setting.
- **Rejected:**
  - Keeping run 0 as the baseline and noting the truncation: the baseline would mix model quality with the cap.
  - Asking the prompt for shorter answers instead: a prompt change confounds the comparison with run 0, and the answers were not rambling. Worth a separate decision if cost or length becomes a concern.
- **Also fixed:** the per-category "false abstention" column counted any abstained row, including these truncation fallbacks, while the overall table (decision 23) counted only gate and model abstentions. Both now use the decision 23 rule; the run 0 files still show the old category numbers.

## 25. Judge v2: reason before verdict, multi-benefit rule; calibration labels written by Claude

- **Decision:** `JudgeItem` field order becomes `fact_index`, `reason`, `present` (was `fact_index`, `present`, `reason`), the judge prompt asks for the reason first, and `judge_schema_version` goes 1 -> 2. The prompt gains one rule: a fact that lists several programs or benefits is stated when the answer states it for the one the question is about (an explicit exception to "ignore the question's wording"; a first wording without the exception was ignored by the judge).
- **Reason:** first calibration (50 labelled facts; 37/40 judge agreement, all 3 disagreements judge-absent vs label-present). The raw replies showed two causes:
  - *Verdict before reasoning.* The schema put `present` first and the judge runs with thinking off, so the model decided before it reasoned. Two replies (ev-012, ev-021) have `"present": false` and a reason that concludes the fact is stated; the ev-012 reason even ends in a stray `','present':true}`. Replies for the same fact with `reason` first were consistent with their reasoning. Not a parse bug: the JSON was valid.
  - *Ambiguous multi-benefit fact* (ev-033: "GIS, Allowance or Allowance for the Survivor", answer names only the Allowance, which is what the question asks about). The cached replies split 3 absent to 1 present. The new prompt rule settles it in favour of present. The frozen dataset (decision 20) is not edited.
- **Effect on results:** judge cache keys change (schema version, prompt), so every judged fact is re-asked on the next run; answer cache keys do not change. The baseline answerable pass rate (0.78, run 1) was scored with judge v1 and must be re-recorded with v2 before it is quoted next to later runs.
- **Calibration labels:** the 50 records in `data/eval/judge_calibration.jsonl` were labelled blind by Claude, not a person (the field and the report call them `human`). They are to be read as Claude vs Haiku agreement until the disagreements are reviewed by a person.
- **Result of the replay (2026-10-20, same 50 labels):** agreement 37/40 -> 40/40 (kappa 0.38 -> 1.00); the other two disagreements went away with the field reorder alone, ev-033 needed the prompt rule. This is in-sample: the ev-033 rule was written for an item in the calibration set, and the first replay ran with the main checkout's copy of the prompt, which differs by a few lines from this repo's committed one. Treat 40/40 as "the known failures are fixed", not as a measured accuracy.
- **Known limits:**
  - Before the constructed cases below, the set had 1 absent fact out of 40 judged, so the judge's false-present rate (the direction that inflates scores) was unmeasured: 0/1, 95% interval [0.00, 0.79].
  - Same-input judge flips are visible in the cache (ev-021, ev-033). Not yet quantified; the decision 23 non-determinism check is still open.
- **Rejected:**
  - Turning thinking on for the judge: more cost and latency on every call, when reordering the fields addresses the observed failure.
  - Editing the ev-033 key fact: the dataset has published results.
- **Constructed absent cases (added 2026-10-20):** 27 records in `data/eval/judge_calibration.jsonl` with `item_id` suffix `~del` (15) or `~val` (12). They are made from labelled-present judge facts: `~del` deletes the sentence(s) that state the fact, `~val` changes the fact's number everywhere it appears in the answer (e.g. 45 weeks -> 52). The label `absent` is true by construction; I read every `~del` answer to confirm no remaining sentence states the fact, and left out the ones where a remaining sentence implied it. Their `machine_present` is `false` (no machine verdict exists at construction time), so the drift row ignores them. `source_run` is `constructed:delete-sentence` or `constructed:change-number`.
  - *Replay (judge v2, 77 records):* agreement 66/67 = 0.98 [0.92, 1.00], kappa 0.97; false present 1/28 = 0.04 [0.01, 0.18]; false absent 0/39. The one false present is `ev-039~del`: the retained sentence says the user is "about three months past" their last day of work, and the judge infers "more than 4 weeks" from it. That is an implication, which the prompt says does not count, so the label stays absent.
  - *Limits:* these are easy negatives (a whole sentence removed, or a number changed). They show the judge does not wave facts through when nothing relevant is left; they do not test subtle partial mentions or paraphrases that drop one condition. Realistic absent cases from model failures are still missing.
- **Ablation from a parallel run (added 2026-10-20):** another session made the field reorder alone (no multi-benefit rule) on branch `claude/project-thread-qwep98`, commit 23ad7d0, run `20261019T174456Z`, files committed in `data/eval/results/`. Same 58 cached answers: answerable pass 40/49 = 0.82 [0.69, 0.90]. Adding the multi-benefit prompt rule (run `20261020T132433Z`) gives 41/49 = 0.84. So the reorder is worth +2 items (ev-021, ev-006) and the rule +1 (ev-033); the interval overlap says none of it is distinguishable from noise at n = 49. In that run ev-011[0] flipped to a false "missing" (the reason mixed in the user's own situation); it passes in the later run. The same session's independent audit of run 1 (all 52 judge-decided facts, graded blind by Claude, contested rows reviewed by the maintainer) found the same four judge errors as the calibration here: [docs/judge_audit_run1.md](judge_audit_run1.md).

## 26. Abstention: decide before writing (answer schema field order), eval set v2

- **Decision:** `AnswerResult` fields are generated in the order `abstained`, `abstain_reason`, `answer`, `citations` (was `answer`, `citations`, `abstained`, `abstain_reason`); `AnswerSettings.schema_version` goes 1 -> 2. The answer prompt's abstain rule now says related information that does not answer the question does not count, and its partial-answer rule applies only when at least one part of the question is answered; if none is, abstain without filler. `data/eval/eval_set_v2.jsonl` relabels two v1 items (ev-030, ev-031) from `unanswerable_in_domain` to `single_page`. v1 is unchanged.
- **Reason:** run 2 had abstention recall 4 of 9. Reading the five misses: ev-027, ev-029 and ev-057 opened with "The documents do not describe/say…" and then added related material with `abstained: false`. Structured output is generated in schema order, so the model wrote the answer prose before it decided whether to abstain, the same flaw as the judge in decision 25. ev-030 and ev-031 were labelling errors: the documents do answer them (automatic GIS enrollment with a letter after the 64th birthday; "use the calculator separately"), and ev-030's v1 note described a different question.
- **Measured one change at a time (same 58 questions, answers regenerated each time):**

| run | change | answerable pass | abstention recall |
|---|---|---|---|
| 20261020T132433Z (run 2) | none (baseline) | 41/49 = 0.84 | 4/9 = 0.44 |
| 20261020T184743Z (run 3) | prompt rules only | 40/49 = 0.82 | 4/9 = 0.44 |
| 20261020T185226Z (run 4) | + schema order | 41/49 = 0.84 | 7/9 = 0.78 [0.45, 0.94] |
| 20261020T185710Z (run 5, v2 labels) | same answers, ev-030/031 relabelled | 42/51 = 0.82 | 7/7 = 1.00 [0.65, 1.00] |

  - The prompt wording alone did nothing: the same five items stayed answered. The order change flipped exactly ev-027, ev-029 and ev-057 to abstentions, with no false abstentions on answerable items (0 in every run) and the answerable pass rate unchanged.
  - Run 3 -> run 4 also shows judge/answer noise: ev-033 failed in run 3 and passed in run 4 with a different answer text. Differences of one item are not distinguishable from noise at this n.
  - Run 5 re-scores run 4's cached answers; only ev-030 and ev-031 are scored differently. ev-030 fails under v2 because the judge marks "you'll get a letter shortly after your 64th birthday" absent (the answer places the letter at the 64th birthday, without "shortly after"). That key fact was written for v2 before the run and is left as is.
- **Limits:**
  - In-sample: the change was designed by reading these five items, and the three that flipped are those items. 7 of 7 has a 95% interval of [0.65, 1.00].
  - Held-out check (out-of-scope only): `data/eval/probes_out_of_scope_v1.jsonl`, 10 questions written after this decision and before any output for them was read (run `20261020T185808Z`). 9 of 10 abstained (1 by the score gate, 8 by the model). The miss, pr-005 ("When is my income tax return due this year?"), was answered with the April 30 filing rule for OAS, GIS and Allowance recipients that the documents do contain, so it is a poorly chosen probe, not a clear failure. This does not test the in-domain half (corpus covers the topic but not the detail): no verified held-out in-domain questions exist yet, and there is no pre-change baseline for these ten.
  - Precision is 7 of 7 on this set; false abstentions on answerable questions were 0 of 49 (51 in v2), but this set has few borderline answerable questions.
  - v1 stays the dataset with published results; use v2 labels (`SDA_EVAL__EVAL_SET_FILE=eval_set_v2.jsonl`) for new comparisons, and do not compare a v2 pass rate with a v1 one directly (51 vs 49 answerable items).
- **Rejected:**
  - A post-hoc string check on "the documents do not say…" openings: brittle, and it would hide the model's decision instead of fixing it.
  - Turning thinking on for the answer model: more cost on every call; reordering addresses the observed failure.
  - A retrieval-score threshold: the best out-of-corpus score is 0.525 and some in-domain misses score higher than answerable questions (retrieval eval, 2026-10-08).

## 27. /ask endpoint: same pipeline as the CLI, validated answers only, no auth, loopback only

- **Decision:** `POST /ask` (`api/app.py`, `create_app`) calls the same `prepare` and `execute` as `sda ask`, so retrieval, the score gate, the response cache, validation and the request log behave identically. Settings (`ApiSettings`): host `127.0.0.1`, port 8000, question length cap 1000, `k` cap 20. `GET /health` for liveness. `sda-api` runs it with uvicorn.
- **Validated answers only:** if `validate_answer` fails (for example a citation that was not retrieved), the response is an abstention with status `validation_failed` and a fixed reason; the answer text is not returned. The CLI prints the failure and the text; an HTTP client has no one to read a warning.
- **Response shape:** `abstained`, `answer`, `abstain_reason`, `citations` (numbered, title and URL, in the model's order), `status` (`ok`, `gate`, `validation_failed`, or a generation failure such as `max_tokens`), `model`, `cost_cad`, `latency_ms`, `cache_hit`. Generation failures are HTTP 200 with `abstained: true` and the status, because the pipeline already turns them into abstentions (decision 15).
- **Errors:** invalid input 422 (empty or over-long question, unknown program, `k` out of range); search index or missing system prompt 503; Anthropic or Voyage failures 502. Error bodies carry no upstream detail.
- **Threading:** the endpoint is a plain `def`, so FastAPI runs the blocking Voyage, Qdrant and Anthropic calls in worker threads. Clients are built on the first request, so the app imports without keys.
- **Verified:** 16 offline tests (`tests/test_api.py`, stubbed ask function; the default wiring is patched); one real run against Qdrant, Voyage and Claude: an EI sickness question returned a cited answer in 2.5 s for CA$0.0007, an off-topic question was abstained by the score gate, an unknown program gave 422.
- **Not done:**
  - No authentication, rate limiting or request-size limit beyond the question length: do not expose it beyond loopback or the Tailscale host without a proxy in front.
  - No per-request spend cap; every uncached request calls Claude. The CLI and eval spend caps do not apply here.
  - No streaming, no conversation history, no Dockerfile or Compose service for the API (compose.yaml still runs Qdrant only).
  - Concurrency was not load-tested; the response cache and request log append are plain files.
- **Rejected:**
  - Returning unvalidated answers with a warning flag: callers would ignore the flag.
  - Async clients throughout: the pipeline and its tests are synchronous; threads give concurrency without a rewrite.
  - A Dockerfile now: the deployment plan is Phase 3 and the API is not exposed yet (README roadmap).

## 28. Ticket triage: queue and priority, labels defined in the prompt, tickets drafted by Haiku and reviewed by a person

- **Decision:**
  - Output per ticket: `queue` (`ei`, `cpp`, `oas`, `sin`, `passport`, `other`) and `priority` (`urgent`, `high`, `normal`, `low`). Not intent, not routing beyond the queue: escalation is Phase 3.
  - `TriageResult` fields are generated `rationale`, `queue`, `priority` (reasoning before labels, decisions 25 and 26). Haiku, thinking off, `max_tokens` 400, cached like the answer pipeline (`data/cache/triage/`).
  - The priority rules are in `prompts/triage.md`: stakes and a time limit, not tone. Urgent = money or status at immediate risk, a deadline within 7 days, someone else using the person's SIN, a passport lost abroad, travel within 7 days; high = time-bound but not within days; normal = own situation, no time pressure; low = general information.
  - Storage: SQLite (`TicketStore`), one short-lived connection per call so FastAPI worker threads are safe. A ticket whose triage fails (refusal, `max_tokens`, invalid output) is stored with status `triage_failed` and null labels, so a person can triage it; Anthropic errors store nothing and return 502.
  - API: `POST /tickets`, `GET /tickets/{id}`, `GET /tickets?queue=&priority=&limit=&offset=`, with limits from `TriageSettings` (body 4000 characters, subject 200, page size 100).
- **Eval set:** 68 candidate tickets drafted by Haiku, one request per (queue, priority) cell (3 per cell for the five programs, 2 per priority for `other`), styles rotating over plain, emotional, short and mixed. A person accepts, rejects or relabels each on a phone page (`sda-eval-triage-review`); the accepted set is `data/eval/triage_set_v1.jsonl` with the reviewer's labels and the draft labels kept beside them. `sda-eval-triage` reports queue and priority accuracy with Wilson intervals, urgent recall and precision, under- and over-triage, per-queue precision and recall, confusion matrices and a by-style table; dry run by default, capped, partial or dirty runs go to `data/eval/runs/`.
- **Known weakness:** the drafter and the classifier are told the same priority rules, so the classifier can look good on tickets written to fit them. The independent check is the person who reviews the labels, and the `emotional` and `mixed` styles test whether tone or a second topic moves the result. Expect the first score to be optimistic; the next dataset (v2) should add tickets written without the rules in view.
- **Not done:**
  - No authentication or rate limit on `/tickets` (same as `/ask`; loopback only).
  - Tickets are stored and sent to the model as typed: no PII redaction, no retention limit, no deletion endpoint.
  - No human override or re-triage endpoint, no ticket status workflow beyond triaged and triage_failed.
  - No escalation or confidence score (Phase 3).
  - No scores yet: they wait for the reviewed set.
- **Rejected:**
  - An intent label as well: about twice the labelling work and more places for labels to disagree at this sample size.
  - Templated tickets: free and fast but less realistic, and the labels would be mine alone.
  - Reusing the answer-eval review tooling: it is built around pages and key facts; a small separate page was less work than generalising it.
