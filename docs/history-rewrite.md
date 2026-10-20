# History rewrite, 2026-10-20

The repository's history was rewritten once, before it was made public, to replace a personal author email on the first commit with the GitHub noreply address. File contents are byte-for-byte unchanged (every tree hash is identical); only commit hashes changed. Commit hashes cited in older result files and decision records (for example the `git` field in the answer-eval `.summary.json` files) are the old ones. Old to new:

| old | new | subject |
|---|---|---|
| 9bf96b4 | 3fa7992 | Fold in the parallel judge audit and reorder-only run 2a; move the impact log out of the repo |
| 6dcb05d | 4798658 | Calibration: 27 constructed absent cases (false present 1/28); README disclaimer and layout fix |
| 1d87d34 | 61b56c9 | Record answer-eval baseline run 2 (judge v2): results, impact log, README status |
| 62b960c | 4bc7e49 | Judge v2: reason before verdict, multi-benefit rule; first judge calibration labels (decision 25) |
| cd81c07 | 2c16956 | docs: run 2 impact entry, judge audit record, decision 25 outcome |
| 08ec2cb | 9c27ebc | Record answer-eval run 2 (reason-first judge): results |
| 23ad7d0 | 4ee41bb | judge: reason before verdict (decision 25) |
| 9ddb972 | 67ee0c1 | Record answer-eval baseline run 1 (answer cap 1000): results, impact log, README status |
| 229f4dc | 27a7b3e | config: raise Haiku answer max_tokens 500 -> 1000 (decision 24) |
| 64b16fe | 8f0afec | fix: category false-abstention rate excludes max_tokens/refusal fallbacks |
| 3020f14 | 2c39329 | Record first full answer-eval run: results, impact log, gitleaks allowlist for cache keys |
| b1d218b | 085f53a | chore: ignore eval review leftovers (candidate backup, decision lists) |
| d917433 | cea9162 | chore: ignore .claude/worktrees, note decision numbering |
| 191b270 | 8edf32b | --calibrate: dry-run by default, cap enforced |
| ed936cc | ed9a5d7 | docs: decision 23, impact log and usage notes for the answer eval runner |
| ebacb48 | 6a52424 | feat: answer eval runner with breakdowns, provenance and judge calibration |
| 7259776 | 514caaa | feat: answer eval set v1 (58 questions), decision 20, impact log |
| 207bc53 | 60145b3 | prompts: tighten eval drafter rules for multi_page, verbatim facts, empty unanswerable key_facts |
| ecf9f45 | 21fd16e | ci: drop unsupported python-version-file input from setup-uv |
| c6c3fbe | 9596057 | feat: Stage A eval set drafting and review (offline, phone-friendly) |
| c96c6c8 | 1feb28c | prompt: flag gaps in the documents, cite every chunk used |
| 9d7b1d0 | 0bd4674 | prompt: answer directly when facts are given, trim tangents |
| 25ac1b5 | 3eda52a | feat: answer system prompt v1 (rules only) |
| 6e1d41c | 359b1c8 | feat: implement validate_answer (hand-written) |
| c0760bc | e6d8750 | feat: answer step pipeline (validator pending) |
| d3b4d5d | fc0e2c1 | ci: add lint and test workflow |
| 17e7a55 | 957d481 | Eval: baseline results for canada_docs_v1 at clean commit |
| 06485c3 | 96cab3f | Phase 1: retrieval eval set v1 (37 Qs) + runner; baseline on canada_docs_v1 |
| 9d7ef80 | 2101a94 | Phase 1: chunk, embed (voyage-4), index to Qdrant, search CLI |
| 9e4174c | 45ca7c0 | Phase 1: canada.ca corpus crawler (5 programs, 129 pages) |
| 7d917ee | 5da2a4a | Add project foundation: src layout, uv, ruff, pytest, pre-commit, docs |
| 0ac2a2f | 1cdb30f | Add project layout, Compose skeleton with Qdrant, env template |
| 6304003 | f4b90f4 | Initial commit |
