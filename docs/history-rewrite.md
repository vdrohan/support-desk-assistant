# Commit hashes in older files

The history of this repository was cleaned up a few times: first the author email on the first commit (2026-10-20, before the repository was public), then the wording of commit messages (2026-11-03). File contents are identical across all of them. Only commit hashes changed, so the `git` field in the committed result files and a few hashes in older decisions (for example `git 3992ac7`, or "clean tree at 2d6cd0c") point at earlier versions of the same commits.

Find the hash you have in the first column; the second column is the same commit today.

| hash in older files | hash now | subject |
|---|---|---|
| c5ad2a7 | 4165cc4 | Merge pull request #4: Phase 4 and README first-screen rewrite |
| c0fb84f | 3c101cd | docs: rewrite README first screen with results table and examples |
| 8d0b1ce | e1fe534 | docs: describe Phase 4 as built and checked in CI, not deployed |
| 6d85788 | 0b056d1 | feat: add API key, Dockerfile and Compose service (Phase 4) |
| 6840460 | 240b910 | Merge pull request #2: Phases 2 and 3, triage and escalation |
| 8824009 | cffa919 | feat: add ticket override endpoint (POST /tickets/{id}/override) |
| c48a9f7 | fe5e729 | docs: record escalation test run and add the write-up |
| f06e369 | 4345fce | feat: add ticket escalation (needs_review) and triage set v2 |
| d87c305 | 180e231 | eval: record triage test-split run (queue 0.90, urgent recall 0.95) |
| 2d6cd0c | 7ff677c | eval: add 50 triage gold labels with provenance and dev score |
| 492443a | 515a8cc | docs: revise triage eval design (decision 28), README and CLAUDE.md |
| 2360341 | b200902 | eval: design triage eval v2 with scenario-drafted tickets |
| a514436 | 1ee4e67 | feat: add triage eval runner (sda-eval-triage) |
| 3d241f5 | 83e8c98 | feat: add triage eval ticket drafting and review tools |
| bd8e127 | 5a3d1e4 | feat: add ticket triage core, SQLite store and /tickets routes |
| e7fb260 | ab06149 | feat: add POST /ask endpoint and sda-api (decision 27) |
| 22e08e2 | 0b60610 | eval: record held-out out-of-scope probe run (9/10 abstained) |
| 9598eee | a7295c9 | eval: add 10 held-out out-of-scope probes |
| cbf7435 | 21503af | eval: record run 5 on eval set v2 (7/7 abstention) |
| 3992ac7 | 0edc8c0 | eval: add eval set v2 with ev-030 and ev-031 relabelled |
| 42be571 | 7cbe60c | eval: record run 4 (abstained-first schema), recall 4/9 to 7/9 |
| 1e60c6f | b326cab | fix: generate abstained and abstain_reason before the answer text |
| f541018 | 8dcf36a | eval: record run 3 (abstention prompt only), no effect (4/9) |
| 77e5087 | f7fac2e | prompt: abstain when the chunks answer none of the asked parts |
| d2e0a28 | 4fffb61 | docs: map old commit hashes to the rewritten ones |
| 3fa7992 | 6d1bf37 | docs: add judge audit and run 2a; move impact log out of repo |
| 9bf96b4 | 6d1bf37 | docs: add judge audit and run 2a; move impact log out of repo |
| 4798658 | 5cdb5e0 | eval: add 27 constructed absent cases to judge calibration |
| 6dcb05d | 5cdb5e0 | eval: add 27 constructed absent cases to judge calibration |
| 61b56c9 | 860b910 | eval: record answer-eval baseline run 2 (judge v2) |
| 1d87d34 | 860b910 | eval: record answer-eval baseline run 2 (judge v2) |
| 4bc7e49 | 777b206 | feat: judge v2 with reason before verdict and multi-benefit rule |
| 62b960c | 777b206 | feat: judge v2 with reason before verdict and multi-benefit rule |
| 67ee0c1 | 2293757 | eval: record answer-eval baseline run 1 (answer cap 1000) |
| 9ddb972 | 2293757 | eval: record answer-eval baseline run 1 (answer cap 1000) |
| 27a7b3e | 32ec8ad | fix: raise Haiku answer max_tokens from 500 to 1000 (decision 24) |
| 229f4dc | 32ec8ad | fix: raise Haiku answer max_tokens from 500 to 1000 (decision 24) |
| 8f0afec | 641a30b | fix: exclude max_tokens and refusal fallbacks from false-abstention |
| 64b16fe | 641a30b | fix: exclude max_tokens and refusal fallbacks from false-abstention |
| 2c39329 | aaf41a8 | eval: record first full answer-eval run |
| 3020f14 | aaf41a8 | eval: record first full answer-eval run |
| 085f53a | 6bcbed5 | chore: ignore eval review leftovers (candidate backup, decision lists) |
| b1d218b | 6bcbed5 | chore: ignore eval review leftovers (candidate backup, decision lists) |
| cea9162 | 4ffb386 | chore: ignore .claude/worktrees, note decision numbering |
| d917433 | 4ffb386 | chore: ignore .claude/worktrees, note decision numbering |
| 8edf32b | d5cd004 | feat: make --calibrate a dry run by default and enforce the cap |
| 191b270 | d5cd004 | feat: make --calibrate a dry run by default and enforce the cap |
| ed9a5d7 | b5f7bb6 | docs: add decision 23, impact log and answer eval usage notes |
| ed936cc | b5f7bb6 | docs: add decision 23, impact log and answer eval usage notes |
| 6a52424 | c6be8d9 | feat: add answer eval runner with breakdowns and judge calibration |
| ebacb48 | c6be8d9 | feat: add answer eval runner with breakdowns and judge calibration |
| 514caaa | 2d2e590 | feat: add answer eval set v1 (58 questions) |
| 7259776 | 2d2e590 | feat: add answer eval set v1 (58 questions) |
| 60145b3 | f990245 | prompt: tighten eval drafter rules |
| 207bc53 | f990245 | prompt: tighten eval drafter rules |
| 21fd16e | 59d3682 | ci: drop unsupported python-version-file input from setup-uv |
| ecf9f45 | 59d3682 | ci: drop unsupported python-version-file input from setup-uv |
| 9596057 | 3465207 | feat: add Stage A eval set drafting and review tools |
| c6c3fbe | 3465207 | feat: add Stage A eval set drafting and review tools |
| 1feb28c | 387eb1d | prompt: flag gaps in the documents, cite every chunk used |
| c96c6c8 | 387eb1d | prompt: flag gaps in the documents, cite every chunk used |
| 0bd4674 | 656cdd7 | prompt: answer directly when facts are given, trim tangents |
| 9d7b1d0 | 656cdd7 | prompt: answer directly when facts are given, trim tangents |
| 3eda52a | 500b776 | feat: add answer system prompt v1 (rules only) |
| 25ac1b5 | 500b776 | feat: add answer system prompt v1 (rules only) |
| 359b1c8 | 6116960 | feat: implement validate_answer (hand-written) |
| 6e1d41c | 6116960 | feat: implement validate_answer (hand-written) |
| e6d8750 | 1b75995 | feat: add answer step pipeline (validator pending) |
| c0760bc | 1b75995 | feat: add answer step pipeline (validator pending) |
| fc0e2c1 | 513c429 | ci: add lint and test workflow |
| d3b4d5d | 513c429 | ci: add lint and test workflow |
| 957d481 | 5b32011 | eval: record retrieval baseline for canada_docs_v1 |
| 17e7a55 | 5b32011 | eval: record retrieval baseline for canada_docs_v1 |
| 96cab3f | 7ddd78d | feat: add retrieval eval set v1 (37 questions) and runner |
| 06485c3 | 7ddd78d | feat: add retrieval eval set v1 (37 questions) and runner |
| 2101a94 | 74c46b5 | feat: add chunking, embedding, Qdrant indexing and search CLI |
| 9d7ef80 | 74c46b5 | feat: add chunking, embedding, Qdrant indexing and search CLI |
| 45ca7c0 | 7953792 | feat: add canada.ca corpus crawler (5 programs, 129 pages) |
| 9e4174c | 7953792 | feat: add canada.ca corpus crawler (5 programs, 129 pages) |
| 5da2a4a | aaa2005 | chore: add project foundation (src layout, uv, ruff, pytest) |
| 7d917ee | aaa2005 | chore: add project foundation (src layout, uv, ruff, pytest) |
| 1cdb30f | e59fc57 | chore: add project layout, Compose skeleton and env template |
| 0ac2a2f | e59fc57 | chore: add project layout, Compose skeleton and env template |
| 6304003 | f4b90f4 | Initial commit |
