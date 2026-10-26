"""uv run sda-eval-escalation [--split dev|test|all] [--yes] [--limit N] [--cap CAD] [--allow-dirty]

Score the escalation policy (docs/decisions.md #29) on the triage set: how many tickets go to a
person, how many of the classifier's errors that catches, what is left among the tickets nobody
reviews, and whether any truly urgent ticket slips through. Every figure is set beside sending the
same share of tickets at random, and the pre-registered targets are checked as written. The
classifier's responses are replayed from the cache when a triage eval already ran them; an
uncached item costs a Haiku call. Dry run by default; ``--yes`` calls Claude (capped).

Dev runs go to the gitignored ``data/eval/runs/`` (the policy is tuned on them); a full test run
from a clean tree goes to ``data/eval/results/`` and is meant once per dataset version.
"""

import argparse
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from support_desk_assistant.answer.pipeline import load_system_prompt
from support_desk_assistant.config import (
    EscalationEvalSettings,
    EscalationSettings,
    Settings,
    get_settings,
)
from support_desk_assistant.evals.provenance import dirty_blocks_results, new_run_id
from support_desk_assistant.evals.stats import rate_ci
from support_desk_assistant.evals.triage import (
    Row,
    build_meta,
    estimate_cad,
    load_items,
    run_items,
    summarize,
    test_already_scored,
)
from support_desk_assistant.triage.escalate import Escalation, escalate, second_opinion_settings
from support_desk_assistant.triage.schema import CONFIDENCES, TriageResult

RESULT_PREFIX = "escalation"


def result_of(row: Row) -> TriageResult | None:
    """The classifier's result for a scored row; None if the triage failed."""
    if row.status != "ok" or not (
        row.pred_queue
        and row.pred_priority
        and row.pred_queue_confidence
        and row.pred_priority_confidence
    ):
        return None
    return TriageResult.model_validate(
        {
            "rationale": row.rationale or "",
            "queue": row.pred_queue,
            "priority": row.pred_priority,
            "queue_confidence": row.pred_queue_confidence,
            "priority_confidence": row.pred_priority_confidence,
        }
    )


def is_error(row: Row) -> bool:
    """Queue or priority wrong; a failed triage counts as wrong."""
    return not (row.queue_ok and row.priority_ok)


def decide(rows: Sequence[Row], cfg: EscalationSettings) -> list[Escalation]:
    return [escalate(result_of(r), cfg, r.second_agrees) for r in rows]


def policy_metrics(rows: Sequence[Row], cfg: EscalationSettings, z: float) -> dict[str, Any]:
    """One policy on one set of rows. Rates over all rows; intervals are Wilson."""
    decisions = decide(rows, cfg)
    n = len(rows)
    flagged = [d.needs_review for d in decisions]
    errors = [is_error(r) for r in rows]
    n_flagged, n_errors = sum(flagged), sum(errors)
    caught = sum(f and e for f, e in zip(flagged, errors, strict=True))
    urgent = [r.priority == "urgent" for r in rows]
    # A truly urgent ticket is reached if it was labelled urgent (it tops the queue anyway) or
    # flagged for review; only the rest are unreached (the target's wording, decision 29).
    reached = [f or r.pred_priority == "urgent" for f, r in zip(flagged, rows, strict=True)]
    under = [r.level_gap is not None and r.level_gap < 0 for r in rows]
    ambiguous = [r.ambiguous for r in rows]
    caught_unsure = sum(a and f and e for a, f, e in zip(ambiguous, flagged, errors, strict=True))
    errors_unsure = sum(a and e for a, e in zip(ambiguous, errors, strict=True))
    n_unsure = sum(ambiguous)
    return {
        "n": n,
        "escalated": rate_ci(n_flagged, n, z),
        "error_rate": rate_ci(n_errors, n, z),
        "error_capture": rate_ci(caught, n_errors, z),
        "escalation_precision": rate_ci(caught, n_flagged, z),
        "residual_error": rate_ci(n_errors - caught, n - n_flagged, z),
        "urgent_unreached": rate_ci(
            sum(u and not ok for u, ok in zip(urgent, reached, strict=True)), sum(urgent), z
        ),
        "under_triage_capture": rate_ci(
            sum(u and f for u, f in zip(under, flagged, strict=True)), sum(under), z
        ),
        # Errors split by whether the labeller was unsure of the ticket: most errors are expected
        # on the tickets a reader hesitated over.
        "error_rate_unsure": rate_ci(errors_unsure, n_unsure, z),
        "error_rate_clear": rate_ci(n_errors - errors_unsure, n - n_unsure, z),
        "error_capture_unsure": rate_ci(caught_unsure, errors_unsure, z),
        "error_capture_clear": rate_ci(caught - caught_unsure, n_errors - errors_unsure, z),
        "ambiguous_capture": rate_ci(
            sum(a and f for a, f in zip(ambiguous, flagged, strict=True)), sum(ambiguous), z
        ),
        # Sending the same share of tickets at random catches that share of the errors.
        "random_capture": round(n_flagged / n, 3) if n else None,
        "reasons": dict(Counter(reason for d in decisions for reason in d.reasons)),
    }


def check_targets(m: Mapping[str, Any], targets: EscalationEvalSettings) -> list[dict[str, Any]]:
    """The pre-registered targets, as written. A target with nothing to measure is not met."""
    rate = m["escalated"]["rate"]
    capture = m["error_capture"]
    unreached = m["urgent_unreached"]

    def row(name: str, value: float | None, met: bool) -> dict[str, Any]:
        return {"target": name, "value": value, "met": met}

    cap_rate, cap_low = capture["rate"], (capture["ci95"] or [None])[0]
    return [
        row(
            f"escalated at most {targets.max_escalation_rate:.2f}",
            rate,
            rate is not None and rate <= targets.max_escalation_rate,
        ),
        row(
            f"error capture at least {targets.min_error_capture:.2f}",
            cap_rate,
            cap_rate is not None and cap_rate >= targets.min_error_capture,
        ),
        row(
            "error capture interval above the escalation rate (beats random)",
            cap_low,
            cap_low is not None and rate is not None and cap_low > rate,
        ),
        row(
            f"urgent unreached at most {targets.max_urgent_unreached:.2f}",
            unreached["rate"],
            unreached["rate"] is not None and unreached["rate"] <= targets.max_urgent_unreached,
        ),
    ]


def sweep(rows: Sequence[Row], base: EscalationSettings, z: float) -> list[dict[str, Any]]:
    """Every pair of confidence floors, with the rules off and with them as configured, and with
    the second-opinion rule off and (if a second pass was run) on."""
    has_second = any(r.second_status is not None for r in rows)
    out = []
    for disagree in (False, True) if has_second else (False,):
        for rules in (False, True):
            for queue_floor in CONFIDENCES:
                for priority_floor in CONFIDENCES:
                    cfg = base.model_copy(
                        update={
                            "on_other_queue": rules and base.on_other_queue,
                            "on_urgent": rules and base.on_urgent,
                            "min_queue_confidence": queue_floor,
                            "min_priority_confidence": priority_floor,
                            "on_disagreement": disagree,
                        }
                    )
                    out.append(
                        {
                            "rules": rules,
                            "disagree": disagree,
                            "queue_floor": queue_floor,
                            "priority_floor": priority_floor,
                            **policy_metrics(rows, cfg, z),
                        }
                    )
    return out


def compared_policies(
    base: EscalationSettings, with_second: bool = False
) -> dict[str, EscalationSettings]:
    """Named policies scored side by side: each signal alone, then the configured one."""
    off = {"min_queue_confidence": "low", "min_priority_confidence": "low"}
    none = {"on_other_queue": False, "on_urgent": False}
    out = {
        "rules only (other queue, urgent)": base.model_copy(
            update={**off, "on_other_queue": True, "on_urgent": True, "on_disagreement": False}
        ),
        "confidence only (configured floors)": base.model_copy(
            update={**none, "on_disagreement": False}
        ),
    }
    if with_second:
        out["second opinion only"] = base.model_copy(
            update={**off, **none, "on_disagreement": True}
        )
    out["configured policy"] = base
    return out


def summarize_bundle(rows: Sequence[Row], settings: Settings) -> dict[str, Any]:
    z = settings.eval.ci_z
    cfg = settings.escalation
    gold = [r for r in rows if r.label_source == "gold"]
    has_second = any(r.second_status is not None for r in rows)
    return {
        "classifier": summarize(rows, z),
        "second_opinion": has_second,
        "policies": {
            name: policy_metrics(rows, policy, z)
            for name, policy in compared_policies(cfg, has_second).items()
        },
        "configured": policy_metrics(rows, cfg, z),
        "targets": check_targets(policy_metrics(rows, cfg, z), settings.escalation_eval),
        "gold": policy_metrics(gold, cfg, z) if gold else None,
        "sweep": sweep(rows, cfg, z),
        "confidence_mix": {
            "queue": dict(Counter(r.pred_queue_confidence or "-" for r in rows)),
            "priority": dict(Counter(r.pred_priority_confidence or "-" for r in rows)),
        },
    }


# --- report ----------------------------------------------------------------------


def fmt(m: Mapping[str, Any]) -> str:
    if m["rate"] is None:
        return "n/a (n=0)"
    return f"{m['rate']:.2f} [{m['ci95'][0]:.2f}, {m['ci95'][1]:.2f}] ({m['k']}/{m['n']})"


def _short(m: Mapping[str, Any]) -> str:
    return "n/a" if m["rate"] is None else f"{m['rate']:.2f} ({m['k']}/{m['n']})"


POLICY_COLUMNS = [
    ("escalated", "escalated"),
    ("error capture", "error_capture"),
    ("precision", "escalation_precision"),
    ("residual error", "residual_error"),
    ("urgent unreached", "urgent_unreached"),
    ("under-triage caught", "under_triage_capture"),
]


def _policy_table(entries: Sequence[tuple[str, Mapping[str, Any]]], first: str) -> list[str]:
    head = f"| {first} | " + " | ".join(name for name, _ in POLICY_COLUMNS) + " |"
    lines = [head, "|---|" + "---|" * len(POLICY_COLUMNS)]
    for label, m in entries:
        lines.append(
            f"| {label} | " + " | ".join(_short(m[key]) for _, key in POLICY_COLUMNS) + " |"
        )
    return lines


def render_markdown(meta: Mapping[str, Any], bundle: Mapping[str, Any], rows: Sequence[Row]) -> str:
    cfg = meta["escalation"]
    conf = bundle["configured"]
    cls = bundle["classifier"]
    out = [
        f"# Escalation eval: {meta['dataset']['name']}, {meta['model']}",
        "",
        f"run {meta['run_id']} · split {meta['split']} · git {meta['git']['sha']}"
        f"{' (dirty)' if meta['git']['dirty'] else ''} · prompt {meta['prompt_sha256'][:12]}",
        "",
        f"Policy: other queue {'on' if cfg['on_other_queue'] else 'off'}, urgent "
        f"{'on' if cfg['on_urgent'] else 'off'}, confidence floors queue "
        f"{cfg['min_queue_confidence']}, priority {cfg['min_priority_confidence']} "
        "(a label below its floor is escalated), second-opinion rule "
        f"{'on' if cfg['on_disagreement'] else 'off'}"
        f"{' (a second pass was run)' if bundle['second_opinion'] else ''}.",
        "",
        "## Classifier on these rows",
        "",
        f"- queue accuracy {fmt(cls['queue_accuracy'])}",
        f"- priority accuracy {fmt(cls['priority_accuracy'])}",
        f"- queue and priority both correct {fmt(cls['both_correct'])}",
        f"- urgent recall {fmt(cls['urgent_recall'])}; generation failures "
        f"{cls['generation_failures']}",
        "",
        "## Targets (fixed before the test run)",
        "",
        "| target | value | met |",
        "|---|---|---|",
    ]
    for t in bundle["targets"]:
        value = "n/a" if t["value"] is None else f"{t['value']:.2f}"
        out.append(f"| {t['target']} | {value} | {'yes' if t['met'] else '**no**'} |")
    out += [
        "",
        "## Configured policy",
        "",
        "| metric | rate [95% CI] (k/n) |",
        "|---|---|",
        f"| tickets escalated | {fmt(conf['escalated'])} |",
        f"| classifier error rate | {fmt(conf['error_rate'])} |",
        f"| errors caught (error capture) | {fmt(conf['error_capture'])} |",
        f"| random escalation at the same rate would catch | {conf['random_capture']} |",
        f"| escalated tickets that were errors (precision) | {fmt(conf['escalation_precision'])} |",
        f"| errors among tickets nobody reviews (residual) | {fmt(conf['residual_error'])} |",
        f"| truly urgent tickets not escalated | {fmt(conf['urgent_unreached'])} |",
        f"| under-triaged tickets escalated | {fmt(conf['under_triage_capture'])} |",
        f"| ambiguous tickets escalated (labeller unsure, or silver and gold differ) | "
        f"{fmt(conf['ambiguous_capture'])} |",
        f"| classifier error rate on ambiguous tickets / on the others | "
        f"{_short(conf['error_rate_unsure'])} / {_short(conf['error_rate_clear'])} |",
        f"| errors caught on ambiguous tickets / on the others | "
        f"{_short(conf['error_capture_unsure'])} / {_short(conf['error_capture_clear'])} |",
        "",
        f"Reasons (a ticket can have several): {conf['reasons'] or 'none'}",
        "",
        "## Signals side by side",
        "",
        *_policy_table(list(bundle["policies"].items()), "policy"),
        "",
    ]
    if bundle["gold"]:
        g = bundle["gold"]
        out += [
            f"## Gold subset (blind labels, n={g['n']})",
            "",
            *_policy_table([("configured policy", g)], "policy"),
            "",
        ]
    out += [
        "## Confidence floors (rules off, then rules as configured)",
        "",
        f"Confidence mix: queue {bundle['confidence_mix']['queue']}, "
        f"priority {bundle['confidence_mix']['priority']}.",
        "",
    ]
    table = [
        (
            f"rules {'on' if s['rules'] else 'off'}, queue>={s['queue_floor']}, "
            f"priority>={s['priority_floor']}{', +disagree' if s['disagree'] else ''}",
            s,
        )
        for s in bundle["sweep"]
    ]
    out += [*_policy_table(table, "policy"), ""]
    missed = [r for r in rows if is_error(r) and not escalate(result_of(r), _cfg(cfg)).needs_review]
    out += [f"## Errors nobody would review ({len(missed)})", ""]
    for r in missed:
        pred = f"{r.pred_queue}/{r.pred_priority}" if r.status == "ok" else r.status
        out.append(
            f"- **{r.id}** ({r.style}) true {r.queue}/{r.priority}, predicted {pred}, "
            f"confidence {r.pred_queue_confidence}/{r.pred_priority_confidence}"
        )
        out.append(f"  - ticket: {(r.subject + ': ' if r.subject else '') + r.body}"[:340])
    out += [
        "",
        "## Cost",
        "",
        f"Spent CA${meta['cost_cad']:.4f}; list cost CA${meta['cost_cad_list']:.4f} "
        f"(as if nothing was cached). Cache hits {meta['cache_hits']} of {cls['n']}.",
        "",
    ]
    return "\n".join(out)


def _cfg(raw: Mapping[str, Any]) -> EscalationSettings:
    return EscalationSettings.model_validate(dict(raw))


def write_results(
    directory: Path, meta: Mapping[str, Any], bundle: Mapping[str, Any], rows: Sequence[Row]
) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    dataset_name = meta["dataset"]["name"].removesuffix(".jsonl")
    stem = f"{RESULT_PREFIX}_{dataset_name}_{meta['model']}_{meta['split']}_{meta['run_id']}"
    cfg = _cfg(meta["escalation"])
    jsonl = directory / f"{stem}.jsonl"
    lines = []
    for r, d in zip(rows, decide(rows, cfg), strict=True):
        record = {k: v for k, v in r.__dict__.items() if k != "extra"}
        record |= {
            "queue_ok": r.queue_ok,
            "priority_ok": r.priority_ok,
            "escalated": d.needs_review,
            "reasons": list(d.reasons),
        }
        lines.append(json.dumps(record, ensure_ascii=False) + "\n")
    jsonl.write_text("".join(lines), encoding="utf-8")
    summary = directory / f"{stem}.summary.json"
    summary.write_text(json.dumps({"meta": meta, "metrics": bundle}, indent=1), encoding="utf-8")
    md = directory / f"{stem}.md"
    md.write_text(render_markdown(meta, bundle, rows), encoding="utf-8")
    return [jsonl, summary, md]


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(prog="sda-eval-escalation", description=__doc__)
    parser.add_argument("--split", choices=["dev", "test", "all"], default="dev")
    parser.add_argument("--yes", action="store_true", help="call Claude for uncached items")
    parser.add_argument("--limit", type=int, help="only the first N items (partial run)")
    parser.add_argument("--cap", type=float, default=settings.triage_eval.spend_cap_cad)
    parser.add_argument(
        "--allow-dirty", action="store_true", help="write results/ from a dirty tree"
    )
    parser.add_argument(
        "--rerun-test", action="store_true", help="score the test split again (it is meant once)"
    )
    parser.add_argument(
        "--second-opinion",
        action="store_true",
        help="also run the second pass (same model, thinking on): a second call per ticket",
    )
    args = parser.parse_args(argv)

    dataset = settings.triage_set_path
    if not dataset.exists():
        print(f"no triage set at {dataset}; build it with sda-eval-triage-label --build")
        return 2
    items = [i for i in load_items(dataset) if args.split == "all" or i["split"] == args.split]
    partial = args.limit is not None
    if partial:
        items = items[: args.limit]
    if args.split == "test" and args.yes and not args.rerun_test:
        done = test_already_scored(settings, dataset, RESULT_PREFIX)
        if done:
            print(f"the test split was already scored for this dataset ({done[0].name}).")
            print("It is meant to be scored once. Use --rerun-test to do it again.")
            return 2
    if settings.escalation.on_disagreement and not args.second_opinion:
        print("the configured policy uses the second opinion: add --second-opinion")
        return 2
    second = second_opinion_settings(settings)
    system = load_system_prompt(settings.triage.system_prompt_path)
    est = estimate_cad(items, system, settings)
    if args.second_opinion:
        est += estimate_cad(items, system, second)
    print(
        f"Escalation eval plan: {dataset.name}, split {args.split}, {len(items)} items, "
        f"model {settings.triage.model}. Upper bound CA${est:.4f} (cached items are free) "
        f"vs cap CA${args.cap:.2f}."
    )
    if est > args.cap:
        print("Over the cap; nothing sent.")
        return 2
    if not args.yes:
        print("dry run: nothing sent; add --yes to run.")
        return 0

    rows, notes = run_items(items, settings=settings, cap_cad=args.cap)
    meta = build_meta(settings, dataset, rows, new_run_id(), args.split)
    if args.second_opinion:
        second_rows, second_notes = run_items(items, settings=second, cap_cad=args.cap)
        notes += second_notes
        by_id = {s.id: s for s in second_rows}
        for r in rows:
            if (s := by_id.get(r.id)) is not None:
                r.second_status, r.second_queue, r.second_priority = (
                    s.status,
                    s.pred_queue,
                    s.pred_priority,
                )
        extra = sum(s.cost_cad for s in second_rows)
        meta["cost_cad"] = round(meta["cost_cad"] + extra, 6)
        meta["cost_cad_list"] = round(
            meta["cost_cad_list"] + sum(s.cost_cad_list for s in second_rows), 6
        )
        meta["second_opinion"] = {
            "model": second.triage.model,
            "thinking": second.triage.thinking,
            "effort": second.triage.effort,
            "max_tokens": second.triage.max_tokens,
            "cost_cad": round(extra, 6),
        }
    meta["escalation"] = settings.escalation.model_dump()
    meta["targets"] = settings.escalation_eval.model_dump()
    bundle = summarize_bundle(rows, settings)
    stopped = bool(notes)
    to_runs = (
        partial
        or stopped
        or args.split == "dev"
        or (dirty_blocks_results(meta["git"]) and not args.allow_dirty)
    )
    directory = settings.runs_dir if to_runs else settings.eval_results_dir
    paths = write_results(directory, meta, bundle, rows)
    c = bundle["configured"]
    print(
        f"escalated {fmt(c['escalated'])}; error capture {fmt(c['error_capture'])}; "
        f"urgent unreached {fmt(c['urgent_unreached'])}"
    )
    for t in bundle["targets"]:
        print(
            f"  target {'met    ' if t['met'] else 'NOT met'}: {t['target']} (value {t['value']})"
        )
    print(f"cost CA${meta['cost_cad']:.4f}. wrote {', '.join(str(p) for p in paths)}")
    if to_runs:
        why = (
            "partial run"
            if partial or stopped
            else "dev split (tuning runs stay local)"
            if args.split == "dev"
            else "dirty working tree"
        )
        print(f"Written to {directory}, not {settings.eval_results_dir}: {why}.")
    for note in notes:
        print(f"  {note}")
    return 1 if stopped else 0


if __name__ == "__main__":
    raise SystemExit(main())
