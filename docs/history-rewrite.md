# History rewrites

## First rewrite, 2026-10-20

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

## Second rewrite, 2026-11-03

The history was reworded once more, on `main` after the repository was public, to give every commit subject one style: `type: summary`, at most 72 characters (decisions #32). 46 of the 55 subjects changed, and what an old subject said beyond the new one moved into the body. For every commit the files, author, committer, dates and parents are unchanged (checked commit by commit, and the final tree is identical), and all 45 `Co-Authored-By` lines were kept. Hashes changed for 54 of the 55 commits, all but the first.

Three things to know:

- **Hashes in older files.** The `git` field in the committed result files (for example `git 3992ac7` in run 5) and phrases such as "clean tree at 2d6cd0c" in decisions #28 are hashes from before this rewrite. Look them up in the table below. A hash from before the first rewrite goes through the first table above, then this one.
- **Pull request pages.** The pages for #2 and #4 may still link to the commits they were merged as; those commits are no longer on `main`.
- **Signatures.** GitHub signed its merge commits for #2 and #4, so they showed "Verified". The reworded copies are unsigned.

| old | new | subject |
|---|---|---|
| c5ad2a7 | 0e65785 | Merge pull request #4: Phase 4 and README first-screen rewrite |
| c0fb84f | 402f487 | docs: rewrite README first screen with results table and examples |
| 8d0b1ce | 786eee1 | docs: describe Phase 4 as built and checked in CI, not deployed |
| 6d85788 | e7424e7 | feat: add API key, Dockerfile and Compose service (Phase 4) |
| 6840460 | c89b44d | Merge pull request #2: Phases 2 and 3, triage and escalation |
| 8824009 | 5d8afe7 | feat: add ticket override endpoint (POST /tickets/{id}/override) |
| c48a9f7 | 161770c | docs: record escalation test run and add the write-up |
| f06e369 | e901fb6 | feat: add ticket escalation (needs_review) and triage set v2 |
| d87c305 | dd0b5c5 | eval: record triage test-split run (queue 0.90, urgent recall 0.95) |
| 2d6cd0c | c9786bd | eval: add 50 triage gold labels with provenance and dev score |
| 492443a | bb66e9f | docs: revise triage eval design (decision 28), README and CLAUDE.md |
| 2360341 | 99deb51 | eval: design triage eval v2 with scenario-drafted tickets |
| a514436 | 842d4e0 | feat: add triage eval runner (sda-eval-triage) |
| 3d241f5 | 51430e0 | feat: add triage eval ticket drafting and review tools |
| bd8e127 | 3a4730d | feat: add ticket triage core, SQLite store and /tickets routes |
| e7fb260 | 47789b9 | feat: add POST /ask endpoint and sda-api (decision 27) |
| 22e08e2 | f73a530 | eval: record held-out out-of-scope probe run (9/10 abstained) |
| 9598eee | cb98ddf | eval: add 10 held-out out-of-scope probes |
| cbf7435 | f8b3b43 | eval: record run 5 on eval set v2 (7/7 abstention) |
| 3992ac7 | a661f70 | eval: add eval set v2 with ev-030 and ev-031 relabelled |
| 42be571 | 92e290c | eval: record run 4 (abstained-first schema), recall 4/9 to 7/9 |
| 1e60c6f | 55d70fd | fix: generate abstained and abstain_reason before the answer text |
| f541018 | e7198af | eval: record run 3 (abstention prompt only), no effect (4/9) |
| 77e5087 | 3264222 | prompt: abstain when the chunks answer none of the asked parts |
| d2e0a28 | 8c2739b | docs: map old commit hashes to the rewritten ones |
| 3fa7992 | cfd190b | docs: add judge audit and run 2a; move impact log out of repo |
| 4798658 | 8c705dd | eval: add 27 constructed absent cases to judge calibration |
| 61b56c9 | 0883e0d | eval: record answer-eval baseline run 2 (judge v2) |
| 4bc7e49 | e6d63b6 | feat: judge v2 with reason before verdict and multi-benefit rule |
| 67ee0c1 | 75cd1e7 | eval: record answer-eval baseline run 1 (answer cap 1000) |
| 27a7b3e | 0dda041 | fix: raise Haiku answer max_tokens from 500 to 1000 (decision 24) |
| 8f0afec | 95540ce | fix: exclude max_tokens and refusal fallbacks from false-abstention |
| 2c39329 | c7e94c9 | eval: record first full answer-eval run |
| 085f53a | 85ccc1d | chore: ignore eval review leftovers (candidate backup, decision lists) |
| cea9162 | 50b59ad | chore: ignore .claude/worktrees, note decision numbering |
| 8edf32b | d9a8d60 | feat: make --calibrate a dry run by default and enforce the cap |
| ed9a5d7 | e2eef46 | docs: add decision 23, impact log and answer eval usage notes |
| 6a52424 | 9314aff | feat: add answer eval runner with breakdowns and judge calibration |
| 514caaa | 2619cf7 | feat: add answer eval set v1 (58 questions) |
| 60145b3 | b9e0a44 | prompt: tighten eval drafter rules |
| 21fd16e | 14c4e4c | ci: drop unsupported python-version-file input from setup-uv |
| 9596057 | 28cd8a7 | feat: add Stage A eval set drafting and review tools |
| 1feb28c | d1b58e0 | prompt: flag gaps in the documents, cite every chunk used |
| 0bd4674 | ca77569 | prompt: answer directly when facts are given, trim tangents |
| 3eda52a | ea5293a | feat: add answer system prompt v1 (rules only) |
| 359b1c8 | 522ce39 | feat: implement validate_answer (hand-written) |
| e6d8750 | f6765d3 | feat: add answer step pipeline (validator pending) |
| fc0e2c1 | 42f9f64 | ci: add lint and test workflow |
| 957d481 | 5b32011 | eval: record retrieval baseline for canada_docs_v1 |
| 96cab3f | 7ddd78d | feat: add retrieval eval set v1 (37 questions) and runner |
| 2101a94 | 74c46b5 | feat: add chunking, embedding, Qdrant indexing and search CLI |
| 45ca7c0 | 7953792 | feat: add canada.ca corpus crawler (5 programs, 129 pages) |
| 5da2a4a | aaa2005 | chore: add project foundation (src layout, uv, ruff, pytest) |
| 1cdb30f | e59fc57 | chore: add project layout, Compose skeleton and env template |

## Third rewrite, 2026-11-03

The `Co-Authored-By: Claude` line was removed from the 47 commit messages that carried it, and one commit body that named a private repository was reworded (decisions #33). Nothing else changed: for every commit the subject, files, author, committer, dates and parents are the same (checked commit by commit, and the final tree is identical). Hashes changed for 50 of the 57 commits; the first seven are untouched.

Hashes in older files may need more than one lookup. A hash from before the second rewrite goes through the second table, then this one. The pull request pages for #2, #4, #6, #7 and #8 still link to the original commits. The two squash commits that GitHub made (#6 and #7) were signed and showed "Verified"; the rewritten copies are unsigned.

| old | new | subject |
|---|---|---|
| d1e49dc | 1465429 | docs: add a what-I-learned section to the README (#7) |
| 1ca63ad | e12afc1 | chore: enforce commit message style and add a changelog (#6) |
| 0e65785 | 4165cc4 | Merge pull request #4: Phase 4 and README first-screen rewrite |
| 402f487 | 3c101cd | docs: rewrite README first screen with results table and examples |
| 786eee1 | e1fe534 | docs: describe Phase 4 as built and checked in CI, not deployed |
| e7424e7 | 0b056d1 | feat: add API key, Dockerfile and Compose service (Phase 4) |
| c89b44d | 240b910 | Merge pull request #2: Phases 2 and 3, triage and escalation |
| 5d8afe7 | cffa919 | feat: add ticket override endpoint (POST /tickets/{id}/override) |
| 161770c | fe5e729 | docs: record escalation test run and add the write-up |
| e901fb6 | 4345fce | feat: add ticket escalation (needs_review) and triage set v2 |
| dd0b5c5 | 180e231 | eval: record triage test-split run (queue 0.90, urgent recall 0.95) |
| c9786bd | 7ff677c | eval: add 50 triage gold labels with provenance and dev score |
| bb66e9f | 515a8cc | docs: revise triage eval design (decision 28), README and CLAUDE.md |
| 99deb51 | b200902 | eval: design triage eval v2 with scenario-drafted tickets |
| 842d4e0 | 1ee4e67 | feat: add triage eval runner (sda-eval-triage) |
| 51430e0 | 83e8c98 | feat: add triage eval ticket drafting and review tools |
| 3a4730d | 5a3d1e4 | feat: add ticket triage core, SQLite store and /tickets routes |
| 47789b9 | ab06149 | feat: add POST /ask endpoint and sda-api (decision 27) |
| f73a530 | 0b60610 | eval: record held-out out-of-scope probe run (9/10 abstained) |
| cb98ddf | a7295c9 | eval: add 10 held-out out-of-scope probes |
| f8b3b43 | 21503af | eval: record run 5 on eval set v2 (7/7 abstention) |
| a661f70 | 0edc8c0 | eval: add eval set v2 with ev-030 and ev-031 relabelled |
| 92e290c | 7cbe60c | eval: record run 4 (abstained-first schema), recall 4/9 to 7/9 |
| 55d70fd | b326cab | fix: generate abstained and abstain_reason before the answer text |
| e7198af | 8dcf36a | eval: record run 3 (abstention prompt only), no effect (4/9) |
| 3264222 | f7fac2e | prompt: abstain when the chunks answer none of the asked parts |
| 8c2739b | 4fffb61 | docs: map old commit hashes to the rewritten ones |
| cfd190b | 6d1bf37 | docs: add judge audit and run 2a; move impact log out of repo |
| 8c705dd | 5cdb5e0 | eval: add 27 constructed absent cases to judge calibration |
| 0883e0d | 860b910 | eval: record answer-eval baseline run 2 (judge v2) |
| e6d63b6 | 777b206 | feat: judge v2 with reason before verdict and multi-benefit rule |
| 75cd1e7 | 2293757 | eval: record answer-eval baseline run 1 (answer cap 1000) |
| 0dda041 | 32ec8ad | fix: raise Haiku answer max_tokens from 500 to 1000 (decision 24) |
| 95540ce | 641a30b | fix: exclude max_tokens and refusal fallbacks from false-abstention |
| c7e94c9 | aaf41a8 | eval: record first full answer-eval run |
| 85ccc1d | 6bcbed5 | chore: ignore eval review leftovers (candidate backup, decision lists) |
| 50b59ad | 4ffb386 | chore: ignore .claude/worktrees, note decision numbering |
| d9a8d60 | d5cd004 | feat: make --calibrate a dry run by default and enforce the cap |
| e2eef46 | b5f7bb6 | docs: add decision 23, impact log and answer eval usage notes |
| 9314aff | c6be8d9 | feat: add answer eval runner with breakdowns and judge calibration |
| 2619cf7 | 2d2e590 | feat: add answer eval set v1 (58 questions) |
| b9e0a44 | f990245 | prompt: tighten eval drafter rules |
| 14c4e4c | 59d3682 | ci: drop unsupported python-version-file input from setup-uv |
| 28cd8a7 | 3465207 | feat: add Stage A eval set drafting and review tools |
| d1b58e0 | 387eb1d | prompt: flag gaps in the documents, cite every chunk used |
| ca77569 | 656cdd7 | prompt: answer directly when facts are given, trim tangents |
| ea5293a | 500b776 | feat: add answer system prompt v1 (rules only) |
| 522ce39 | 6116960 | feat: implement validate_answer (hand-written) |
| f6765d3 | 1b75995 | feat: add answer step pipeline (validator pending) |
| 42f9f64 | 513c429 | ci: add lint and test workflow |
