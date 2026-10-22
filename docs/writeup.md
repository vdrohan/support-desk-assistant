# Support desk assistant: what it does and how well it works

A retrieval-augmented question answerer and ticket triager for a support desk, built on 129 Government of Canada citizen-service pages (Employment Insurance, Canada Pension Plan, Old Age Security, Social Insurance Number, passports). Every claim below comes from a committed, versioned eval with the sample size next to it. The reasoning behind each choice is in [decisions.md](decisions.md); the raw reports are in [`data/eval/results/`](../data/eval/results/).

This is an independent portfolio project, not affiliated with the Government of Canada. It does not give legal, financial or benefits advice.

## Summary

| Part | What it does | Headline result | Read it with |
|---|---|---|---|
| Retrieval | Finds the right passages for a question | The right page is in the top 5 for 0.91 of 34 questions | Small set; ambiguous and multi-program questions fail |
| Answers | Cited answers, or an abstention | 0.82 of 51 answerable questions pass; 7 of 7 unanswerable ones abstain | The judge was calibrated on labels written by Claude |
| Triage | Queue and priority for a ticket | Test split, v1: queue 0.90, priority 0.80, urgent recall 19/20. Test split, v2: queue 0.88, priority 0.81, urgent recall 28/29 | Tickets are model-drafted; v2 labels are one model's reading |
| Escalation | Flags tickets a person should check | Test split, v2: 40% of tickets flagged, 24 of the 30 classifier errors caught, 1 of 29 truly urgent tickets unreached | Four targets fixed beforehand, all met, one of them by a single ticket |

Paid API calls use Haiku only. A full 58-question answer run costs about CA$0.05; triaging a ticket costs about CA$0.0004, and the escalation second opinion adds about CA$0.0005.

## What it is

```
docs ─▶ crawl ─▶ chunk ─▶ embed (Voyage) ─▶ Qdrant
                                              │ top k
question ─▶ retrieve ─▶ Claude (Haiku): answer + citations, or abstain
                              │ validated: every citation must be a retrieved chunk
ticket ─▶ Claude (Haiku): queue, priority, confidence ─▶ escalation rules ─▶ SQLite
                              └─ second Haiku pass with thinking on, compared with the first
```

- **Answers** are validated twice. The output schema forces a structure, and a validator rejects any answer whose citations are not among the retrieved chunks; a rejected answer is returned as an abstention. The retrieval score gate and the model's own abstention are two separate layers (decision 17).
- **Triage** puts a ticket in one of six queues (`ei`, `cpp`, `oas`, `sin`, `passport`, `other`) with one of four priorities (`urgent`, `high`, `normal`, `low`). The priority rules are about what is at stake and by when, not tone ([prompts/triage.md](../prompts/triage.md)).
- **Escalation** sets `needs_review` and lists the reasons. `POST /ask` also returns `escalate` when there is no answer.

## How accuracy was measured

- **Versioned sets, frozen once they have results.** A change to a prompt or rule that is judged on a set needs a new set.
- **Every rate carries its n and a Wilson 95% interval.** Most sets are 50 to 100 items, so the intervals are wide and are quoted.
- **Test splits are scored once.** The triage and escalation runners refuse a second test run on the same dataset version. The first triage set's test split was used up in October; escalation was developed on a second, fresh set.
- **Targets before the run.** The escalation targets were written and confirmed before any test result, and are reported as met or not met without rewording.
- **Label provenance is recorded and reported.** See the limits below: much of the labelling is by models, and the reports say so.

## Results

### Retrieval

34 answerable questions (`retrieval_v1`), Voyage `voyage-4` at 1024 dimensions, top 5, no program filter:

| page hit@1 | page hit@5 | MRR@5 | answer hit@5 |
|---|---|---|---|
| 0.65 | 0.91 | 0.75 | 0.88 |

Routing each question to its own program first (an oracle) did not help overall (0.91), so the weak spots are the questions themselves. Direct questions reach page hit@5 of 1.00 (n=12) and colloquial ones 0.93 (n=15). The three ambiguous questions reach 0.33, and both questions that span CPP, EI and OAS miss entirely. Passport is the weakest single program (0.89, n=9).

### Answers

58 questions: 51 answerable, 7 not (after two labelling errors in the first version were corrected; decision 26). Haiku writes the answers; short facts are checked by string matching and the rest by a Haiku judge.

| | Result |
|---|---|
| Answerable questions that pass | 42 of 51 = 0.82 [0.70, 0.90] |
| Unanswerable questions that abstain | 7 of 7 = 1.00 [0.65, 1.00] |
| False abstentions on answerable questions | 0 of 51 |
| Held-out out-of-scope probes written afterwards | 9 of 10 abstained |
| List cost of a full run | CA$0.052 (answers 0.044, judge 0.009) |

The abstention number is the interesting one. At first only 4 of 9 unanswerable questions abstained. The model was writing a "the documents do not say" paragraph and then setting `abstained: false`, because structured output is generated in schema order and the answer text came before the decision. Rewording the prompt changed nothing; putting `abstained` first fixed it (decision 26). The same ordering flaw was in the judge, which decided before it reasoned (decision 25). The fix was in-sample, so the ten held-out probes matter more than the 7 of 7. The one probe that was answered was a poorly chosen question the documents do partly cover.

### Triage

Tickets are drafted by Haiku from a scenario (a program or off-corpus topic, a situation, a style such as emotional, French, very short, or containing a prompt-injection attempt), never from a label.

| | v1 test split (n=80) | v2 test split (n=103) |
|---|---|---|
| Queue accuracy | 0.90 [0.81, 0.95] | 0.88 [0.81, 0.93] |
| Priority accuracy | 0.80 [0.70, 0.87] | 0.81 [0.72, 0.87] |
| Queue and priority both right | 0.76 | 0.71 |
| Urgent recall | 19/20 | 28/29 |
| Keyword baseline (queue / urgent recall) | 0.72 / 0.55 | not run |

Priority is never off by more than one level on v1 (80 of 80). The errors are mostly boundary calls: normal against low, high against normal, and tickets that name no program (`other`).

One miss repeated across both sets: a passport lost abroad is listed as urgent in the prompt, but a ticket that also gives a return date more than a week away gets `high` (`tc-099` in v1, `t2-196` in v2). That is a prompt weakness, not noise.

### Escalation

The classifier reports a confidence for each label, and a second Haiku pass with thinking on labels the same ticket again. A ticket is flagged when triage failed, the queue is `other`, the queue confidence is below high, or the two passes disagree. Urgent tickets are not flagged for doubt: they top the queue by priority, and the safety target counts a ticket labelled urgent as reached.

| | v2 dev (93 tickets, 19 errors) | v2 test (103 tickets, 30 errors) |
|---|---|---|
| Tickets flagged (target at most 40%) | 29 = 0.31 | 41 = 0.40 |
| Classifier errors caught (target at least 60%) | 10 of 19 = 0.53 | 24 of 30 = 0.80 [0.63, 0.91] |
| Lower bound of that interval above the flag rate | 0.32 vs 0.31 | 0.63 vs 0.40 |
| Truly urgent tickets neither labelled urgent nor flagged (target at most 5%) | 0 of 28 | 1 of 29 |
| Errors left among tickets nobody reviews | 0.14 | 0.10 (6 of 62), against 0.29 overall |

The policy was chosen on dev, from 72 combinations on 19 errors, and not touched before the single test run, which met all four targets. Read the test result with care:

- The flag rate of 0.398 is under the limit by one ticket.
- Capture rose from 0.53 to 0.80 between dev and test, with overlapping intervals. The test half is harder: 29% of its tickets are misclassified against 20% on dev, and half of its errors are tickets whose true queue is `other`.
- The three signals catch different errors. On test, the second opinion alone flags 23% of tickets and catches 17 of 30 (71% of what it flags is an error); queue confidence alone flags 23% and catches 13; the other-queue and urgent rules flag 45% and catch 16. Together they flag 40% and catch 24.
- Self-reported priority confidence turned out to be useless. The model never said "low", and flagging "medium" worked about as well as chance, so that floor is off.
- Flagging 40% of tickets is a heavy review load. The second opinion alone, at 23%, is the cheaper setting.

## What went wrong along the way

- The answer and judge schemas decided before they reasoned (above). Two prompt rewrites did nothing before field order did.
- The first plan for triage tickets built the label into the ticket and showed the draft label to the reviewer. That is circular and anchoring, and it was dropped before it produced a result (decision 28).
- A metric (`urgent unreached`) was first coded differently from its written definition, which was found on dev and fixed before any test run (decision 29).
- A 20-ticket Sonnet labelling probe ran on the API (CA$0.14) before the Haiku-only rule was spelled out. It was not used. The labeller now refuses to call anything but Haiku.
- The triage sets turned out to be mostly labelled by models (see limits), which changed the claims that can honestly be made from them.

## Limits

- **Labels.** The first triage set has 50 gold labels: 10 by the project owner, 40 where three model passes agreed and no person reviewed them. Every label in the second triage set (196 tickets) is one Claude reading of the rubric the classifier also reads, by a related model family, with 57 tickets marked as ones the labeller was unsure of. A rubric ambiguity that both resolve the same way is invisible. The judge was calibrated on labels written by Claude, in-sample. No human has reviewed any of the v2 labels or the judge labels; for the first triage set and the judge that review was skipped on purpose.
- **Synthetic tickets.** Tickets are model-drafted, so their language and difficulty may differ from real ones. The drafter also repeats stories (a stolen passport in Lisbon appears in both sets); 4 near-duplicates of the first set were dropped from the second.
- **Sample sizes.** Intervals on the headline rates are 10 to 30 points wide.
- **Corpus.** A snapshot from October 2026, 129 pages. Answers can be out of date, and the pages are the Government of Canada's.
- **Not built.** No authentication (the API binds to localhost), no personal-information handling (tickets are stored and sent to the model as typed), no human-override or re-triage endpoint, so nothing records that a flagged ticket was reviewed, and no deployment image.
- **Both triage test splits are spent.** Further prompt or policy changes need a new set.

## What I would do next

1. A person reviews the 57 unsure tickets in the second triage set and a random sample of the rest. (The human review of the first set's 40 model-agreed labels and of the judge's labels was skipped on purpose.)
2. Test on real tickets, even a small number, to see how far the drafted ones are from them.
3. Fix the passport-lost-abroad rule on a third set, with the test split held back until the prompt is frozen.
4. Add an override endpoint so a reviewed ticket is recorded, then measure how many flagged tickets a person changes.
5. Auth and deployment before anything leaves localhost.

## Reproduce

```bash
uv sync
uv run pytest                                           # no live calls
uv run sda-eval-retrieval                               # needs the Qdrant tunnel and VOYAGE_API_KEY
uv run sda-eval-answers                                 # dry run; --yes calls Haiku
uv run sda-eval-triage --split dev                      # dry run; --yes calls Haiku
uv run sda-eval-escalation --split dev --second-opinion # dry run; --yes calls Haiku
```

The committed reports for the numbers above are `data/eval/results/canada_docs_v1_2026-10-08.md` (retrieval), `answers_canada_docs_v1_claude-haiku-5-5_20261020T185710Z.md` (answers), `triage_triage_set_v1_claude-haiku-5-5_test_20261021T193327Z.md` (triage v1) and `escalation_triage_set_v2_claude-haiku-5-5_test_20261022T180507Z.md` (triage v2 and escalation).
