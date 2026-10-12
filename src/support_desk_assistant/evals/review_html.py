"""The phone-friendly review page written by ``sda-eval-review --export``.

One self-contained HTML file: inline CSS and JS, no external assets, so it opens from the
Files app or a chat attachment with no network. Pure view; ``review.py`` gathers the data.

Everything is escaped here except ``ExcerptView.body_html``, which the caller must have
escaped (``review.highlight_html`` does it). Decisions live in memory and are mirrored to
``localStorage`` (every access wrapped, since private modes and some file viewers throw);
"Copy decisions" copies ``<candidate id> a|r`` lines for ``sda-eval-review --apply``.
"""

from dataclasses import dataclass, field
from html import escape


@dataclass(frozen=True)
class ExcerptView:
    role: str  # "GOLD" or "SOURCE (not gold)"
    program: str
    title: str
    url: str
    date_modified: str | None
    body_html: str  # already escaped, key facts wrapped in <mark>


@dataclass(frozen=True)
class MatchView:
    program: str
    title: str
    url: str
    score: float
    text: str


@dataclass(frozen=True)
class CardView:
    id: str
    question: str
    category: str
    answerable: bool
    key_facts: list[str]
    notes: str
    # Changes when the question, facts or gold pages change, so a decision stored on the
    # phone for an older version of the same id is not applied to the new one.
    fingerprint: str
    problems: list[str] = field(default_factory=list)  # non-empty: Accept is disabled
    excerpts: list[ExcerptView] = field(default_factory=list)
    matches: list[MatchView] = field(default_factory=list)


def _link(url: str, label: str) -> str:
    return f'<a href="{escape(url, quote=True)}" rel="noreferrer">{escape(label)}</a>'


def _card(c: CardView, number: int, total: int) -> str:
    parts = [
        f'<article class="card" data-id="{escape(c.id, quote=True)}"'
        f' data-h="{escape(c.fingerprint, quote=True)}">',
        '<div class="meta">',
        f'<span class="count">{number}/{total}</span>',
        f'<span class="id">{escape(c.id)}</span>',
        f'<span class="chip">{escape(c.category)}</span>',
        f'<span class="chip {"ok" if c.answerable else "abstain"}">'
        f"{'answerable' if c.answerable else 'should abstain'}</span>",
        "</div>",
        f'<p class="q">{escape(c.question)}</p>',
    ]
    if c.key_facts:
        facts = "".join(f"<li>{escape(f)}</li>" for f in c.key_facts)
        parts.append(f'<div class="label">Key facts</div><ul class="facts">{facts}</ul>')
    if c.notes:
        parts.append(f'<div class="label">Notes</div><p class="notes">{escape(c.notes)}</p>')
    if c.problems:
        items = "".join(f"<li>{escape(p)}</li>" for p in c.problems)
        parts.append(
            f'<div class="problems"><strong>Auto-check failed, so Accept is disabled:</strong>'
            f"<ul>{items}</ul></div>"
        )
    for e in c.excerpts:
        modified = f" · modified {escape(e.date_modified)}" if e.date_modified else ""
        gold = " gold" if e.role == "GOLD" else ""
        parts.append(
            f'<section class="page{gold}"><div class="page-head">'
            f"<strong>{escape(e.role)}</strong> · [{escape(e.program)}] {escape(e.title)}"
            f"{modified}<br>{_link(e.url, e.url)}</div>"
            f'<pre class="excerpt">{e.body_html}</pre></section>'
        )
    if c.matches:
        parts.append(
            '<div class="label">Closest pages elsewhere in the corpus '
            "(is it answered anywhere?)</div>"
        )
        for m in c.matches:
            parts.append(
                f'<section class="page"><div class="page-head">[{escape(m.program)}] '
                f"{escape(m.title)} · score {m.score:.1f}<br>{_link(m.url, m.url)}</div>"
                f'<pre class="excerpt">{escape(m.text)}</pre></section>'
            )
    elif not c.answerable:
        parts.append('<div class="label">Closest pages elsewhere</div><p>(no keyword overlap)</p>')
    disabled = " disabled" if c.problems else ""
    parts.append(
        '<div class="actions">'
        f'<button type="button" class="btn accept" data-act="a" aria-pressed="false"{disabled}>'
        "Accept</button>"
        '<button type="button" class="btn reject" data-act="r" aria-pressed="false">Reject</button>'
        "</div></article>"
    )
    return "\n".join(parts)


def render_review_page(cards: list[CardView]) -> str:
    total = len(cards)
    body = "\n".join(_card(c, n, total) for n, c in enumerate(cards, 1))
    if not cards:
        body = "<p>No candidates to review.</p>"
    return (
        _HEAD
        + f'<body data-total="{total}">\n'
        + _BAR
        + f"<main>\n{body}\n</main>\n<script>{_SCRIPT}</script>\n</body>\n</html>\n"
    )


_HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Eval review</title>
<style>
:root {
  --bg: #f6f6f4; --card: #fff; --text: #1d1d1b; --muted: #5d5d58; --line: #d9d9d3;
  --accept: #1a7f37; --reject: #b42318; --mark: #ffe58a; --warn-bg: #fdecea;
  --code-bg: #f1f1ee;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #121212; --card: #1c1c1c; --text: #ececea; --muted: #a2a29b; --line: #38383a;
    --accept: #4cc26b; --reject: #ff7b6e; --mark: #6b5a00; --warn-bg: #3a1c19;
    --code-bg: #242424;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 16px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
.bar { position: sticky; top: 0; z-index: 2; background: var(--card);
  border-bottom: 1px solid var(--line); padding: 8px 16px; display: flex; flex-wrap: wrap;
  gap: 4px 12px; align-items: center; }
.counts { font-weight: 600; flex: 1 1 12em; font-size: 0.95rem; }
.counts .a { color: var(--accept); } .counts .r { color: var(--reject); }
.bar button { min-height: 44px; padding: 0 16px; border-radius: 8px; border: 1px solid var(--line);
  background: var(--bg); color: var(--text); font: inherit; font-weight: 600; }
#status { color: var(--muted); font-size: 0.9rem; flex-basis: 100%; }
#status:empty { display: none; }
details.out { max-width: 760px; margin: 12px auto 0; padding: 0 16px; }
details.out textarea { width: 100%; min-height: 8em; font: 14px/1.4 ui-monospace, monospace;
  background: var(--code-bg); color: var(--text); border: 1px solid var(--line); }
main { max-width: 760px; margin: 0 auto; padding: 16px; }
.card { background: var(--card); border: 1px solid var(--line); border-left: 6px solid var(--line);
  border-radius: 10px; padding: 14px 16px; margin: 0 0 16px; }
.card.is-a { border-left-color: var(--accept); }
.card.is-r { border-left-color: var(--reject); }
.meta { display: flex; flex-wrap: wrap; gap: 6px 8px; align-items: center;
  color: var(--muted); font-size: 0.85rem; }
.id { font-family: ui-monospace, monospace; color: var(--text); font-weight: 600; }
.chip { border: 1px solid var(--line); border-radius: 999px; padding: 0 8px; }
.chip.abstain { border-color: var(--reject); color: var(--reject); }
.q { font-size: 1.15rem; font-weight: 600; margin: 10px 0; }
.label { color: var(--muted); font-size: 0.8rem; text-transform: uppercase;
  letter-spacing: 0.04em; margin-top: 10px; }
.facts { margin: 4px 0 0; padding-left: 1.2em; }
.notes { margin: 4px 0 0; }
.problems { background: var(--warn-bg); border: 1px solid var(--reject); border-radius: 8px;
  padding: 8px 12px; margin-top: 10px; }
.problems ul { margin: 4px 0 0; padding-left: 1.2em; }
.page { margin-top: 12px; }
.page-head { font-size: 0.85rem; color: var(--muted); overflow-wrap: anywhere; }
.page.gold .page-head strong { color: var(--accept); }
.excerpt { background: var(--code-bg); border-radius: 8px; padding: 10px 12px; margin: 6px 0 0;
  white-space: pre-wrap; overflow-wrap: anywhere; font: inherit; font-size: 0.92rem; }
mark { background: var(--mark); color: inherit; border-radius: 3px; padding: 0 1px; }
.actions { display: flex; gap: 12px; margin-top: 14px; }
.btn { flex: 1; min-height: 52px; border-radius: 10px; border: 2px solid var(--line);
  background: var(--bg); color: var(--text); font: inherit; font-weight: 700; }
.btn.accept { border-color: var(--accept); } .btn.reject { border-color: var(--reject); }
.btn[aria-pressed="true"].accept { background: var(--accept); color: #fff; }
.btn[aria-pressed="true"].reject { background: var(--reject); color: #fff; }
.btn:disabled { opacity: 0.4; }
</style>
</head>
"""

_BAR = """<header class="bar">
<div class="counts" aria-live="polite">
<span class="a">Accepted <span id="n-a">0</span></span> ·
<span class="r">Rejected <span id="n-r">0</span></span> ·
Pending <span id="n-p">0</span>
</div>
<button type="button" id="copy">Copy decisions</button>
<span id="status" role="status"></span>
</header>
<details class="out" id="out"><summary>Show decisions as text</summary>
<textarea id="text" readonly rows="8"></textarea></details>
"""

_SCRIPT = """
(function () {
  var KEY = "sda-eval-review:v1";
  var cards = Array.prototype.slice.call(document.querySelectorAll(".card"));
  var stored = {};      // id -> {d: "a"|"r", h: fingerprint}; also holds ids not on this page
  var decided = {};     // id -> "a"|"r" for cards on this page
  try {
    var raw = window.localStorage.getItem(KEY);
    var parsed = raw ? JSON.parse(raw) : {};
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) stored = parsed;
  } catch (e) { stored = {}; }

  function persist() {
    try { window.localStorage.setItem(KEY, JSON.stringify(stored)); } catch (e) {}
  }
  function byId(id) { return document.getElementById("n-" + id); }
  function lines() {
    return cards.map(function (c) {
      var d = decided[c.dataset.id];
      return d ? c.dataset.id + " " + d : null;
    }).filter(Boolean).join("\\n");
  }
  function paint() {
    var a = 0, r = 0;
    cards.forEach(function (c) {
      var d = decided[c.dataset.id];
      if (d === "a") a++;
      if (d === "r") r++;
      c.classList.toggle("is-a", d === "a");
      c.classList.toggle("is-r", d === "r");
      Array.prototype.forEach.call(c.querySelectorAll(".btn"), function (b) {
        b.setAttribute("aria-pressed", String(b.dataset.act === d));
      });
    });
    byId("a").textContent = a;
    byId("r").textContent = r;
    byId("p").textContent = cards.length - a - r;
    document.getElementById("text").value = lines();
  }
  function say(msg) { document.getElementById("status").textContent = msg; }

  cards.forEach(function (c) {
    var s = stored[c.dataset.id];
    if (s && s.h === c.dataset.h && (s.d === "a" || s.d === "r")) decided[c.dataset.id] = s.d;
    Array.prototype.forEach.call(c.querySelectorAll(".btn"), function (b) {
      b.addEventListener("click", function () {
        var id = c.dataset.id;
        if (decided[id] === b.dataset.act) {      // tap again to undo
          delete decided[id];
          delete stored[id];
        } else {
          decided[id] = b.dataset.act;
          stored[id] = { d: b.dataset.act, h: c.dataset.h };
        }
        persist();
        paint();
        say("");
      });
    });
  });

  document.getElementById("copy").addEventListener("click", function () {
    var text = lines();
    if (!text) { say("Nothing decided yet."); return; }
    function manual() {
      var box = document.getElementById("text");
      document.getElementById("out").open = true;
      box.focus();
      box.select();
      var ok = false;
      try { ok = document.execCommand("copy"); } catch (e) {}
      say(ok ? "Copied." : "Select the text below and copy it.");
    }
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function () { say("Copied."); }, manual);
        return;
      }
    } catch (e) {}
    manual();
  });

  paint();
})();
"""
