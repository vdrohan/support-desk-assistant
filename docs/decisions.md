# Decisions

Short architecture decision records. Newest decisions go at the bottom. If a decision changes, add a new entry that supersedes the old one instead of editing it.

## 1. Qdrant as the vector database

- **Decision:** Qdrant, run as a container and pinned to a specific image version in `compose.yaml`.
- **Reason:** It's a purpose-built vector DB with payload filtering (needed later to scope retrieval by product or ticket category), hybrid search, and a clean Python client. It runs as one small container on low-power hardware.
- **Rejected:**
  - *Chroma:* easy to start with, but fewer filtering and ops features. Its embedded mode also blurs the line between app and DB.
  - *pgvector:* strong if Postgres is already in the stack, but it isn't. Adding Postgres only for vectors means more to run for less vector-specific tooling.

## 2. Voyage AI for embeddings

- **Decision:** Voyage AI embedding models through its API.
- **Reason:** Strong retrieval quality on benchmarks, separate query and document input types, and it's the embedding provider Anthropic recommends alongside Claude.
- **Rejected:**
  - *OpenAI embeddings:* a good alternative, but they add a second large vendor for no clear quality gain.
  - *Local sentence-transformers:* see decision 4.

## 3. Claude for generation

- **Decision:** Anthropic Claude models through the Messages API. Model names are set in config.
- **Reason:** Good at grounded answers with citations and at following instructions for structured output (needed for triage). Prompt caching cuts token cost for repeated system prompts and context.
- **Rejected:**
  - *Other hosted LLMs:* possible. Keeping model names in config means generation can be re-evaluated against the eval set later.

## 4. Cloud APIs only, no local models

- **Decision:** All embedding and generation goes through hosted APIs.
- **Reason:** The deploy host is a 2017 MacBook Air (no usable GPU, little RAM). Local models would be slow and lower quality, and hard to evaluate fairly. Cloud APIs keep the host's job to orchestration and Qdrant.
- **Rejected:**
  - *Ollama or local models:* not viable on the target hardware.
  - *Hybrid (local embeddings, cloud generation):* runs into the same hardware limit.
- **Cost:** Needs network access and API keys, and costs money per call, so token efficiency is an explicit project goal.

## 5. SQLite for ticket data (Phase 2)

- **Decision:** SQLite file for ticket and triage data once the triage layer exists.
- **Reason:** No server to run, a single file that's easy to back up, and plenty for one-node, portfolio-scale volume. Standard SQL keeps a later move to Postgres cheap.
- **Rejected:**
  - *Postgres:* another container and more operational work with no benefit at this scale.
  - *Storing tickets as Qdrant payloads:* mixes relational and vector concerns, and querying is awkward.

## 6. Docker Compose with ports bound to 127.0.0.1

- **Decision:** All services run through `compose.yaml`, and every published port binds to `127.0.0.1`.
- **Reason:** Docker's port publishing goes around host firewalls like ufw. Binding to `0.0.0.0` would expose Qdrant (which has no auth by default) to the whole LAN. The host is reached only over Tailscale SSH, so localhost binding plus SSH tunnelling is enough.
- **Rejected:**
  - *Binding to 0.0.0.0 and relying on the firewall:* Docker goes around it.
  - *Kubernetes or k3s:* too heavy for one node.
