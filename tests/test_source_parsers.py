"""Per-source scrapers against saved HTML fixtures (tests/fixtures/).

Fixtures are trimmed copies of real pages fetched once (Nov 2026) from
lawgowhere.sg, lab.mlaw.gov.sg and probono.sg, plus a hand-written legacy-LAB
page for the primary content selector. Parsing runs through the real
PoliteClient + httpx.MockTransport, BeautifulSoup and lxml; no network.
"""

from __future__ import annotations

import pytest
from conftest import fixture_text

from sources import lab, lawgowhere, manual, probono

REQUIRED_KEYS = {
    "source_url",
    "title",
    "publisher",
    "publisher_type",
    "topic",
    "audience",
    "resource_kind",
    "is_routing_destination",
    "content_text",
    "license",
    "license_url",
    "attribution",
    "can_store_fulltext",
    "volatility",
    "fragments",
}
FRAGMENT_KEYS = {"heading", "content_type", "content_text"}


def assert_page_shape(page: dict) -> None:
    assert REQUIRED_KEYS <= set(page), REQUIRED_KEYS - set(page)
    assert page["source_url"].startswith("https://")
    assert page["title"]
    assert len(page["content_text"]) >= 80
    assert page["can_store_fulltext"] is False  # all-rights-reserved sources
    assert page["volatility"] in (1, 2, 3)
    assert isinstance(page["is_routing_destination"], bool)
    for frag in page["fragments"]:
        assert FRAGMENT_KEYS <= set(frag)
        assert frag["content_text"].strip()


# --------------------------------------------------------------------- lawgowhere
LGW_ROUTES = {
    "/get-information/": (404, ""),
    "/get-information/issue-type-family/": "lawgowhere_category.html",
    "/get-information/issue-type-family/i-have-problems-with-my-spouse/": "lawgowhere_leaf.html",
    "/get-help/": "lawgowhere_get_help.html",
    "/contact-us/": "lawgowhere_contact.html",
}
LEAF_URL = "https://lawgowhere.sg/get-information/issue-type-family/i-have-problems-with-my-spouse/"


def test_lawgowhere_discovers_leaf_urls_from_category_grid(make_client):
    leaves = lawgowhere._discover_info_leaves(make_client(LGW_ROUTES))
    assert leaves == [
        "https://lawgowhere.sg/get-information/issue-type-family/i-have-problems-with-my-spouse/",
        "https://lawgowhere.sg/get-information/issue-type-family/someone-in-my-family-died-suddenly/",
    ]


def test_lawgowhere_leaf_page(make_client):
    page = lawgowhere._parse_info_leaf(make_client(LGW_ROUTES), LEAF_URL)
    assert_page_shape(page)
    assert page["source_url"] == LEAF_URL
    assert page["topic"] == "family"
    assert page["resource_kind"] == "rights_explainer"
    assert page["is_routing_destination"] is False
    assert page["license"] == "All rights reserved"
    assert page["fragments"], "accordion Q&A should become fragments"
    assert {f["content_type"] for f in page["fragments"]} == {"faq"}
    assert all(f["heading"] for f in page["fragments"])


def test_lawgowhere_get_help_page(make_client):
    page = lawgowhere._parse_get_help(make_client(LGW_ROUTES))
    assert_page_shape(page)
    assert page["source_url"] == lawgowhere.GET_HELP_URL
    assert page["is_routing_destination"] is True
    assert page["resource_kind"] == "contact_directory"
    assert "Contacts:" in page["content_text"]  # tel:/mailto: harvested
    assert any("@" in c for c in page["content_text"].split("Contacts:")[1].split(","))
    assert len(page["fragments"]) > 3


def test_lawgowhere_contact_page_one_fragment_per_org(make_client):
    page = lawgowhere._parse_contact_us(make_client(LGW_ROUTES))
    assert_page_shape(page)
    headings = [f["heading"] for f in page["fragments"]]
    assert headings[0] == "Pro Bono SG"
    assert len(headings) == len(set(headings)), "orgs must not be double-matched"
    assert page["is_routing_destination"] is True


def test_lawgowhere_leaf_rejects_thin_or_missing_content(make_client):
    thin = "<html><body><div class='km_web-container'><p>tiny</p></div></body></html>"
    assert (
        lawgowhere._parse_info_leaf(make_client({"/x/": (200, thin)}), "https://lawgowhere.sg/x/")
        is None
    )
    assert lawgowhere._parse_info_leaf(make_client({}), "https://lawgowhere.sg/x/") is None


def test_lawgowhere_topic_and_path_helpers():
    assert (
        lawgowhere._topic_for_url("https://lawgowhere.sg/get-information/issue-type-civil/a/")
        == "civil"
    )
    assert lawgowhere._topic_for_url("https://lawgowhere.sg/other/") == "legal_information"
    assert lawgowhere._is_internal("/get-help/") and not lawgowhere._is_internal("tel:123")
    assert not lawgowhere._is_internal("https://evil.test/x")
    assert lawgowhere._path_depth("https://lawgowhere.sg/a/b/c/") == 3


def test_lawgowhere_fetch_end_to_end(patch_polite_client, capsys):
    seen = patch_polite_client(LGW_ROUTES)
    pages = lawgowhere.fetch()
    urls = [p["source_url"] for p in pages]
    assert LEAF_URL in urls
    assert lawgowhere.GET_HELP_URL in urls and lawgowhere.CONTACT_URL in urls
    for p in pages:
        assert_page_shape(p)
    assert all(r.url.host == "lawgowhere.sg" for r in seen)


# --------------------------------------------------------------------------- lab
LAB_URL = "https://lab.mlaw.gov.sg/legal-services/grant-of-aid/"


def test_lab_sitemap_discovery_filters_pdfs_and_stubs(make_client):
    entries = lab._discover(make_client({"/sitemap.xml": "lab_sitemap.xml"}))
    urls = [e["url"] for e in entries]
    assert urls and all(u.startswith("https://lab.mlaw.gov.sg/") for u in urls)
    assert not any(".pdf" in u or "/files/" in u or "/resource-room" in u for u in urls)
    assert all(e["lastmod"] for e in entries)


def test_lab_sitemap_discovery_synthetic_rules(make_client):
    sitemap = (
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<url><loc>https://lab.mlaw.gov.sg/legal-services/a/</loc><lastmod>2026-02-03</lastmod></url>"
        "<url><loc>https://lab.mlaw.gov.sg/files/x.pdf</loc></url>"
        "<url><loc>https://lab.mlaw.gov.sg/faq/</loc></url>"
        "<url><loc>https://lab.mlaw.gov.sg/about-us/permalink/</loc></url>"
        "<url><loc>https://other.test/legal-services/b/</loc></url>"
        "</urlset>"
    )
    entries = lab._discover(make_client({"/sitemap.xml": (200, sitemap)}))
    assert entries == [
        {"url": "https://lab.mlaw.gov.sg/legal-services/a/", "lastmod": "2026-02-03"}
    ]


def test_lab_discover_returns_empty_when_sitemap_unreachable(make_client):
    assert lab._discover(make_client({})) == []


def test_lab_real_page_fixture_parses(make_client):
    page = lab._parse_page(
        make_client({"/legal-services/grant-of-aid/": "lab_page.html"}), LAB_URL, "2026-03-17"
    )
    assert_page_shape(page)
    assert page["source_url"] == LAB_URL
    assert page["title"] == "Grant of Aid"  # " | Legal Aid Bureau" stripped
    assert page["source_last_updated"] == "2026-03-17"
    assert page["publisher"] == "Legal Aid Bureau"
    assert page["topic"] == "legal_aid" and page["resource_kind"] == "how_to"
    assert "reproduction prohibited" in page["license"]


LEGACY_LAB = """<html><head><title>How do I apply for legal aid? | Legal Aid Bureau</title>
<link rel="canonical" href="https://lab.mlaw.gov.sg/legal-services/how-do-i-apply-for-legal-aid/"></head>
<body><nav class="bp-breadcrumb">Home &gt; Legal Services</nav>
<div class="print-content"><div class="content">
<h2>Who can apply</h2><p>Singapore citizens and permanent residents who pass the means and merits tests may apply for civil legal aid.</p>
<h2>Fees</h2><table><tr><th>Income</th><th>Contribution</th></tr><tr><td>Below $1,000</td><td>Minimal</td></tr></table>
<p>Pay your contribution at the counter once your application has been approved by the Board.</p>
<h5 class="sub-header">Legal Aid Bureau</h5>
</div></div></body></html>"""


def test_lab_primary_selector_noise_tables_and_fragments(make_client):
    url = "https://lab.mlaw.gov.sg/legal-services/how-do-i-apply-for-legal-aid/"
    page = lab._parse_page(
        make_client({"/legal-services/how-do-i-apply-for-legal-aid/": (200, LEGACY_LAB)}), url, None
    )
    assert_page_shape(page)
    assert page["title"] == "How do I apply for legal aid?"
    assert page["resource_kind"] == "how_to"  # slug in PROCESS_SLUGS
    assert "Income | Contribution" in page["content_text"]
    assert "Home" not in page["content_text"]  # breadcrumb nav stripped
    assert [f["heading"] for f in page["fragments"]] == ["Who can apply", "Fees"]


def test_lab_identity_mismatch_and_thin_pages_are_skipped(make_client):
    # CDN fallback: page declares a different canonical than the one requested.
    c = make_client({"/legal-services/grant-of-aid/": (200, LEGACY_LAB)})
    assert lab._parse_page(c, LAB_URL, None) is None
    thin = '<html><head><link rel="canonical" href="https://lab.mlaw.gov.sg/a/"></head><body><main>tiny</main></body></html>'
    assert (
        lab._parse_page(make_client({"/a/": (200, thin)}), "https://lab.mlaw.gov.sg/a/", None)
        is None
    )


@pytest.mark.parametrize(
    "path, kind, topic, routing",
    [
        ("/legal-services/grant-of-aid/", "how_to", "legal_aid", False),
        ("/legal-services/some-explainer/", "rights_explainer", "legal_aid", False),
        ("/contact-us/", "contact_directory", "access_to_justice", True),
        ("/resources/divorce-guide/", "how_to", "family", True),
        ("/lab-volunteer-schemes/x/", "rights_explainer", "access_to_justice", False),
        ("/unknown/", "rights_explainer", "access_to_justice", False),
    ],
)
def test_lab_classification(path, kind, topic, routing):
    meta = lab._classify("https://lab.mlaw.gov.sg" + path)
    assert (meta["resource_kind"], meta["topic"], meta["routing"]) == (kind, topic, routing)


def test_lab_fetch_end_to_end(patch_polite_client):
    sitemap = (
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"<url><loc>{LAB_URL}</loc><lastmod>2026-03-17T13:10:12.362Z</lastmod></url>"
        "<url><loc>https://lab.mlaw.gov.sg/files/skip.pdf</loc></url>"
        "<url><loc>https://lab.mlaw.gov.sg/legal-services/gone/</loc></url>"
        "</urlset>"
    )
    seen = patch_polite_client(
        {"/sitemap.xml": (200, sitemap), "/legal-services/grant-of-aid/": "lab_page.html"}
    )
    pages = lab.fetch()
    assert [p["source_url"] for p in pages] == [LAB_URL]  # 404 page skipped, pdf never fetched
    assert not any(r.url.path.endswith(".pdf") for r in seen)


# ------------------------------------------------------------------------ probono
FAQ_PATH = "/get-legal-help/legal-representation/clas-frequently-asked-questions/"
FAQ_SEED = next(s for s in probono.SEEDS if s["path"] == FAQ_PATH)


def test_probono_faq_fixture(make_client):
    page = probono._parse_page(make_client({FAQ_PATH: "probono_faq.html"}), FAQ_SEED)
    assert_page_shape(page)
    assert page["title"] == "CLAS – Frequently Asked Questions"
    assert page["source_last_updated"] == "2025-04-15T07:03:30+00:00"
    assert page["resource_kind"] == "faq" and page["topic"] == "legal_aid"
    assert len(page["fragments"]) >= 5
    assert page["fragments"][0]["heading"].endswith("?")


def test_probono_skips_identity_mismatch_and_missing(make_client):
    other = fixture_text("probono_faq.html")
    c = make_client({"/get-legal-help/legal-representation/": (200, other)})
    assert probono._parse_page(c, probono.SEEDS[0]) is None  # canonical points at the FAQ page
    assert probono._parse_page(make_client({}), probono.SEEDS[0]) is None


def test_probono_seed_set_is_sane():
    assert len({s["path"] for s in probono.SEEDS}) == len(probono.SEEDS)
    assert all(s["path"].startswith("/") and s["path"].endswith("/") for s in probono.SEEDS)


def test_probono_fetch_end_to_end(patch_polite_client):
    seen = patch_polite_client({FAQ_PATH: "probono_faq.html"})
    pages = probono.fetch()
    assert len(pages) == 1 and pages[0]["resource_kind"] == "faq"
    assert len(seen) == len(probono.SEEDS)
    assert all(r.url.host == "www.probono.sg" for r in seen)


# ------------------------------------------------------------------------- manual
def test_manual_adapter_is_a_noop_stub():
    assert manual.fetch() == []
