# API image (docs/decisions.md #31). Two stages: build the virtualenv, then copy only it.
# The base is pinned by tag and digest (python:3.12.15-slim-bookworm, checked 2026-10-26), spelled
# out in both FROM lines so Dependabot can bump them; keep the two identical.

FROM python:3.15.0-slim-bookworm@sha256:0e019bbdfb5d9874cb2389f1c2884b64aa2ac97c99960b5bc521cef978c0617d AS build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
RUN pip install --no-cache-dir uv==0.12.23
WORKDIR /app

# Dependencies first: this layer is rebuilt only when pyproject.toml or uv.lock change.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --locked --no-dev --no-install-project --no-editable

COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.15.0-slim-bookworm@sha256:0e019bbdfb5d9874cb2389f1c2884b64aa2ac97c99960b5bc521cef978c0617d
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
RUN useradd --system --uid 10001 --home-dir /app app
WORKDIR /app
COPY --from=build /app/.venv ./.venv
# Settings paths are relative to the working directory (config.py), so the files the API reads
# live under /app: the system prompts and the manifest (crawl dates for the answer prompt).
COPY prompts ./prompts
COPY data/manifest.jsonl ./data/manifest.jsonl
# Written at run time: the ticket database, the response caches and the request log. compose.yaml
# mounts a volume on each; a new volume takes its owner from these directories.
RUN mkdir -p data/db data/cache logs && chown -R app:app data logs
USER app
EXPOSE 8000
# No SDA_API_KEY, no start (decision 31). All keys come from the environment, never the image.
CMD ["sda-api"]
