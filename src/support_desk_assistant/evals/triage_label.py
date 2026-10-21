"""uv run sda-eval-triage-label [--yes] [--cap CAD] | --build

Labels the drafted tickets. A stronger model (``label_model``) labels every ticket from the
triage rules: these are *silver* labels. A person labels a stratified sample blind
(``sda-eval-triage-review``): *gold*. ``--build`` merges both into the versioned triage set
with a fixed dev/test split. Dry run by default (docs/decisions.md #28).
"""

import argparse
import hashlib
import json
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from support_desk_assistant.answer.cache import ResponseCache
from support_desk_assistant.answer.cost import Usage, cost_usd, usd_to_cad
from support_desk_assistant.answer.pipeline import (
    CHARS_PER_TOKEN_ESTIMATE,
    MessagesClient,
    load_system_prompt,
)
from support_desk_assistant.config import Settings, get_settings
from support_desk_assistant.evals.triage_draft import TicketCandidate, load_candidates
from support_desk_assistant.triage.pipeline import (
    STATUS_OK,
    render_user_message,
    request_key,
    triage_ticket,
)
from support_desk_assistant.triage.schema import PRIORITIES, QUEUES


class SilverLabel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    status: str
    queue: str | None
    priority: str | None
    rationale: str | None
    model: str
    cost_cad: float = 0.0


class GoldLabel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    queue: str
    priority: str
    labelled_by: str = "human"


def labeller_settings(settings: Settings) -> Settings:
    """The triage settings with the labelling model and its larger token budget."""
    cfg = settings.triage_eval
    clone = settings.model_copy(deep=True)
    clone.triage.model = cfg.label_model
    clone.triage.max_tokens = cfg.label_max_tokens
    return clone


def split_of(ticket_id: str, settings: Settings) -> str:
    """Deterministic dev/test split from the ticket id; stable as tickets are added."""
    cfg = settings.triage_eval
    digest = hashlib.sha256(f"{cfg.split_salt}:{ticket_id}".encode()).digest()
    fraction = int.from_bytes(digest[:8], "big") / 2**64
    return "test" if fraction < cfg.test_share else "dev"


def estimate_cad(candidates: Sequence[TicketCandidate], settings: Settings) -> float:
    lab = labeller_settings(settings)
    system = load_system_prompt(lab.triage.system_prompt_path)
    total = 0.0
    for c in candidates:
        chars = len(system) + len(render_user_message(c.subject, c.body))
        usage = Usage(int(chars / CHARS_PER_TOKEN_ESTIMATE), lab.triage.max_tokens)
        total += usd_to_cad(cost_usd(usage, lab.triage.model, lab.answer), lab.answer)
    return total


def label_silver(
    candidates: Sequence[TicketCandidate],
    settings: Settings,
    *,
    client: MessagesClient | None = None,
    cap_cad: float | None = None,
) -> tuple[list[SilverLabel], list[str]]:
    """Label each ticket (or replay its cached response). Stops before a call whose upper
    bound would pass the cap."""
    lab = labeller_settings(settings)
    system = load_system_prompt(lab.triage.system_prompt_path)
    cache = ResponseCache(lab.triage_cache_dir)
    out: list[SilverLabel] = []
    notes: list[str] = []
    spent = 0.0
    for c in candidates:
        key = request_key(system, render_user_message(c.subject, c.body), lab)
        over_cap = cap_cad is not None and spent + estimate_cad([c], settings) > cap_cad
        if over_cap and cache.get(key) is None:
            notes.append(f"stopped by the spend cap before {c.id}")
            break
        outcome = triage_ticket(c.subject, c.body, settings=lab, client=client)
        spent += outcome.cost_cad
        r = outcome.result
        out.append(
            SilverLabel(
                id=c.id,
                status=outcome.status,
                queue=r.queue if r else None,
                priority=r.priority if r else None,
                rationale=r.rationale if r else None,
                model=outcome.model,
                cost_cad=outcome.cost_cad,
            )
        )
    return out, notes


def load_jsonl(path: Path, model: type[BaseModel]) -> list[Any]:
    if not path.exists():
        return []
    return [
        model.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def write_jsonl(path: Path, rows: Sequence[BaseModel | Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        "".join(
            (r.model_dump_json() if isinstance(r, BaseModel) else json.dumps(r, ensure_ascii=False))
            + "\n"
            for r in rows
        ),
        encoding="utf-8",
    )
    tmp.replace(path)


@dataclass(frozen=True)
class Sample:
    ids: list[str]
    shortfalls: list[str]


def gold_sample(
    candidates: Sequence[TicketCandidate], silver: Sequence[SilverLabel], settings: Settings
) -> Sample:
    """Stratified by silver priority and split, so the test half and the urgent tickets are
    covered by design, not by luck. ``gold_sample[p]`` tickets per priority, divided between
    test and dev by ``test_share``."""
    cfg = settings.triage_eval
    rng = random.Random(cfg.sample_seed)
    by_id = {c.id: c for c in candidates}
    pools: dict[tuple[str, str], list[str]] = {}
    for s in sorted(silver, key=lambda s: s.id):
        if s.status == STATUS_OK and s.priority and s.id in by_id:
            pools.setdefault((s.priority, split_of(s.id, settings)), []).append(s.id)
    ids: list[str] = []
    shortfalls: list[str] = []
    for priority in PRIORITIES:
        want = cfg.gold_sample.get(priority, 0)
        want_test = round(want * cfg.test_share)
        for split, n in (("test", want_test), ("dev", want - want_test)):
            pool = pools.get((priority, split), [])
            if len(pool) < n:
                shortfalls.append(f"{priority}/{split}: wanted {n}, only {len(pool)} available")
            ids += rng.sample(pool, min(n, len(pool)))
    rng.shuffle(ids)
    return Sample(ids, shortfalls)


def build_set(
    candidates: Sequence[TicketCandidate],
    silver: Sequence[SilverLabel],
    gold: Sequence[GoldLabel],
    settings: Settings,
) -> list[dict[str, Any]]:
    """One item per ticket that has a usable label. The label is the gold label where the blind
    gold sample has one, otherwise the silver label; ``label_source`` says which and
    ``labelled_by`` who gave a gold label."""
    silver_by = {s.id: s for s in silver if s.status == STATUS_OK}
    gold_by = {g.id: g for g in gold}
    items = []
    for c in candidates:
        s, g = silver_by.get(c.id), gold_by.get(c.id)
        if s is None and g is None:
            continue
        queue = g.queue if g else s.queue  # type: ignore[union-attr]
        priority = g.priority if g else s.priority  # type: ignore[union-attr]
        assert queue in QUEUES and priority in PRIORITIES
        items.append(
            {
                "id": c.id,
                "subject": c.subject,
                "body": c.body,
                "style": c.style,
                "topic": c.topic,
                "situation": c.situation,
                "split": split_of(c.id, settings),
                "queue": queue,
                "priority": priority,
                "label_source": "gold" if g else "silver",
                "labelled_by": g.labelled_by if g else None,
                "silver_queue": s.queue if s else None,
                "silver_priority": s.priority if s else None,
                "gold_queue": g.queue if g else None,
                "gold_priority": g.priority if g else None,
            }
        )
    return items


def agreement(silver: Sequence[SilverLabel], gold: Sequence[GoldLabel]) -> dict[str, int]:
    """How often the silver labeller matches the person on the gold sample."""
    silver_by = {s.id: s for s in silver}
    pairs = [(silver_by[g.id], g) for g in gold if g.id in silver_by and silver_by[g.id].queue]
    return {
        "n": len(pairs),
        "queue": sum(s.queue == g.queue for s, g in pairs),
        "priority": sum(s.priority == g.priority for s, g in pairs),
        "both": sum(s.queue == g.queue and s.priority == g.priority for s, g in pairs),
    }


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    cfg = settings.triage_eval
    parser = argparse.ArgumentParser(prog="sda-eval-triage-label", description=__doc__)
    parser.add_argument("--yes", action="store_true", help="call Claude (costs money)")
    parser.add_argument("--cap", type=float, default=cfg.spend_cap_cad, help="spend cap, CAD")
    parser.add_argument("--build", action="store_true", help="merge labels into the triage set")
    args = parser.parse_args(argv)

    candidates = load_candidates(settings.triage_candidates_path)
    if not candidates:
        print(f"no candidates in {settings.triage_candidates_path}; run sda-eval-triage-draft")
        return 2
    if args.build:
        silver = load_jsonl(settings.triage_silver_path, SilverLabel)
        gold = load_jsonl(settings.triage_gold_path, GoldLabel)
        if not silver and not gold:
            print("no labels yet; run this command with --yes first")
            return 2
        items = build_set(candidates, silver, gold, settings)
        write_jsonl(settings.triage_set_path, items)
        agree = agreement(silver, gold)
        print(
            f"wrote {len(items)} items to {settings.triage_set_path} "
            f"({sum(i['label_source'] == 'gold' for i in items)} gold, "
            f"{sum(i['split'] == 'test' for i in items)} in the test split)"
        )
        if agree["n"]:
            print(
                f"silver vs gold on {agree['n']}: queue {agree['queue']}, "
                f"priority {agree['priority']}, both {agree['both']}"
            )
        return 0

    est = estimate_cad(candidates, settings)
    print(
        f"{len(candidates)} tickets, labeller {cfg.label_model}. "
        f"Upper bound CA${est:.4f} vs cap CA${args.cap:.2f}."
    )
    if est > args.cap:
        print("Over the cap; nothing sent. Raise it with --cap.")
        return 2
    if not args.yes:
        print("dry run: nothing sent; add --yes to label.")
        return 0
    silver, notes = label_silver(candidates, settings, cap_cad=args.cap)
    write_jsonl(settings.triage_silver_path, silver)
    failed = sum(s.status != STATUS_OK for s in silver)
    print(
        f"wrote {len(silver)} silver labels to {settings.triage_silver_path} "
        f"({failed} failed); cost CA${sum(s.cost_cad for s in silver):.4f}"
    )
    for note in notes:
        print(f"  {note}")
    return 1 if notes else 0


if __name__ == "__main__":
    raise SystemExit(main())
