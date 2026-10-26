"""uv run sda-eval-triage-review [--status | --terminal | --show | --export | --apply FILE]
(offline)

Blind labelling by a person. The sample is a stratified draw of tickets (chosen from the silver
labels, which are never shown). ``--terminal`` asks about one ticket at a time and records each
answer at once, so you can quit and come back. ``--export`` writes a page instead: each card
shows only the subject and body, with empty queue and priority dropdowns, and a Skip button for
tickets that are not a real request or that the reviewer cannot decide. "Copy decisions" copies
``<id> <queue> <priority>`` / ``<id> skip`` lines; ``--apply`` records them as gold labels
(``--labelled-by`` says who made them: a model session must not be recorded as ``human``).
``--show`` prints the pending sample as plain text for a reviewer that reads it (docs/decisions.md
#28, #29).
"""

import argparse
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from html import escape
from pathlib import Path

from support_desk_assistant.config import Settings, get_settings
from support_desk_assistant.evals.triage_draft import TicketCandidate, load_candidates
from support_desk_assistant.evals.triage_label import (
    GoldLabel,
    SilverLabel,
    agreement,
    gold_sample,
    load_jsonl,
    write_jsonl,
)
from support_desk_assistant.triage.schema import PRIORITIES, QUEUES


def load_state(path: Path) -> dict[str, dict[str, str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(path: Path, state: dict[str, dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


@dataclass(frozen=True)
class Decision:
    action: str  # "label" | "skip"
    queue: str | None = None
    priority: str | None = None
    unsure: bool = False  # the labeller was torn between two labels (a trailing "?")


def parse_decisions(text: str) -> tuple[dict[str, Decision], list[str]]:
    """``<id> <queue> <priority> [?]`` or ``<id> skip`` lines; a trailing ``?`` marks a label the
    labeller was unsure of. Blank lines and ``#`` comments are ignored; a malformed line is
    reported, not fatal; if an id repeats, the last line wins."""
    decisions: dict[str, Decision] = {}
    problems: list[str] = []
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip().lstrip("﻿")
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        unsure = len(parts) == 4 and parts[3] == "?"
        if unsure:
            parts = parts[:3]
        if len(parts) == 2 and parts[1].lower() == "skip":
            decision = Decision("skip")
        elif len(parts) == 3 and parts[1].lower() in QUEUES and parts[2].lower() in PRIORITIES:
            decision = Decision("label", parts[1].lower(), parts[2].lower(), unsure)
        else:
            problems.append(f"line {number}: expected '<id> <queue> <priority> [?]' or '<id> skip'")
            continue
        if parts[0] in decisions and decisions[parts[0]] != decision:
            problems.append(f"line {number}: {parts[0]} appears twice; using the last")
        decisions[parts[0]] = decision
    return decisions, problems


@dataclass
class ApplyReport:
    labelled: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def apply_decisions(
    text: str,
    candidates: list[TicketCandidate],
    settings: Settings,
    labelled_by: str = "human",
) -> ApplyReport:
    """Record each decision as a gold label (or a skip), saying who made it. Safe to run again:
    a decision already on record is left alone."""
    decisions, problems = parse_decisions(text)
    report = ApplyReport(problems=problems)
    state_path, gold_path = settings.triage_review_state_path, settings.triage_gold_path
    state = load_state(state_path)
    gold = load_jsonl(gold_path, GoldLabel)
    known = {c.id for c in candidates}
    for tid, d in decisions.items():
        if tid not in known:
            report.problems.append(f"{tid}: no such ticket")
            continue
        if tid in state:
            if state[tid]["action"] == d.action:
                report.unchanged.append(tid)
            else:
                report.problems.append(f"{tid}: already {state[tid]['action']}, left unchanged")
            continue
        state[tid] = {"action": d.action}
        if d.action == "skip":
            report.skipped.append(tid)
        else:
            assert d.queue and d.priority
            gold.append(
                GoldLabel(
                    id=tid,
                    queue=d.queue,
                    priority=d.priority,
                    labelled_by=labelled_by,
                    unsure=d.unsure,
                )
            )
            report.labelled.append(tid)
    write_jsonl(gold_path, gold)
    save_state(state_path, state)
    return report


def pending_all(candidates: list[TicketCandidate], settings: Settings) -> list[TicketCandidate]:
    """Every candidate with no decision on record: for a reviewer that labels the whole set."""
    state = load_state(settings.triage_review_state_path)
    return [c for c in candidates if c.id not in state]


def pending_sample(
    candidates: list[TicketCandidate], silver: list[SilverLabel], settings: Settings
) -> tuple[list[TicketCandidate], list[str]]:
    sample = gold_sample(candidates, silver, settings)
    state = load_state(settings.triage_review_state_path)
    by_id = {c.id: c for c in candidates}
    return [by_id[i] for i in sample.ids if i not in state], sample.shortfalls


def status_lines(
    candidates: list[TicketCandidate], silver: list[SilverLabel], settings: Settings
) -> list[str]:
    sample = gold_sample(candidates, silver, settings)
    state = load_state(settings.triage_review_state_path)
    gold = load_jsonl(settings.triage_gold_path, GoldLabel)
    skipped = sum(1 for v in state.values() if v["action"] == "skip")
    pending = [i for i in sample.ids if i not in state]
    lines = [
        f"sample {len(sample.ids)}: labelled {len(gold)}, skipped {skipped}, pending {len(pending)}"
    ]
    lines += [f"  shortfall: {s}" for s in sample.shortfalls]
    agree = agreement(silver, gold)
    if agree["n"]:
        lines.append(
            f"silver vs your labels on {agree['n']}: queue {agree['queue']}, "
            f"priority {agree['priority']}, both {agree['both']}"
        )
    return lines


# --- Terminal review -------------------------------------------------------------

PRIORITY_KEYS = {"u": "urgent", "h": "high", "n": "normal", "l": "low"}

TERMINAL_RULES = """\
Queue: the program the main request is about (ei, cpp, oas, sin, passport), or other for
anything else or no real request.
Priority, by what is at stake and by when, not by tone (u, h, n, l also work):
  urgent: money or status at immediate risk, a deadline within 7 days, someone else using the
          person's SIN, a passport lost abroad, travel or a job start within 7 days that
          needs the document
  high:   time-bound but not within days: a stuck or overdue claim, a wrong payment, an
          appeal, a trip or deadline within about a month
  normal: a question about their own situation, no time pressure
  low:    general information, no time pressure, nothing to fix
Answer '<queue> <priority>' (for example 'ei h'), 'skip' (not a real request, or unsure) or
'quit'.
"""


def terminal_decision(answer: str) -> str | None:
    """'ei h' / 'passport urgent' / 'skip' -> the decision text for ``apply_decisions``;
    anything else -> None."""
    parts = answer.lower().split()
    if parts == ["skip"]:
        return "skip"
    if len(parts) == 2:
        queue, priority = parts[0], PRIORITY_KEYS.get(parts[1], parts[1])
        if queue in QUEUES and priority in PRIORITIES:
            return f"{queue} {priority}"
    return None


def render_text(candidates: list[TicketCandidate]) -> str:
    """The pending sample as plain text: id, subject, body, nothing else (blind)."""
    return "\n\n".join(
        f"=== {c.id} ===\nSubject: {c.subject or '(no subject)'}\n\n{c.body}" for c in candidates
    )


def review_in_terminal(
    candidates: list[TicketCandidate],
    silver: list[SilverLabel],
    settings: Settings,
    *,
    read: Callable[[str], str] = input,
    write: Callable[[str], None] = print,
) -> ApplyReport:
    """One pending ticket at a time, blind (no draft topic, no silver label). Each answer is
    recorded at once through ``apply_decisions``; ``quit`` (or end of input) stops."""
    queue, _ = pending_sample(candidates, silver, settings)
    total = ApplyReport()
    write(TERMINAL_RULES)
    for number, c in enumerate(queue, 1):
        subject = c.subject or "(no subject)"
        write(f"\n[{number}/{len(queue)}] {c.id}\nSubject: {subject}\n\n{c.body}\n")
        while True:
            try:
                answer = read("> ").strip()
            except EOFError:
                return total
            if answer.lower() in ("q", "quit"):
                return total
            decision = terminal_decision(answer)
            if decision is not None:
                break
            write("expected '<queue> <priority>', 'skip' or 'quit'")
        report = apply_decisions(f"{c.id} {decision}\n", candidates, settings)
        total.labelled += report.labelled
        total.skipped += report.skipped
        total.unchanged += report.unchanged
        total.problems += report.problems
    return total


# --- Phone page ------------------------------------------------------------------


def _options(values: tuple[str, ...]) -> str:
    return '<option value="">choose</option>' + "".join(
        f'<option value="{v}">{v}</option>' for v in values
    )


def _card(c: TicketCandidate, number: int, total: int) -> str:
    subject = escape(c.subject) if c.subject else "<em>(no subject)</em>"
    return f"""<section class="card" data-id="{c.id}">
<div class="meta"><span class="id">{c.id}</span><span>{number} of {total}</span></div>
<div class="subject">{subject}</div>
<pre class="body">{escape(c.body)}</pre>
<div class="row">
<label>Queue <select class="q">{_options(QUEUES)}</select></label>
<label>Priority <select class="p">{_options(PRIORITIES)}</select></label>
</div>
<div class="actions">
<button type="button" class="btn skip" aria-pressed="false">
Skip (not a real request, or unsure)</button>
</div>
</section>"""


def render_page(candidates: list[TicketCandidate]) -> str:
    total = len(candidates)
    body = "\n".join(_card(c, n, total) for n, c in enumerate(candidates, 1))
    return (
        _HEAD
        + f"<body>\n{_BAR}<main>\n{_RULES}{body or '<p>No tickets to review.</p>'}\n</main>\n"
        + (f"<script>{_SCRIPT}</script>\n</body>\n</html>\n")
    )


_RULES = """<details class="rules"><summary>Queue and priority rules</summary>
<p><b>Queue</b>: the program the main request is about (ei, cpp, oas, sin, passport), or other for
anything else or no real request.</p>
<p><b>Priority</b>, by what is at stake and by when, not by tone:</p>
<ul><li><b>urgent</b>: money or status at immediate risk, a deadline within 7 days, someone else
using the person's SIN, a passport lost abroad, travel or a job start within 7 days that needs the
document.</li>
<li><b>high</b>: time-bound but not within days: a stuck or overdue claim, a wrong payment, an
appeal, a trip or deadline within about a month.</li>
<li><b>normal</b>: a question about their own situation, no time pressure.</li>
<li><b>low</b>: general information, no time pressure, nothing to fix.</li></ul></details>
"""

_HEAD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark"><title>Triage labelling</title>
<style>
:root { --bg:#f6f6f4; --card:#fff; --text:#1d1d1b; --muted:#5d5d58; --line:#d9d9d3;
  --done:#1a7f37; --skip:#8a6d00; --code:#f1f1ee; }
@media (prefers-color-scheme: dark) { :root { --bg:#121212; --card:#1c1c1c; --text:#ececea;
  --muted:#a2a29b; --line:#38383a; --done:#4cc26b; --skip:#e0b84a; --code:#242424; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--text);
  font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }
.bar { position:sticky; top:0; z-index:2; background:var(--card);
  border-bottom:1px solid var(--line);
  padding:8px 16px; display:flex; flex-wrap:wrap; gap:4px 12px; align-items:center; }
.counts { font-weight:600; flex:1 1 12em; font-size:.95rem; }
.bar button { min-height:44px; padding:0 16px; border-radius:8px; border:1px solid var(--line);
  background:var(--bg); color:var(--text); font:inherit; font-weight:600; }
#status { color:var(--muted); font-size:.9rem; flex-basis:100%; } #status:empty { display:none; }
details.out, details.rules { max-width:760px; margin:12px auto 0; padding:0 16px; }
details.rules { background:var(--card); border:1px solid var(--line); border-radius:10px;
  padding:8px 16px; margin-bottom:16px; }
details.out textarea { width:100%; min-height:8em; font:14px/1.4 ui-monospace,monospace;
  background:var(--code); color:var(--text); border:1px solid var(--line); }
main { max-width:760px; margin:0 auto; padding:16px; }
.card { background:var(--card); border:1px solid var(--line); border-left:6px solid var(--line);
  border-radius:10px; padding:14px 16px; margin:0 0 16px; }
.card.is-done { border-left-color:var(--done); } .card.is-skip { border-left-color:var(--skip); }
.meta { display:flex; flex-wrap:wrap; gap:6px 10px; color:var(--muted); font-size:.85rem; }
.id { font-family:ui-monospace,monospace; color:var(--text); font-weight:600; }
.subject { font-weight:700; margin:10px 0 4px; }
.body { background:var(--code); border-radius:8px; padding:10px 12px; margin:0; font:inherit;
  white-space:pre-wrap; overflow-wrap:anywhere; }
.row { display:flex; flex-wrap:wrap; gap:12px; margin-top:12px; }
.row label { display:flex; flex-direction:column; font-size:.85rem; color:var(--muted);
  flex:1 1 9em; }
.row select { min-height:44px; font:inherit; color:var(--text); background:var(--bg);
  border:1px solid var(--line); border-radius:8px; padding:0 8px; }
.actions { display:flex; gap:12px; margin-top:12px; }
.btn { flex:1; min-height:48px; border-radius:10px; border:2px solid var(--line);
  background:var(--bg); color:var(--text); font:inherit; font-weight:600; }
.btn[aria-pressed="true"] { background:var(--skip); color:#fff; border-color:var(--skip); }
</style></head>
"""

_BAR = """<header class="bar">
<div class="counts" aria-live="polite">Labelled <span id="n-l">0</span> ·
Skipped <span id="n-s">0</span> · Pending <span id="n-p">0</span></div>
<button type="button" id="copy">Copy decisions</button><span id="status" role="status"></span>
</header>
<details class="out" id="out"><summary>Show decisions as text</summary>
<textarea id="text" readonly rows="8"></textarea></details>
"""

_SCRIPT = """
(function () {
  var KEY = "sda-triage-label:v1";
  var cards = Array.prototype.slice.call(document.querySelectorAll(".card"));
  var stored = {};
  try { var raw = window.localStorage.getItem(KEY); var p = raw ? JSON.parse(raw) : {};
    if (p && typeof p === "object" && !Array.isArray(p)) stored = p; } catch (e) { stored = {}; }
  function persist() {
    try { window.localStorage.setItem(KEY, JSON.stringify(stored)); } catch (e) {}
  }
  function sel(c, k) { return c.querySelector("select." + k); }
  function state(c) {
    var id = c.dataset.id, s = stored[id] || {};
    if (s.skip) return "skip";
    return sel(c, "q").value && sel(c, "p").value ? "label" : "";
  }
  function line(c) {
    var st = state(c);
    if (st === "skip") return c.dataset.id + " skip";
    if (st === "label") return c.dataset.id + " " + sel(c, "q").value + " " + sel(c, "p").value;
    return null;
  }
  function paint() {
    var l = 0, s = 0;
    cards.forEach(function (c) {
      var st = state(c);
      if (st === "label") l++; if (st === "skip") s++;
      c.classList.toggle("is-done", st === "label"); c.classList.toggle("is-skip", st === "skip");
      c.querySelector(".btn").setAttribute("aria-pressed", String(st === "skip"));
    });
    document.getElementById("n-l").textContent = l;
    document.getElementById("n-s").textContent = s;
    document.getElementById("n-p").textContent = cards.length - l - s;
    document.getElementById("text").value = cards.map(line).filter(Boolean).join("\\n");
  }
  function say(m) { document.getElementById("status").textContent = m; }
  cards.forEach(function (c) {
    var id = c.dataset.id, s = stored[id];
    if (s) { if (s.q) sel(c, "q").value = s.q; if (s.p) sel(c, "p").value = s.p; }
    function save(skip) {
      stored[id] = { q: sel(c, "q").value, p: sel(c, "p").value, skip: skip };
      persist(); paint(); say("");
    }
    ["q", "p"].forEach(function (k) {
      sel(c, k).addEventListener("change", function () { save(false); });
    });
    c.querySelector(".btn").addEventListener("click", function () {
      save(!(stored[id] && stored[id].skip));
    });
  });
  document.getElementById("copy").addEventListener("click", function () {
    var text = document.getElementById("text").value;
    if (!text) { say("Nothing decided yet."); return; }
    function manual() {
      var box = document.getElementById("text");
      document.getElementById("out").open = true;
      box.focus(); box.select(); var ok = false;
      try { ok = document.execCommand("copy"); } catch (e) {}
      say(ok ? "Copied." : "Select the text below and copy it.");
    }
    try { if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { say("Copied."); }, manual);
      return; } } catch (e) {}
    manual();
  });
  paint();
})();
"""


def export_html(candidates: list[TicketCandidate], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(render_page(candidates), encoding="utf-8")
    tmp.replace(path)


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(prog="sda-eval-triage-review", description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--status", action="store_true", help="show progress (default)")
    group.add_argument("--terminal", action="store_true", help="label tickets here, one by one")
    group.add_argument("--show", action="store_true", help="print the pending sample, blind")
    parser.add_argument(
        "--all", action="store_true", help="with --show: every undecided ticket, not the sample"
    )
    group.add_argument("--export", action="store_true", help="write the blind labelling page")
    group.add_argument("--apply", metavar="FILE", help="record a decisions file as gold labels")
    parser.add_argument(
        "--labelled-by",
        default="human",
        help="who made the decisions in --apply (default human; a model session says so)",
    )
    args = parser.parse_args(argv)
    if args.all and not args.show:
        parser.error("--all goes with --show")

    candidates = load_candidates(settings.triage_candidates_path)
    silver = load_jsonl(settings.triage_silver_path, SilverLabel)
    whole_set = bool(args.apply) or (args.show and args.all)  # no silver-chosen sample involved
    if not candidates or (not silver and not whole_set):
        print("need candidates and silver labels: run sda-eval-triage-draft and -label first")
        print("(or label every ticket: --show --all, then --apply FILE --labelled-by NAME)")
        return 2
    if args.terminal:
        report = review_in_terminal(candidates, silver, settings)
        print(f"\nlabelled {len(report.labelled)}, skipped {len(report.skipped)}")
        for problem in report.problems:
            print(f"  problem: {problem}")
    elif args.show:
        queue = (
            pending_all(candidates, settings)
            if args.all
            else pending_sample(candidates, silver, settings)[0]
        )
        print(render_text(queue))
        return 0
    elif args.export:
        queue, shortfalls = pending_sample(candidates, silver, settings)
        export_html(queue, settings.triage_review_html_path)
        print(f"wrote {settings.triage_review_html_path} ({len(queue)} tickets to label)")
        for s in shortfalls:
            print(f"  shortfall: {s}")
        return 0
    if args.apply:
        text = Path(args.apply).read_text(encoding="utf-8")
        report = apply_decisions(text, candidates, settings, args.labelled_by)
        print(
            f"labelled {len(report.labelled)}, skipped {len(report.skipped)}, "
            f"unchanged {len(report.unchanged)}"
        )
        for problem in report.problems:
            print(f"  problem: {problem}")
        if not silver:
            gold = load_jsonl(settings.triage_gold_path, GoldLabel)
            print(f"labels on record: {len(gold)} of {len(candidates)} tickets")
            return 0
    print("\n".join(status_lines(candidates, silver, settings)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
