# Escalation eval: triage_set_v2.jsonl, claude-haiku-5-5

run 20261022T180507Z · split test · git f06e369 · prompt d16c086c4b41

Policy: other queue on, urgent off, confidence floors queue high, priority low (a label below its floor is escalated), second-opinion rule on (a second pass was run).

## Classifier on these rows

- queue accuracy 0.88 [0.81, 0.93] (91/103)
- priority accuracy 0.81 [0.72, 0.87] (83/103)
- queue and priority both correct 0.71 [0.61, 0.79] (73/103)
- urgent recall 0.97 [0.83, 0.99] (28/29); generation failures 0

## Targets (fixed before the test run)

| target | value | met |
|---|---|---|
| escalated at most 0.40 | 0.40 | yes |
| error capture at least 0.60 | 0.80 | yes |
| error capture interval above the escalation rate (beats random) | 0.63 | yes |
| urgent unreached at most 0.05 | 0.03 | yes |

## Configured policy

| metric | rate [95% CI] (k/n) |
|---|---|
| tickets escalated | 0.40 [0.31, 0.49] (41/103) |
| classifier error rate | 0.29 [0.21, 0.39] (30/103) |
| errors caught (error capture) | 0.80 [0.63, 0.91] (24/30) |
| random escalation at the same rate would catch | 0.398 |
| escalated tickets that were errors (precision) | 0.58 [0.43, 0.72] (24/41) |
| errors among tickets nobody reviews (residual) | 0.10 [0.04, 0.20] (6/62) |
| truly urgent tickets not escalated | 0.03 [0.01, 0.17] (1/29) |
| under-triaged tickets escalated | 0.78 [0.45, 0.94] (7/9) |
| ambiguous tickets escalated (labeller unsure, or silver and gold differ) | 0.53 [0.36, 0.70] (16/30) |
| classifier error rate on ambiguous tickets / on the others | 0.40 (12/30) / 0.25 (18/73) |
| errors caught on ambiguous tickets / on the others | 0.83 (10/12) / 0.78 (14/18) |

Reasons (a ticket can have several): {'no_program_queue': 20, 'low_queue_confidence': 24, 'second_opinion_differs': 24}

## Signals side by side

| policy | escalated | error capture | precision | residual error | urgent unreached | under-triage caught |
|---|---|---|---|---|---|---|
| rules only (other queue, urgent) | 0.45 (46/103) | 0.53 (16/30) | 0.35 (16/46) | 0.25 (14/57) | 0.03 (1/29) | 0.44 (4/9) |
| confidence only (configured floors) | 0.23 (24/103) | 0.43 (13/30) | 0.54 (13/24) | 0.21 (17/79) | 0.03 (1/29) | 0.00 (0/9) |
| second opinion only | 0.23 (24/103) | 0.57 (17/30) | 0.71 (17/24) | 0.17 (13/79) | 0.03 (1/29) | 0.56 (5/9) |
| configured policy | 0.40 (41/103) | 0.80 (24/30) | 0.58 (24/41) | 0.10 (6/62) | 0.03 (1/29) | 0.78 (7/9) |

## Gold subset (blind labels, n=103)

| policy | escalated | error capture | precision | residual error | urgent unreached | under-triage caught |
|---|---|---|---|---|---|---|
| configured policy | 0.40 (41/103) | 0.80 (24/30) | 0.58 (24/41) | 0.10 (6/62) | 0.03 (1/29) | 0.78 (7/9) |

## Confidence floors (rules off, then rules as configured)

Confidence mix: queue {'high': 79, 'low': 12, 'medium': 12}, priority {'high': 49, 'medium': 54}.

| policy | escalated | error capture | precision | residual error | urgent unreached | under-triage caught |
|---|---|---|---|---|---|---|
| rules off, queue>=low, priority>=low | 0.00 (0/103) | 0.00 (0/30) | n/a | 0.29 (30/103) | 0.03 (1/29) | 0.00 (0/9) |
| rules off, queue>=low, priority>=medium | 0.00 (0/103) | 0.00 (0/30) | n/a | 0.29 (30/103) | 0.03 (1/29) | 0.00 (0/9) |
| rules off, queue>=low, priority>=high | 0.52 (54/103) | 0.57 (17/30) | 0.32 (17/54) | 0.27 (13/49) | 0.00 (0/29) | 0.67 (6/9) |
| rules off, queue>=medium, priority>=low | 0.12 (12/103) | 0.13 (4/30) | 0.33 (4/12) | 0.29 (26/91) | 0.03 (1/29) | 0.00 (0/9) |
| rules off, queue>=medium, priority>=medium | 0.12 (12/103) | 0.13 (4/30) | 0.33 (4/12) | 0.29 (26/91) | 0.03 (1/29) | 0.00 (0/9) |
| rules off, queue>=medium, priority>=high | 0.56 (58/103) | 0.60 (18/30) | 0.31 (18/58) | 0.27 (12/45) | 0.00 (0/29) | 0.67 (6/9) |
| rules off, queue>=high, priority>=low | 0.23 (24/103) | 0.43 (13/30) | 0.54 (13/24) | 0.21 (17/79) | 0.03 (1/29) | 0.00 (0/9) |
| rules off, queue>=high, priority>=medium | 0.23 (24/103) | 0.43 (13/30) | 0.54 (13/24) | 0.21 (17/79) | 0.03 (1/29) | 0.00 (0/9) |
| rules off, queue>=high, priority>=high | 0.64 (66/103) | 0.80 (24/30) | 0.36 (24/66) | 0.16 (6/37) | 0.00 (0/29) | 0.67 (6/9) |
| rules on, queue>=low, priority>=low | 0.19 (20/103) | 0.20 (6/30) | 0.30 (6/20) | 0.29 (24/83) | 0.03 (1/29) | 0.44 (4/9) |
| rules on, queue>=low, priority>=medium | 0.19 (20/103) | 0.20 (6/30) | 0.30 (6/20) | 0.29 (24/83) | 0.03 (1/29) | 0.44 (4/9) |
| rules on, queue>=low, priority>=high | 0.64 (66/103) | 0.70 (21/30) | 0.32 (21/66) | 0.24 (9/37) | 0.00 (0/29) | 0.89 (8/9) |
| rules on, queue>=medium, priority>=low | 0.22 (23/103) | 0.30 (9/30) | 0.39 (9/23) | 0.26 (21/80) | 0.03 (1/29) | 0.44 (4/9) |
| rules on, queue>=medium, priority>=medium | 0.22 (23/103) | 0.30 (9/30) | 0.39 (9/23) | 0.26 (21/80) | 0.03 (1/29) | 0.44 (4/9) |
| rules on, queue>=medium, priority>=high | 0.64 (66/103) | 0.70 (21/30) | 0.32 (21/66) | 0.24 (9/37) | 0.00 (0/29) | 0.89 (8/9) |
| rules on, queue>=high, priority>=low | 0.31 (32/103) | 0.57 (17/30) | 0.53 (17/32) | 0.18 (13/71) | 0.03 (1/29) | 0.44 (4/9) |
| rules on, queue>=high, priority>=medium | 0.31 (32/103) | 0.57 (17/30) | 0.53 (17/32) | 0.18 (13/71) | 0.03 (1/29) | 0.44 (4/9) |
| rules on, queue>=high, priority>=high | 0.69 (71/103) | 0.87 (26/30) | 0.37 (26/71) | 0.12 (4/32) | 0.00 (0/29) | 0.89 (8/9) |
| rules off, queue>=low, priority>=low, +disagree | 0.23 (24/103) | 0.57 (17/30) | 0.71 (17/24) | 0.17 (13/79) | 0.03 (1/29) | 0.56 (5/9) |
| rules off, queue>=low, priority>=medium, +disagree | 0.23 (24/103) | 0.57 (17/30) | 0.71 (17/24) | 0.17 (13/79) | 0.03 (1/29) | 0.56 (5/9) |
| rules off, queue>=low, priority>=high, +disagree | 0.62 (64/103) | 0.80 (24/30) | 0.38 (24/64) | 0.15 (6/39) | 0.00 (0/29) | 0.67 (6/9) |
| rules off, queue>=medium, priority>=low, +disagree | 0.28 (29/103) | 0.63 (19/30) | 0.66 (19/29) | 0.15 (11/74) | 0.03 (1/29) | 0.56 (5/9) |
| rules off, queue>=medium, priority>=medium, +disagree | 0.28 (29/103) | 0.63 (19/30) | 0.66 (19/29) | 0.15 (11/74) | 0.03 (1/29) | 0.56 (5/9) |
| rules off, queue>=medium, priority>=high, +disagree | 0.63 (65/103) | 0.80 (24/30) | 0.37 (24/65) | 0.16 (6/38) | 0.00 (0/29) | 0.67 (6/9) |
| rules off, queue>=high, priority>=low, +disagree | 0.34 (35/103) | 0.73 (22/30) | 0.63 (22/35) | 0.12 (8/68) | 0.03 (1/29) | 0.56 (5/9) |
| rules off, queue>=high, priority>=medium, +disagree | 0.34 (35/103) | 0.73 (22/30) | 0.63 (22/35) | 0.12 (8/68) | 0.03 (1/29) | 0.56 (5/9) |
| rules off, queue>=high, priority>=high, +disagree | 0.67 (69/103) | 0.87 (26/30) | 0.38 (26/69) | 0.12 (4/34) | 0.00 (0/29) | 0.67 (6/9) |
| rules on, queue>=low, priority>=low, +disagree | 0.34 (35/103) | 0.63 (19/30) | 0.54 (19/35) | 0.16 (11/68) | 0.03 (1/29) | 0.78 (7/9) |
| rules on, queue>=low, priority>=medium, +disagree | 0.34 (35/103) | 0.63 (19/30) | 0.54 (19/35) | 0.16 (11/68) | 0.03 (1/29) | 0.78 (7/9) |
| rules on, queue>=low, priority>=high, +disagree | 0.70 (72/103) | 0.87 (26/30) | 0.36 (26/72) | 0.13 (4/31) | 0.00 (0/29) | 0.89 (8/9) |
| rules on, queue>=medium, priority>=low, +disagree | 0.36 (37/103) | 0.70 (21/30) | 0.57 (21/37) | 0.14 (9/66) | 0.03 (1/29) | 0.78 (7/9) |
| rules on, queue>=medium, priority>=medium, +disagree | 0.36 (37/103) | 0.70 (21/30) | 0.57 (21/37) | 0.14 (9/66) | 0.03 (1/29) | 0.78 (7/9) |
| rules on, queue>=medium, priority>=high, +disagree | 0.70 (72/103) | 0.87 (26/30) | 0.36 (26/72) | 0.13 (4/31) | 0.00 (0/29) | 0.89 (8/9) |
| rules on, queue>=high, priority>=low, +disagree | 0.40 (41/103) | 0.80 (24/30) | 0.58 (24/41) | 0.10 (6/62) | 0.03 (1/29) | 0.78 (7/9) |
| rules on, queue>=high, priority>=medium, +disagree | 0.40 (41/103) | 0.80 (24/30) | 0.58 (24/41) | 0.10 (6/62) | 0.03 (1/29) | 0.78 (7/9) |
| rules on, queue>=high, priority>=high, +disagree | 0.72 (74/103) | 0.93 (28/30) | 0.38 (28/74) | 0.07 (2/29) | 0.00 (0/29) | 0.89 (8/9) |

## Errors nobody would review (13)

- **t2-007** (mixed) true ei/high, predicted ei/low, confidence high/high
  - ticket: Question about my weekly EI amount: I got my first EI payment this week and the weekly amount is lower than I expected. How do they work out the weekly rate? Also my CPP contribution was taken out of my last paycheque too, is that normal?
- **t2-012** (emotional) true ei/normal, predicted ei/high, confidence high/medium
  - ticket: Do I qualify??: I'm so stressed. I took three months off work when my baby was born and I have no idea if the EI people will consider that as enough hours. I've been working since I was 19 and I'm terrified I'm going to get nothing and have to pay rent with no income. Please just tell me if I qualify.
- **t2-040** (plain) true cpp/high, predicted cpp/normal, confidence high/medium
  - ticket: Request to review CPP decision: I received a letter saying my retirement pension amount was reduced because of my work history. I think some of my earlier contributions were not counted. How do I ask for this decision to be looked at again?
- **t2-102** (emotional) true oas/normal, predicted oas/high, confidence high/medium
  - ticket: I'm losing it over my OAS application: I've been trying to apply for Old Age Security for two weeks and nobody tells me what papers I actually need. I'm 66 and my savings are almost gone, I'm so tired of getting bounced from page to page. Please just tell me exactly which documents to send so I can stop panicking.
- **t2-117** (plain) true sin/low, predicted sin/normal, confidence high/medium
  - ticket: How do I apply for a SIN?: I recently moved to Canada to start a job and my employer asked for my SIN. I would like to know the steps to apply and whether I can do it online or need to go to an office. Thank you.
- **t2-133** (mixed) true sin/normal, predicted sin/low, confidence high/medium
  - ticket: How long to update my SIN record?: I changed my last name after my marriage and want to update my Social Insurance Number record. How long does this usually take to process? I also have a passport renewal coming up, so I'm wondering if I should wait for the SIN change first.
- **t2-137** (french) true sin/normal, predicted sin/urgent, confidence high/high
  - ticket: Numéro d'assurance sociale perdu: Bonjour, pendant mon voyage au Mexique, mon passeport a été volé avec mon portefeuille. Ma carte d'assurance sociale était dedans aussi. Est-ce que je peux faire un nouveau NAS à distance ou je dois me présenter quelque part?
- **t2-139** (plain) true sin/low, predicted sin/urgent, confidence high/high
  - ticket: My passport was stolen in Lisbon last week together with my wallet. My SIN card was in the wallet too. I am back home now. What do I need to bring to get a replacement card and does my record stay the same?
- **t2-142** (mixed) true sin/urgent, predicted passport/urgent, confidence high/medium
  - ticket: Passport and SIN mix-up: I got a letter about a passport I never applied for and my sister says someone may have used my SIN on a form. My passport renewal is due next month for my trip to Portugal, so I need to know whether the passport I have is safe to use and what to do.
- **t2-152** (emotional) true passport/low, predicted passport/normal, confidence high/medium
  - ticket: I moved last week and I'm furious that my passport still has my old address and my last name is wrong from my marriage. I can't keep waiting, how do I change this and is it going to cost me extra?
- **t2-162** (plain) true passport/low, predicted passport/normal, confidence high/high
  - ticket: Change of address and name on my passport: I moved to a new apartment last month and I also changed my last name after my marriage. I want to update both on my passport record. What documents do I need to send along, and does the change of name require a new passport or can it be done on the current one?
- **t2-170** (mixed) true passport/high, predicted passport/normal, confidence high/medium
  - ticket: Passport status: Applied for my passport renewal 9 weeks ago and the online tracker still says received. My cousin got his done faster and I also need to get my EI claim sorted since my job ended. How long should this take?
- **t2-196** (plain) true passport/urgent, predicted passport/high, confidence high/medium
  - ticket: I lost my passport somewhere during my trip last week and I'm trying to figure out how to get a replacement. I'm booked to fly home in ten days so I need to know the steps and how long it takes.

## Cost

Spent CA$0.0882; list cost CA$0.0882 (as if nothing was cached). Cache hits 0 of 103.
