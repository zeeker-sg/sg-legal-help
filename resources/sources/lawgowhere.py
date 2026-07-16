"""LawGoWhere (lawgowhere.sg) source adapter.

LawGoWhere is the MinLaw-supported "where to go for legal help" portal operated
by Pro Bono SG. It is server-rendered static HTML (Microsoft-IIS/ASP.NET) — no
headless browser or XHR replay is needed; JS only toggles ``display:none``.

This adapter catalogues the three substantive, high-value groups (per the recon
parser spec):

1. ``get-information`` LEAF explainer articles — the rights-explainer corpus.
   Discovered from the category index grids (the indexes themselves are used
   for discovery only, not stored). Accordion Q&A becomes section fragments.
2. ``get-help`` — THE routing asset. One static page embedding the whole legal-
   aid scheme directory (CLAS, FJSS, Community Legal Clinics, Legal Aid Bureau)
   in hidden ``div.form-html`` blocks, with tel:/mailto: contacts. Each block
   becomes a fragment. ``is_routing_destination = True``.
3. ``contact-us`` — directory of legal-help organisations. Routing destination.

DEFERRED (documented TODO, lower value / fiddlier): multimedia (past-webinars /
podcasts / events) and the about/privacy institutional pages.

Compliance: the source is all-rights-reserved (Pro Bono SG). Full ``content_text``
is stored for FTS but every row carries ``can_store_fulltext = False`` so the
serving layer shows only summary + link + attribution. Honest ZeekerBot UA;
2s polite delay (the site serves no robots.txt).

Dev knob: ``SGLEGALHELP_HTML_CACHE=1`` caches raw HTML under
``.cache/lawgowhere_html`` so repeated dev builds don't re-hit the site. OFF by
default — production crawls live so change-detection stays honest.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlsplit

import click
from bs4 import BeautifulSoup

# Sibling modules resolve because zeeker (>= 0.9.0) puts the resources/ dir on
# sys.path while the resource module loads — top-level imports only.
import http_client

BASE = "https://lawgowhere.sg"
HOST = "lawgowhere.sg"

INFO_HUB = f"{BASE}/get-information/"
CATEGORY_URLS = [
    f"{BASE}/get-information/issue-type-family/",
    f"{BASE}/get-information/issue-type-civil/",
    f"{BASE}/get-information/issue-type-criminal/",
    f"{BASE}/get-information/issue-type-caregiving/",
]
GET_HELP_URL = f"{BASE}/get-help/"
CONTACT_URL = f"{BASE}/contact-us/"

# Curation defaults (from the recon licensing determination).
PUBLISHER = "LawGoWhere"
PUBLISHER_TYPE = "a2j_body"  # access-to-justice body (Pro Bono SG, MinLaw-supported)
LICENSE = "All rights reserved"
LICENSE_URL = f"{BASE}/privacy-policy/"
ATTRIBUTION = (
    "Pro Bono SG (LawGoWhere), an initiative supported by the Singapore "
    "Ministry of Law. Source: lawgowhere.sg"
)
CAN_STORE_FULLTEXT = False
DEFAULT_VOLATILITY = 3  # explainers re-check monthly (README Tier 3)

# Map the site's issue-type category to our controlled topic vocabulary.
TOPIC_BY_CATEGORY = {
    "family": "family",
    "civil": "civil",
    "criminal": "criminal",
    "caregiving": "caregiving",
}

# Min characters of extracted text below which a page is treated as empty/noise.
MIN_CONTENT_CHARS = 80

# Noise selectors stripped before text extraction (chrome that repeats sitewide
# or intra-page nav that would pollute content_text / content_hash).
_NOISE_SELECTORS = [
    "script",
    "style",
    "noscript",
    "header",
    "footer",
    "nav",
    "#ht_header-web-white",
    "#ht_footer-web",
    ".ht_social",
    ".ht_terms",
    ".ht_issue-list",  # intra-page jump-nav tiles
]


def _html_cache_enabled() -> bool:
    return os.environ.get("SGLEGALHELP_HTML_CACHE") == "1"


def _cache_path(url: str) -> Path:
    import hashlib

    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return Path(".cache/lawgowhere_html") / f"{digest}.html"


def _abs(href: str) -> str:
    return urljoin(BASE + "/", href)


def _is_internal(href: str) -> bool:
    if not href or href.startswith(("#", "tel:", "mailto:", "sms:", "javascript:")):
        return False
    host = urlsplit(_abs(href)).netloc.lower()
    return host == HOST


def _path_depth(url: str) -> int:
    return len([seg for seg in urlsplit(url).path.split("/") if seg])


def _clean(node) -> str:
    if node is None:
        return ""
    text = node.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def _strip_noise(node) -> None:
    for selector in _NOISE_SELECTORS:
        for el in node.select(selector):
            el.decompose()
    # Drop intra-page anchor links (href="#...") — jump nav, not content.
    for a in node.select('a[href^="#"]'):
        a.decompose()


def _get_soup(client: http_client.PoliteClient, url: str) -> Optional[BeautifulSoup]:
    cache = _cache_path(url)
    if _html_cache_enabled() and cache.exists():
        html = cache.read_text(encoding="utf-8", errors="replace")
    else:
        try:
            resp = client.get(url)
        except Exception as exc:  # noqa: BLE001
            click.echo(f"  lawgowhere: fetch failed {url}: {exc}", err=True)
            return None
        html = resp.text
        if _html_cache_enabled():
            http_client.cache_write(cache, html)
    return BeautifulSoup(html, "lxml")


def _derive_title(soup: BeautifulSoup, fallbacks: List[str]) -> str:
    """Titles must come from on-page h1/h2 — the <title> tag is empty sitewide."""
    for selector in fallbacks:
        el = soup.select_one(selector)
        text = _clean(el)
        if text:
            return text
    # Last resort: humanise the URL slug.
    return ""


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def _discover_info_leaves(client: http_client.PoliteClient) -> List[str]:
    """Find leaf explainer article URLs from the category index grids."""
    category_urls = set(CATEGORY_URLS)
    hub = _get_soup(client, INFO_HUB)
    if hub is not None:
        for a in hub.select('a[href^="/get-information/issue-type-"]'):
            url = _abs(a.get("href", "")).split("#")[0]
            if _path_depth(url) == 2:
                category_urls.add(url.rstrip("/") + "/")

    leaves: set[str] = set()
    for cat_url in sorted(category_urls):
        soup = _get_soup(client, cat_url)
        if soup is None:
            continue
        for a in soup.select('a[href^="/get-information/issue-type-"]'):
            href = a.get("href", "")
            if href.startswith("#") or "#" in href:
                continue
            url = _abs(href).split("#")[0].rstrip("/") + "/"
            if _is_internal(url) and _path_depth(url) >= 3:
                leaves.add(url)
    return sorted(leaves)


# ---------------------------------------------------------------------------
# Per-group parsers
# ---------------------------------------------------------------------------
def _accordion_fragments(main) -> List[Dict]:
    """Pull accordion Q&A from a leaf article into FAQ-style fragments."""
    fragments: List[Dict] = []
    items = main.select("div.accordion-item, li.accordion-item")
    if items:
        for item in items:
            q = item.select_one(
                "button.accordion-button, .accordion-header button, .accordion-header"
            )
            a = item.select_one("div.km_accordion-body, div.accordion-body")
            answer = _clean(a)
            if answer:
                fragments.append(
                    {"heading": _clean(q), "content_type": "faq", "content_text": answer}
                )
    else:
        # Fallback: pair buttons and bodies in document order.
        buttons = main.select("button.accordion-button, .accordion-button")
        bodies = main.select("div.km_accordion-body, div.accordion-body")
        for q, a in zip(buttons, bodies):
            answer = _clean(a)
            if answer:
                fragments.append(
                    {"heading": _clean(q), "content_type": "faq", "content_text": answer}
                )
    return fragments


def _topic_for_url(url: str) -> str:
    match = re.search(r"/issue-type-([a-z]+)/", url)
    if match:
        return TOPIC_BY_CATEGORY.get(match.group(1), "legal_information")
    return "legal_information"


def _parse_info_leaf(client: http_client.PoliteClient, url: str) -> Optional[Dict]:
    soup = _get_soup(client, url)
    if soup is None:
        return None

    canonical = soup.select_one('link[rel="canonical"]')
    source_url = _abs(canonical["href"]) if canonical and canonical.get("href") else url

    title = _derive_title(
        soup,
        ["div.ht_content-text h1", "section.ht_all_content h1", "h1", "._issuedetails h2"],
    )

    main = soup.select_one("div.km_web-container") or soup.select_one("section.ht_all_content")
    if main is None:
        return None

    fragments = _accordion_fragments(main)

    _strip_noise(main)
    content_text = _clean(main)
    if len(content_text) < MIN_CONTENT_CHARS:
        return None

    return {
        "source_url": source_url,
        "title": title,
        "publisher": PUBLISHER,
        "publisher_type": PUBLISHER_TYPE,
        "source_last_updated": None,
        "topic": _topic_for_url(source_url),
        "audience": "individual",
        "resource_kind": "rights_explainer",
        "is_routing_destination": False,
        "content_text": content_text,
        "license": LICENSE,
        "license_url": LICENSE_URL,
        "attribution": ATTRIBUTION,
        "can_store_fulltext": CAN_STORE_FULLTEXT,
        "volatility": DEFAULT_VOLATILITY,
        "fragments": fragments,
    }


def _parse_get_help(client: http_client.PoliteClient) -> Optional[Dict]:
    soup = _get_soup(client, GET_HELP_URL)
    if soup is None:
        return None

    fragments: List[Dict] = []
    contacts: set[str] = set()
    parts: List[str] = []
    for block in soup.select("div.form-html"):
        text = _clean(block)
        if len(text) < 20:
            continue
        strong = block.find("strong")
        heading = _clean(strong)
        for a in block.select('a[href^="tel:"], a[href^="mailto:"]'):
            contacts.add(a.get("href", "").replace("tel:", "").replace("mailto:", "").strip())
        fragments.append(
            {"heading": heading, "content_type": "contact_directory", "content_text": text}
        )
        parts.append(f"{heading}\n{text}" if heading else text)

    content_text = "\n\n".join(parts).strip()
    if contacts:
        content_text += "\n\nContacts: " + ", ".join(sorted(contacts))
    if len(content_text) < MIN_CONTENT_CHARS:
        return None

    return {
        "source_url": GET_HELP_URL,
        "title": "LawGoWhere — Get Legal Help (legal-aid schemes & where to go)",
        "publisher": PUBLISHER,
        "publisher_type": PUBLISHER_TYPE,
        "source_last_updated": None,
        "topic": "access_to_justice",
        "audience": "individual",
        "resource_kind": "contact_directory",
        "is_routing_destination": True,
        "content_text": content_text,
        "license": LICENSE,
        "license_url": LICENSE_URL,
        "attribution": ATTRIBUTION,
        "can_store_fulltext": CAN_STORE_FULLTEXT,
        "volatility": DEFAULT_VOLATILITY,
        "fragments": fragments,
    }


def _org_fragments_from_text(content_text: str, org_names: List[str]) -> List[Dict]:
    """Split the contact page text into one fragment per org.

    The org NAME (``<h2>``) and its contact details (``km_icon_text`` siblings)
    sit in separate DOM nodes, so slicing the flattened text at each org name —
    rather than reading the ``km_about_usred`` name block alone — keeps each
    org's address/phone/email/hours with its name. Using a de-duplicated,
    ordered name list also avoids the parent+nested ``km_about_usred``
    double-match that previously emitted a duplicate fragment.
    """
    positions = sorted(
        (content_text.find(name), name) for name in org_names if content_text.find(name) >= 0
    )
    fragments: List[Dict] = []
    for i, (start, name) in enumerate(positions):
        end = positions[i + 1][0] if i + 1 < len(positions) else len(content_text)
        segment = content_text[start:end].strip()
        if segment:
            fragments.append(
                {"heading": name, "content_type": "contact_directory", "content_text": segment}
            )
    return fragments


def _parse_contact_us(client: http_client.PoliteClient) -> Optional[Dict]:
    soup = _get_soup(client, CONTACT_URL)
    if soup is None:
        return None

    main = soup.select_one("section.ht_all_content .support_container") or soup.select_one(
        "section.ht_all_content"
    )
    if main is None:
        return None

    # Org names from the contact cards, de-duplicated in document order (the
    # parent+nested km_about_usred blocks repeat a name; dedup collapses them).
    org_names: List[str] = []
    for block in main.select("div.km_about_usred"):
        name = _clean(block.find("h2") or block.find(["h1", "h3"]))
        if name and name not in org_names:
            org_names.append(name)

    # content_text from the whole container so each org's address/phone/email/
    # hours (which live outside the name block) are captured.
    _strip_noise(main)
    content_text = _clean(main)
    if len(content_text) < MIN_CONTENT_CHARS:
        return None

    fragments = _org_fragments_from_text(content_text, org_names)

    return {
        "source_url": CONTACT_URL,
        "title": "LawGoWhere — Contact Us (legal-help organisations)",
        "publisher": PUBLISHER,
        "publisher_type": PUBLISHER_TYPE,
        "source_last_updated": None,
        "topic": "access_to_justice",
        "audience": "individual",
        "resource_kind": "contact_directory",
        "is_routing_destination": True,
        "content_text": content_text,
        "license": LICENSE,
        "license_url": LICENSE_URL,
        "attribution": ATTRIBUTION,
        "can_store_fulltext": CAN_STORE_FULLTEXT,
        "volatility": DEFAULT_VOLATILITY,
        "fragments": fragments,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def fetch() -> List[Dict]:
    """Crawl LawGoWhere and return page dicts (see resources/sources/__init__.py)."""
    pages: List[Dict] = []
    with http_client.PoliteClient() as client:
        leaves = _discover_info_leaves(client)
        click.echo(f"lawgowhere: discovered {len(leaves)} leaf explainer articles")
        for url in leaves:
            page = _parse_info_leaf(client, url)
            if page:
                pages.append(page)

        get_help = _parse_get_help(client)
        if get_help:
            pages.append(get_help)
            click.echo(f"lawgowhere: get-help captured ({len(get_help['fragments'])} schemes)")

        contact = _parse_contact_us(client)
        if contact:
            pages.append(contact)
            click.echo(f"lawgowhere: contact-us captured ({len(contact['fragments'])} orgs)")

    click.echo(f"lawgowhere: {len(pages)} pages total")
    return pages
