# Impact log

What each project phase produced, kept as source material for CV accomplishment statements. Every number was checked against the repo; the **Evidence** part names the file, command, decision or commit it comes from.
"Pending: baseline run" means the measurement does not exist yet. "Source: maintainer's notes" means the fact is not verifiable from this repo.
Verified against commit 207bc53 on 2026-10-12.

## Infrastructure: home server

- **Built:** a 2017 MacBook Air set up as the project's deploy host (decision 4).
- **How:** Source: maintainer's notes (not verifiable from this repo): Ubuntu Server, key-only SSH, ufw, Tailscale and Docker. In the repo: Docker Compose runs Qdrant with the image pinned (`qdrant/qdrant:v1.19.1`) and the only published port bound to `127.0.0.1` (`127.0.0.1:6333:6333`); the host is reached only over Tailscale SSH, with SSH tunnelling to localhost ports.
- **Result:** all embedding and generation goes through cloud APIs because the hardware has no usable GPU and little RAM; the host runs orchestration and Qdrant only.
- **Evidence:** `compose.yaml`; `docs/decisions.md` #4 and #6; `README.md` (Deployment paragraph); commit 0ac2a2f (Compose skeleton with Qdrant).

## Phase 1: corpus and retrieval

- **Built:** a polite canada.ca crawler, a chunk -> embed -> index pipeline into Qdrant, a search CLI (`sda-search`) and a retrieval eval runner (`sda-eval-retrieval`) with a versioned question set.
- **How:** protego for robots.txt, BeautifulSoup + markdownify for content; heading-aware chunking (target about 350 tokens, hard max 600, counted with voyage-4's tokenizer) with within-page dedup; `voyage-4` embeddings (1024-dim float, asymmetric `input_type`, SQLite embedding cache); Qdrant collection `canada_docs_v1` with deterministic IDs and a mass-delete guard; page-hit and answer-hit metrics, unfiltered and oracle-filtered by program.
- **Result:** corpus of 129 pages across 5 programs (cpp 30, ei 30, passport 30, oas 28, sin 11). The current corpus chunks to 735 chunks (median 229 tokens, max 600, 0 over max); dedup dropped 1,702 repeated blocks on 29 pages, and passport went from 445 to 204 chunks. First full embedding run: 174k tokens; re-runs on an unchanged corpus: 0. Qdrant point count: not verified in this pass (decision 10 says about 735 points). Retrieval baseline on `retrieval_v1` (37 questions, n = 34 answerable, k = 5): unfiltered page_hit@5 0.912, answer_hit@5 0.882, page_hit@1 0.647; oracle page_hit@5 0.912, answer_hit@5 0.882, page_hit@1 0.676.
- **Evidence:** `wc -l data/manifest.jsonl` and its `program` field; in-memory `chunk_corpus` run (same code path as `uv run sda-ingest --dry-run`, no API calls); `docs/decisions.md` #9 (dedup, passport 445 -> 204), #10 (174k tokens, re-runs 0), #11, #12; `data/eval/retrieval_v1.jsonl`; `data/eval/results/canada_docs_v1_2026-10-08.json` (`metrics.*.overall`); commits 9e4174c (crawler), 9d7ef80 (chunk, embed, index, search), 06485c3 (eval set and runner), 17e7a55 (baseline results at a clean commit).

## Phase 1: answer step with citation validator

- **Built:** an answer pipeline (`sda ask`) that returns a grounded answer citing retrieved chunk IDs, or abstains, with a hand-written citation validator.
- **How:** native structured output (`output_config` JSON schema from the pydantic `AnswerResult`); `validate_answer` checks that every citation was retrieved, non-abstained answers cite, and abstentions are empty and uncited; two-layer abstention (a retrieval-score gate before any API call, then the model's own `abstained` flag); a response cache keyed on the rendered prompt; cost accounting in USD and CAD; `claude-haiku-5-5` as the default model with `claude-sonnet-5-5` in config for a one-off comparison; optional Anthropic prompt caching on the system prompt.
- **Result:** 44 offline unit tests (`test_validate_answer.py` 8, `test_answer_pipeline.py` 31, `test_answer_cost.py` 5). Cost per smoke run: not recorded in the repo; decision 16 calls the USD/CAD rate in config (`usd_cad = 1.42`) a placeholder. Haiku vs Sonnet comparison: not recorded in the repo. Answer quality: Pending: baseline run.
- **Evidence:** `src/support_desk_assistant/answer/`; `uv run pytest --collect-only -q tests/test_validate_answer.py tests/test_answer_pipeline.py tests/test_answer_cost.py`; `docs/decisions.md` #15-#18; commits c0760bc (pipeline), 6e1d41c (`validate_answer`), 25ac1b5, 9d7b1d0, c96c6c8 (system prompt).

## Repo quality

- **Built:** pre-commit hooks, a GitHub Actions CI workflow, Dependabot for the workflow's actions, and a test suite that runs with no network, API keys or Qdrant.
- **How:** pre-commit runs check-added-large-files, check-merge-conflict, check-toml, check-yaml, detect-private-key, end-of-file-fixer, trailing-whitespace, ruff-check (`--fix`), ruff-format and gitleaks; the ruff rev (v0.16.10) matches the `ruff==0.16.10` pin in `pyproject.toml`. CI runs `uv sync --locked`, `ruff check`, `ruff format --check` and `pytest` on push to `main` and on pull requests, with both actions pinned to full commit SHAs and read-only `contents` permission. Dependabot bumps `github-actions` monthly. Tests use fakes and mock transports instead of live network calls.
- **Result:** 233 tests passed in the current run. Full-history gitleaks scan when CI was added: 7 commits, no findings.
- **Evidence:** `.pre-commit-config.yaml`; `pyproject.toml`; `.github/workflows/ci.yml`; `.github/dependabot.yml`; `uv run pytest -q`; `CLAUDE.md` ("No live network in tests"); `docs/decisions.md` #14; commits 7d917ee (pre-commit, ruff, pytest), d3b4d5d (CI and Dependabot), ecf9f45 (CI fix).

## Answer eval set v1

- **Built:** a 58-question answer eval set and offline tooling to draft, review and accept candidates, including review from a phone.
- **How:** `sda-eval-draft` drafts candidates with `claude-haiku-5-5`; `check_candidate` requires every key fact to appear verbatim on a gold page before a candidate can be accepted; `sda-eval-review` runs in the terminal or exports one self-contained HTML file for phone review, and `--apply` replays decisions idempotently. Scoring design decided (hybrid: normalized substring match for numbers and codes, a Haiku rubric for sentence facts, deterministic citation and abstention checks, reported per category); the scoring runner is not built.
- **Result:** 66 candidates reviewed: 58 accepted, 8 rejected, 0 pending. The first candidate file had 46 items; 20 were added during review and 6 of the first 46 were reworked. Accepted mix: single_page 32, multi_page 8, confusable 6, unanswerable_out_of_scope 6, unanswerable_in_domain 3, time_sensitive 3. Ids `ev-001` to `ev-058`, unique and contiguous. 7 accepted questions are the maintainer's raw questions, same text as in `data/eval/human_questions.txt` (typos kept, e.g. `ev-047`, `ev-058`). `check_candidate` on all 58: 0 problems. Highest question-pair word Jaccard: 0.40 (`ev-045` vs `ev-058`), against a near-duplicate threshold of 0.7. Answer accuracy: Pending: baseline run.
- **Evidence:** `data/eval/eval_set_v1.jsonl`; `data/eval/review_state.json` and `data/eval/candidates.jsonl` (gitignored working files); `check_candidate` and `jaccard` from `src/support_desk_assistant/evals/draft_questions.py` run over the set on 2026-10-12; `EvalSettings` in `src/support_desk_assistant/config.py`; `docs/decisions.md` #19 and #20; commits c6c3fbe (drafting and review tooling), 207bc53 (drafter prompt rules); this commit: hash assigned at commit time.
