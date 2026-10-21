"""uv run sda-eval-triage-draft [--yes] [--force] [--cap CAD]

Haiku drafts candidate tickets from a scenario (topic, situation, style). It is never told a
queue or a priority and never sees the triage rules, so the labels are not built into the
tickets; they are added afterwards (``sda-eval-triage-label``, then a person's blind review).
Dry run by default (docs/decisions.md #28).
"""

import argparse
import json
import random
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from anthropic import transform_schema
from pydantic import BaseModel, ConfigDict, ValidationError

from support_desk_assistant.answer.cache import ResponseCache, cache_key
from support_desk_assistant.answer.cost import Usage, cost_usd, usd_to_cad
from support_desk_assistant.answer.pipeline import (
    CHARS_PER_TOKEN_ESTIMATE,
    MessagesClient,
    load_system_prompt,
    make_anthropic_client,
    usage_from_raw,
)
from support_desk_assistant.config import Settings, get_settings
from support_desk_assistant.triage.schema import QUEUES

PROGRAMS: tuple[str, ...] = tuple(q for q in QUEUES if q != "other")
NO_SITUATION = "none"


class DraftedTicket(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str
    body: str


class DraftResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tickets: list[DraftedTicket]


class TicketCandidate(BaseModel):
    """A drafted ticket. ``topic`` is what the drafter was asked to write about, not a label."""

    model_config = ConfigDict(extra="forbid")

    id: str
    subject: str
    body: str
    style: str
    topic: str  # a program, or "other"
    situation: str


@dataclass(frozen=True)
class Spec:
    program: str  # one of PROGRAMS, or "other"
    topic: str  # only for "other": the off-corpus topic
    situation: str
    style: str


@dataclass(frozen=True)
class Task:
    program: str
    specs: tuple[Spec, ...]
    hints: tuple[str, ...]  # page titles for a program; empty for "other"


def load_titles(settings: Settings) -> dict[str, list[str]]:
    by_program: dict[str, list[str]] = {}
    for line in settings.manifest_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            by_program.setdefault(row["program"], []).append(row["title"])
    return by_program


def plan_specs(settings: Settings) -> list[Spec]:
    cfg = settings.triage_eval
    rng = random.Random(cfg.draft_seed)
    styles = [s for s, n in cfg.style_counts.items() for _ in range(n)]
    rng.shuffle(styles)
    total = len(styles)
    n_other = round(total * cfg.other_share)
    programs = ["other"] * n_other + [PROGRAMS[i % len(PROGRAMS)] for i in range(total - n_other)]
    rng.shuffle(programs)
    specs = []
    for style, program in zip(styles, programs, strict=True):
        if style == "greeting":
            situation = NO_SITUATION
        elif rng.random() < cfg.high_stakes_share:
            situation = rng.choice(cfg.high_stakes_situations)
        else:
            situation = rng.choice(cfg.other_situations)
        topic = rng.choice(cfg.other_topics) if program == "other" else ""
        specs.append(Spec(program, topic, situation, style))
    return specs


def plan_tasks(settings: Settings, titles: dict[str, list[str]]) -> list[Task]:
    cfg = settings.triage_eval
    rng = random.Random(cfg.draft_seed + 1)
    by_program: dict[str, list[Spec]] = {}
    for spec in plan_specs(settings):
        by_program.setdefault(spec.program, []).append(spec)
    tasks = []
    for program in (*PROGRAMS, "other"):
        group = by_program.get(program, [])
        pool = sorted(set(titles.get(program, [])))
        for i in range(0, len(group), cfg.tickets_per_request):
            hints = tuple(rng.sample(pool, min(6, len(pool)))) if pool else ()
            tasks.append(Task(program, tuple(group[i : i + cfg.tickets_per_request]), hints))
    return tasks


def render_user_message(task: Task) -> str:
    lines = [f"topic: {task.program}"]
    if task.hints:
        lines += ["page titles from this program (hints only):", *[f"- {h}" for h in task.hints]]
    lines.append(f"tickets: {len(task.specs)}")
    for i, spec in enumerate(task.specs, 1):
        parts = [f"{i}."]
        if spec.topic:
            parts.append(f"topic: {spec.topic};")
        parts.append(f"situation: {spec.situation}; style: {spec.style}")
        lines.append(" ".join(parts))
    return "\n".join(lines)


def build_request(system: str, user_message: str, settings: Settings) -> dict[str, Any]:
    cfg = settings.triage_eval
    profile = settings.answer.models[cfg.draft_model]
    return {
        "model": cfg.draft_model,
        "max_tokens": cfg.draft_max_tokens,
        "system": [{"type": "text", "text": system}],
        "messages": [{"role": "user", "content": user_message}],
        "thinking": {"type": profile.thinking},
        "output_config": {
            "effort": profile.effort,
            "format": {"type": "json_schema", "schema": transform_schema(DraftResponse)},
        },
    }


def request_key(system: str, user_message: str, settings: Settings) -> str:
    cfg = settings.triage_eval
    profile = settings.answer.models[cfg.draft_model]
    return cache_key(
        {
            "kind": "triage_draft",
            "schema_version": cfg.draft_schema_version,
            "model": cfg.draft_model,
            "system": system,
            "user": user_message,
            "max_tokens": cfg.draft_max_tokens,
            "effort": profile.effort,
            "thinking": profile.thinking,
        }
    )


def parse_response(raw: dict[str, Any]) -> tuple[DraftResponse | None, str]:
    stop = raw.get("stop_reason")
    if stop in ("refusal", "max_tokens"):
        return None, str(stop)
    text = "".join(b.get("text", "") for b in raw.get("content", []) if b.get("type") == "text")
    try:
        return DraftResponse.model_validate_json(text), "ok"
    except ValidationError:
        return None, "invalid_output"


@dataclass
class DraftRun:
    candidates: list[TicketCandidate] = field(default_factory=list)
    calls: int = 0
    cache_hits: int = 0
    usage: Usage = field(default_factory=Usage)
    failures: list[str] = field(default_factory=list)
    shortfalls: list[str] = field(default_factory=list)


def estimate_cad(tasks: Sequence[Task], system: str, settings: Settings, cached: set[str]) -> float:
    """Upper bound for the calls not already cached: input from characters, output at the cap."""
    cfg = settings.triage_eval
    usd = 0.0
    for task in tasks:
        user = render_user_message(task)
        if request_key(system, user, settings) in cached:
            continue
        usage = Usage(
            input_tokens=int((len(system) + len(user)) / CHARS_PER_TOKEN_ESTIMATE),
            output_tokens=cfg.draft_max_tokens,
        )
        usd += cost_usd(usage, cfg.draft_model, settings.answer)
    return usd_to_cad(usd, settings.answer)


def run_tasks(
    tasks: Sequence[Task],
    *,
    system: str,
    settings: Settings,
    client: MessagesClient | None = None,
    cap_cad: float | None = None,
) -> DraftRun:
    """Call (or replay from cache) each task. Calls that completed are cached, so a re-run
    resumes without re-spending. Stops before a call that would pass the cap."""
    cfg = settings.triage_eval
    cache = ResponseCache(settings.eval_cache_dir)
    run = DraftRun()
    spent = 0.0
    counter = 0
    for task in tasks:
        user = render_user_message(task)
        key = request_key(system, user, settings)
        raw = cache.get(key)
        label = f"{task.program}/{task.specs[0].style}"
        if raw is not None:
            run.cache_hits += 1
        else:
            if cap_cad is not None:
                bound = estimate_cad([task], system, settings, set())
                if spent + bound > cap_cad:
                    run.failures.append(f"{label}: stopped by spend cap")
                    break
            client = client or make_anthropic_client(settings)
            message = client.messages.create(**build_request(system, user, settings))
            raw = message.to_dict()
            cache.put(key, raw)
            run.calls += 1
            usage = usage_from_raw(raw)
            run.usage = Usage(
                run.usage.input_tokens + usage.input_tokens,
                run.usage.output_tokens + usage.output_tokens,
            )
            spent += usd_to_cad(cost_usd(usage, cfg.draft_model, settings.answer), settings.answer)
        parsed, status = parse_response(raw)
        if parsed is None:
            run.failures.append(f"{label}: {status}")
            continue
        if len(parsed.tickets) < len(task.specs):
            run.shortfalls.append(
                f"{label}: asked for {len(task.specs)}, got {len(parsed.tickets)}"
            )
        for drafted, spec in zip(parsed.tickets, task.specs, strict=False):
            counter += 1
            run.candidates.append(
                TicketCandidate(
                    id=f"tc-{counter:03d}",
                    subject=drafted.subject.strip(),
                    body=drafted.body.strip(),
                    style=spec.style,
                    topic=spec.program,
                    situation=spec.situation,
                )
            )
    return run


def write_candidates(path: Path, candidates: Sequence[TicketCandidate]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(c.model_dump_json() + "\n" for c in candidates), encoding="utf-8")
    tmp.replace(path)


def load_candidates(path: Path) -> list[TicketCandidate]:
    if not path.exists():
        return []
    return [
        TicketCandidate.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    cfg = settings.triage_eval
    parser = argparse.ArgumentParser(prog="sda-eval-triage-draft", description=__doc__)
    parser.add_argument("--yes", action="store_true", help="call Claude (costs money)")
    parser.add_argument("--force", action="store_true", help="overwrite existing candidates")
    parser.add_argument("--cap", type=float, default=cfg.spend_cap_cad, help="spend cap, CAD")
    args = parser.parse_args(argv)

    out = settings.triage_candidates_path
    if out.exists() and args.yes and not args.force:
        print(f"{out} exists; labelling or review may be in progress. Use --force to overwrite.")
        return 2
    system = load_system_prompt(cfg.draft_prompt_path)
    tasks = plan_tasks(settings, load_titles(settings))
    cache = ResponseCache(settings.eval_cache_dir)
    cached = {
        key
        for t in tasks
        if cache.get(key := request_key(system, render_user_message(t), settings)) is not None
    }
    total = sum(len(t.specs) for t in tasks)
    est = estimate_cad(tasks, system, settings, cached)
    print(
        f"{len(tasks)} requests for {total} tickets "
        f"({len(cached)} cached). Upper bound CA${est:.4f} vs cap CA${args.cap:.2f}."
    )
    if est > args.cap:
        print("Estimate is over the cap; nothing sent.")
        return 2
    if not args.yes:
        print("dry run: nothing sent; add --yes to draft.")
        return 0
    run = run_tasks(tasks, system=system, settings=settings, cap_cad=args.cap)
    write_candidates(out, run.candidates)
    styles = Counter(c.style for c in run.candidates)
    print(
        f"wrote {len(run.candidates)} candidates to {out} "
        f"({run.calls} calls, {run.cache_hits} cached); styles {dict(styles)}"
    )
    for line in (*run.failures, *run.shortfalls):
        print(f"  problem: {line}")
    return 1 if run.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
