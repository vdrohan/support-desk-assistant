# Answer eval: canada_docs_v1, claude-haiku-5-5

run 20261020T185808Z · 2026-10-20T18:58:21+00:00 · git 9598eee · probes_out_of_scope_v1.jsonl · k=5 · judge claude-haiku-5-5

Category tables first (decisions.md #20), then overall: micro, with macro beside it; answerable and abstention are never blended. Every row carries n; every rate in the overall and breakdown sections also carries a 95% Wilson interval. Decoy cited applies to confusable items only.

## Single page (baseline)

| category | n | pass | all facts | mean facts | validator ok | gold cited | decoy cited | false abstention | fail: retrieval page | fail: retrieval chunk | fail: generation | cost CA$ |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| single_page | 0 | - | - | - | - | - | - | - | - | - | - | 0.0000 |

## Hard categories

| category | n | pass | all facts | mean facts | validator ok | gold cited | decoy cited | false abstention | fail: retrieval page | fail: retrieval chunk | fail: generation | cost CA$ |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| multi_page | 0 | - | - | - | - | - | - | - | - | - | - | 0.0000 |
| confusable | 0 | - | - | - | - | - | - | - | - | - | - | 0.0000 |
| time_sensitive | 0 | - | - | - | - | - | - | - | - | - | - | 0.0000 |

## Abstention

| category | n | correct abstention | abstained by gate | abstained by model | answered confidently (fail) | validator ok | cost CA$ |
|---|---|---|---|---|---|---|---|
| unanswerable_out_of_scope | 10 | 0.90 | 1 | 8 | 1 | 1.00 | 0.0051 |
| unanswerable_in_domain | 0 | - | - | - | - | - | 0.0000 |

## Overall

10 items. Answerable and unanswerable are reported separately.

| metric | n | k | rate [95% CI] |
|---|---|---|---|
| answerable pass (micro) | 0 | 0 | - |
| abstention recall (unanswerable) | 10 | 9 | 0.90 [0.60, 0.98] |

Macro pass rate (mean of answerable categories): -

## By program

Answerable items; an item whose gold pages span programs counts under each.

| program | n | pass [95% CI] | mean facts |
|---|---|---|---|

Unanswerable (n/a): n=10, correct abstention 0.90 [0.60, 0.98]

## Abstention

Positive class: should abstain (unanswerable). Precision counts gate and model abstentions only; refusal/max_tokens fallbacks are generation failures.

| metric | n | k | rate [95% CI] |
|---|---|---|---|
| recall | 10 | 9 | 0.90 [0.60, 0.98] |
| precision | 9 | 9 | 1.00 [0.70, 1.00] |

| count | n |
|---|---|
| should abstain | 10 |
| intentional abstentions | 9 |
| by gate | 1 |
| by model | 8 |
| false abstentions (answerable) | 0 |
| missed (unanswerable not abstained correctly) | 1 |
| generation failures | 0 |

## Citation validity

Answered items with status ok (n=1); abstentions excluded.

| metric | n | k | rate [95% CI] |
|---|---|---|---|
| validator ok | 1 | 1 | 1.00 [0.21, 1.00] |
| gold cited (answerable) | 0 | 0 | - |

Citation precision (cited pages that are gold, mean): - (n=0)

## Failure attribution

Failed answerable items: n=0.

| metric | n | k | rate [95% CI] |
|---|---|---|---|
| retrieval_page | 0 | 0 | - |
| retrieval_chunk | 0 | 0 | - |
| generation | 0 | 0 | - |

## Fact decisions

| category | n | numbers | judge | none | judge errors |
|---|---|---|---|---|---|
| single_page | 0 | 0 | 0 | 0 | 0 |
| multi_page | 0 | 0 | 0 | 0 | 0 |
| confusable | 0 | 0 | 0 | 0 | 0 |
| unanswerable_out_of_scope | 10 | 0 | 0 | 0 | 0 |
| unanswerable_in_domain | 0 | 0 | 0 | 0 | 0 |
| time_sensitive | 0 | 0 | 0 | 0 | 0 |

## Failures

- **pr-005** (unanswerable_out_of_scope, answered confidently): When is my income tax return due this year?

## Cost and tokens

Tokens used by this run (cache hits and gate abstentions use none):

| part | input | output | cache write | cache read |
|---|---|---|---|---|
| answer | 30968 | 1004 | 0 | 0 |
| judge | 0 | 0 | 0 | 0 |

Spent this run vs list cost (what the Anthropic responses cost when made, as if nothing was cached):

| part | spent CA$ | list CA$ |
|---|---|---|
| answers | 0.0051 | 0.0051 |
| judge | 0.0000 | 0.0000 |
| voyage | 0.0000 | - |
| sum | 0.0051 | 0.0051 |

Answer calls: 9 (0 cache hits, 1 gate abstentions). Judge calls: 0 (0 cache hits, 0 judge errors).
