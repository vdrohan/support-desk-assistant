from pathlib import Path

from support_desk_assistant.ingest.crawl import extract_page, render_markdown

FIXTURE = Path(__file__).parent / "fixtures" / "canada_page.html"
BASE = "https://www.canada.ca/en/services/benefits/example/eligibility.html"


def _page():
    return extract_page(FIXTURE.read_text(encoding="utf-8"), BASE)


def test_metadata():
    page = _page()
    assert page.title == "Example benefit: Eligibility"
    assert page.canonical == BASE
    assert page.date_modified == "2026-08-31"  # meta wins over the footer


def test_main_content_kept_as_markdown():
    md = _page().markdown
    assert md.startswith("# Example benefit: Eligibility\n")
    assert "## Who can apply" in md
    assert "- You are 18 or older" in md
    assert "**meet all**" in md
    assert "| Under $30,000 | $500 |" in md
    assert "applied on time" in md  # link text kept, URL dropped
    assert "](" not in md


def test_boilerplate_removed():
    md = _page().markdown
    for gone in (
        "Skip to main content",
        "Benefits",  # breadcrumb
        "Overview",  # in-page nav
        "Most requested",
        "Report a problem",
        "Page details",
        "2026-08-30",
        "All contacts",  # footer
        "analytics",  # script
    ):
        assert gone not in md, gone


def test_links_normalized_and_deduped():
    links = _page().links
    assert "https://www.canada.ca/en/services/benefits/example/apply.html" in links
    assert links.count("https://www.canada.ca/en/services/benefits/example/apply.html") == 1
    assert "https://www.canada.ca/content/dam/example/guide.pdf" in links
    assert not any(link.startswith("mailto:") for link in links)


def test_date_from_gcds_footer_when_meta_missing():
    html = FIXTURE.read_text(encoding="utf-8").replace('name="dcterms.modified"', 'name="x"')
    assert extract_page(html, BASE).date_modified == "2026-08-30"


def test_multi_page_guide_keeps_single_h1():
    html = """<html><body><main>
      <h1 class="wb-inv" id="wb-cont">EI caregiving – How much you could get</h1>
      <h1 id="gc-document-nav">EI caregiving</h1>
      <h1 class="gc-thickline">How much you could get</h1>
      <p>Body text.</p>
      <section class="provisional most-requested-bullets"><h2>Most requested</h2></section>
      </main></body></html>"""
    page = extract_page(html, BASE)
    assert page.title == "EI caregiving – How much you could get"
    assert page.markdown == "# EI caregiving – How much you could get\n\nBody text.\n"


def test_date_from_legacy_wb_dtmd_footer():
    html = """<html><body><main><h1>T</h1><p>x</p>
      <dl id="wb-dtmd"><dt>Date modified:</dt>
      <dd><time property="dateModified">2024-01-15</time></dd></dl>
      </main></body></html>"""
    assert extract_page(html, BASE).date_modified == "2024-01-15"


def test_missing_date_is_none():
    html = (
        "<html><head><title>T - Canada.ca</title></head><body><main><p>x</p></main></body></html>"
    )
    page = extract_page(html, BASE)
    assert page.date_modified is None
    assert page.title == "T"
    assert page.markdown.startswith("# T\n")


def test_render_markdown_has_attribution_header():
    out = render_markdown(BASE, _page())
    assert out.startswith(f"Source: {BASE}\nDate modified: 2026-08-31\n\n# ")
