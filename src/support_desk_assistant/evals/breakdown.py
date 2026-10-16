"""Answer-eval breakdowns: overall, per program, abstention, citation, failure attribution.

Works on the per-question result rows of ``sda-eval-answers`` (``ItemRun.row()`` plus
``gold_urls``), so it never imports ``answers.py`` (which imports this module). Every
rate carries k, n and a Wilson interval (``stats.rate_ci``); answerable and unanswerable
items are never blended into one number (decisions.md #20).

Everything is pure except ``load_program_map``, which reads ``data/manifest.jsonl``.
"""

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from support_desk_assistant.answer.pipeline import STATUS_GATE, STATUS_OK
from support_desk_assistant.evals.stats import rate_ci

Row = Mapping[str, Any]

ATTRIBUTIONS = ("retrieval_page", "retrieval_chunk", "generation")
UNKNOWN_PROGRAM = "unknown"  # answerable row whose gold pages map to no program
NO_PROGRAM = "n/a"  # unanswerable rows: no gold pages
INTENTIONAL_STATUSES = (STATUS_OK, STATUS_GATE)
DASH = "-"


def load_program_map(manifest_path: Path) -> dict[str, str]:
    """url -> program from the manifest (JSON lines). Missing file -> {}."""
    if not manifest_path.exists():
        return {}
    out: dict[str, str] = {}
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            entry = json.loads(line)
            out[entry["url"]] = entry["program"]
    return out


# --- Helpers ---------------------------------------------------------------------


def _mean(values: Sequence[float]) -> float | None:
    return round(sum(values) / len(values), 3) if values else None


def _count(rows: Sequence[Row], pred: Any) -> int:
    return sum(1 for r in rows if pred(r))


def _intentional(row: Row) -> bool:
    """Abstained by the gate or the model, not a refusal/max_tokens fallback."""
    return bool(row["abstained"]) and row["status"] in INTENTIONAL_STATUSES


def _programs(row: Row, program_of: Mapping[str, str]) -> list[str]:
    return sorted({program_of[u] for u in row.get("gold_urls") or () if u in program_of})


# --- Sections --------------------------------------------------------------------


def _overall(answerable: Sequence[Row], unanswerable: Sequence[Row], z: float) -> dict[str, Any]:
    by_category: dict[str, list[Row]] = {}
    for r in answerable:
        by_category.setdefault(r["category"], []).append(r)
    category_rates = [_count(rs, lambda r: r["passed"]) / len(rs) for rs in by_category.values()]
    return {
        "answerable": rate_ci(_count(answerable, lambda r: r["passed"]), len(answerable), z),
        "macro_pass_rate": _mean(category_rates),
        "abstention_recall": rate_ci(
            _count(unanswerable, lambda r: r["passed"]), len(unanswerable), z
        ),
        "n_items": len(answerable) + len(unanswerable),
    }


def _by_program(
    answerable: Sequence[Row],
    unanswerable: Sequence[Row],
    program_of: Mapping[str, str],
    z: float,
) -> dict[str, Any]:
    groups: dict[str, list[Row]] = {}
    for r in answerable:
        for p in _programs(r, program_of) or [UNKNOWN_PROGRAM]:
            groups.setdefault(p, []).append(r)
    order = sorted(p for p in groups if p != UNKNOWN_PROGRAM)
    if UNKNOWN_PROGRAM in groups:
        order.append(UNKNOWN_PROGRAM)
    out: dict[str, Any] = {
        p: {
            "n": len(groups[p]),
            "pass": rate_ci(_count(groups[p], lambda r: r["passed"]), len(groups[p]), z),
            "mean_facts_fraction": _mean([r["facts_fraction"] or 0.0 for r in groups[p]]),
        }
        for p in order
    }
    if unanswerable:
        out[NO_PROGRAM] = {
            "n": len(unanswerable),
            "correct_abstention": rate_ci(
                _count(unanswerable, lambda r: r["passed"]), len(unanswerable), z
            ),
        }
    return out


def _abstention(rows: Sequence[Row], unanswerable: Sequence[Row], z: float) -> dict[str, Any]:
    """Positive class: "should abstain" (unanswerable)."""
    intentional = [r for r in rows if _intentional(r)]
    tp = _count(unanswerable, lambda r: r["passed"])
    return {
        "recall": rate_ci(tp, len(unanswerable), z),
        "precision": rate_ci(tp, len(intentional), z),
        "n_should_abstain": len(unanswerable),
        "n_intentional": len(intentional),
        "false_abstentions": _count(intentional, lambda r: r["answerable"]),
        "missed": _count(unanswerable, lambda r: not r["passed"]),
        "generation_failures": _count(
            rows, lambda r: r["abstained"] and r["status"] not in INTENTIONAL_STATUSES
        ),
        "by_gate": _count(intentional, lambda r: r["abstained_by"] == "gate"),
        "by_model": _count(intentional, lambda r: r["abstained_by"] == "model"),
    }


def _citation(rows: Sequence[Row], z: float) -> dict[str, Any]:
    eligible = [r for r in rows if not r["abstained"] and r["status"] == STATUS_OK]
    answerable = [r for r in eligible if r["answerable"]]
    precisions = []
    for r in answerable:
        cited = r["cited_urls"]
        if cited:
            gold = set(r.get("gold_urls") or ())
            precisions.append(sum(1 for u in cited if u in gold) / len(cited))
    return {
        "n_eligible": len(eligible),
        "validity": rate_ci(_count(eligible, lambda r: r["validator_ok"]), len(eligible), z),
        "gold_cited": rate_ci(_count(answerable, lambda r: r["gold_cited"]), len(answerable), z),
        "citation_precision": {"mean": _mean(precisions), "n": len(precisions)},
    }


def _attribution(answerable: Sequence[Row], z: float) -> dict[str, Any]:
    failed = [r for r in answerable if not r["passed"]]
    counts = Counter(r["attribution"] for r in failed)
    return {
        "n_failed_answerable": len(failed),
        **{a: counts[a] for a in ATTRIBUTIONS},
        "shares": {a: rate_ci(counts[a], len(failed), z) for a in ATTRIBUTIONS},
    }


def summarize(rows: Sequence[Row], *, program_of: Mapping[str, str], z: float) -> dict[str, Any]:
    """Every breakdown for one run's result rows (see the module docstring)."""
    answerable = [r for r in rows if r["answerable"]]
    unanswerable = [r for r in rows if not r["answerable"]]
    return {
        "overall": _overall(answerable, unanswerable, z),
        "by_program": _by_program(answerable, unanswerable, program_of, z),
        "abstention": _abstention(rows, unanswerable, z),
        "citation": _citation(rows, z),
        "attribution": _attribution(answerable, z),
    }


# --- Markdown --------------------------------------------------------------------


def _fmt(value: float | None) -> str:
    return DASH if value is None else f"{value:.2f}"


def _fmt_ci(rc: Mapping[str, Any]) -> str:
    if rc["rate"] is None:
        return DASH
    lo, hi = rc["ci95"]
    return f"{rc['rate']:.2f} [{lo:.2f}, {hi:.2f}]"


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    return [
        "| " + " | ".join(header) + " |",
        "|" + "---|" * len(header),
        *("| " + " | ".join(r) + " |" for r in rows),
    ]


def _rate_row(label: str, rc: Mapping[str, Any]) -> list[str]:
    return [label, str(rc["n"]), str(rc["k"]), _fmt_ci(rc)]


RATE_HEADER = ["metric", "n", "k", "rate [95% CI]"]


def _overall_lines(o: Mapping[str, Any]) -> list[str]:
    return [
        "## Overall",
        "",
        f"{o['n_items']} items. Answerable and unanswerable are reported separately.",
        "",
        *_table(
            RATE_HEADER,
            [
                _rate_row("answerable pass (micro)", o["answerable"]),
                _rate_row("abstention recall (unanswerable)", o["abstention_recall"]),
            ],
        ),
        "",
        f"Macro pass rate (mean of answerable categories): {_fmt(o['macro_pass_rate'])}",
    ]


def _program_lines(by_program: Mapping[str, Any]) -> list[str]:
    rows = []
    for p, m in by_program.items():
        if p == NO_PROGRAM:
            continue
        rows.append([p, str(m["n"]), _fmt_ci(m["pass"]), _fmt(m["mean_facts_fraction"])])
    out = [
        "## By program",
        "",
        "Answerable items; an item whose gold pages span programs counts under each.",
        "",
        *_table(["program", "n", "pass [95% CI]", "mean facts"], rows),
    ]
    if NO_PROGRAM in by_program:
        na = by_program[NO_PROGRAM]
        out += [
            "",
            f"Unanswerable ({NO_PROGRAM}): n={na['n']}, correct abstention "
            f"{_fmt_ci(na['correct_abstention'])}",
        ]
    return out


def _abstention_lines(a: Mapping[str, Any]) -> list[str]:
    return [
        "## Abstention",
        "",
        "Positive class: should abstain (unanswerable). Precision counts gate and model "
        "abstentions only; refusal/max_tokens fallbacks are generation failures.",
        "",
        *_table(
            RATE_HEADER,
            [_rate_row("recall", a["recall"]), _rate_row("precision", a["precision"])],
        ),
        "",
        *_table(
            ["count", "n"],
            [
                ["should abstain", str(a["n_should_abstain"])],
                ["intentional abstentions", str(a["n_intentional"])],
                ["by gate", str(a["by_gate"])],
                ["by model", str(a["by_model"])],
                ["false abstentions (answerable)", str(a["false_abstentions"])],
                ["missed (unanswerable not abstained correctly)", str(a["missed"])],
                ["generation failures", str(a["generation_failures"])],
            ],
        ),
    ]


def _citation_lines(c: Mapping[str, Any]) -> list[str]:
    cp = c["citation_precision"]
    return [
        "## Citation validity",
        "",
        f"Answered items with status ok (n={c['n_eligible']}); abstentions excluded.",
        "",
        *_table(
            RATE_HEADER,
            [
                _rate_row("validator ok", c["validity"]),
                _rate_row("gold cited (answerable)", c["gold_cited"]),
            ],
        ),
        "",
        f"Citation precision (cited pages that are gold, mean): {_fmt(cp['mean'])} (n={cp['n']})",
    ]


def _attribution_lines(a: Mapping[str, Any]) -> list[str]:
    return [
        "## Failure attribution",
        "",
        f"Failed answerable items: n={a['n_failed_answerable']}.",
        "",
        *_table(RATE_HEADER, [_rate_row(x, a["shares"][x]) for x in ATTRIBUTIONS]),
    ]


def render_summary_markdown(summary: Mapping[str, Any]) -> list[str]:
    """Markdown lines for ``summarize(...)``: every row with n and a 95% CI."""
    sections = [
        _overall_lines(summary["overall"]),
        _program_lines(summary["by_program"]),
        _abstention_lines(summary["abstention"]),
        _citation_lines(summary["citation"]),
        _attribution_lines(summary["attribution"]),
    ]
    out: list[str] = []
    for s in sections:
        out += ["", *s] if out else s
    return out
