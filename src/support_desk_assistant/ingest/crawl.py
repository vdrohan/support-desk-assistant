"""Polite crawler for the canada.ca corpus.

For each program seed, crawl breadth-first within the seed's URL path prefix,
cache raw HTML under ``data/raw/<program>/``, extract the ``<main>`` content to
Markdown under ``data/processed/<program>/`` and write ``data/manifest.jsonl``.

    uv run python -m support_desk_assistant.ingest.crawl [--program ei] [--refresh]
"""

import argparse
import hashlib
import json
import logging
import random
import re
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup
from markdownify import MarkdownConverter
from protego import Protego

from support_desk_assistant.config import CrawlSettings, Settings, get_settings

logger = logging.getLogger(__name__)

HTML_SUFFIXES = (".html", ".htm")
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
# Boilerplate inside <main> on canada.ca pages: menus, breadcrumbs, the
# "Report a problem" / "Date modified" block and the "Most requested" box.
REMOVE_SELECTORS = (
    "nav",
    "#wb-bc",
    ".pagedetails",
    ".gc-most-requested",
    ".most-requested-bullets",
    "gcds-date-modified",
    "#wb-dtmd",
    "script",
    "style",
    "noscript",
    "form",
)
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


# --- URLs --------------------------------------------------------------------


def normalize_url(href: str, base: str | None = None) -> str | None:
    """Resolve ``href`` against ``base`` and drop query and fragment.

    Returns None for non-HTTP(S) links (mailto:, tel:, javascript:, ...).
    """
    url = urljoin(base, href.strip()) if base else href.strip()
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return None
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", "", ""))


def scope_prefix(seed: str) -> str:
    """``/en/x/ei.html`` -> ``/en/x/ei/``: the path prefix a program's pages live under."""
    path = urlsplit(seed).path
    return path.removesuffix(".html").removesuffix(".htm").rstrip("/") + "/"


def in_scope(url: str, seed: str) -> bool:
    """Same host as the seed, English, and the seed itself or under its prefix."""
    u, s = urlsplit(url), urlsplit(seed)
    return (
        u.netloc == s.netloc
        and u.path.startswith("/en/")
        and (u.path == s.path or u.path.startswith(scope_prefix(seed)))
    )


def looks_like_html(url: str) -> bool:
    """True for ``*.html``/``*.htm`` or extensionless paths; False for PDFs etc."""
    last = urlsplit(url).path.rsplit("/", 1)[-1]
    return "." not in last or last.lower().endswith(HTML_SUFFIXES)


def slug_for(url: str) -> str:
    """Filesystem-safe, readable name for a page URL."""
    path = urlsplit(url).path.removeprefix("/en/").strip("/")
    slug = path.removesuffix(".html").removesuffix(".htm").replace("/", "__") or "index"
    slug = re.sub(r"[^A-Za-z0-9._-]", "-", slug)
    if len(slug) > 150:
        slug = f"{slug[:120]}-{hashlib.sha1(url.encode()).hexdigest()[:10]}"
    return slug


# --- HTTP ----------------------------------------------------------------------


class Fetcher:
    """Throttled GET with retries and exponential backoff on 429/5xx/transport errors."""

    def __init__(
        self,
        client: httpx.Client,
        cfg: CrawlSettings,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self.cfg = cfg
        self.sleep = sleep
        self.clock = clock
        self._last_request: float | None = None
        self.requests = 0

    def _throttle(self) -> None:
        if self._last_request is not None:
            wait = self.cfg.request_interval_s - (self.clock() - self._last_request)
            if wait > 0:
                self.sleep(wait)
        self._last_request = self.clock()

    def _backoff(self, attempt: int, response: httpx.Response | None) -> float:
        retry_after = response.headers.get("Retry-After", "") if response is not None else ""
        if retry_after.isdigit():
            delay = float(retry_after)
        else:
            delay = self.cfg.backoff_base_s * 2**attempt + random.uniform(0, 0.5)
        return min(delay, self.cfg.max_backoff_s)

    def get(self, url: str) -> httpx.Response:
        for attempt in range(self.cfg.max_retries + 1):
            self._throttle()
            self.requests += 1
            try:
                response = self.client.get(url)
            except httpx.TransportError as exc:
                if attempt == self.cfg.max_retries:
                    raise
                reason, response = repr(exc), None
            else:
                if response.status_code not in RETRY_STATUSES or attempt == self.cfg.max_retries:
                    return response
                reason = f"HTTP {response.status_code}"
            delay = self._backoff(attempt, response)
            logger.warning(
                "retry %d for %s after %s; sleeping %.1fs", attempt + 1, url, reason, delay
            )
            self.sleep(delay)
        raise AssertionError("unreachable")


class RobotsPolicy:
    """robots.txt per host via protego, which supports ``*`` and ``$`` wildcards
    (urllib.robotparser does not; see docs/decisions.md #8)."""

    def __init__(self, fetcher: Fetcher, user_agent: str) -> None:
        self.fetcher = fetcher
        self.user_agent = user_agent
        self._parsers: dict[str, Protego] = {}

    def _parser_for(self, url: str) -> Protego:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._parsers:
            self._parsers[origin] = self._load(origin)
        return self._parsers[origin]

    def _load(self, origin: str) -> Protego:
        try:
            response = self.fetcher.get(f"{origin}/robots.txt")
        except httpx.HTTPError as exc:
            logger.error("robots.txt for %s unreachable (%r); disallowing all", origin, exc)
            return Protego.parse("User-agent: *\nDisallow: /")
        if response.status_code == 200:
            return Protego.parse(response.text)
        if 400 <= response.status_code < 500:
            return Protego.parse("")  # no robots.txt: everything allowed
        logger.error("robots.txt for %s returned %d; disallowing all", origin, response.status_code)
        return Protego.parse("User-agent: *\nDisallow: /")

    def allowed(self, url: str) -> bool:
        return self._parser_for(url).can_fetch(url, self.user_agent)


# --- Extraction ----------------------------------------------------------------


@dataclass
class ExtractedPage:
    title: str
    date_modified: str | None
    markdown: str
    canonical: str | None
    links: list[str]


class _Converter(MarkdownConverter):
    """ATX headings, '-' bullets, links reduced to their text (URLs cost tokens
    and the source URL is recorded per page), images dropped."""

    def __init__(self) -> None:
        super().__init__(heading_style="ATX", bullets="-", strip=["a", "img"])


def _date_modified(soup: BeautifulSoup) -> str | None:
    candidates = []
    meta = soup.find("meta", attrs={"name": "dcterms.modified"})
    if meta and meta.get("content"):
        candidates.append(meta["content"])
    if tag := soup.find("gcds-date-modified"):
        candidates.append(tag.get_text())
    if tag := soup.select_one("#wb-dtmd time"):
        candidates.append(tag.get("datetime") or tag.get_text())
    for text in candidates:
        if match := DATE_RE.search(text):
            return match.group()
    return None


def _title(soup: BeautifulSoup, main) -> str:
    if h1 := main.find("h1"):
        return h1.get_text(" ", strip=True)
    if soup.title and soup.title.string:
        return soup.title.string.strip().removesuffix(" - Canada.ca").strip()
    return ""


def _clean_markdown(text: str) -> str:
    lines = [line.rstrip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip() + "\n"


def extract_page(html: str, base_url: str) -> ExtractedPage:
    """Parse a canada.ca page: metadata, outgoing links and ``<main>`` as Markdown."""
    soup = BeautifulSoup(html, "lxml")

    canonical = None
    if (link := soup.find("link", rel="canonical")) and link.get("href"):
        canonical = normalize_url(link["href"], base_url)
    links = [
        url for a in soup.find_all("a", href=True) if (url := normalize_url(a["href"], base_url))
    ]
    date_modified = _date_modified(soup)  # before .pagedetails is removed

    main = soup.find("main") or soup.body or soup
    for selector in REMOVE_SELECTORS:
        for tag in main.select(selector):
            tag.decompose()
    # Multi-page guides carry several <h1>s (hidden full title, guide name, section
    # name). The first is the most complete; keep it as the only top heading.
    title = _title(soup, main)
    for h1 in main.find_all("h1"):
        h1.decompose()
    body = _clean_markdown(f"# {title}\n\n" + _Converter().convert_soup(main))
    return ExtractedPage(title, date_modified, body, canonical, list(dict.fromkeys(links)))


def render_markdown(url: str, page: ExtractedPage) -> str:
    """Processed file: attribution header (canada.ca terms) followed by the content."""
    header = f"Source: {url}\nDate modified: {page.date_modified or 'unknown'}\n\n"
    return header + page.markdown


# --- Cache ---------------------------------------------------------------------


@dataclass
class RawPage:
    final_url: str
    html: str
    fetched_at: str


class FetchError(Exception):
    pass


class NotHTML(Exception):
    pass


def load_or_fetch(url: str, cache_dir: Path, fetcher: Fetcher, refresh: bool) -> RawPage:
    """Return the cached page, or fetch, cache and return it."""
    html_path = cache_dir / f"{slug_for(url)}.html"
    meta_path = html_path.with_suffix(".meta.json")
    if not refresh and html_path.exists() and meta_path.exists():
        meta = json.loads(meta_path.read_text())
        return RawPage(meta["final_url"], html_path.read_text(encoding="utf-8"), meta["fetched_at"])

    try:
        response = fetcher.get(url)
    except httpx.HTTPError as exc:
        raise FetchError(repr(exc)) from exc
    if response.status_code != 200:
        raise FetchError(f"HTTP {response.status_code}")
    content_type = response.headers.get("content-type", "")
    if "text/html" not in content_type:
        raise NotHTML(content_type or "no content-type")

    page = RawPage(str(response.url), response.text, _utc_now())
    cache_dir.mkdir(parents=True, exist_ok=True)
    html_path.write_text(page.html, encoding="utf-8")
    meta_path.write_text(
        json.dumps({"url": url, "final_url": page.final_url, "fetched_at": page.fetched_at}) + "\n"
    )
    return page


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# --- Crawl ---------------------------------------------------------------------


@dataclass
class ProgramReport:
    program: str
    saved: int = 0
    skipped_robots: list[str] = field(default_factory=list)
    skipped_canonical: list[str] = field(default_factory=list)
    skipped_non_html: list[str] = field(default_factory=list)
    skipped_duplicate: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    cap_hit: bool = False
    null_date_modified: list[str] = field(default_factory=list)
    requests: int = 0


def crawl_program(
    program: str,
    seed: str,
    *,
    settings: Settings,
    fetcher: Fetcher,
    robots: RobotsPolicy,
    refresh: bool = False,
) -> tuple[list[dict], ProgramReport]:
    """Breadth-first crawl of one program. Returns manifest records and a report."""
    cfg = settings.crawl
    report = ProgramReport(program)
    raw_dir = settings.raw_dir / program
    out_dir = settings.processed_dir / program
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob("*.md"):
        stale.unlink()
    requests_before = fetcher.requests

    def skip(bucket: list[str], url: str, reason: str) -> None:
        bucket.append(url)
        logger.info("[%s] skip %s: %s", program, url, reason)

    seed = normalize_url(seed)
    queue: deque[tuple[str, int]] = deque([(seed, 0)])
    seen = {seed}
    saved: set[str] = set()
    records: list[dict] = []

    while queue and len(records) < cfg.max_pages_per_program:
        url, depth = queue.popleft()
        if not robots.allowed(url):
            skip(report.skipped_robots, url, "disallowed by robots.txt")
            continue
        try:
            raw = load_or_fetch(url, raw_dir, fetcher, refresh)
        except NotHTML as exc:
            skip(report.skipped_non_html, url, f"non-HTML response ({exc})")
            continue
        except FetchError as exc:
            report.failed.append(url)
            logger.warning("[%s] failed %s: %s", program, url, exc)
            continue

        page = extract_page(raw.html, raw.final_url)
        canonical = page.canonical or normalize_url(raw.final_url)
        if not in_scope(canonical, seed):
            skip(report.skipped_canonical, url, f"canonical {canonical} outside prefix")
            continue
        if canonical in saved:
            skip(report.skipped_duplicate, url, f"duplicate of {canonical}")
            continue
        saved.add(canonical)

        content = render_markdown(canonical, page)
        (out_dir / f"{slug_for(canonical)}.md").write_text(content, encoding="utf-8")
        records.append(
            {
                "url": canonical,
                "title": page.title,
                "program": program,
                "date_modified": page.date_modified,
                "crawled_at": raw.fetched_at,
                "content_hash": "sha256:" + hashlib.sha256(content.encode()).hexdigest(),
            }
        )
        if page.date_modified is None:
            report.null_date_modified.append(canonical)
        logger.info("[%s] saved (depth %d) %s", program, depth, canonical)

        if depth >= cfg.max_depth:
            continue
        for link in page.links:
            if link in seen or not in_scope(link, seed):
                continue
            seen.add(link)
            if not looks_like_html(link):
                skip(report.skipped_non_html, link, "non-HTML link")
                continue
            queue.append((link, depth + 1))

    report.saved = len(records)
    report.cap_hit = len(records) >= cfg.max_pages_per_program and bool(queue)
    report.requests = fetcher.requests - requests_before
    return records, report


# --- Manifest ------------------------------------------------------------------


def read_manifest(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def write_manifest(path: Path, records: Iterable[dict]) -> None:
    """Sorted by (program, url) so re-runs give stable diffs."""
    rows = sorted(records, key=lambda r: (r["program"], r["url"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


def merge_manifest(existing: list[dict], new: list[dict], programs: Iterable[str]) -> list[dict]:
    """Replace the records of the crawled programs, keep the others."""
    crawled = set(programs)
    return [r for r in existing if r["program"] not in crawled] + new


# --- CLI -----------------------------------------------------------------------


def _dir_size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) if path.exists() else 0


def _format_reports(reports: list[ProgramReport], settings: Settings) -> Iterator[str]:
    cols = "program saved robots canon non_html dup failed cap_hit null_date requests"
    yield "{:<9} {:>5} {:>6} {:>5} {:>8} {:>3} {:>6} {:>7} {:>9} {:>8}".format(*cols.split())
    for r in reports:
        yield (
            f"{r.program:<9} {r.saved:>5} {len(r.skipped_robots):>6} "
            f"{len(r.skipped_canonical):>5} {len(r.skipped_non_html):>8} "
            f"{len(r.skipped_duplicate):>3} {len(r.failed):>6} {str(r.cap_hit):>7} "
            f"{len(r.null_date_modified):>9} {r.requests:>8}"
        )
    for r in reports:
        for url in r.failed:
            yield f"FAILED [{r.program}] {url}"
    raw, processed = _dir_size(settings.raw_dir), _dir_size(settings.processed_dir)
    yield f"total saved: {sum(r.saved for r in reports)} pages"
    yield f"size: raw {raw / 1e6:.1f} MB, processed {processed / 1e6:.2f} MB"


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    cfg = settings.crawl
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--program", action="append", choices=sorted(cfg.seeds), help="repeatable; default all"
    )
    parser.add_argument("--max-pages", type=int, default=cfg.max_pages_per_program)
    parser.add_argument("--max-depth", type=int, default=cfg.max_depth)
    parser.add_argument("--refresh", action="store_true", help="re-download cached pages")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    settings = settings.model_copy(
        update={
            "crawl": cfg.model_copy(
                update={"max_pages_per_program": args.max_pages, "max_depth": args.max_depth}
            )
        }
    )
    programs = args.program or list(cfg.seeds)

    headers = {"User-Agent": cfg.user_agent}
    with httpx.Client(headers=headers, timeout=cfg.timeout_s, follow_redirects=True) as client:
        fetcher = Fetcher(client, settings.crawl)
        robots = RobotsPolicy(fetcher, cfg.user_agent)
        records, reports = [], []
        for program in programs:
            program_records, report = crawl_program(
                program,
                cfg.seeds[program],
                settings=settings,
                fetcher=fetcher,
                robots=robots,
                refresh=args.refresh,
            )
            records += program_records
            reports.append(report)

    existing = read_manifest(settings.manifest_path)
    write_manifest(settings.manifest_path, merge_manifest(existing, records, programs))
    for line in _format_reports(reports, settings):
        print(line)
    return 1 if any(r.failed for r in reports) else 0


if __name__ == "__main__":
    raise SystemExit(main())
