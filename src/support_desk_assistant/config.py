"""Single source of truth for runtime configuration.

All tunable values (chunk size, top_k, model names, collection names) and the
environment variables that hold API keys will be defined here in Phase 1, using
pydantic-settings. Other modules import from here; they never hardcode these
values or read the environment directly.
"""
