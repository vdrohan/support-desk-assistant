# Changelog

What changed, by phase, in plain words. The commits have the details and [docs/decisions.md](docs/decisions.md) has the reasons. Dates are when the work reached `main`. Numbers come from the committed reports in [`data/eval/results/`](data/eval/results/); [docs/writeup.md](docs/writeup.md) explains how to read them.

## Phase 4: API key and Docker image (2026-11-03)

- `/ask` and every `/tickets` route need an `X-API-Key` header, and the API refuses to start without a key (decision 31).
- A Dockerfile, a Compose `api` service and a CI job that builds and checks the image. The API is not deployed, by choice.
- The README opens with a results table and three real examples.
- One commit style, checked by a hook and by CI (decision 32).

## Phases 2 and 3: triage and escalation (2026-10-20 to 2026-10-26, PR #2)

- Ticket triage: `POST /tickets` puts a ticket in one of six queues with one of four priorities and stores it in SQLite (decision 28).
- Triage evaluation: two ticket sets drafted by Haiku, each with a dev/test split that is scored once. Test split: queue 0.90 and urgent recall 19/20 on set v1, queue 0.88 and 28/29 on set v2. Most labels were written or agreed by models, not people.
- Escalation: a `needs_review` flag from rules, the classifier's own confidence and a second Haiku opinion. On the v2 test split it flags 41 of 103 tickets and catches 24 of the 30 classifier errors. Its four targets were written first and all were met (decision 29).
- `POST /tickets/{id}/override` records a person's decision beside the model's answer (decision 30).
- The write-up with all results, limits and costs: [docs/writeup.md](docs/writeup.md).

## Phase 1: question answering (2026-10-05 to 2026-10-20)

- A crawler for 129 canada.ca pages, then chunking, Voyage embeddings, a Qdrant index and a search command.
- Answers with citations, checked by a hand-written validator, or an abstention (decisions 15 to 17).
- Retrieval eval: the right page is in the top 5 for 0.91 of 34 questions.
- Answer eval: 42 of 51 answerable questions pass and 7 of 7 unanswerable ones abstain. Moving the `abstained` field ahead of the answer text took abstention from 4/9 to 7/9 (decision 26), and the judge got the same fix (decision 25).
- `POST /ask` serves the same pipeline over HTTP (decision 27).

## Phase 0: scaffolding (2026-10-04)

- Project layout, uv, ruff, pytest, pre-commit and a Compose file for Qdrant.
