"""Shared extraction helpers for sg-legal-help source adapters.

Used by the newer adapters (lab.py, probono.py). lawgowhere.py predates this and
keeps its own copies (it is verified — left untouched). New adapters should use
these so table-aware content extraction and heading-split fragmenting stay
consistent.

Key ideas:
- ``text_with_tables`` linearises ``<table>`` cells to ``a | b | c`` before
  flattening, so tabular clinic/eligibility data survives as searchable text.
- ``split_by_heading_text`` slices the flattened content at each section heading
  — the same robust approach lawgowhere.py uses for org blocks — so fragments
  pick up the prose AND tables that follow a heading, even when the source markup
  nests inconsistently.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import click
from bs4 import BeautifulSoup, NavigableString

# Sibling modules resolve because zeeker (>= 0.9.0) puts the resources/ dir on
# sys.path while the resource module loads — top-level imports only.
import http_client

# Chrome that repeats site-wide on most CMS themes. Adapters pass extra,
# source-specific selectors (sidebars, mega-menus, signup forms).
BASE_NOISE = [
    "script",
    "style",
    "noscript",
    "header",
    "footer",
    "nav",
    "form",
]

MIN_CONTENT_CHARS = 120


def html_cache_enabled() -> bool:
    return os.environ.get("SGLEGALHELP_HTML_CACHE") == "1"


def _cache_path(url: str, subdir: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return Path(".cache") / subdir / f"{digest}.html"


def get_soup(
    client: http_client.PoliteClient, url: str, cache_subdir: str, parser: str = "lxml"
) -> Optional[BeautifulSoup]:
    """Fetch + parse a page; optional raw-HTML cache for dev (see lawgowhere.py).

    Pass ``parser="xml"`` for sitemaps so ``<loc>``/``<lastmod>`` parse cleanly.
    """
    cache = _cache_path(url, cache_subdir)
    if html_cache_enabled() and cache.exists():
        html = cache.read_text(encoding="utf-8", errors="replace")
    else:
        try:
            resp = client.get(url)
        except Exception as exc:  # noqa: BLE001
            click.echo(f"  {cache_subdir}: fetch failed {url}: {exc}", err=True)
            return None
        html = resp.text
        if html_cache_enabled():
            http_client.cache_write(cache, html)
    return BeautifulSoup(html, parser)


def clean(node) -> str:
    if node is None:
        return ""
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


def strip_noise(node, extra: Sequence[str] = ()) -> None:
    for selector in list(BASE_NOISE) + list(extra):
        for el in node.select(selector):
            el.decompose()
    for a in node.select('a[href^="#"]'):
        a.decompose()


def linearize_tables(node) -> None:
    """Replace each ``<table>`` with ``cell | cell`` rows so tabular data is
    preserved when the tree is flattened to text."""
    for table in node.select("table"):
        rows = []
        for tr in table.select("tr"):
            cells = [clean(c) for c in tr.select("td, th")]
            cells = [c for c in cells if c]
            if cells:
                rows.append(" | ".join(cells))
        table.replace_with(NavigableString("\n" + "\n".join(rows) + "\n"))


def text_with_tables(node) -> str:
    """Flattened text with tables linearised to ``a | b | c`` rows."""
    linearize_tables(node)
    return clean(node)


def derive_title(
    soup: BeautifulSoup, selectors: Sequence[str], strip_suffixes: Sequence[str] = ()
) -> str:
    for selector in selectors:
        el = soup.select_one(selector)
        text = clean(el)
        if text:
            for suffix in strip_suffixes:
                if text.endswith(suffix):
                    text = text[: -len(suffix)].strip(" -|")
            return text
    return ""


def _norm_path(url: str) -> str:
    from urllib.parse import urlsplit

    return (urlsplit(url).path or "/").rstrip("/").lower() or "/"


def identity_ok(soup: BeautifulSoup, requested_url: str) -> bool:
    """True if the page's own canonical/og:url path matches the requested path.

    Defends against CDN/edge fallback pages served under HTTP 200 (no redirect)
    — e.g. lab.mlaw.gov.sg intermittently serving 'Overview of Resources' for an
    unrelated URL. A mismatch means we fetched the wrong page; skip it rather than
    store wrong content under the requested URL (and it also drops defunct URLs
    whose canonical now points elsewhere). Pages with no canonical pass (we can't
    disprove identity).
    """
    el = soup.select_one('link[rel="canonical"]') or soup.select_one('meta[property="og:url"]')
    declared = (el.get("href") or el.get("content")) if el else None
    if not declared:
        return True
    return _norm_path(declared) == _norm_path(requested_url)


def list_item_fragments(container, content_type: str) -> List[Dict]:
    """Fragment a page by its top-level ``<li>`` items.

    Fallback for pages whose sections are numbered/bulleted list items or
    ``<li><strong>Q</strong> A</li>`` Q&A rather than heading tags (LAB how-to
    steps, Pro Bono clinic rows / FAQ)."""
    fragments: List[Dict] = []
    for li in container.find_all("li"):
        if li.find_parent("li"):
            continue  # top-level items only
        text = clean(li)
        if len(text) < 20:
            continue
        strong = li.find(["strong", "b"])
        heading = clean(strong) if strong else text[:80]
        fragments.append({"heading": heading, "content_type": content_type, "content_text": text})
    return fragments


def build_fragments(
    container, content_text: str, headings: Sequence[str], content_type: str
) -> List[Dict]:
    """Heading-split first; fall back to list items when a long page yields ≤1
    heading fragment (so multi-section non-heading pages don't collapse to one)."""
    fragments = split_by_heading_text(content_text, headings, content_type)
    if len(fragments) <= 1:
        li_fragments = list_item_fragments(container, content_type)
        if len(li_fragments) > len(fragments):
            return li_fragments
    return fragments


def heading_texts(container, tags: Sequence[str] = ("h2", "h3", "h4")) -> List[str]:
    """Distinct section-heading texts in document order."""
    out: List[str] = []
    for h in container.find_all(list(tags)):
        text = clean(h)
        if text and text not in out:
            out.append(text)
    return out


def split_by_heading_text(
    content_text: str, headings: Sequence[str], content_type: str = "paragraph"
) -> List[Dict]:
    """Slice ``content_text`` into one fragment per heading.

    Robust to nested/irregular markup: finds each heading string in the
    flattened text and takes the span up to the next heading, so prose and
    tables that follow a heading travel with it.
    """
    positions = sorted((content_text.find(h), h) for h in headings if content_text.find(h) >= 0)
    fragments: List[Dict] = []
    for i, (start, heading) in enumerate(positions):
        end = positions[i + 1][0] if i + 1 < len(positions) else len(content_text)
        segment = content_text[start:end].strip()
        if len(segment) >= 20:
            fragments.append(
                {"heading": heading, "content_type": content_type, "content_text": segment}
            )
    return fragments
