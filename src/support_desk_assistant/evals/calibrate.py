"""Judge calibration: how often the Haiku judge (and the free number check) agree with a
human who labelled the same facts blind.

Human labels come from the blind hand-review sheet (``render_judge_review`` without the
machine verdict line) and are stored one JSON object per line in
``data/eval/judge_calibration.jsonl``. A calibration run replays every judge-decided
record through ``run_judge`` with the exact production message (so a cached verdict is
a free hit) and compares the current verdict with the human label; number-check records
are compared with the verdict stored at labelling time, no API call.

Pure functions apart from ``load_records``/``append_records`` (file I/O) and
``run_calibration`` (judge calls through the response cache).
"""

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from support_desk_assistant.answer.cache import ResponseCache
from support_desk_assistant.answer.cost import usd_to_cad
from support_desk_assistant.answer.pipeline import MessagesClient
from support_desk_assistant.config import Settings
from support_desk_assistant.evals.answers import FactDecision, estimate_judge_call, run_judge
from support_desk_assistant.evals.draft_questions import CATEGORIES
from support_desk_assistant.evals.stats import cohens_kappa, confusion, rate_ci

Verdict = Literal["present", "absent"]
CalibratedMethod = Literal["judge", "numbers"]
DASH = "-"


# --- Records ---------------------------------------------------------------------


class CalibrationRecord(BaseModel):
    """One human label on one fact of one answer."""

    model_config = ConfigDict(extra="forbid")

    id: str  # record_id(item_id, fact)
    item_id: str
    category: str
    question: str
    answer: str
    fact: str
    # Every fact sent in the original judge call for this item, in order; [] for numbers.
    judge_facts: list[str]
    fact_index: int | None  # 1-based into judge_facts; None for numbers
    method: CalibratedMethod
    machine_present: bool  # the machine verdict at labelling time
    human: Verdict
    source_run: str
    labelled_at: str  # ISO date

    @model_validator(mode="after")
    def _consistent(self) -> "CalibrationRecord":
        if self.id != record_id(self.item_id, self.fact):
            raise ValueError(f"id {self.id!r} does not match item_id and fact")
        if self.method == "numbers":
            if self.fact_index is not None or self.judge_facts:
                raise ValueError("numbers records have no judge_facts or fact_index")
        else:
            i = self.fact_index
            if i is None or not 1 <= i <= len(self.judge_facts):
                raise ValueError(f"fact_index {i} outside judge_facts 1..{len(self.judge_facts)}")
            if self.judge_facts[i - 1] != self.fact:
                raise ValueError(f"judge_facts[{i}] is not the record's fact")
        return self


def record_id(item_id: str, fact: str) -> str:
    """Deterministic: the item id plus the first 8 hex chars of sha256(fact)."""
    return f"{item_id}:{hashlib.sha256(fact.encode('utf-8')).hexdigest()[:8]}"


def load_records(path: Path) -> list[CalibrationRecord]:
    """Missing file -> []. Blank lines skipped. A malformed line raises ValueError naming
    its line number."""
    if not path.exists():
        return []
    records = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            records.append(CalibrationRecord.model_validate(json.loads(line)))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise ValueError(f"{path}: line {n}: {exc}") from exc
    return records


@dataclass(frozen=True)
class AppendResult:
    """Record ids, by outcome. A conflict (same id, different human label) is NOT written:
    the stored label stays until it is fixed by hand."""

    added: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)


def _json_line(record: CalibrationRecord) -> str:
    return json.dumps(record.model_dump(), ensure_ascii=False) + "\n"


def append_records(path: Path, records: Iterable[CalibrationRecord]) -> AppendResult:
    """Append new records (one JSON object per line, model key order), deduplicated by id
    against the file and within ``records``."""
    known = {r.id: r.human for r in load_records(path)}
    result = AppendResult()
    new: list[CalibrationRecord] = []
    for r in records:
        if r.id not in known:
            known[r.id] = r.human
            new.append(r)
            result.added.append(r.id)
        elif known[r.id] == r.human:
            result.duplicates.append(r.id)
        else:
            result.conflicts.append(r.id)
    if new:
        path.parent.mkdir(parents=True, exist_ok=True)
        prefix = ""
        if path.exists() and path.stat().st_size and not path.read_bytes().endswith(b"\n"):
            prefix = "\n"
        with path.open("a", encoding="utf-8") as f:
            f.write(prefix + "".join(_json_line(r) for r in new))
    return result


# --- Sheet import ----------------------------------------------------------------

_BLOCK_RE = re.compile(r"^### (?P<tag>[AB]\d+)\. (?P<item>\S+) \((?P<category>[^)]*)\)\s*$")
_OTHER_BLOCK_RE = re.compile(r"^(## |### [A-Z]\d+\. )")
_FACT_PREFIX = "- Fact:"
_VERDICT_PREFIX = "- Your verdict:"
_UNLABELLED_RE = re.compile(r"^_*$")


class SheetLabel(NamedTuple):
    tag: str  # "A1", "B3"
    item_id: str
    fact: str  # stripped
    verdict: Verdict
    line_no: int  # of the "Your verdict" line, 1-based


@dataclass(frozen=True)
class SheetParse:
    labels: list[SheetLabel]
    unlabelled: int
    problems: list[str]


def parse_sheet(text: str) -> SheetParse:
    """Human verdicts from a (blind) hand-review sheet. Only section A/B blocks carry a
    fact; section C blocks and everything outside a block are ignored."""
    labels: list[SheetLabel] = []
    problems: list[str] = []
    unlabelled = 0
    block: tuple[str, str] | None = None  # (tag, item id)
    fact: str | None = None
    for n, line in enumerate(text.splitlines(), 1):
        m = _BLOCK_RE.match(line)
        if m:
            block, fact = (m["tag"], m["item"]), None
            continue
        if _OTHER_BLOCK_RE.match(line):
            block, fact = None, None
            continue
        if block is None:
            continue
        if line.startswith(_FACT_PREFIX):
            fact = line[len(_FACT_PREFIX) :].strip()
        elif line.startswith(_VERDICT_PREFIX):
            value = line[len(_VERDICT_PREFIX) :].strip().lower()
            if _UNLABELLED_RE.match(value):
                unlabelled += 1
            elif value not in ("present", "absent"):
                problems.append(
                    f"line {n}: {block[0]} {block[1]}: verdict {value!r} is not present/absent"
                )
            elif fact is None:
                problems.append(f"line {n}: {block[0]} {block[1]}: verdict without a Fact line")
            else:
                labels.append(SheetLabel(block[0], block[1], fact, value, n))
    return SheetParse(labels, unlabelled, problems)


@dataclass(frozen=True)
class ImportResult:
    records: list[CalibrationRecord]
    unlabelled: int
    problems: list[str]


def import_labels(
    sheet_text: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    source_run: str,
    labelled_at: str,
) -> ImportResult:
    """Turn sheet labels into records using the result rows (``ItemRun.row()`` shape).
    The method comes from the row's fact decision, never from the sheet tag. Labels that
    cannot be compared with a machine verdict are skipped with a problem."""
    parsed = parse_sheet(sheet_text)
    problems = list(parsed.problems)
    by_id = {r["id"]: r for r in rows}
    seen: set[tuple[str, str]] = set()
    records: list[CalibrationRecord] = []
    for label in parsed.labels:
        where = f"line {label.line_no}: {label.tag} {label.item_id}"
        key = (label.item_id, label.fact)
        if key in seen:
            problems.append(f"{where}: duplicate label for this fact, kept the first")
            continue
        seen.add(key)
        row = by_id.get(label.item_id)
        if row is None:
            problems.append(f"{where}: item not in the results file")
            continue
        decision = next((d for d in row["facts"] if d["fact"].strip() == label.fact), None)
        if decision is None:
            problems.append(f"{where}: fact not found in the item: {label.fact!r}")
            continue
        method = decision["method"]
        if method == "none":
            problems.append(f"{where}: fact has no machine verdict (method none)")
            continue
        if decision.get("judge_error"):
            problems.append(f"{where}: judge error on this fact, no verdict to compare")
            continue
        judge_facts: list[str] = []
        fact_index = None
        if method == "judge":
            judge_facts = [d["fact"] for d in row["facts"] if d["method"] == "judge"]
            fact_index = judge_facts.index(decision["fact"]) + 1
        records.append(
            CalibrationRecord(
                id=record_id(label.item_id, decision["fact"]),
                item_id=label.item_id,
                category=row["category"],
                question=row["question"],
                answer=row["answer"],
                fact=decision["fact"],
                judge_facts=judge_facts,
                fact_index=fact_index,
                method=method,
                machine_present=bool(decision["present"]),
                human=label.verdict,
                source_run=source_run,
                labelled_at=labelled_at,
            )
        )
    return ImportResult(records, parsed.unlabelled, problems)


# --- Calibration run -------------------------------------------------------------


@dataclass(frozen=True)
class _Compared:
    record: CalibrationRecord
    machine: bool  # the verdict compared with the human label
    reason: str


def _verdict(flag: bool) -> Verdict:
    return "present" if flag else "absent"


def _category_order(categories: Iterable[str]) -> list[str]:
    order = {c: i for i, c in enumerate(CATEGORIES)}
    return sorted(set(categories), key=lambda c: (order.get(c, len(order)), c))


def _section(compared: Sequence[_Compared], z: float) -> dict[str, Any]:
    pairs = [(c.record.human == "present", c.machine) for c in compared]
    n = len(pairs)
    agree = [h == m for h, m in pairs]
    counts = confusion(pairs)
    human_absent = sum(1 for h, _ in pairs if not h)
    human_present = n - human_absent
    per_category = {}
    for cat in _category_order(c.record.category for c in compared):
        flags = [a for a, c in zip(agree, compared, strict=True) if c.record.category == cat]
        per_category[cat] = rate_ci(sum(flags), len(flags), z)
    return {
        "n": n,
        "agreement": rate_ci(sum(agree), n, z),
        "kappa": cohens_kappa(pairs),
        "confusion": counts,
        "false_present": rate_ci(counts["fp"], human_absent, z),
        "false_absent": rate_ci(counts["fn"], human_present, z),
        "per_category": per_category,
        "disagreements": [
            {
                "id": c.record.id,
                "item_id": c.record.item_id,
                "category": c.record.category,
                "fact": c.record.fact,
                "human": c.record.human,
                "machine": _verdict(c.machine),
                "reason": c.reason,
            }
            for c, a in zip(compared, agree, strict=True)
            if not a
        ],
    }


GroupKey = tuple[str, str, str, tuple[str, ...]]  # item id, question, answer, judge facts


def _split_records(
    records: Sequence[CalibrationRecord],
) -> tuple[dict[GroupKey, list[CalibrationRecord]], list[CalibrationRecord]]:
    """Judge records grouped by the production judge call they belong to, and the
    number-check records (no call)."""
    groups: dict[GroupKey, list[CalibrationRecord]] = {}
    numbers: list[CalibrationRecord] = []
    for r in records:
        if r.method == "judge":
            key = (r.item_id, r.question, r.answer, tuple(r.judge_facts))
            groups.setdefault(key, []).append(r)
        else:
            numbers.append(r)
    return groups, numbers


def _group_call(
    key: GroupKey, *, settings: Settings, system: str, cache: ResponseCache
) -> tuple[list[FactDecision], int, float]:
    """(pending decisions, API calls needed: 0 on a cache hit else 1, upper-bound CAD)."""
    _, question, answer, judge_facts = key
    decisions = [FactDecision(f, "judge", None, "") for f in judge_facts]
    calls, usd = estimate_judge_call(question, answer, decisions, system, settings, cache)
    return decisions, calls, usd_to_cad(usd, settings.answer)


@dataclass(frozen=True)
class CalibrationPlan:
    """What a calibration run would do: judge groups (one call each), how many are
    already cached, how many need an API call, and the upper bound for those in CAD."""

    groups: int
    cached: int
    misses: int
    upper_bound_cad: float


def plan_calibration(
    records: Sequence[CalibrationRecord], *, settings: Settings, system: str
) -> CalibrationPlan:
    """Price a calibration run without calling Anthropic."""
    groups, _ = _split_records(records)
    cache = ResponseCache(settings.eval_cache_dir)
    misses, bound = 0, 0.0
    for key in groups:
        _, calls, cad = _group_call(key, settings=settings, system=system, cache=cache)
        misses += calls
        bound += cad
    return CalibrationPlan(len(groups), len(groups) - misses, misses, bound)


def run_calibration(
    records: Sequence[CalibrationRecord],
    *,
    settings: Settings,
    system: str,
    client: MessagesClient | None,
    cap_cad: float | None = None,
) -> dict[str, Any]:
    """Agreement of the current judge (one replayed call per item group, cached responses
    are free) and of the stored number-check verdicts with the human labels. JSON-able.
    With ``cap_cad``, a group that needs an API call is not replayed if the spend so far
    plus its upper bound would pass the cap (``stopped_by_cap``), and the
    records not replayed are counted in ``n_unreplayed`` (groups already cached are
    still replayed, they cost nothing). API/network errors from the judge call
    propagate."""
    z = settings.eval.ci_z
    groups, number_records = _split_records(records)
    numbers = [_Compared(r, r.machine_present, "") for r in number_records]
    cache = ResponseCache(settings.eval_cache_dir)

    judged: list[_Compared] = []
    calls = cache_hits = errors = drift = 0
    cost_cad = 0.0
    stopped = False
    unreplayed = 0
    for key, group in groups.items():
        _, question, answer, _ = key
        decisions, needed, bound_cad = _group_call(
            key, settings=settings, system=system, cache=cache
        )
        if cap_cad is not None and needed and cost_cad + bound_cad > cap_cad:
            stopped = True  # a cached group later in the file is still free to replay
            unreplayed += len(group)
            continue
        run = run_judge(
            question, answer, decisions, settings=settings, system=system, client=client
        )
        calls += run.calls
        cache_hits += int(run.cache_hit)
        cost_cad += run.cost_cad
        for r in group:
            d = run.decisions[(r.fact_index or 0) - 1]
            if run.error is not None or d.judge_error or d.present is None:
                errors += 1
                continue
            drift += int(d.present != r.machine_present)
            judged.append(_Compared(r, d.present, d.reason))

    judge_section = _section(judged, z) | {"n_errors": errors, "drift": drift}
    min_n = settings.eval.calibration_min_n
    return {
        "n_records": len(records),
        "min_n": min_n,
        "below_min_n": judge_section["n"] < min_n,
        "calls": calls,
        "cache_hits": cache_hits,
        "cost_cad": round(cost_cad, 6),
        "cap_cad": cap_cad,
        "stopped_by_cap": stopped,
        "n_unreplayed": unreplayed,
        "judge": judge_section,
        "numbers": _section(numbers, z),
    }


# --- Markdown --------------------------------------------------------------------


def _fmt_ci(rc: Mapping[str, Any]) -> str:
    if rc["rate"] is None:
        return DASH
    lo, hi = rc["ci95"]
    return f"{rc['rate']:.2f} [{lo:.2f}, {hi:.2f}] ({rc['k']}/{rc['n']})"


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    return [
        "| " + " | ".join(header) + " |",
        "|" + "---|" * len(header),
        *("| " + " | ".join(r) + " |" for r in rows),
    ]


def _fmt_kappa(value: float | None) -> str:
    return DASH if value is None else f"{value:.2f}"


def _section_lines(heading: str, s: Mapping[str, Any], *, judge: bool) -> list[str]:
    rows = [["n", str(s["n"])]]
    if judge:
        rows.append(["errors (excluded)", str(s["n_errors"])])
    rows += [
        ["agreement", _fmt_ci(s["agreement"])],
        ["Cohen's kappa", _fmt_kappa(s["kappa"])],
        ["false present (inflates scores; of human-absent)", _fmt_ci(s["false_present"])],
        ["false absent (deflates scores; of human-present)", _fmt_ci(s["false_absent"])],
    ]
    if judge:
        rows.append(["drift (current verdict differs from the one labelled)", str(s["drift"])])
    c = s["confusion"]
    out = ["", f"## {heading}", "", *_table(["metric", "value"], rows)]
    out += [
        "",
        *_table(
            ["", "machine present", "machine absent"],
            [
                ["human present", str(c["tp"]), str(c["fn"])],
                ["human absent", str(c["fp"]), str(c["tn"])],
            ],
        ),
    ]
    out += ["", "Agreement per category:", ""]
    if s["per_category"]:
        out += _table(
            ["category", "agreement"],
            [[cat, _fmt_ci(rc)] for cat, rc in s["per_category"].items()],
        )
    else:
        out.append("None.")
    out += ["", "Disagreements:", ""]
    if not s["disagreements"]:
        out.append("None.")
    for d in s["disagreements"]:
        reason = f" Judge: {d['reason']}" if d["reason"] else ""
        out.append(
            f"- **{d['item_id']}** ({d['category']}): human {d['human']}, machine "
            f"{d['machine']}. Fact: {d['fact']}.{reason}"
        )
    return out


def render_calibration_markdown(report: Mapping[str, Any]) -> str:
    judge, numbers = report["judge"], report["numbers"]
    out = [
        "# Judge calibration",
        "",
        "Human labels were given blind: the sheet did not show the judge or number-check "
        "verdict. Judge verdicts are replayed now with the current prompt and model; "
        "number-check verdicts are the ones stored at labelling time.",
    ]
    if report["below_min_n"]:
        out += [
            "",
            f"> **WARNING: provisional.** Only n={judge['n']} judge-decided facts are "
            f"labelled, fewer than min_n={report['min_n']}; treat these agreement numbers "
            "as indicative only.",
        ]
    if report.get("stopped_by_cap"):
        out += [
            "",
            f"> **WARNING: stopped by the spend cap (CA${report['cap_cad']:.2f}).** "
            f"{report['n_unreplayed']} judge record(s) were not replayed; the judge numbers "
            "below cover the rest only. Re-run: completed calls are cached.",
        ]
    out += [
        "",
        f"Records: {report['n_records']} (judge {judge['n'] + judge['n_errors']}, "
        f"numbers {numbers['n']}). Judge calls: {report['calls']}, cache hits: "
        f"{report['cache_hits']}, cost CA${report['cost_cad']:.4f}.",
    ]
    out += _section_lines("LLM judge vs human", judge, judge=True)
    out += _section_lines("Number check vs human (not the LLM)", numbers, judge=False)
    return "\n".join(out) + "\n"
