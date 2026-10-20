"""uv run sda-eval-triage-review [--status | --export | --apply FILE]   (offline)

Review of drafted tickets. ``--export`` writes a phone page where each ticket shows its draft
queue and priority; the reviewer keeps or changes them and accepts or rejects. "Copy
decisions" copies ``<id> a <queue> <priority>`` / ``<id> r`` lines; ``--apply`` records them
and appends the accepted tickets, with the reviewer's labels, to the versioned triage set.
"""

import argparse
import hashlib
import json
from dataclasses import dataclass, field
from html import escape
from pathlib import Path

from support_desk_assistant.config import Settings, get_settings
from support_desk_assistant.evals.triage_draft import TicketCandidate, load_candidates
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


def load_set(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


@dataclass(frozen=True)
class Decision:
    action: str  # "accept" | "reject"
    queue: str | None = None
    priority: str | None = None


def parse_decisions(text: str) -> tuple[dict[str, Decision], list[str]]:
    """``<id> a <queue> <priority>`` or ``<id> r`` lines. Blank lines and ``#`` comments are
    ignored; a malformed line is reported, not fatal; if an id repeats, the last line wins."""
    decisions: dict[str, Decision] = {}
    problems: list[str] = []
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip().lstrip("﻿")
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        verb = parts[1].lower() if len(parts) > 1 else ""
        if len(parts) == 2 and verb in ("r", "reject"):
            decision = Decision("reject")
        elif (
            len(parts) == 4
            and verb in ("a", "accept")
            and parts[2].lower() in QUEUES
            and parts[3].lower() in PRIORITIES
        ):
            decision = Decision("accept", parts[2].lower(), parts[3].lower())
        else:
            problems.append(f"line {number}: expected '<id> a <queue> <priority>' or '<id> r'")
            continue
        if parts[0] in decisions and decisions[parts[0]] != decision:
            problems.append(f"line {number}: {parts[0]} appears twice; using the last")
        decisions[parts[0]] = decision
    return decisions, problems


@dataclass
class ApplyReport:
    accepted: list[tuple[str, str]] = field(default_factory=list)  # (candidate id, set id)
    rejected: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    relabelled: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def apply_decisions(
    text: str, candidates: list[TicketCandidate], settings: Settings
) -> ApplyReport:
    """Record each decision. Safe to run again: a decision already on record is left alone."""
    decisions, problems = parse_decisions(text)
    report = ApplyReport(problems=problems)
    state_path, set_path = settings.triage_review_state_path, settings.triage_set_path
    state = load_state(state_path)
    items = load_set(set_path)
    by_id = {c.id: c for c in candidates}
    for cid, d in decisions.items():
        if cid in state:
            if state[cid]["action"] == d.action:
                report.unchanged.append(cid)
            else:
                report.problems.append(f"{cid}: already {state[cid]['action']}, left unchanged")
            continue
        candidate = by_id.get(cid)
        if candidate is None:
            report.problems.append(f"{cid}: no such candidate")
            continue
        if d.action == "reject":
            state[cid] = {"action": "reject"}
            report.rejected.append(cid)
            continue
        set_id = f"tt-{len(items) + 1:03d}"
        item = {
            "id": set_id,
            "subject": candidate.subject,
            "body": candidate.body,
            "queue": d.queue,
            "priority": d.priority,
            "style": candidate.style,
            "draft_queue": candidate.queue,
            "draft_priority": candidate.priority,
            "notes": candidate.notes,
            "source_candidate": cid,
        }
        items.append(item)
        state[cid] = {"action": "accept", "set_id": set_id}
        report.accepted.append((cid, set_id))
        if (d.queue, d.priority) != (candidate.queue, candidate.priority):
            report.relabelled.append(cid)
    set_path.parent.mkdir(parents=True, exist_ok=True)
    set_path.write_text(
        "".join(json.dumps(i, ensure_ascii=False) + "\n" for i in items), encoding="utf-8"
    )
    save_state(state_path, state)
    return report


def status_lines(candidates: list[TicketCandidate], settings: Settings) -> list[str]:
    state = load_state(settings.triage_review_state_path)
    accepted = sum(1 for v in state.values() if v["action"] == "accept")
    rejected = sum(1 for v in state.values() if v["action"] == "reject")
    pending = [c for c in candidates if c.id not in state]
    return [
        f"candidates {len(candidates)}: accepted {accepted}, rejected {rejected}, "
        f"pending {len(pending)}",
        f"triage set: {len(load_set(settings.triage_set_path))} items in "
        f"{settings.triage_set_path}",
    ]


# --- Phone page ------------------------------------------------------------------


def _options(values: tuple[str, ...], selected: str) -> str:
    return "".join(
        f'<option value="{v}"{" selected" if v == selected else ""}>{v}</option>' for v in values
    )


def _card(c: TicketCandidate, number: int, total: int) -> str:
    fp = hashlib.sha256(
        json.dumps([c.subject, c.body, c.queue, c.priority], ensure_ascii=False).encode()
    ).hexdigest()[:10]
    subject = escape(c.subject) if c.subject else "<em>(no subject)</em>"
    return f"""<section class="card" data-id="{c.id}" data-h="{fp}"
 data-q="{c.queue}" data-p="{c.priority}">
<div class="meta"><span class="id">{c.id}</span><span>{number} of {total}</span>
<span class="chip">{escape(c.style)}</span></div>
<div class="subject">{subject}</div>
<pre class="body">{escape(c.body)}</pre>
<div class="label">Why the draft labels are right</div>
<p class="notes">{escape(c.notes)}</p>
<div class="row">
<label>Queue <select class="q">{_options(QUEUES, c.queue)}</select></label>
<label>Priority <select class="p">{_options(PRIORITIES, c.priority)}</select></label>
</div>
<div class="actions">
<button type="button" class="btn accept" data-act="a">Accept</button>
<button type="button" class="btn reject" data-act="r">Reject</button>
</div>
</section>"""


def render_page(candidates: list[TicketCandidate]) -> str:
    total = len(candidates)
    body = "\n".join(_card(c, n, total) for n, c in enumerate(candidates, 1))
    return (
        _HEAD
        + f"<body>\n{_BAR}<main>\n{body or '<p>No tickets to review.</p>'}\n</main>\n"
        + (f"<script>{_SCRIPT}</script>\n</body>\n</html>\n")
    )


_HEAD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark"><title>Triage review</title>
<style>
:root { --bg:#f6f6f4; --card:#fff; --text:#1d1d1b; --muted:#5d5d58; --line:#d9d9d3;
  --accept:#1a7f37; --reject:#b42318; --code:#f1f1ee; }
@media (prefers-color-scheme: dark) { :root { --bg:#121212; --card:#1c1c1c; --text:#ececea;
  --muted:#a2a29b; --line:#38383a; --accept:#4cc26b; --reject:#ff7b6e; --code:#242424; } }
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
details.out { max-width:760px; margin:12px auto 0; padding:0 16px; }
details.out textarea { width:100%; min-height:8em; font:14px/1.4 ui-monospace,monospace;
  background:var(--code); color:var(--text); border:1px solid var(--line); }
main { max-width:760px; margin:0 auto; padding:16px; }
.card { background:var(--card); border:1px solid var(--line); border-left:6px solid var(--line);
  border-radius:10px; padding:14px 16px; margin:0 0 16px; }
.card.is-a { border-left-color:var(--accept); } .card.is-r { border-left-color:var(--reject); }
.meta { display:flex; flex-wrap:wrap; gap:6px 10px; color:var(--muted); font-size:.85rem; }
.id { font-family:ui-monospace,monospace; color:var(--text); font-weight:600; }
.chip { border:1px solid var(--line); border-radius:999px; padding:0 8px; }
.subject { font-weight:700; margin:10px 0 4px; }
.body { background:var(--code); border-radius:8px; padding:10px 12px; margin:0; font:inherit;
  white-space:pre-wrap; overflow-wrap:anywhere; }
.label { color:var(--muted); font-size:.8rem; text-transform:uppercase;
  letter-spacing:.04em; margin-top:10px; }
.notes { margin:4px 0 0; }
.row { display:flex; flex-wrap:wrap; gap:12px; margin-top:12px; }
.row label { display:flex; flex-direction:column; font-size:.85rem; color:var(--muted);
  flex:1 1 9em; }
.row select { min-height:44px; font:inherit; color:var(--text); background:var(--bg);
  border:1px solid var(--line); border-radius:8px; padding:0 8px; }
.row select.changed { border-color:var(--reject); border-width:2px; }
.actions { display:flex; gap:12px; margin-top:14px; }
.btn { flex:1; min-height:52px; border-radius:10px; border:2px solid var(--line);
  background:var(--bg);
  color:var(--text); font:inherit; font-weight:700; }
.btn.accept { border-color:var(--accept); } .btn.reject { border-color:var(--reject); }
.btn[aria-pressed="true"].accept { background:var(--accept); color:#fff; }
.btn[aria-pressed="true"].reject { background:var(--reject); color:#fff; }
</style></head>
"""

_BAR = """<header class="bar">
<div class="counts" aria-live="polite">Accepted <span id="n-a">0</span> ·
Rejected <span id="n-r">0</span> ·
Pending <span id="n-p">0</span></div>
<button type="button" id="copy">Copy decisions</button><span id="status" role="status"></span>
</header>
<details class="out" id="out"><summary>Show decisions as text</summary>
<textarea id="text" readonly rows="8"></textarea></details>
"""

_SCRIPT = """
(function () {
  var KEY = "sda-triage-review:v1";
  var cards = Array.prototype.slice.call(document.querySelectorAll(".card"));
  var stored = {}, decided = {};
  try { var raw = window.localStorage.getItem(KEY); var p = raw ? JSON.parse(raw) : {};
    if (p && typeof p === "object" && !Array.isArray(p)) stored = p; } catch (e) { stored = {}; }
  function persist() {
    try { window.localStorage.setItem(KEY, JSON.stringify(stored)); } catch (e) {}
  }
  function sel(c, k) { return c.querySelector("select." + k); }
  function line(c) {
    var d = decided[c.dataset.id];
    if (!d) return null;
    return d === "r" ? c.dataset.id + " r"
      : c.dataset.id + " a " + sel(c, "q").value + " " + sel(c, "p").value;
  }
  function paint() {
    var a = 0, r = 0;
    cards.forEach(function (c) {
      var d = decided[c.dataset.id];
      if (d === "a") a++; if (d === "r") r++;
      c.classList.toggle("is-a", d === "a"); c.classList.toggle("is-r", d === "r");
      sel(c, "q").classList.toggle("changed", sel(c, "q").value !== c.dataset.q);
      sel(c, "p").classList.toggle("changed", sel(c, "p").value !== c.dataset.p);
      Array.prototype.forEach.call(c.querySelectorAll(".btn"), function (b) {
        b.setAttribute("aria-pressed", String(b.dataset.act === d)); });
    });
    document.getElementById("n-a").textContent = a;
    document.getElementById("n-r").textContent = r;
    document.getElementById("n-p").textContent = cards.length - a - r;
    document.getElementById("text").value = cards.map(line).filter(Boolean).join("\\n");
  }
  function say(m) { document.getElementById("status").textContent = m; }
  cards.forEach(function (c) {
    var s = stored[c.dataset.id];
    if (s && s.h === c.dataset.h) {
      if (s.q) sel(c, "q").value = s.q; if (s.p) sel(c, "p").value = s.p;
      if (s.d === "a" || s.d === "r") decided[c.dataset.id] = s.d;
    }
    function save() { stored[c.dataset.id] = { d: decided[c.dataset.id], h: c.dataset.h,
      q: sel(c, "q").value, p: sel(c, "p").value }; persist(); paint(); say(""); }
    ["q", "p"].forEach(function (k) { sel(c, k).addEventListener("change", save); });
    Array.prototype.forEach.call(c.querySelectorAll(".btn"), function (b) {
      b.addEventListener("click", function () {
        var id = c.dataset.id;
        if (decided[id] === b.dataset.act) delete decided[id]; else decided[id] = b.dataset.act;
        save();
      });
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
      say(ok ? "Copied." : "Select the text below and copy it."); }
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
    group.add_argument("--export", action="store_true", help="write the phone review page")
    group.add_argument("--apply", metavar="FILE", help="record a decisions file")
    args = parser.parse_args(argv)

    candidates = load_candidates(settings.triage_candidates_path)
    if not candidates:
        print(f"no candidates in {settings.triage_candidates_path}; run sda-eval-triage-draft")
        return 2
    if args.export:
        state = load_state(settings.triage_review_state_path)
        queue = [c for c in candidates if c.id not in state]
        export_html(queue, settings.triage_review_html_path)
        print(f"wrote {settings.triage_review_html_path} ({len(queue)} pending tickets)")
        return 0
    if args.apply:
        report = apply_decisions(Path(args.apply).read_text(encoding="utf-8"), candidates, settings)
        print(
            f"accepted {len(report.accepted)} ({len(report.relabelled)} relabelled), "
            f"rejected {len(report.rejected)}, unchanged {len(report.unchanged)}"
        )
        for problem in report.problems:
            print(f"  problem: {problem}")
    print("\n".join(status_lines(candidates, settings)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
