"""The structured answer Claude must return (validated by pydantic, enforced by the API)."""

from typing import Any

from anthropic import transform_schema
from pydantic import BaseModel, ConfigDict


class AnswerResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Generated in this order. The abstain decision comes first so the model commits to it
    # before it writes any prose (decisions.md #26).
    abstained: bool
    abstain_reason: str | None
    answer: str
    citations: list[str]  # chunk_ids from the retrieved set


def output_schema() -> dict[str, Any]:
    """JSON schema for ``output_config.format``, adapted to what the API supports."""
    return transform_schema(AnswerResult)
