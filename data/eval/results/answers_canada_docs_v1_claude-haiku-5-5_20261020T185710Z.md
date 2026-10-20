# Answer eval: canada_docs_v1, claude-haiku-5-5

run 20261020T185710Z · 2026-10-20T18:57:14+00:00 · git 3992ac7 · eval_set_v2.jsonl · k=5 · judge claude-haiku-5-5

Category tables first (decisions.md #20), then overall: micro, with macro beside it; answerable and abstention are never blended. Every row carries n; every rate in the overall and breakdown sections also carries a 95% Wilson interval. Decoy cited applies to confusable items only.

## Single page (baseline)

| category | n | pass | all facts | mean facts | validator ok | gold cited | decoy cited | false abstention | fail: retrieval page | fail: retrieval chunk | fail: generation | cost CA$ |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| single_page | 34 | 0.88 | 0.94 | 0.97 | 1.00 | 0.94 | - | 0.00 | 1 | 0 | 3 | 0.0005 |

## Hard categories

| category | n | pass | all facts | mean facts | validator ok | gold cited | decoy cited | false abstention | fail: retrieval page | fail: retrieval chunk | fail: generation | cost CA$ |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| multi_page | 8 | 0.62 | 0.62 | 0.69 | 1.00 | 0.62 | - | 0.00 | 2 | 0 | 1 | 0.0000 |
| confusable | 6 | 0.67 | 1.00 | 1.00 | 1.00 | 0.83 | 0.17 | 0.00 | 1 | 0 | 1 | 0.0000 |
| time_sensitive | 3 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | - | 0.00 | 0 | 0 | 0 | 0.0000 |

## Abstention

| category | n | correct abstention | abstained by gate | abstained by model | answered confidently (fail) | validator ok | cost CA$ |
|---|---|---|---|---|---|---|---|
| unanswerable_out_of_scope | 6 | 1.00 | 0 | 6 | 0 | 1.00 | 0.0000 |
| unanswerable_in_domain | 1 | 1.00 | 0 | 1 | 0 | 1.00 | 0.0000 |

## Overall

58 items. Answerable and unanswerable are reported separately.

| metric | n | k | rate [95% CI] |
|---|---|---|---|
| answerable pass (micro) | 51 | 42 | 0.82 [0.70, 0.90] |
| abstention recall (unanswerable) | 7 | 7 | 1.00 [0.65, 1.00] |

Macro pass rate (mean of answerable categories): 0.79

## By program

Answerable items; an item whose gold pages span programs counts under each.

| program | n | pass [95% CI] | mean facts |
|---|---|---|---|
| cpp | 12 | 0.75 [0.47, 0.91] | 0.88 |
| ei | 14 | 0.79 [0.52, 0.92] | 0.96 |
| oas | 9 | 0.78 [0.45, 0.94] | 0.94 |
| passport | 7 | 1.00 [0.65, 1.00] | 1.00 |
| sin | 10 | 0.90 [0.60, 0.98] | 0.90 |

Unanswerable (n/a): n=7, correct abstention 1.00 [0.65, 1.00]

## Abstention

Positive class: should abstain (unanswerable). Precision counts gate and model abstentions only; refusal/max_tokens fallbacks are generation failures.

| metric | n | k | rate [95% CI] |
|---|---|---|---|
| recall | 7 | 7 | 1.00 [0.65, 1.00] |
| precision | 7 | 7 | 1.00 [0.65, 1.00] |

| count | n |
|---|---|
| should abstain | 7 |
| intentional abstentions | 7 |
| by gate | 0 |
| by model | 7 |
| false abstentions (answerable) | 0 |
| missed (unanswerable not abstained correctly) | 0 |
| generation failures | 0 |

## Citation validity

Answered items with status ok (n=51); abstentions excluded.

| metric | n | k | rate [95% CI] |
|---|---|---|---|
| validator ok | 51 | 51 | 1.00 [0.93, 1.00] |
| gold cited (answerable) | 51 | 45 | 0.88 [0.77, 0.94] |

Citation precision (cited pages that are gold, mean): 0.59 (n=51)

## Failure attribution

Failed answerable items: n=9.

| metric | n | k | rate [95% CI] |
|---|---|---|---|
| retrieval_page | 9 | 4 | 0.44 [0.19, 0.73] |
| retrieval_chunk | 9 | 0 | 0.00 [0.00, 0.30] |
| generation | 9 | 5 | 0.56 [0.27, 0.81] |

## Fact decisions

| category | n | numbers | judge | none | judge errors |
|---|---|---|---|---|---|
| single_page | 34 | 16 | 37 | 0 | 0 |
| multi_page | 8 | 6 | 10 | 0 | 0 |
| confusable | 6 | 1 | 5 | 0 | 0 |
| unanswerable_out_of_scope | 6 | 0 | 0 | 0 | 0 |
| unanswerable_in_domain | 1 | 0 | 0 | 0 | 0 |
| time_sensitive | 3 | 2 | 3 | 0 | 0 |

## Failures

- **ev-009** (single_page, generation, gold not cited): I was laid off from my warehouse job last month because the company closed, and I'm wondering what kind of EI benefit I could apply for to get some income while I look for new work.
- **ev-020** (multi_page, retrieval_page, gold not cited): I'm 61 and have worked in Canada since I was 20. Can I start collecting a CPP retirement pension now, and what's the highest monthly amount someone can get at 65?
  - missing (judge): be at least 60 years old
- **ev-024** (confusable, generation, decoy cited): I'm turning 65 and wondering whether to start my government retirement payments right away or hold off. If I wait, will my monthly amount go up, and by how much per year?
- **ev-030** (single_page, generation): My mom turns 64 next month and she's worried she might miss out on extra monthly money from the government because she's on a limited income. Does she have to do anything herself to get that extra support, or will it happen automatically?
  - missing (judge): you’ll get a letter shortly after your 64th birthday
- **ev-039** (single_page, retrieval_page, gold not cited): I was laid off from my job about three months ago and only just got around to looking into EI. Can I still get a benefit amount for those weeks I missed?
- **ev-046** (single_page, generation): Who is eligible for EI?
  - missing (numbers): between 420 and 700 hours of insurable employment
- **ev-051** (multi_page, retrieval_page, gold not cited): My husband passed away and I'm trying to figure out what CPP money I can get from him. I'm 58 and was never on any disability pension. Could I get both a one-time payment from his contributions and a monthly survivor pension, and if so, roughly how much would each be?
  - missing (numbers): The maximum benefit is $5,000.
  - missing (numbers): 37.5% of the contributor's retirement pension
- **ev-052** (multi_page, generation, gold not cited): My SIN card got badly damaged and I'm not sure I can still use it. Do I need to apply for a new one or do something else?
  - missing (judge): you do not need to take any action
  - missing (judge): Plastic SIN cards can still be used.
- **ev-053** (confusable, retrieval_page, gold not cited): I'm 66 and I've been living in Canada for decades but I'm not sure whether I get the monthly payment from the government for seniors or the one from my work contributions. Which one should I look into, and how much would the maximum be for someone my age?

## Cost and tokens

Tokens used by this run (cache hits and gate abstentions use none):

| part | input | output | cache write | cache read |
|---|---|---|---|---|
| answer | 0 | 0 | 0 | 0 |
| judge | 2257 | 225 | 0 | 0 |

Spent this run vs list cost (what the Anthropic responses cost when made, as if nothing was cached):

| part | spent CA$ | list CA$ |
|---|---|---|
| answers | 0.0000 | 0.0436 |
| judge | 0.0005 | 0.0090 |
| voyage | 0.0000 | - |
| sum | 0.0005 | 0.0526 |

Answer calls: 0 (58 cache hits, 0 gate abstentions). Judge calls: 2 (38 cache hits, 0 judge errors).
