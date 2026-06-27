"""Legal Aid Bureau (lab.mlaw.gov.sg) source adapter.

LAB is the government bureau (a department of the Ministry of Law) that provides
civil legal aid. The site is a static Isomer (Jekyll) build on the SG Government
Design System — server-rendered HTML, httpx + BeautifulSoup, no JS.

Strategy (per the recon parser spec): sitemap-driven discovery.
- Fetch /sitemap.xml, DROP PDFs (`/files/`, `.pdf` — ~106 assets, out of scope)
  and empty Isomer template stubs (`/faq/`, `/search/`, `/resource-room*`,
  `/about-us/permalink/`), leaving ~50 real HTML pages.
- Per page: title from `<title>`, content from `div.print-content div.content`
  (fallback chain), noise stripped, and an empty-body guard (skip < 120 chars —
  catches unbuilt Isomer shells). Fragments split on section headings.
- Classify resource_kind / topic / is_routing_destination by URL prefix; routing
  pages (useful-links, contact-us, clinic lists) are thin stubs that POINT to
  destinations already in the catalogue — kept as routing rows, not re-ingested.

Licensing: STRICT. The MLAW Terms of Use forbid reproduction without prior
written approval, so every row sets `can_store_fulltext = False`; full text is
retained for internal FTS/routing only, served as summary + link + attribution.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlsplit

import click

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import http_client  # noqa: E402
from sources import _common  # noqa: E402

BASE = "https://lab.mlaw.gov.sg"
HOST = "lab.mlaw.gov.sg"
SITEMAP_URL = f"{BASE}/sitemap.xml"
CACHE_SUBDIR = "lab_html"

PUBLISHER = "Legal Aid Bureau"
PUBLISHER_TYPE = "ministry"  # a department of the Ministry of Law
LICENSE = "All rights reserved (Crown copyright — MLAW; reproduction prohibited without prior written approval)"
LICENSE_URL = f"{BASE}/terms-of-use/"
ATTRIBUTION = "Legal Aid Bureau, Ministry of Law (Singapore). Source: lab.mlaw.gov.sg"
CAN_STORE_FULLTEXT = False

# Content-container fallback chain (prose nests differently across templates).
CONTENT_SELECTORS = [
    "div.print-content div.content",
    "div.col.is-8.print-content",
    "div.print-content",
    "main",
    "div.content",
]
TITLE_SELECTORS = ["title", "section.bp-section-pagetitle h1", "h1.has-text-white"]
TITLE_SUFFIXES = [" | Legal Aid Bureau", " - Legal Aid Bureau"]

# Source-specific chrome to strip before extraction.
NOISE_EXTRA = [
    "nav.bp-breadcrumb",
    "aside.bp-menu",
    "nav.sidenav",
    "div.bp-dropdown-menu",
    "footer.bp-footer",
    "p.footer-credits",
    "section.bp-section-pagetitle",
    "div.bp-sharing",
    "h5.sub-header",  # trailing 'Legal Aid Bureau' sign-off
]

# URL routing → curation.
PROCESS_SLUGS = {
    "grant-of-aid",
    "after-aid-is-granted",
    "how-do-i-pay-my-contribution-or-other-charges-to-lab",
    "cancellation-of-legal-aid",
    "how-do-i-apply-for-legal-aid",
}
ROUTING_PATHS = {
    "/useful-external-links/",
    "/useful-links/",
    "/contact-us/",
    "/overview-of-resources/",
}
FAMILY_KEYWORDS = ("divorce", "syariah", "matrimonial", "mental-capacity")


def _norm_path(url: str) -> str:
    path = urlsplit(url).path
    return path if path.endswith("/") else path + "/"


def _should_skip(url: str) -> bool:
    path = urlsplit(url).path.lower()
    if path.endswith(".pdf") or "/files/" in path:
        return True
    if path.rstrip("/") in ("/faq", "/search"):
        return True
    if path.startswith("/resource-room") or path.endswith("/permalink/"):
        return True
    return False


def _classify(url: str) -> Dict:
    path = _norm_path(url)
    slug = path.rstrip("/").rsplit("/", 1)[-1]
    if path.startswith("/legal-services/"):
        kind = "how_to" if slug in PROCESS_SLUGS else "rights_explainer"
        return {
            "resource_kind": kind,
            "topic": "legal_aid",
            "routing": False,
            "audience": "individual",
            "volatility": 3,
        }
    if path == "/your-guide-to-attending-court/":
        return {
            "resource_kind": "how_to",
            "topic": "legal_aid",
            "routing": False,
            "audience": "individual",
            "volatility": 3,
        }
    if path in ROUTING_PATHS or "list-of-legal-clinics" in path:
        return {
            "resource_kind": "contact_directory",
            "topic": "access_to_justice",
            "routing": True,
            "audience": "individual",
            "volatility": 2,
        }
    if path.startswith("/resources/"):
        topic = "family" if any(k in path for k in FAMILY_KEYWORDS) else "access_to_justice"
        return {
            "resource_kind": "how_to",
            "topic": topic,
            "routing": True,
            "audience": "individual",
            "volatility": 2,
        }
    if path.startswith("/lab-volunteer-schemes/"):
        return {
            "resource_kind": "rights_explainer",
            "topic": "access_to_justice",
            "routing": False,
            "audience": "both",
            "volatility": 3,
        }
    if path.startswith("/about-us/"):
        return {
            "resource_kind": "rights_explainer",
            "topic": "access_to_justice",
            "routing": False,
            "audience": "individual",
            "volatility": 3,
        }
    return {
        "resource_kind": "rights_explainer",
        "topic": "access_to_justice",
        "routing": False,
        "audience": "individual",
        "volatility": 3,
    }


def _discover(client: http_client.PoliteClient) -> List[Dict]:
    """Return [{url, lastmod}] for in-scope HTML pages from the sitemap."""
    soup = _common.get_soup(client, SITEMAP_URL, CACHE_SUBDIR, parser="xml")
    if soup is None:
        click.echo("lab: sitemap fetch failed", err=True)
        return []
    entries = []
    for url_el in soup.find_all("url"):
        loc = url_el.find("loc")
        if not loc:
            continue
        url = loc.get_text(strip=True)
        if not url or urlsplit(url).netloc.lower() != HOST or _should_skip(url):
            continue
        lastmod_el = url_el.find("lastmod")
        entries.append(
            {"url": url, "lastmod": lastmod_el.get_text(strip=True) if lastmod_el else None}
        )
    return entries


def _parse_page(
    client: http_client.PoliteClient, url: str, lastmod: Optional[str]
) -> Optional[Dict]:
    soup = _common.get_soup(client, url, CACHE_SUBDIR)
    if soup is None:
        return None

    # Guard against the LAB CDN serving a fallback page (e.g. 'Overview of
    # Resources') under HTTP 200 for an unrelated URL — store the right page or
    # nothing. Also drops defunct URLs whose canonical now points elsewhere.
    if not _common.identity_ok(soup, url):
        click.echo(f"  lab: identity mismatch (CDN fallback?) — skipping {url}", err=True)
        return None

    canonical = soup.select_one('link[rel="canonical"]')
    source_url = canonical["href"] if canonical and canonical.get("href") else url

    title = _common.derive_title(soup, TITLE_SELECTORS, TITLE_SUFFIXES)

    container = None
    for selector in CONTENT_SELECTORS:
        container = soup.select_one(selector)
        if container is not None:
            break
    if container is None:
        return None

    _common.strip_noise(container, NOISE_EXTRA)
    headings = _common.heading_texts(container)
    content_text = _common.text_with_tables(container)
    if len(content_text) < _common.MIN_CONTENT_CHARS:
        return None  # empty Isomer shell / placeholder

    meta = _classify(source_url)
    fragments = _common.build_fragments(container, content_text, headings, meta["resource_kind"])

    return {
        "source_url": source_url,
        "title": title,
        "publisher": PUBLISHER,
        "publisher_type": PUBLISHER_TYPE,
        "source_last_updated": lastmod,
        "topic": meta["topic"],
        "audience": meta["audience"],
        "resource_kind": meta["resource_kind"],
        "is_routing_destination": meta["routing"],
        "content_text": content_text,
        "license": LICENSE,
        "license_url": LICENSE_URL,
        "attribution": ATTRIBUTION,
        "can_store_fulltext": CAN_STORE_FULLTEXT,
        "volatility": meta["volatility"],
        "fragments": fragments,
    }


def fetch() -> List[Dict]:
    pages: List[Dict] = []
    with http_client.PoliteClient() as client:
        entries = _discover(client)
        click.echo(f"lab: {len(entries)} in-scope HTML pages from sitemap")
        skipped = 0
        for entry in entries:
            page = _parse_page(client, entry["url"], entry["lastmod"])
            if page:
                pages.append(page)
            else:
                skipped += 1
        click.echo(f"lab: {len(pages)} pages captured, {skipped} skipped (empty/placeholder)")
    return pages
