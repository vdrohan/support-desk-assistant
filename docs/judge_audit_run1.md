# Judge audit: run 1 verdicts vs an independent re-grade

Records the check behind [decision 25](decisions.md#25-judge-writes-its-reason-before-its-verdict). Not calibration data: these are model labels plus a maintainer review of the contested rows, so do not import them into `data/eval/judge_calibration.jsonl`. The human calibration (blind sheet) is separate and was not done when this was written.

## Method
- Source: `data/eval/results/answers_canada_docs_v1_claude-haiku-5-5_20261019T150102Z.jsonl` (run 1, commit 229f4dc), all 52 judge-decided facts (the other 25 facts were decided by the free number check).
- A Claude session built a sheet of question, answer and fact with no verdicts, graded all 52, then compared with the judge's verdicts.
- The maintainer (Val) reviewed the 5 contested rows and, after the fix, 7 more items (below).

## Result on run 1
- Agreement 48 of 52 = 0.92 [0.82, 0.97] (Wilson 95%). Cohen's kappa 0.31: low despite 92% agreement, because almost every fact is "present" (grader 51 of 52, judge 47 of 52).
- All 4 disagreements are the judge saying "missing" where the grader said "present". Both agree ev-052 [1] ("Plastic SIN cards can still be used") is genuinely missing.

| item | fact | cause |
|---|---|---|
| ev-006 [0] | "You do not need to inform the SIN Program when" | dataset: key fact is half a sentence |
| ev-021 [0] | "issued within 15 years of the date you apply" | judge: reason concluded present, verdict false |
| ev-024 [0] | "Payments increase by 0.7% each month (8.4% per year), up to 42% at age 70." | judge: reason concluded present, verdict false (the question is also ambiguous between CPP and OAS) |
| ev-033 [1] | "...Guaranteed Income Supplement, Allowance or Allowance for the Survivor will be reduced by 50 cents..." | dataset: fact names three benefits, the question asks about one |

The maintainer agreed with the grader on all 5 contested rows (the 4 above and ev-052 [1]).

## Limits of the audit
- The grader is a model, and the judge is a model of the same family: shared blind spots are possible.
- Not fully blind: the grader had already read the run report listing the four facts the judge marked missing, so it knew where the disagreements would be. The other 48 were graded without that knowledge.
- Only 5 of 52 facts were "missing", so errors where the judge passes a wrong answer are hard to see.

## Follow-up after the fix (run 2, commit 23ad7d0)
Run `20261019T174456Z` re-judged the same 58 cached answers with the reason-first schema (`docs/decisions.md` #25). Reproduce the comparison from the two committed `.jsonl` files:

```bash
OLD=data/eval/results/answers_canada_docs_v1_claude-haiku-5-5_20261019T150102Z.jsonl
NEW=data/eval/results/answers_canada_docs_v1_claude-haiku-5-5_20261019T174456Z.jsonl
# items whose pass/fail changed
jq -rn --slurpfile o $OLD --slurpfile n $NEW '($o|map({(.id):.})|add) as $p | $n[] | . as $r | $p[$r.id] as $q | select($q.passed != $r.passed) | "\($r.id) \($q.passed)->\($r.passed)"'
# facts whose verdict changed
jq -rn --slurpfile o $OLD --slurpfile n $NEW '($o|map({(.id):.})|add) as $p | $n[] | . as $r | $p[$r.id] as $q | range(0; ($r.facts|length)) as $i | select($q.facts[$i].present != $r.facts[$i].present) | "\($r.id)[\($i)] \($q.facts[$i].present)->\($r.facts[$i].present)"'
```

- Items that changed: ev-006, ev-021, ev-033 fail to pass; ev-011 pass to fail.
- Facts that changed: ev-006[0], ev-021[0], ev-024[0], ev-033[1] missing to present (all four audit errors); ev-011[0] present to missing. The answer text of all 58 items is identical between the runs (checked with jq on the `answer` field), so every change is the judge's.
- ev-011[0] ("more than 10 business days since you applied online") is a new false "missing" introduced by the change: the answer states the rule verbatim, and the judge's reason mixes in the user's "about two weeks" situation. The maintainer agreed it is a judge error.
- Judge-decided facts still marked missing: ev-011[0] and ev-052[1]. Against the grader's labels that is 51 of 52 agreement, but the fix was designed from the same facts, so this is in-sample.
- ev-024 still fails: its fact is now present but it cites the OAS decoy page.
- Seven items reviewed by the maintainer after the fix, all in agreement with the grader's reading: ev-011 (judge error); ev-009, ev-020, ev-039, ev-053 (every key fact present, fail only because the gold page was not cited); ev-046 and ev-051 (genuine misses, retrieval and generation). For ev-051, the key fact "The maximum benefit is $5,000." is on the death benefit page (`data/processed/cpp/services__benefits__publicpensions__cpp__cpp-death-benefit.md`, line 52), so it is not a typo.
