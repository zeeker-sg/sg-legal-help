"""sources/_common.py helpers: BeautifulSoup + lxml behaviour on small inline HTML."""

from __future__ import annotations

from bs4 import BeautifulSoup

from sources import _common


def soup(html: str, parser: str = "lxml") -> BeautifulSoup:
    return BeautifulSoup(html, parser)


def test_clean_collapses_whitespace_and_handles_none():
    assert _common.clean(None) == ""
    assert _common.clean(soup("<p>a \n  <b>b</b>\t c</p>").p) == "a b c"


def test_strip_noise_removes_chrome_and_jump_links():
    s = soup(
        "<div id=c><nav>menu</nav><script>x()</script><p>keep</p>"
        '<a href="#top">jump</a><footer>foot</footer><aside class=x>side</aside></div>'
    )
    c = s.select_one("#c")
    _common.strip_noise(c, extra=["aside.x"])
    assert _common.clean(c) == "keep"


def test_text_with_tables_linearises_rows():
    s = soup(
        "<div><p>Intro</p><table><tr><th>Income</th><th>Fee</th></tr>"
        "<tr><td>$1,000</td><td>$0</td></tr></table></div>"
    )
    text = _common.text_with_tables(s.div)
    assert "Income | Fee" in text and "$1,000 | $0" in text
    assert "<table" not in text


def test_derive_title_prefers_first_selector_and_strips_suffix():
    s = soup(
        "<html><head><title>Grant of Aid | Legal Aid Bureau</title></head><body><h1>H</h1></body></html>"
    )
    assert _common.derive_title(s, ["title", "h1"], [" | Legal Aid Bureau"]) == "Grant of Aid"
    assert _common.derive_title(s, ["h2", "h1"]) == "H"
    assert _common.derive_title(s, ["h2"]) == ""


def test_identity_ok_compares_canonical_path():
    s = soup('<head><link rel="canonical" href="https://x.test/a/b/"></head>')
    assert _common.identity_ok(s, "https://x.test/a/b")
    assert not _common.identity_ok(s, "https://x.test/other/")
    assert _common.identity_ok(
        soup("<head></head>"), "https://x.test/anything/"
    )  # nothing to disprove
    og = soup('<head><meta property="og:url" content="https://x.test/a"></head>')
    assert _common.identity_ok(og, "https://x.test/a/")


def test_heading_texts_distinct_in_document_order():
    s = soup("<div><h2>One</h2><h3>Two</h3><h2>One</h2><h4>Three</h4><h1>No</h1></div>")
    assert _common.heading_texts(s.div) == ["One", "Two", "Three"]


def test_split_by_heading_text_slices_between_headings():
    text = "Eligibility You must be a citizen aged 21 and above. How to apply Fill the form online today."
    frags = _common.split_by_heading_text(text, ["Eligibility", "How to apply"], "how_to")
    assert [f["heading"] for f in frags] == ["Eligibility", "How to apply"]
    assert frags[0]["content_text"].endswith("above.")
    assert frags[1]["content_text"].startswith("How to apply")
    assert all(f["content_type"] == "how_to" for f in frags)


def test_split_ignores_unknown_headings_and_tiny_segments():
    assert _common.split_by_heading_text("short", ["short"]) == []
    assert _common.split_by_heading_text("some text here that is long enough", ["nope"]) == []


def test_list_item_fragments_top_level_only_with_strong_heading():
    s = soup(
        "<ul><li><strong>Can I apply?</strong> Yes, if you meet the means test criteria.</li>"
        "<li>Plain list item that has no bold marker at all in it<ul><li>nested nested nested nested</li></ul></li>"
        "<li>tiny</li></ul>"
    )
    frags = _common.list_item_fragments(s.ul, "faq")
    assert len(frags) == 2
    assert frags[0]["heading"] == "Can I apply?"
    assert frags[1]["heading"].startswith("Plain list item")


def test_build_fragments_falls_back_to_list_items():
    s = soup(
        "<div><ul><li><strong>Step one</strong> Do the first thing carefully.</li>"
        "<li><strong>Step two</strong> Do the second thing afterwards.</li></ul></div>"
    )
    text = _common.text_with_tables(s.div)
    frags = _common.build_fragments(s.div, text, [], "process_steps")
    assert [f["heading"] for f in frags] == ["Step one", "Step two"]


def test_xml_parser_reads_sitemap_loc_and_lastmod():
    s = soup(
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://x.test/a/</loc>'
        "<lastmod>2026-01-02</lastmod></url></urlset>",
        "xml",
    )
    url = s.find("url")
    assert url.find("loc").get_text(strip=True) == "https://x.test/a/"
    assert url.find("lastmod").get_text(strip=True) == "2026-01-02"


def test_get_soup_returns_none_on_fetch_failure(make_client):
    c = make_client({})
    assert _common.get_soup(c, "https://x.test/missing", "t") is None


def test_get_soup_html_cache_roundtrip(make_client, monkeypatch, tmp_path):
    monkeypatch.setenv("SGLEGALHELP_HTML_CACHE", "1")
    c = make_client({"/p": (200, "<html><body><p>cached</p></body></html>")})
    s1 = _common.get_soup(c, "https://x.test/p", "sub")
    assert s1.p.text == "cached"
    assert list((tmp_path / ".cache" / "sub").glob("*.html"))
    # Second read comes from disk: a client that 500s everything still works.
    dead = make_client({"/p": (500, "")})
    assert _common.get_soup(dead, "https://x.test/p", "sub").p.text == "cached"
