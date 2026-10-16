"""Reproducibility block recorded with every answer-eval run.

Everything here is cheap and offline: file hashes, git state, config hashes and SDK
versions. It never touches secrets and never dumps the whole ``Settings`` object.
"""

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any

from support_desk_assistant.config import Settings
from support_desk_assistant.evals.retrieval import git_output

# Bumped when the shape of the provenance block changes.
PROVENANCE_VERSION = 1
MAX_UNTRACKED = 50
SDK_PACKAGES = ("anthropic", "voyageai", "qdrant-client", "pydantic")


def file_sha256(path: Path) -> str | None:
    """sha256 of a file's bytes, or None if it doesn't exist."""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        return None


def _canonical(obj: Any) -> Any:
    if isinstance(obj, Mapping):
        return {str(k): _canonical(v) for k, v in obj.items()}
    if isinstance(obj, set | frozenset):
        items = [_canonical(v) for v in obj]
        return sorted(items, key=lambda v: json.dumps(v, sort_keys=True, ensure_ascii=False))
    if isinstance(obj, list | tuple):
        return [_canonical(v) for v in obj]
    if isinstance(obj, Path):
        return str(obj)
    return obj


def config_hash(obj: Any) -> str:
    """sha256 of canonical JSON; dict key order and set order don't matter."""
    text = json.dumps(_canonical(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def new_run_id(now: datetime | None = None) -> str:
    """UTC timestamp id, ``YYYYMMDDTHHMMSSZ``."""
    now = now or datetime.now(UTC)
    if now.tzinfo is not None:
        now = now.astimezone(UTC)
    return now.strftime("%Y%m%dT%H%M%SZ")


def git_state() -> dict[str, Any]:
    """Short sha, dirty flag, and a hash of uncommitted changes (diff + untracked paths).

    Never raises: if git is unavailable the sha is ``"unknown"`` and the tree counts as clean.
    """
    sha = git_output("rev-parse", "--short", "HEAD")
    if not sha:
        return {"sha": "unknown", "dirty": False, "diff_sha256": None, "untracked": []}
    status = git_output("status", "--porcelain")
    untracked = sorted(line[3:].strip() for line in status.splitlines() if line.startswith("??"))
    dirty = bool(status)
    diff_sha = None
    if dirty:
        diff = git_output("diff", "HEAD")
        payload = diff + "\n\0untracked\0\n" + "\n".join(untracked)
        diff_sha = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return {
        "sha": sha,
        "dirty": dirty,
        "diff_sha256": diff_sha,
        "untracked": untracked[:MAX_UNTRACKED],
    }


def dirty_blocks_results(git: Mapping[str, Any]) -> bool:
    """Rule: a dirty tree may not write into the committed results/ directory."""
    return bool(git["dirty"])


def sdk_versions() -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for name in SDK_PACKAGES:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def _model_block(settings: Settings, model_id: str) -> dict[str, Any]:
    profile = settings.answer.models.get(model_id)
    return {
        "id": model_id,
        "max_tokens": profile.max_tokens if profile else None,
        "thinking": profile.thinking if profile else None,
        "effort": profile.effort if profile else None,
    }


def collect_provenance(
    settings: Settings,
    *,
    dataset_path: Path,
    answer_model: str,
    k: int,
    scorer_config: Mapping[str, Any],
    points_count: int | None,
    run_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """JSON-able provenance block for one answer-eval run. Contains no secrets."""
    now = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    answer = settings.answer
    scorer = _canonical(dict(scorer_config))
    return {
        "provenance_version": PROVENANCE_VERSION,
        "run_id": run_id or new_run_id(now),
        "created_at": now.isoformat(),
        "git": git_state(),
        "dataset": {"name": Path(dataset_path).name, "sha256": file_sha256(dataset_path)},
        "corpus": {
            "manifest_sha256": file_sha256(settings.manifest_path),
            "collection": settings.index.collection,
            "points_count": points_count,
            "embed_model": settings.embed.model,
            "embed_dimension": settings.embed.output_dimension,
        },
        "prompts": {
            "answer_sha256": file_sha256(answer.system_prompt_path),
            "judge_sha256": file_sha256(settings.eval.judge_prompt_path),
        },
        "models": {
            "answer": _model_block(settings, answer_model),
            "judge": _model_block(settings, settings.eval.judge_model),
        },
        "k": k,
        "min_score": answer.min_score,
        "prompt_caching": answer.prompt_caching,
        "schema_version": answer.schema_version,
        "judge_schema_version": settings.eval.judge_schema_version,
        "usd_cad": answer.usd_cad,
        "prices_as_of": answer.prices_as_of,
        "scorer": {"config": scorer, "sha256": config_hash(scorer)},
        "sdk": sdk_versions(),
    }
