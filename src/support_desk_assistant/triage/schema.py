from typing import Any, Literal, get_args

from anthropic import transform_schema
from pydantic import BaseModel, ConfigDict

# One queue per program in the corpus, plus "other" for anything outside them.
Queue = Literal["ei", "cpp", "oas", "sin", "passport", "other"]
Priority = Literal["urgent", "high", "normal", "low"]

QUEUES: tuple[str, ...] = get_args(Queue)
PRIORITIES: tuple[str, ...] = get_args(Priority)


class TriageResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Generated in this order: the reasoning comes before the labels it justifies
    # (decisions.md #25, #26).
    rationale: str
    queue: Queue
    priority: Priority


def output_schema() -> dict[str, Any]:
    """JSON schema for ``output_config.format``, adapted to what the API supports."""
    return transform_schema(TriageResult)
