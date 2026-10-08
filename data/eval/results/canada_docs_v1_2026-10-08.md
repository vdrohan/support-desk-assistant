# Retrieval eval: canada_docs_v1, k=5

2026-10-08T00:09:08+00:00 · git 06485c3 · retrieval_v1.jsonl (37 questions, sha256 75a0703054bb) · voyage-4 1024d

n = answerable questions in the group (out_of_corpus and corpus_gap items have no hit metrics). Answer hit = a key fact in a top-k chunk from an expected page.

## Overall

| mode | n | page_hit@1 | page_hit@5 | mrr@5 | answer_hit@5 |
|---|---|---|---|---|---|
| unfiltered | 34 | 0.65 | 0.91 | 0.75 | 0.88 |
| oracle | 34 | 0.68 | 0.91 | 0.76 | 0.88 |

## By program (unfiltered)

| program | n | page_hit@1 | page_hit@5 | mrr@5 | answer_hit@5 |
|---|---|---|---|---|---|
| cpp | 6 | 0.67 | 1.00 | 0.79 | 0.83 |
| cpp+ei+oas | 2 | 0.00 | 0.00 | 0.00 | 0.00 |
| cpp+oas | 1 | 0.00 | 1.00 | 0.50 | 1.00 |
| ei | 9 | 0.67 | 1.00 | 0.81 | 1.00 |
| none | 0 | – | – | – | – |
| oas | 4 | 1.00 | 1.00 | 1.00 | 1.00 |
| passport | 9 | 0.56 | 0.89 | 0.66 | 0.89 |
| sin | 3 | 1.00 | 1.00 | 1.00 | 1.00 |

## By program (oracle)

| program | n | page_hit@1 | page_hit@5 | mrr@5 | answer_hit@5 |
|---|---|---|---|---|---|
| cpp | 6 | 0.67 | 1.00 | 0.79 | 0.83 |
| cpp+ei+oas | 2 | 0.00 | 0.00 | 0.00 | 0.00 |
| cpp+oas | 1 | 1.00 | 1.00 | 1.00 | 1.00 |
| ei | 9 | 0.67 | 1.00 | 0.81 | 1.00 |
| oas | 4 | 1.00 | 1.00 | 1.00 | 1.00 |
| passport | 9 | 0.56 | 0.89 | 0.66 | 0.89 |
| sin | 3 | 1.00 | 1.00 | 1.00 | 1.00 |

## By category (unfiltered)

| category | n | page_hit@1 | page_hit@5 | mrr@5 | answer_hit@5 |
|---|---|---|---|---|---|
| ambiguous | 3 | 0.00 | 0.33 | 0.17 | 0.33 |
| colloquial | 15 | 0.67 | 0.93 | 0.76 | 0.87 |
| corpus_gap | 0 | – | – | – | – |
| direct | 12 | 0.75 | 1.00 | 0.83 | 1.00 |
| known_issue | 4 | 0.75 | 1.00 | 0.88 | 1.00 |
| out_of_corpus | 0 | – | – | – | – |

## By category (oracle)

| category | n | page_hit@1 | page_hit@5 | mrr@5 | answer_hit@5 |
|---|---|---|---|---|---|
| ambiguous | 3 | 0.33 | 0.33 | 0.33 | 0.33 |
| colloquial | 15 | 0.67 | 0.93 | 0.76 | 0.87 |
| direct | 12 | 0.75 | 1.00 | 0.83 | 1.00 |
| known_issue | 4 | 0.75 | 1.00 | 0.88 | 1.00 |

## By source (unfiltered)

| source | n | page_hit@1 | page_hit@5 | mrr@5 | answer_hit@5 |
|---|---|---|---|---|---|
| generated | 24 | 0.58 | 0.92 | 0.72 | 0.88 |
| human | 10 | 0.80 | 0.90 | 0.82 | 0.90 |

## By source (oracle)

| source | n | page_hit@1 | page_hit@5 | mrr@5 | answer_hit@5 |
|---|---|---|---|---|---|
| generated | 24 | 0.62 | 0.92 | 0.74 | 0.88 |
| human | 10 | 0.80 | 0.90 | 0.82 | 0.90 |

## Top-1 scores (unfiltered)

| kind | n | min | p10 | median | max |
|---|---|---|---|---|---|
| answerable | 31 | 0.526 | 0.559 | 0.626 | 0.700 |
| ambiguous | 3 | 0.461 | 0.461 | 0.465 | 0.525 |
| out_of_corpus | 2 | 0.448 | 0.448 | 0.486 | 0.525 |

Refusal threshold at the best out-of-corpus score (0.525) would refuse 0 of 31 answerable questions (0%).

## Corpus gaps (in scope, answer not in corpus; no hit metrics)

- gen-gap-01: How much does a passport cost? → top-1 0.609 https://www.canada.ca/en/immigration-refugees-citizenship/services/canadian-passports/child-passport.html

## Misses

- **human-10** (unfiltered, page + answer miss): When do I get paid?
  - expected: https://www.canada.ca/en/services/benefits/ei/ei-regular-benefit/after-applying.html, https://www.canada.ca/en/services/benefits/publicpensions/old-age-security/while-receiving.html, https://www.canada.ca/en/services/benefits/publicpensions/cpp/receive-benefits.html
  - got #1: 0.465 https://www.canada.ca/en/services/benefits/ei/various-types-earnings.html (Employment Insurance and the various types of earnings > Earnings paid or payable)
- **human-10** (oracle, page + answer miss): When do I get paid?
  - expected: https://www.canada.ca/en/services/benefits/ei/ei-regular-benefit/after-applying.html, https://www.canada.ca/en/services/benefits/publicpensions/old-age-security/while-receiving.html, https://www.canada.ca/en/services/benefits/publicpensions/cpp/receive-benefits.html
  - got #1: 0.465 https://www.canada.ca/en/services/benefits/ei/various-types-earnings.html (Employment Insurance and the various types of earnings > Earnings paid or payable)
- **gen-cpp-06** (unfiltered, answer miss, expected page at #1): Can my wife and I split our CPP to pay less tax?
  - expected: https://www.canada.ca/en/services/benefits/publicpensions/cpp/share-cpp.html, https://www.canada.ca/en/services/benefits/publicpensions/cpp/amount.html
  - got #1: 0.568 https://www.canada.ca/en/services/benefits/publicpensions/cpp/amount.html (How much you could receive > Situations that can affect your pension amount > Periods of low or no earnings)
- **gen-cpp-06** (oracle, answer miss, expected page at #1): Can my wife and I split our CPP to pay less tax?
  - expected: https://www.canada.ca/en/services/benefits/publicpensions/cpp/share-cpp.html, https://www.canada.ca/en/services/benefits/publicpensions/cpp/amount.html
  - got #1: 0.568 https://www.canada.ca/en/services/benefits/publicpensions/cpp/amount.html (How much you could receive > Situations that can affect your pension amount > Periods of low or no earnings)
- **gen-pp-05** (unfiltered, page + answer miss): Do I need someone to vouch for me when I renew my passport?
  - expected: https://www.canada.ca/en/immigration-refugees-citizenship/services/canadian-passports/renew-adult-passport.html
  - got #1: 0.549 https://www.canada.ca/en/immigration-refugees-citizenship/services/canadian-passports/help-centre/apply.html (Applying for a passport > Submitting your application)
- **gen-pp-05** (oracle, page + answer miss): Do I need someone to vouch for me when I renew my passport?
  - expected: https://www.canada.ca/en/immigration-refugees-citizenship/services/canadian-passports/renew-adult-passport.html
  - got #1: 0.549 https://www.canada.ca/en/immigration-refugees-citizenship/services/canadian-passports/help-centre/apply.html (Applying for a passport > Submitting your application)
- **gen-amb-01** (unfiltered, page + answer miss): When will the money start coming in after I apply?
  - expected: https://www.canada.ca/en/services/benefits/ei/ei-regular-benefit/after-applying.html, https://www.canada.ca/en/services/benefits/publicpensions/old-age-security/while-receiving.html, https://www.canada.ca/en/services/benefits/publicpensions/cpp/cpp-survivor-pension.html
  - got #1: 0.525 https://www.canada.ca/en/services/benefits/publicpensions/old-age-security/guaranteed-income-supplement/allowance/after-apply.html (After you apply > Application processing times > What your letter could include)
- **gen-amb-01** (oracle, page + answer miss): When will the money start coming in after I apply?
  - expected: https://www.canada.ca/en/services/benefits/ei/ei-regular-benefit/after-applying.html, https://www.canada.ca/en/services/benefits/publicpensions/old-age-security/while-receiving.html, https://www.canada.ca/en/services/benefits/publicpensions/cpp/cpp-survivor-pension.html
  - got #1: 0.525 https://www.canada.ca/en/services/benefits/publicpensions/old-age-security/guaranteed-income-supplement/allowance/after-apply.html (After you apply > Application processing times > What your letter could include)
