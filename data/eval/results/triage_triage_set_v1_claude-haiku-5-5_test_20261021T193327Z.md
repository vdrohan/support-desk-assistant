# Triage eval: triage_set_v1.jsonl, claude-haiku-5-5

run 20261021T193327Z · split test · git 2d6cd0c · prompt 52e43f755ab5

## Overall

| metric | rate [95% CI] (k/n) |
|---|---|
| queue accuracy | 0.90 [0.81, 0.95] (72/80) |
| priority accuracy | 0.80 [0.70, 0.87] (64/80) |
| queue and priority both correct | 0.76 [0.66, 0.84] (61/80) |
| priority within one level | 1.00 [0.95, 1.00] (80/80) |
| urgent recall (true urgent found) | 0.95 [0.76, 0.99] (19/20) |
| urgent precision | 0.79 [0.59, 0.91] (19/24) |
| under-triage (predicted less urgent) | 0.09 [0.04, 0.17] (7/80) |
| over-triage (predicted more urgent) | 0.11 [0.06, 0.20] (9/80) |

Generation failures (counted as wrong): 0

## Gold subset (blind labels, n=26)

Labels by source: human 6, model-agreed 20. Model-agreed labels were not reviewed by a person.

| metric | rate [95% CI] (k/n) |
|---|---|
| queue | 1.00 [0.87, 1.00] (26/26) |
| priority | 0.92 [0.76, 0.98] (24/26) |
| urgent recall | 0.88 [0.53, 0.98] (7/8) |
| within one level | 1.00 [0.87, 1.00] (26/26) |

**Human-labelled subset (n=6)**

| metric | rate [95% CI] (k/n) |
|---|---|
| queue | 1.00 [0.61, 1.00] (6/6) |
| priority | 0.83 [0.44, 0.97] (5/6) |
| urgent recall | n/a (n=0) |
| within one level | 1.00 [0.61, 1.00] (6/6) |

## Baselines (same rows)

| baseline | queue | priority | urgent recall | within one level |
|---|---|---|---|---|
| majority (dev labels) | 0.21 [0.14, 0.31] (17/80) | 0.25 [0.17, 0.35] (20/80) | 1.00 [0.84, 1.00] (20/20) | 0.59 [0.48, 0.69] (47/80) |
| keywords | 0.72 [0.62, 0.81] (58/80) | 0.31 [0.22, 0.42] (25/80) | 0.55 [0.34, 0.74] (11/20) | 0.88 [0.79, 0.93] (70/80) |

## Per queue

| queue | recall | precision |
|---|---|---|
| ei | 0.91 [0.62, 0.98] (10/11) | 1.00 [0.72, 1.00] (10/10) |
| cpp | 1.00 [0.74, 1.00] (11/11) | 0.92 [0.65, 0.98] (11/12) |
| oas | 1.00 [0.61, 1.00] (6/6) | 1.00 [0.61, 1.00] (6/6) |
| sin | 0.88 [0.66, 0.97] (15/17) | 0.88 [0.66, 0.97] (15/17) |
| passport | 1.00 [0.82, 1.00] (18/18) | 0.90 [0.70, 0.97] (18/20) |
| other | 0.71 [0.47, 0.87] (12/17) | 0.80 [0.55, 0.93] (12/15) |

## By style

| style | n | queue | priority |
|---|---|---|---|
| confused_program | 9 | 0.67 [0.35, 0.88] (6/9) | 0.78 [0.45, 0.94] (7/9) |
| emotional | 15 | 0.87 [0.62, 0.96] (13/15) | 0.73 [0.48, 0.89] (11/15) |
| french | 5 | 1.00 [0.57, 1.00] (5/5) | 0.60 [0.23, 0.88] (3/5) |
| greeting | 3 | 1.00 [0.44, 1.00] (3/3) | 1.00 [0.44, 1.00] (3/3) |
| injection | 3 | 1.00 [0.44, 1.00] (3/3) | 1.00 [0.44, 1.00] (3/3) |
| mixed | 10 | 1.00 [0.72, 1.00] (10/10) | 0.80 [0.49, 0.94] (8/10) |
| plain | 21 | 0.86 [0.65, 0.95] (18/21) | 0.76 [0.55, 0.89] (16/21) |
| short | 14 | 1.00 [0.79, 1.00] (14/14) | 0.93 [0.69, 0.99] (13/14) |

## Confusion

**Queue** (rows true, columns predicted)

| true \ pred | ei | cpp | oas | sin | passport | other |
|---|---|---|---|---|---|---|
| ei | 10 | 0 | 0 | 0 | 0 | 1 |
| cpp | 0 | 11 | 0 | 0 | 0 | 0 |
| oas | 0 | 0 | 6 | 0 | 0 | 0 |
| sin | 0 | 0 | 0 | 15 | 0 | 2 |
| passport | 0 | 0 | 0 | 0 | 18 | 0 |
| other | 0 | 1 | 0 | 2 | 2 | 12 |

**Priority** (rows true, columns predicted)

| true \ pred | urgent | high | normal | low |
|---|---|---|---|---|
| urgent | 19 | 1 | 0 | 0 |
| high | 5 | 19 | 3 | 0 |
| normal | 0 | 2 | 9 | 3 |
| low | 0 | 0 | 2 | 17 |

## Misses (19)

- **tc-003** (french) true ei/normal, predicted ei/low
  - ticket: Délai de traitement de ma demande: Bonjour, j'ai envoyé ma demande de prestations de maladie il y a trois semaines. Pouvez-vous me dire combien de temps prend normalement le traitement? Je voudrais simplement savoir à quoi m'attendre.
  - rationale: The ticket asks, in French, how long processing normally takes for a sickness benefits application submitted three weeks ago; sickness benefits are Employment Insurance benefits. The writer only wants general expectations with no stated deadline, financial hardship, or problem to fix.
- **tc-009** (plain) true ei/normal, predicted other/normal
  - ticket: Trip next month, need to know about my benefits: I am planning to go to visit my sister in Alberta for three weeks starting on the 3rd of next month. I am currently getting regular benefits. Do I need to tell someone before I leave, and will my payments stop while I am away?
  - rationale: The ticket asks whether the writer must report a planned three-week trip and whether regular benefit payments will stop while away. The benefits are not specified as EI, CPP or OAS, so the program is not stated; the trip is about a month away and there is no immediate risk to payments, so the priority is normal.
- **tc-025** (plain) true ei/high, predicted ei/urgent
  - ticket: EI reporting deadline: I received a letter saying I need to file my biweekly report by Friday the 14th. I have been working part-time this month and want to make sure I report the earnings correctly before the deadline.
  - rationale: The ticket is about an Employment Insurance biweekly report due Friday the 14th, which is a deadline within the next 7 days, so it belongs in the EI queue with urgent priority.
- **tc-033** (mixed) true cpp/low, predicted cpp/normal
  - ticket: Hi, I changed my last name after my wedding in June and wanted to tell you so my CPP statements match. I also moved to Calgary in August for work, so my mailing address is different now, could you update both? Also my EI claim is still under my old name, not sure if that matters.
  - rationale: The writer reports a name change after a June wedding and a new mailing address in Calgary, asking that CPP records be updated to match. They also ask whether their EI claim under the old name matters. No payment has stopped, no deadline is stated, and no one is at risk, so this is a routine update with no time pressure. The main request concerns CPP records, so the queue is cpp.
- **tc-039** (confused_program) true sin/urgent, predicted other/urgent
  - ticket: My pension money: I got a letter saying my government money was paid out in my name and I never asked for it. Somebody might be using my details, what do I do about my pension?
  - rationale: The writer received a notice that government pension money was paid in their name without their request and suspects someone is using their identity, which puts their money and status at immediate risk. The main request concerns a pension, most likely OAS or CPP, but the ticket does not name the program, so the queue is other.
- **tc-058** (confused_program) true other/high, predicted passport/urgent
  - ticket: My pension people said I can't get the thing I need until something gets fixed, and I'm flying out on Friday. The paper I have is old and I think it ran out last year. Can someone tell me what to do before the trip?
  - rationale: The ticket says the person needs a document to fly out on Friday, that their current passport appears to have expired last year, and that it must be fixed before the trip; travel within the next 7 days with no valid passport is urgent. The mention of 'pension people' is incidental, since the actual need is a passport renewal.
- **tc-072** (emotional) true oas/high, predicted oas/normal
  - ticket: Why is my OAS so low??: I opened my latest statement and my OAS payment is way less than I expected. I've worked hard my whole life and I feel like I'm being shortchanged. How exactly do you work out the amount, because nothing I read makes sense to me?
  - rationale: The ticket is about an Old Age Security payment that is lower than the person expected, and asks how the amount is calculated. No deadline or payment stoppage is mentioned, so it is a question about their own situation with no stated time pressure. A wrong payment amount would normally rank as high, but the ticket describes the amount as a surprise and asks for an explanation, not a correction, so normal fits.
- **tc-074** (plain) true oas/high, predicted oas/urgent
  - ticket: Question about my start date: I'm turning 65 next month and I'm starting a new part-time job on the 3rd. My employer needs written proof of when my old age pension starts and the amount before my first paycheque. Can you tell me how to get a letter or confirmation of my payment amount in the next fe
  - rationale: The ticket asks how to get written confirmation of the Old Age Security pension start date and amount, which the employer needs before the person's new job starts on the 3rd, within a few days. This is a deadline within 7 days, so the priority is urgent.
- **tc-080** (short) true oas/high, predicted oas/normal
  - ticket: OAS amount: How do they work out my monthly OAS amount? I got less than I expected this month.
  - rationale: The ticket asks how the monthly OAS amount is calculated and says the person received less than expected this month. This is a question about an Old Age Security payment amount, with no stated deadline or stopped payment, so it fits oas and normal priority.
- **tc-083** (emotional) true other/normal, predicted sin/high
  - ticket: I was supposed to get a certain amount for the work I did through the placement agency, but my pay stub shows way less and I don't understand why. My SIN is on the paperwork so maybe they put the wrong number on my file? This is making me so angry because I budgeted around the full amount and now I'
  - rationale: The ticket describes a pay stub showing less than expected from a placement agency, and the writer wonders whether the SIN on file is wrong. The main issue is a possibly incorrect SIN record, which falls under the SIN queue, but there is no deadline or stopped benefit, only a wrong amount of pay that the writer is short on, so it is a time-bound problem without immediate danger.
- **tc-086** (confused_program) true sin/normal, predicted other/low
  - ticket: Changement d'adresse: Hi, I moved last month and changed my last name after my marriage. My old government card still has my old name. Where do I tell them about this change so my card and records get updated?
  - rationale: The ticket asks how to report a move and a last-name change so that a government card and records are updated, with no deadline or stated loss of benefits. The card is not specified as a SIN card or passport, so the request is a general how-to question about updating personal information, which fits no specific program queue well and is best routed to other.
- **tc-087** (emotional) true other/high, predicted cpp/normal
  - ticket: Why is my number so low??: I went to check my payments and the amount is way less than I expected and nobody explains how they got it. I've been paying into this for years and it feels like I'm being cheated. Can someone tell me exactly how the calculation works before I lose my mind?
  - rationale: The ticket asks why the amount of a payment is lower than expected and wants an explanation of how it is calculated, with no stated deadline or missing payment. The program is not named, but contributions over years and a payment amount point to a pension benefit, most likely CPP, and the request is a general explanation with no time pressure.
- **tc-089** (plain) true other/low, predicted sin/low
  - ticket: Eligibility question: I'm a student who worked part-time for two years and stopped last spring. I'm wondering if I'm eligible for a SIN-related benefit or any support connected to my SIN, and what documents I would need to show. Thanks for any guidance.
  - rationale: The ticket asks in general terms whether a former part-time student is eligible for a SIN-related benefit and what documents are needed, with no stated deadline, lost payment, or problem to fix. Because the question is about the SIN and possible benefits, the best fit is the SIN queue.
- **tc-099** (french) true passport/urgent, predicted passport/high
  - ticket: Passeport perdu en voyage: Bonjour, mon sac contenant mon passeport a été volé pendant mon séjour à Lisbonne la semaine dernière. Je dois rentrer au Canada dans dix jours. Que dois-je faire pour obtenir un nouveau passeport rapidement?
  - rationale: The ticket reports a passport stolen while abroad in Lisbon and a return to Canada in ten days, asking how to get a replacement quickly. This is a passport matter, and the stolen passport abroad with travel within 7 days rule applies only partly since return is in 10 days, so it falls in the high band for a travel deadline within about a month.
- **tc-103** (plain) true sin/normal, predicted sin/low
  - ticket: Hi, I applied for a new SIN about three weeks ago after moving to Canada. How long does it usually take to get the card in the mail? I don't have a deadline, just want to plan my job search.
  - rationale: The ticket asks how long a SIN card usually takes to arrive after applying about three weeks ago, with no deadline or time pressure stated. This is a general information question about the sin program.
- **tc-107** (mixed) true passport/low, predicted passport/normal
  - ticket: Hi, I'm applying for my first passport for a trip in the fall. Also got a letter from EI saying my payments were cut back, not sure why. How long does the passport part usually take to process after you send in the forms?
  - rationale: The ticket asks mainly how long a first passport takes to process, with travel planned for the fall, which is about a month or more away and not within 7 days. It also mentions an EI payment reduction, but that is a secondary concern, so the main queue is passport and the priority is normal.
- **tc-113** (emotional) true passport/normal, predicted passport/high
  - ticket: Passport address and name still wrong!!: I moved last month and my passport still has my old address and I got married in March so my last name changed too and nobody told me what to do about it. I am so frustrated, I feel like I'm being ignored and I need this fixed before my trip, please help.
  - rationale: The ticket is about a Canadian passport that shows an old address and a former last name after a move and a marriage, and the writer is asking for guidance on updating it. The writer says the fix is needed before a trip, but no departure date is given, so the time pressure is not clearly within days. This is a stated problem to fix before travel of unknown timing, which fits high rather than urgent.
- **tc-129** (plain) true passport/high, predicted passport/urgent
  - ticket: Passport before trip: My flight to Lisbon leaves on the 14th. My passport expires in two months but I think the officer said I need six months, so I may need to renew it. Can you tell me how fast I can get one with a trip next week?
  - rationale: The ticket asks about a passport renewal for a flight to Lisbon on the 14th, which is within about a week and so falls within the urgent travel window. The passport is still valid for two months, so the traveller may not be refused, but they are unsure whether it meets the six-month requirement and want to know how fast a renewal can be done.
- **tc-139** (plain) true other/high, predicted passport/urgent
  - ticket: Health card question: Hi, my passport was stolen while I was on a trip last week. I'm not sure whether my provincial health card still works for the hospital visit I need next month, or if I have to do something first. Can someone tell me what to do?
  - rationale: The ticket reports a passport stolen while travelling last week, which the rules treat as a lost or stolen passport abroad and therefore urgent. The health card question is secondary to the passport problem, so the main request belongs to the passport queue.

## Cost

Spent CA$0.0229; list cost CA$0.0229 (as if nothing was cached). Cache hits 0 of 80.
