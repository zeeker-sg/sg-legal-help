"""Pro Bono SG (probono.sg) source adapter.

Pro Bono SG is the SAME publisher as LawGoWhere (already in the catalogue), so
most of its routing/navigation prose is duplicative. Per the recon dedup
strategy this adapter ingests only the NET-NEW / substantive pages and DROPS the
LawGoWhere-equivalent hubs (the /get-legal-help/ hub, section indexes, and
contact-us, whose HQ contact is already on LawGoWhere's contact-us page).

Ingested (a small fixed seed set — the site has no pagination):
- legal-representation: CLAS + FJSS application steps, Means & Merits test (with
  income tables) — net-new.
- clas-frequently-asked-questions: ~8 verbatim Q&A — net-new (FAQ fallback used
  because the Q&A render as <li><strong>Q</strong> A</li>, not headings).
- legal-clinics-in-singapore: Pro Bono SG's own clinic roster (tables) — net-new.
- the four audience-guidance pages (general public / migrant workers /
  transnational families / non-profits) — substantive routing guidance.

Server-rendered WordPress (Elementor); httpx + BeautifulSoup. All-rights-reserved
(Pro Bono SG), so `can_store_fulltext = False` on every row.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List, Optional

import click

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import http_client  # noqa: E402
from sources import _common  # noqa: E402

BASE = "https://www.probono.sg"
CACHE_SUBDIR = "probono_html"

PUBLISHER = "Pro Bono SG"
PUBLISHER_TYPE = "a2j_body"
LICENSE = "All rights reserved"
LICENSE_URL = ""  # no Terms-of-Use / reuse-grant page published
ATTRIBUTION = "Pro Bono SG. Source: probono.sg"
CAN_STORE_FULLTEXT = False
DEFAULT_VOLATILITY = 2  # monthly re-check (clinic data + scheme details move)

CONTENT_SELECTORS = [
    "div.main-content-wrapper",
    "div.main-container",
    "main",
    "article",
]
TITLE_SELECTORS = ["h1.innerBannerHeading1", "div.pageTitle h2", "title"]
TITLE_SUFFIXES = [" - Pro Bono SG | Pro Bono SG", " - Pro Bono SG", " | Pro Bono SG"]

NOISE_EXTRA = [
    "div.bannerWrapper",
    "div.inner-banner",
    "div.breadcrumb-container",
    "form#mc4wp-form",
    "div.form-group",
    "div.footer-contact-info-box",
    "ul.social",
    ".elementor-location-header",
    ".mega-menu",
    "div.link-btn",  # external CTA buttons (MS Forms etc.)
]

# Fixed seed set with per-URL curation (path -> meta).
SEEDS: List[Dict] = [
    {
        "path": "/get-legal-help/legal-representation/",
        "resource_kind": "process_steps",
        "topic": "legal_aid",
        "routing": False,
        "audience": "individual",
    },
    {
        "path": "/get-legal-help/legal-representation/clas-frequently-asked-questions/",
        "resource_kind": "faq",
        "topic": "legal_aid",
        "routing": False,
        "audience": "individual",
    },
    {
        "path": "/get-legal-help/legal-guidance/the-general-public/legal-clinics-in-singapore/",
        "resource_kind": "contact_directory",
        "topic": "access_to_justice",
        "routing": True,
        "audience": "individual",
    },
    {
        "path": "/get-legal-help/legal-guidance/the-general-public/",
        "resource_kind": "how_to",
        "topic": "access_to_justice",
        "routing": True,
        "audience": "individual",
    },
    {
        "path": "/get-legal-help/legal-guidance/the-migrant-worker-community/",
        "resource_kind": "how_to",
        "topic": "access_to_justice",
        "routing": True,
        "audience": "individual",
    },
    {
        "path": "/get-legal-help/legal-guidance/a-transnation-family/",
        "resource_kind": "how_to",
        "topic": "family",
        "routing": True,
        "audience": "individual",
    },
    {
        "path": "/get-legal-help/legal-guidance/a-non-profit-organisation/",
        "resource_kind": "how_to",
        "topic": "access_to_justice",
        "routing": True,
        "audience": "both",
    },
]


def _faq_fragments(container) -> List[Dict]:
    """Pro Bono's CLAS FAQ renders as <li><strong>Q</strong> A</li> with no
    headings — fall back to one fragment per such list item."""
    fragments: List[Dict] = []
    for li in container.select("li"):
        strong = li.find("strong")
        if not strong:
            continue
        question = _common.clean(strong)
        answer = _common.clean(li)
        if question and len(answer) > len(question):
            fragments.append({"heading": question, "content_type": "faq", "content_text": answer})
    return fragments


def _best_container(soup):
    """Pick the candidate content container with the most text (templates vary)."""
    best = None
    best_len = 0
    for selector in CONTENT_SELECTORS:
        el = soup.select_one(selector)
        if el is None:
            continue
        length = len(_common.clean(el))
        if length > best_len:
            best, best_len = el, length
    return best


def _parse_page(client: http_client.PoliteClient, seed: Dict) -> Optional[Dict]:
    url = BASE + seed["path"]
    soup = _common.get_soup(client, url, CACHE_SUBDIR)
    if soup is None:
        return None

    # Guard against an edge/cache serving a different page under HTTP 200.
    if not _common.identity_ok(soup, url):
        click.echo(f"  probono: identity mismatch — skipping {url}", err=True)
        return None

    canonical = soup.select_one('link[rel="canonical"]')
    source_url = canonical["href"] if canonical and canonical.get("href") else url

    title = _common.derive_title(soup, TITLE_SELECTORS, TITLE_SUFFIXES)

    modified_el = soup.select_one('meta[property="article:modified_time"]')
    source_last_updated = modified_el.get("content") if modified_el else None

    container = _best_container(soup)
    if container is None:
        return None

    _common.strip_noise(container, NOISE_EXTRA)
    headings = _common.heading_texts(container)
    content_text = _common.text_with_tables(container)
    if len(content_text) < _common.MIN_CONTENT_CHARS:
        return None

    fragments = _common.build_fragments(container, content_text, headings, seed["resource_kind"])
    if not fragments and seed["resource_kind"] == "faq":
        fragments = _faq_fragments(container)

    return {
        "source_url": source_url,
        "title": title,
        "publisher": PUBLISHER,
        "publisher_type": PUBLISHER_TYPE,
        "source_last_updated": source_last_updated,
        "topic": seed["topic"],
        "audience": seed["audience"],
        "resource_kind": seed["resource_kind"],
        "is_routing_destination": seed["routing"],
        "content_text": content_text,
        "license": LICENSE,
        "license_url": LICENSE_URL,
        "attribution": ATTRIBUTION,
        "can_store_fulltext": CAN_STORE_FULLTEXT,
        "volatility": DEFAULT_VOLATILITY,
        "fragments": fragments,
    }


def fetch() -> List[Dict]:
    pages: List[Dict] = []
    with http_client.PoliteClient() as client:
        for seed in SEEDS:
            page = _parse_page(client, seed)
            if page:
                pages.append(page)
    click.echo(f"probono: {len(pages)} pages captured (of {len(SEEDS)} seeds)")
    return pages
