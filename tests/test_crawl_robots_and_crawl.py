"""Robots handling, retries and an end-to-end crawl against httpx.MockTransport."""

import json

import httpx
import pytest

from support_desk_assistant.config import CrawlSettings, Settings
from support_desk_assistant.ingest.crawl import (
    Fetcher,
    RobotsPolicy,
    crawl_program,
    merge_manifest,
    read_manifest,
    write_manifest,
)

HOST = "https://www.canada.ca"
SEED = f"{HOST}/en/prog.html"
UA = "test-bot/1.0"

ROBOTS = """\
User-agent: *
Disallow: /en/*/private.html
Disallow: /en/prog/blocked/
"""


def page(title, links=(), canonical=None, date="2026-01-01"):
    canon = f'<link rel="canonical" href="{canonical}">' if canonical else ""
    meta = f'<meta name="dcterms.modified" content="{date}">' if date else ""
    anchors = "".join(f'<a href="{href}">{href}</a>' for href in links)
    body = f"<main><h1>{title}</h1><p>{anchors}</p></main>"
    return f"<html><head>{canon}{meta}</head><body>{body}</body></html>"


SITE = {
    "/robots.txt": ROBOTS,
    "/en/prog.html": page(
        "Seed",
        [
            "/en/prog/a.html#frag",
            "/en/prog/a.html?x=1",
            "/en/prog/guide.pdf",
            "/en/prog/private.html",
            "/en/prog/blocked/x.html",
            "/en/prog/moved.html",
            "/en/prog/dup.html",
            "/en/prog/flaky.html",
            "/en/prog/nodate.html",
            "/en/other/outside.html",
            "/fr/prog/a.html",
        ],
        canonical=SEED,
    ),
    "/en/prog/a.html": page("A", ["/en/prog/a/deep.html"]),
    "/en/prog/a/deep.html": page("Deep", ["/en/prog/too-deep.html"]),
    "/en/prog/too-deep.html": page("Too deep"),
    "/en/prog/moved.html": page("Moved", canonical=f"{HOST}/en/other/moved.html"),
    "/en/prog/dup.html": page("Dup", canonical=f"{HOST}/en/prog/a.html"),
    "/en/prog/flaky.html": page("Flaky"),
    "/en/prog/nodate.html": page("No date", date=None),
}


class Site:
    def __init__(self, flaky_failures=1):
        self.calls: list[str] = []
        self.flaky_failures = flaky_failures

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        if path == "/en/prog/flaky.html" and self.flaky_failures > 0:
            self.flaky_failures -= 1
            return httpx.Response(503, headers={"Retry-After": "1"})
        if path.endswith(".pdf"):
            return httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf"})
        if path not in SITE:
            return httpx.Response(404)
        ctype = "text/plain" if path == "/robots.txt" else "text/html; charset=utf-8"
        return httpx.Response(200, text=SITE[path], headers={"content-type": ctype})


@pytest.fixture
def sleeps():
    return []


def make(tmp_path, sleeps, site=None, **crawl):
    site = site or Site()
    cfg = CrawlSettings(seeds={"prog": SEED}, user_agent=UA, **crawl)
    settings = Settings(data_dir=tmp_path, crawl=cfg)
    client = httpx.Client(transport=httpx.MockTransport(site), base_url=HOST)
    fetcher = Fetcher(client, cfg, sleep=sleeps.append)
    return settings, fetcher, RobotsPolicy(fetcher, UA), site


# --- robots ----------------------------------------------------------------------


def test_robots_wildcard_and_prefix_rules(tmp_path, sleeps):
    _, _, robots, _ = make(tmp_path, sleeps)
    assert robots.allowed(f"{HOST}/en/prog/a.html")
    assert not robots.allowed(f"{HOST}/en/prog/private.html")  # wildcard rule
    assert not robots.allowed(f"{HOST}/en/anything/private.html")
    assert not robots.allowed(f"{HOST}/en/prog/blocked/x.html")


def test_robots_404_allows_all(tmp_path, sleeps):
    site = lambda request: httpx.Response(404)  # noqa: E731
    _, _, robots, _ = make(tmp_path, sleeps, site=site)
    assert robots.allowed(f"{HOST}/en/prog/private.html")


def test_robots_5xx_disallows_all(tmp_path, sleeps):
    site = lambda request: httpx.Response(503)  # noqa: E731
    _, _, robots, _ = make(tmp_path, sleeps, site=site, max_retries=0)
    assert not robots.allowed(f"{HOST}/en/prog/a.html")


def test_robots_fetched_once_per_host(tmp_path, sleeps):
    _, _, robots, site = make(tmp_path, sleeps)
    robots.allowed(f"{HOST}/en/a.html")
    robots.allowed(f"{HOST}/en/b.html")
    assert site.calls.count("/robots.txt") == 1


# --- fetcher ---------------------------------------------------------------------


def test_retry_honours_retry_after_then_succeeds(tmp_path, sleeps):
    _, fetcher, _, site = make(tmp_path, sleeps, site=Site(flaky_failures=2), request_interval_s=0)
    response = fetcher.get(f"{HOST}/en/prog/flaky.html")
    assert response.status_code == 200
    assert site.calls.count("/en/prog/flaky.html") == 3
    assert sleeps == [1.0, 1.0]


def test_retry_gives_up_after_max_retries(tmp_path, sleeps):
    site = Site(flaky_failures=10)
    _, fetcher, _, _ = make(tmp_path, sleeps, site=site, max_retries=2, request_interval_s=0)
    assert fetcher.get(f"{HOST}/en/prog/flaky.html").status_code == 503
    assert site.calls.count("/en/prog/flaky.html") == 3


def test_throttle_spaces_requests():
    now = [0.0]
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    fetcher = Fetcher(
        client, CrawlSettings(request_interval_s=1.0), sleep=sleep, clock=lambda: now[0]
    )
    fetcher.get(f"{HOST}/a")
    now[0] += 0.25
    fetcher.get(f"{HOST}/b")
    assert slept == [0.75]


# --- crawl -----------------------------------------------------------------------


def test_crawl_program_end_to_end(tmp_path, sleeps):
    settings, fetcher, robots, site = make(tmp_path, sleeps)
    records, report = crawl_program("prog", SEED, settings=settings, fetcher=fetcher, robots=robots)

    urls = {r["url"] for r in records}
    assert urls == {
        SEED,
        f"{HOST}/en/prog/a.html",
        f"{HOST}/en/prog/a/deep.html",  # depth 2
        f"{HOST}/en/prog/flaky.html",  # after one retry
        f"{HOST}/en/prog/nodate.html",
    }
    assert f"{HOST}/en/prog/too-deep.html" not in urls  # depth 3
    assert site.calls.count("/en/prog/a.html") == 1  # fragment/query variants deduped
    assert "/fr/prog/a.html" not in site.calls
    assert "/en/other/outside.html" not in site.calls

    assert report.saved == 5
    assert report.skipped_robots == [
        f"{HOST}/en/prog/private.html",
        f"{HOST}/en/prog/blocked/x.html",
    ]
    assert report.skipped_canonical == [f"{HOST}/en/prog/moved.html"]
    assert report.skipped_duplicate == [f"{HOST}/en/prog/dup.html"]
    assert report.skipped_non_html == [f"{HOST}/en/prog/guide.pdf"]
    assert report.null_date_modified == [f"{HOST}/en/prog/nodate.html"]
    assert report.failed == []
    assert report.cap_hit is False

    nodate = next(r for r in records if r["url"].endswith("nodate.html"))
    assert nodate["date_modified"] is None
    assert set(nodate) == {"url", "title", "program", "date_modified", "crawled_at", "content_hash"}
    assert (
        (tmp_path / "processed" / "prog" / "prog__a.md")
        .read_text()
        .startswith(f"Source: {HOST}/en/prog/a.html\n")
    )


def test_cap_hit(tmp_path, sleeps):
    settings, fetcher, robots, _ = make(tmp_path, sleeps, max_pages_per_program=2)
    records, report = crawl_program("prog", SEED, settings=settings, fetcher=fetcher, robots=robots)
    assert len(records) == 2
    assert report.cap_hit is True


def test_rerun_uses_cache_and_gives_identical_records(tmp_path, sleeps):
    settings, fetcher, robots, site = make(tmp_path, sleeps)
    first, _ = crawl_program("prog", SEED, settings=settings, fetcher=fetcher, robots=robots)
    calls_after_first = len(site.calls)

    second, _ = crawl_program("prog", SEED, settings=settings, fetcher=fetcher, robots=robots)
    assert second == first  # same crawled_at and hashes -> stable manifest
    assert len(site.calls) == calls_after_first  # nothing re-downloaded

    crawl_program("prog", SEED, settings=settings, fetcher=fetcher, robots=robots, refresh=True)
    assert len(site.calls) > calls_after_first


def test_manifest_merge_keeps_other_programs_and_sorts(tmp_path):
    path = tmp_path / "manifest.jsonl"
    write_manifest(path, [{"program": "b", "url": "u2"}, {"program": "a", "url": "u1"}])
    merged = merge_manifest(read_manifest(path), [{"program": "b", "url": "u3"}], ["b"])
    write_manifest(path, merged)
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert lines == [{"program": "a", "url": "u1"}, {"program": "b", "url": "u3"}]
