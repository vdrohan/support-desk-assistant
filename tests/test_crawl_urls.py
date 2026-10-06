import pytest

from support_desk_assistant.ingest.crawl import (
    in_scope,
    looks_like_html,
    normalize_url,
    scope_prefix,
    slug_for,
)

SEED = "https://www.canada.ca/en/services/benefits/ei.html"


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        (
            "/en/services/benefits/ei/apply.html#step-2",
            "https://www.canada.ca/en/services/benefits/ei/apply.html",
        ),
        (
            "ei/apply.html?utm_source=x&a=1",
            "https://www.canada.ca/en/services/benefits/ei/apply.html",
        ),
        ("HTTPS://WWW.Canada.CA/en/Page.html", "https://www.canada.ca/en/Page.html"),
        ("  /en/x.html  ", "https://www.canada.ca/en/x.html"),
        ("https://www.canada.ca", "https://www.canada.ca/"),
        ("#top", "https://www.canada.ca/en/services/benefits/ei.html"),
        ("mailto:someone@example.org", None),
        ("tel:18002067218", None),
        ("javascript:void(0)", None),
    ],
)
def test_normalize_url(href, expected):
    assert normalize_url(href, SEED) == expected


def test_scope_prefix():
    assert scope_prefix(SEED) == "/en/services/benefits/ei/"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (SEED, True),
        ("https://www.canada.ca/en/services/benefits/ei/ei-regular-benefit.html", True),
        ("https://www.canada.ca/en/services/benefits/ei/a/b/c.html", True),
        ("https://www.canada.ca/en/services/benefits/eiextra.html", False),  # prefix, not substring
        ("https://www.canada.ca/en/services/benefits/publicpensions.html", False),
        ("https://www.canada.ca/fr/services/prestations/ae.html", False),
        ("https://other.example.org/en/services/benefits/ei/x.html", False),
    ],
)
def test_in_scope(url, expected):
    assert in_scope(url, SEED) is expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.canada.ca/en/x.html", True),
        ("https://www.canada.ca/en/x.HTM", True),
        ("https://www.canada.ca/en/x/", True),
        ("https://www.canada.ca/en/x", True),
        ("https://www.canada.ca/content/dam/x/guide.pdf", False),
        ("https://www.canada.ca/en/x/form.docx", False),
    ],
)
def test_looks_like_html(url, expected):
    assert looks_like_html(url) is expected


def test_slug_for():
    assert slug_for("https://www.canada.ca/en/services/benefits/ei/apply.html") == (
        "services__benefits__ei__apply"
    )
    long_url = "https://www.canada.ca/en/" + "a" * 300 + ".html"
    assert len(slug_for(long_url)) <= 131
