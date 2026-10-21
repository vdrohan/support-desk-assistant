"""uv run sda-eval-triage [--yes] [--limit N] [--cap CAD] [--allow-dirty]

Score the triage classifier on the reviewed triage set: queue and priority accuracy with
Wilson intervals, urgent recall (the safety-critical row), under- and over-triage, per-queue
precision and recall, confusion matrices and a by-style table. Dry run by default; ``--yes``
calls Claude (capped). A full run from a clean tree writes to ``data/eval/results/``, anything
else to the gitignored ``data/eval/runs/`` (docs/decisions.md #28).
"""

import argparse
import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from support_desk_assistant.answer.cache import ResponseCache
from support_desk_assistant.answer.cost import Usage, cost_usd, usd_to_cad
from support_desk_assistant.answer.pipeline import CHARS_PER_TOKEN_ESTIMATE, load_system_prompt
from support_desk_assistant.config import Settings, get_settings
from support_desk_assistant.evals.provenance import (
    dirty_blocks_results,
    file_sha256,
    git_state,
    new_run_id,
)
from support_desk_assistant.evals.stats import rate_ci
from support_desk_assistant.triage.pipeline import (
    MessagesClient,
    TriageOutcome,
    render_user_message,
    request_key,
    triage_ticket,
)
from support_desk_assistant.triage.schema import PRIORITIES, QUEUES

RESULT_PREFIX = "triage"


@dataclass
class Row:
    id: str
    style: str
    subject: str
    body: str
    queue: str
    priority: str
    split: str = ""
    label_source: str = ""
    labelled_by: str = ""
    status: str = "pending"
    pred_queue: str | None = None
    pred_priority: str | None = None
    rationale: str | None = None
    cache_hit: bool = False
    cost_cad: float = 0.0
    cost_cad_list: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def queue_ok(self) -> bool:
        return self.pred_queue == self.queue

    @property
    def priority_ok(self) -> bool:
        return self.pred_priority == self.priority

    @property
    def level_gap(self) -> int | None:
        """Predicted minus true urgency, positive = over-triage (more urgent than true)."""
        if self.pred_priority is None:
            return None
        return PRIORITIES.index(self.priority) - PRIORITIES.index(self.pred_priority)


def load_items(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def summarize(rows: Sequence[Row], z: float) -> dict[str, Any]:
    """Every rate over all rows; a failed generation counts as wrong and is also counted."""
    n = len(rows)
    ok = [r for r in rows if r.status == "ok"]
    gaps = [r.level_gap for r in ok]
    true_urgent = [r for r in rows if r.priority == "urgent"]
    pred_urgent = [r for r in ok if r.pred_priority == "urgent"]
    queue_cm: dict[str, Counter[str]] = defaultdict(Counter)
    prio_cm: dict[str, Counter[str]] = defaultdict(Counter)
    for r in ok:
        queue_cm[r.queue][r.pred_queue or "-"] += 1
        prio_cm[r.priority][r.pred_priority or "-"] += 1
    per_queue = {}
    for q in QUEUES:
        tp = sum(1 for r in ok if r.queue == q and r.pred_queue == q)
        support = sum(1 for r in rows if r.queue == q)
        predicted = sum(1 for r in ok if r.pred_queue == q)
        per_queue[q] = {"recall": rate_ci(tp, support, z), "precision": rate_ci(tp, predicted, z)}
    by_style = {}
    for style in sorted({r.style for r in rows}):
        group = [r for r in rows if r.style == style]
        by_style[style] = {
            "n": len(group),
            "queue": rate_ci(sum(r.queue_ok for r in group), len(group), z),
            "priority": rate_ci(sum(r.priority_ok for r in group), len(group), z),
        }
    return {
        "n": n,
        "generation_failures": n - len(ok),
        "queue_accuracy": rate_ci(sum(r.queue_ok for r in rows), n, z),
        "priority_accuracy": rate_ci(sum(r.priority_ok for r in rows), n, z),
        "both_correct": rate_ci(sum(r.queue_ok and r.priority_ok for r in rows), n, z),
        "priority_within_one": rate_ci(sum(1 for g in gaps if g is not None and abs(g) <= 1), n, z),
        "urgent_recall": rate_ci(sum(r.priority_ok for r in true_urgent), len(true_urgent), z),
        "urgent_precision": rate_ci(
            sum(1 for r in pred_urgent if r.priority == "urgent"), len(pred_urgent), z
        ),
        "under_triage": rate_ci(sum(1 for g in gaps if g is not None and g < 0), n, z),
        "over_triage": rate_ci(sum(1 for g in gaps if g is not None and g > 0), n, z),
        "per_queue": per_queue,
        "by_style": by_style,
        "queue_confusion": {t: dict(c) for t, c in queue_cm.items()},
        "priority_confusion": {t: dict(c) for t, c in prio_cm.items()},
    }


def summarize_bundle(
    rows: Sequence[Row], train_items: Sequence[Mapping[str, Any]], settings: Settings
) -> dict[str, Any]:
    """The all-items summary, the gold-only summary (blind labels, by a person or agreed by
    model passes), its human-labelled part, and two baselines scored on the same rows: the
    dev majority label and a keyword rule."""
    z = settings.eval.ci_z
    gold = [r for r in rows if r.label_source == "gold"]
    human = [r for r in gold if r.labelled_by == "human"]
    train = [
        Row(id=i["id"], style="", subject="", body="", queue=i["queue"], priority=i["priority"])
        for i in train_items
    ]
    return {
        "all": summarize(rows, z),
        "gold": summarize(gold, z) if gold else None,
        "gold_human": summarize(human, z) if human else None,
        "baselines": {
            "majority (dev labels)": summarize(majority_baseline(rows, train), z)
            if train
            else None,
            "keywords": summarize(keyword_baseline(rows, settings), z),
        },
    }


def _ci(m: Mapping[str, Any]) -> str:
    if m["rate"] is None:
        return "n/a (n=0)"
    return f"{m['rate']:.2f} [{m['ci95'][0]:.2f}, {m['ci95'][1]:.2f}] ({m['k']}/{m['n']})"


def _matrix(title: str, labels: Sequence[str], cm: Mapping[str, Mapping[str, int]]) -> list[str]:
    cols = [*labels, "-"] if any("-" in c for c in cm.values()) else list(labels)
    lines = [f"**{title}** (rows true, columns predicted)", ""]
    lines += ["| true \\ pred | " + " | ".join(cols) + " |", "|---|" + "---|" * len(cols)]
    for t in labels:
        row = cm.get(t, {})
        lines.append(f"| {t} | " + " | ".join(str(row.get(c, 0)) for c in cols) + " |")
    return [*lines, ""]


def _labelled_by_line(rows: Sequence[Row]) -> str:
    """Who gave the gold labels: a person, agreeing model passes, or anything else recorded."""
    counts: Counter[str] = Counter()
    for r in rows:
        if r.label_source != "gold":
            continue
        if r.labelled_by == "human":
            counts["human"] += 1
        elif r.labelled_by.startswith("model-agreed"):
            counts["model-agreed"] += 1
        else:
            counts[r.labelled_by or "unknown"] += 1
    other = sorted(k for k in counts if k not in ("human", "model-agreed"))
    parts = [f"{k} {counts[k]}" for k in ["human", "model-agreed", *other]]
    line = f"Labels by source: {', '.join(parts)}."
    if counts["model-agreed"]:
        line += " Model-agreed labels were not reviewed by a person."
    return line


def _gold_and_baselines(bundle: Mapping[str, Any], rows: Sequence[Row]) -> list[str]:
    keys = [
        ("queue", "queue_accuracy"),
        ("priority", "priority_accuracy"),
        ("urgent recall", "urgent_recall"),
        ("within one level", "priority_within_one"),
    ]
    out: list[str] = []
    gold = bundle.get("gold")
    if gold:
        out += [f"## Gold subset (blind labels, n={gold['n']})", "", _labelled_by_line(rows), ""]
        out += ["| metric | rate [95% CI] (k/n) |", "|---|---|"]
        out += [f"| {name} | {_ci(gold[key])} |" for name, key in keys]
        out.append("")
    human = bundle.get("gold_human")
    if human and human["n"] >= 1:
        out += [f"**Human-labelled subset (n={human['n']})**", ""]
        out += ["| metric | rate [95% CI] (k/n) |", "|---|---|"]
        out += [f"| {name} | {_ci(human[key])} |" for name, key in keys]
        out.append("")
    out += ["## Baselines (same rows)", "", "| baseline | " + " | ".join(n for n, _ in keys) + " |"]
    out.append("|---|" + "---|" * len(keys))
    for name, b in bundle["baselines"].items():
        if b:
            out.append(f"| {name} | " + " | ".join(_ci(b[key]) for _, key in keys) + " |")
    return [*out, ""]


def render_markdown(meta: Mapping[str, Any], bundle: Mapping[str, Any], rows: Sequence[Row]) -> str:
    s = bundle["all"]
    out = [
        f"# Triage eval: {meta['dataset']['name']}, {meta['model']}",
        "",
        f"run {meta['run_id']} · split {meta['split']} · git {meta['git']['sha']}"
        f"{' (dirty)' if meta['git']['dirty'] else ''} · prompt {meta['prompt_sha256'][:12]}",
        "",
        "## Overall",
        "",
        "| metric | rate [95% CI] (k/n) |",
        "|---|---|",
    ]
    for label, key in [
        ("queue accuracy", "queue_accuracy"),
        ("priority accuracy", "priority_accuracy"),
        ("queue and priority both correct", "both_correct"),
        ("priority within one level", "priority_within_one"),
        ("urgent recall (true urgent found)", "urgent_recall"),
        ("urgent precision", "urgent_precision"),
        ("under-triage (predicted less urgent)", "under_triage"),
        ("over-triage (predicted more urgent)", "over_triage"),
    ]:
        out.append(f"| {label} | {_ci(s[key])} |")
    out += ["", f"Generation failures (counted as wrong): {s['generation_failures']}", ""]
    out += _gold_and_baselines(bundle, rows)
    out += ["## Per queue", "", "| queue | recall | precision |", "|---|---|---|"]
    out += [
        f"| {q} | {_ci(v['recall'])} | {_ci(v['precision'])} |" for q, v in s["per_queue"].items()
    ]
    out += ["", "## By style", "", "| style | n | queue | priority |", "|---|---|---|---|"]
    out += [
        f"| {k} | {v['n']} | {_ci(v['queue'])} | {_ci(v['priority'])} |"
        for k, v in s["by_style"].items()
    ]
    out += ["", "## Confusion", ""]
    out += _matrix("Queue", QUEUES, s["queue_confusion"])
    out += _matrix("Priority", PRIORITIES, s["priority_confusion"])
    wrong = [r for r in rows if not (r.queue_ok and r.priority_ok)]
    out += [f"## Misses ({len(wrong)})", ""]
    for r in wrong:
        pred = f"{r.pred_queue}/{r.pred_priority}" if r.status == "ok" else r.status
        out.append(f"- **{r.id}** ({r.style}) true {r.queue}/{r.priority}, predicted {pred}")
        text = r.subject + (": " if r.subject else "") + r.body
        out.append(f"  - ticket: {text[:300]}")
        if r.rationale:
            out.append(f"  - rationale: {r.rationale}")
    out += [
        "",
        "## Cost",
        "",
        f"Spent CA${meta['cost_cad']:.4f}; list cost CA${meta['cost_cad_list']:.4f} "
        f"(as if nothing was cached). Cache hits {meta['cache_hits']} of {s['n']}.",
        "",
    ]
    return "\n".join(out)


def keyword_baseline(rows: Sequence[Row], settings: Settings) -> list[Row]:
    """A rule with no model: queue by program keywords (else other), urgent if an urgent word
    appears (else normal)."""
    cfg = settings.triage_eval
    out = []
    for r in rows:
        text = f"{r.subject} {r.body}".lower() + " "
        scores = {q: sum(w in text for w in words) for q, words in cfg.baseline_queue_words.items()}
        best = max(scores, key=lambda q: scores[q])
        urgent = any(w in text for w in cfg.baseline_urgent_words)
        out.append(
            Row(
                **{
                    **r.__dict__,
                    "status": "ok",
                    "rationale": None,
                    "pred_queue": best if scores[best] else "other",
                    "pred_priority": "urgent" if urgent else "normal",
                }
            )
        )
    return out


def majority_baseline(rows: Sequence[Row], train: Sequence[Row]) -> list[Row]:
    """Always predict the most common queue and priority of ``train`` (the dev labels)."""
    queue = Counter(r.queue for r in train).most_common(1)[0][0]
    priority = Counter(r.priority for r in train).most_common(1)[0][0]
    return [
        Row(
            **{
                **r.__dict__,
                "status": "ok",
                "rationale": None,
                "pred_queue": queue,
                "pred_priority": priority,
            }
        )
        for r in rows
    ]


def estimate_cad(items: Sequence[Mapping[str, Any]], system: str, settings: Settings) -> float:
    cfg = settings.triage
    usage = Usage(output_tokens=cfg.max_tokens)
    total = 0.0
    for it in items:
        chars = len(system) + len(render_user_message(it["subject"], it["body"]))
        u = Usage(
            input_tokens=int(chars / CHARS_PER_TOKEN_ESTIMATE), output_tokens=usage.output_tokens
        )
        total += usd_to_cad(cost_usd(u, cfg.model, settings.answer), settings.answer)
    return total


def score_item(
    it: Mapping[str, Any], settings: Settings, client: MessagesClient | None
) -> tuple[Row, TriageOutcome]:
    outcome = triage_ticket(it["subject"], it["body"], settings=settings, client=client)
    r = outcome.result
    row = Row(
        id=it["id"],
        style=it.get("style", ""),
        split=it.get("split", ""),
        label_source=it.get("label_source", ""),
        labelled_by=it.get("labelled_by") or "",
        subject=it["subject"],
        body=it["body"],
        queue=it["queue"],
        priority=it["priority"],
        status=outcome.status,
        pred_queue=r.queue if r else None,
        pred_priority=r.priority if r else None,
        rationale=r.rationale if r else None,
        cache_hit=outcome.cache_hit,
        cost_cad=outcome.cost_cad,
    )
    return row, outcome


def list_cost_cad(outcome: TriageOutcome, settings: Settings, key_raw: Mapping[str, Any]) -> float:
    usage = key_raw.get("usage") or {}
    u = Usage(usage.get("input_tokens") or 0, usage.get("output_tokens") or 0)
    return usd_to_cad(cost_usd(u, settings.triage.model, settings.answer), settings.answer)


def run_items(
    items: Sequence[Mapping[str, Any]],
    *,
    settings: Settings,
    client: MessagesClient | None = None,
    cap_cad: float | None = None,
) -> tuple[list[Row], list[str]]:
    """Score each item (or replay its cached response). Stops before an item whose upper
    bound would pass the cap; returns the rows done and any stop notice."""
    system = load_system_prompt(settings.triage.system_prompt_path)
    cache = ResponseCache(settings.triage_cache_dir)
    rows: list[Row] = []
    notes: list[str] = []
    spent = 0.0
    for it in items:
        key = request_key(system, render_user_message(it["subject"], it["body"]), settings)
        if cap_cad is not None and cache.get(key) is None:
            bound = estimate_cad([it], system, settings)
            if spent + bound > cap_cad:
                notes.append(f"stopped by the spend cap before {it['id']}")
                break
        row, outcome = score_item(it, settings, client)
        spent += outcome.cost_cad
        raw = cache.get(key) or {}
        row.cost_cad_list = list_cost_cad(outcome, settings, raw)
        rows.append(row)
    return rows, notes


def build_meta(
    settings: Settings, dataset: Path, rows: Sequence[Row], run_id: str, split: str = "all"
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "split": split,
        "created_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "git": git_state(),
        "dataset": {"name": dataset.name, "sha256": file_sha256(dataset)},
        "model": settings.triage.model,
        "prompt_sha256": file_sha256(settings.triage.system_prompt_path),
        "schema_version": settings.triage.schema_version,
        "max_tokens": settings.triage.max_tokens,
        "cost_cad": round(sum(r.cost_cad for r in rows), 6),
        "cost_cad_list": round(sum(r.cost_cad_list for r in rows), 6),
        "cache_hits": sum(r.cache_hit for r in rows),
    }


def write_results(
    directory: Path, meta: Mapping[str, Any], summary: Mapping[str, Any], rows: Sequence[Row]
) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    dataset_name = meta["dataset"]["name"].removesuffix(".jsonl")
    stem = f"{RESULT_PREFIX}_{dataset_name}_{meta['model']}_{meta['split']}_{meta['run_id']}"
    jsonl = directory / f"{stem}.jsonl"
    jsonl.write_text(
        "".join(
            json.dumps(
                {k: v for k, v in r.__dict__.items() if k != "extra"}
                | {"queue_ok": r.queue_ok, "priority_ok": r.priority_ok},
                ensure_ascii=False,
            )
            + "\n"
            for r in rows
        ),
        encoding="utf-8",
    )
    summary_path = directory / f"{stem}.summary.json"
    summary_path.write_text(
        json.dumps({"meta": meta, "metrics": summary}, indent=1), encoding="utf-8"
    )
    md = directory / f"{stem}.md"
    md.write_text(render_markdown(meta, summary, rows), encoding="utf-8")
    return [jsonl, summary_path, md]


def test_already_scored(settings: Settings, dataset: Path) -> list[Path]:
    """Committed results of a test-split run on this exact dataset version."""
    sha = file_sha256(dataset)
    found = []
    for path in sorted(settings.eval_results_dir.glob(f"{RESULT_PREFIX}_*.summary.json")):
        meta = json.loads(path.read_text(encoding="utf-8")).get("meta", {})
        if meta.get("split") == "test" and meta.get("dataset", {}).get("sha256") == sha:
            found.append(path)
    return found


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(prog="sda-eval-triage", description=__doc__)
    parser.add_argument("--split", choices=["dev", "test", "all"], default="dev")
    parser.add_argument("--yes", action="store_true", help="call Claude (costs money)")
    parser.add_argument("--limit", type=int, help="only the first N items (partial run)")
    parser.add_argument("--cap", type=float, default=settings.triage_eval.spend_cap_cad)
    parser.add_argument(
        "--allow-dirty", action="store_true", help="write results/ from a dirty tree"
    )
    parser.add_argument(
        "--rerun-test", action="store_true", help="score the test split again (it is meant once)"
    )
    args = parser.parse_args(argv)

    dataset = settings.triage_set_path
    if not dataset.exists():
        print(f"no triage set at {dataset}; build it with sda-eval-triage-label --build")
        return 2
    all_items = load_items(dataset)
    items = [i for i in all_items if args.split == "all" or i["split"] == args.split]
    train_items = [i for i in all_items if i["split"] == "dev"]
    partial = args.limit is not None
    if partial:
        items = items[: args.limit]
    if args.split == "test" and args.yes and not args.rerun_test:
        done = test_already_scored(settings, dataset)
        if done:
            print(f"the test split was already scored for this dataset ({done[0].name}).")
            print("It is meant to be scored once. Use --rerun-test to do it again.")
            return 2
    system = load_system_prompt(settings.triage.system_prompt_path)
    est = estimate_cad(items, system, settings)
    print(
        f"Triage eval plan: {dataset.name}, split {args.split}, {len(items)} items, "
        f"model {settings.triage.model}. Upper bound CA${est:.4f} vs cap CA${args.cap:.2f}."
    )
    if est > args.cap:
        print("Over the cap; nothing sent.")
        return 2
    if not args.yes:
        print("dry run: nothing sent; add --yes to run.")
        return 0

    rows, notes = run_items(items, settings=settings, cap_cad=args.cap)
    meta = build_meta(settings, dataset, rows, new_run_id(), args.split)
    bundle = summarize_bundle(rows, train_items, settings)
    stopped = bool(notes)
    to_runs = partial or stopped or (dirty_blocks_results(meta["git"]) and not args.allow_dirty)
    directory = settings.runs_dir if to_runs else settings.eval_results_dir
    paths = write_results(directory, meta, bundle, rows)
    s = bundle["all"]
    print(
        f"queue {_ci(s['queue_accuracy'])}; priority {_ci(s['priority_accuracy'])}; "
        f"urgent recall {_ci(s['urgent_recall'])}"
    )
    print(f"cost CA${meta['cost_cad']:.4f}. wrote {', '.join(str(p) for p in paths)}")
    if to_runs:
        why = "partial run" if partial or stopped else "dirty working tree"
        print(f"Written to {directory}, not {settings.eval_results_dir}: {why}.")
    for note in notes:
        print(f"  {note}")
    return 1 if stopped else 0


if __name__ == "__main__":
    raise SystemExit(main())
