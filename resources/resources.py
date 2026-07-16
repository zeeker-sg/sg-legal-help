"""The ``resources`` catalogue — the single Zeeker resource for sg-legal-help.

Per the README data model, ALL sources land in ONE URL-keyed catalogue
(``resources``) plus an optional section-level ``resources_fragments`` table.
This module is the orchestrator: it calls each source adapter in
``resources/sources/``, fills the housekeeping fields, generates the safe-to-
serve AI ``summary``, and splits long guides into fragments. Adapters own the
site-specific crawling + curation; this module owns schema, identity, and the
zeeker build-loop plumbing.

Zeeker build-loop notes (requires zeeker >= 0.9.0):
- ``fetch_data`` runs ONCE per build (single-fetch lifecycle: the module is
  loaded once and NOT reloaded between the main and fragments phases), so
  module-level state survives into ``fetch_fragments_data``.
  :data:`_pending_fragment_pages` is that in-memory bridge — no build marker,
  no fragments disk cache. A crash between the main insert and the fragments
  insert loses nothing durable: ``scripts/nightly.sh`` starts every build from
  the S3 copy (``--sync-from-s3``), so an undeployed partial build is simply
  re-crawled next run.
- ``ensure_schema`` pre-creates both tables + unique-index tripwires up front,
  because zeeker only creates tables from returned rows AFTER the resource runs.
- If EVERY adapter raises, ``fetch_data`` raises ``Skip(kind="blocked")`` —
  the source was never checked, so the ``_zeeker_updates`` freshness marker
  must not advance. Partial adapter failure keeps the build going with the
  healthy adapters' rows.
- ``__zeeker_report__`` surfaces per-build counters (pages_crawled, new_rows,
  unchanged, adapters_failed) on the build status line and in ``--json``.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import click
from sqlite_utils.db import Table
from zeeker import Skip

# Sibling modules resolve because zeeker (>= 0.9.0) puts the resources/ dir on
# sys.path while the resource module loads — top-level imports only.
import build_state
import ids as ids_mod
import summary as summary_mod
from sources import lab, lawgowhere, manual, probono

# Adapters run in order. Add new sources here.
ADAPTERS = (lawgowhere, lab, probono, manual)

#: In-memory bridge between the main-table phase and the fragments phase.
#: Written by ``fetch_data``, consumed (and cleared) by ``fetch_fragments_data``
#: in the same module instance — zeeker 0.9.0 loads the module once per build.
_pending_fragment_pages: List[Dict[str, Any]] = []

# =============================================================================
# SCHEMA  (pre-created so the unique-index tripwires exist after a first build)
# =============================================================================
RESOURCES_SCHEMA: Dict[str, Any] = {
    "id": str,
    "source_url": str,
    "title": str,
    "publisher": str,
    "publisher_type": str,
    "source_last_updated": str,
    "topic": str,
    "audience": str,
    "resource_kind": str,
    "is_routing_destination": int,
    "summary": str,
    "content_text": str,
    "license": str,
    "license_url": str,
    "attribution": str,
    "can_store_fulltext": int,
    "source_method": str,
    "volatility": int,
    "last_checked": str,
    "content_hash": str,
    "status": str,
    "created_at": str,
}

FRAGMENTS_SCHEMA: Dict[str, Any] = {
    "id": str,
    "item_id": str,
    "fragment_order": int,
    "heading": str,
    "content_type": str,
    "content_text": str,
    "char_count": int,
}


def ensure_schema(db) -> None:
    """Create both tables + unique indexes if absent (idempotent)."""
    db["resources"].create(RESOURCES_SCHEMA, pk="id", if_not_exists=True)
    db["resources_fragments"].create(FRAGMENTS_SCHEMA, pk="id", if_not_exists=True)
    if db["resources"].exists():
        db["resources"].create_index(["source_url"], unique=True, if_not_exists=True)
    if db["resources_fragments"].exists():
        db["resources_fragments"].create_index(
            ["item_id", "fragment_order"], unique=True, if_not_exists=True
        )


# =============================================================================
# MAIN TABLE
# =============================================================================
def fetch_data(existing_table: Optional[Table]) -> List[Dict[str, Any]]:
    """Crawl every adapter, dedupe vs the catalogue, and emit new rows.

    Runs exactly once per build under zeeker >= 0.9.0. Raises
    ``Skip(kind="blocked")`` when EVERY adapter fails (nothing was checked);
    a partial failure continues with the healthy adapters' pages.
    """
    global _pending_fragment_pages, __zeeker_report__

    db = existing_table.db if existing_table is not None else build_state.connect_db()
    ensure_schema(db)

    existing_urls = set()
    if existing_table is not None:
        existing_urls = {ids_mod.normalize_url(r["source_url"]) for r in existing_table.rows}

    # Gather page dicts from every adapter (one adapter failing must not sink
    # the whole build).
    pages: List[Dict[str, Any]] = []
    adapters_failed = 0
    first_error: Optional[str] = None
    for adapter in ADAPTERS:
        name = getattr(adapter, "__name__", str(adapter))
        try:
            adapter_pages = adapter.fetch() or []
            click.echo(f"resources: adapter {name} returned {len(adapter_pages)} pages")
            pages.extend(adapter_pages)
        except Exception as exc:  # noqa: BLE001 — isolate adapter failures
            adapters_failed += 1
            if first_error is None:
                first_error = f"{name}: {exc}"
            click.echo(f"resources: adapter {name} failed: {exc}", err=True)

    if adapters_failed == len(ADAPTERS):
        # Total outage — nothing was checked, so don't let zeeker advance the
        # freshness marker (kind="blocked" skips that).
        __zeeker_report__ = {
            "pages_crawled": 0,
            "new_rows": 0,
            "unchanged": 0,
            "adapters_failed": adapters_failed,
            "notes": f"all adapters failed: {first_error}",
        }
        raise Skip(f"all adapters failed: {first_error}", kind="blocked")

    # Dedupe within the run and against the existing catalogue (URL-keyed).
    seen: set[str] = set()
    new_pages: List[Dict[str, Any]] = []
    for page in pages:
        url = (page.get("source_url") or "").strip()
        if not url:
            continue
        norm = ids_mod.normalize_url(url)
        if norm in existing_urls or norm in seen:
            continue
        seen.add(norm)
        new_pages.append(page)

    now = datetime.now(timezone.utc).isoformat()
    rows: List[Dict[str, Any]] = []
    cache_pages: List[Dict[str, Any]] = []
    for page in new_pages:
        url = page["source_url"].strip()
        rid = ids_mod.resource_id(url)
        content_text = (page.get("content_text") or "").strip()
        page_summary = page.get("summary") or summary_mod.get_summary(content_text)
        rows.append(
            {
                "id": rid,
                "source_url": url,
                "title": page.get("title", ""),
                "publisher": page.get("publisher", ""),
                "publisher_type": page.get("publisher_type", ""),
                "source_last_updated": page.get("source_last_updated"),
                "topic": page.get("topic"),
                "audience": page.get("audience"),
                "resource_kind": page.get("resource_kind"),
                "is_routing_destination": int(bool(page.get("is_routing_destination", False))),
                "summary": page_summary,
                "content_text": content_text,
                "license": page.get("license", ""),
                "license_url": page.get("license_url", ""),
                "attribution": page.get("attribution", ""),
                "can_store_fulltext": int(bool(page.get("can_store_fulltext", False))),
                "source_method": page.get("source_method", "crawled"),
                "volatility": int(page.get("volatility", 3)),
                "last_checked": now,
                "content_hash": _content_hash(content_text),
                "status": page.get("status", "active"),
                "created_at": now,
            }
        )
        cache_pages.append({"item_id": rid, "fragments": page.get("fragments", []) or []})

    _pending_fragment_pages = cache_pages
    click.echo(f"resources: {len(rows)} new rows (from {len(pages)} crawled pages)")
    __zeeker_report__ = {
        "pages_crawled": len(pages),
        "new_rows": len(rows),
        "unchanged": len(pages) - len(new_pages),
        "adapters_failed": adapters_failed,
    }
    if first_error is not None:
        __zeeker_report__["notes"] = f"{adapters_failed} adapter(s) failed; first: {first_error}"
    return rows


def transform_data(raw_data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Optional transform before insertion (pass-through)."""
    return raw_data


# =============================================================================
# FRAGMENTS TABLE
# =============================================================================
def fetch_fragments_data(
    existing_fragments_table: Optional[Table],
    main_data_context: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Emit section fragments for the pages crawled this build.

    Consumes :data:`_pending_fragment_pages`, populated by ``fetch_data`` in
    this same module instance (zeeker 0.9.0 loads the module once per build —
    no disk bridge needed). ``main_data_context`` (the crawled rows) is
    accepted per the zeeker contract but unused: row dicts carry no fragments,
    the adapters' page dicts do. Each page's fragments are replaced wholesale
    (delete-then-reinsert) so a re-fragmented page can't accumulate stale rows.
    """
    global _pending_fragment_pages
    cache_pages, _pending_fragment_pages = _pending_fragment_pages, []
    if not cache_pages:
        return []

    db = (
        existing_fragments_table.db
        if existing_fragments_table is not None
        else build_state.connect_db()
    )
    ensure_schema(db)

    out: List[Dict[str, Any]] = []
    for page in cache_pages:
        item_id = page["item_id"]
        if db["resources_fragments"].exists():
            db["resources_fragments"].delete_where("item_id = ?", [item_id])
        order = 0
        for frag in page.get("fragments", []):
            text = (frag.get("content_text") or "").strip()
            if not text:
                continue
            out.append(
                {
                    "id": ids_mod.fragment_id(item_id, order),
                    "item_id": item_id,
                    "fragment_order": order,
                    "heading": frag.get("heading", "") or "",
                    "content_type": frag.get("content_type", "paragraph") or "paragraph",
                    "content_text": text,
                    "char_count": len(text),
                }
            )
            order += 1

    click.echo(f"resources: {len(out)} fragments from {len(cache_pages)} pages")
    return out


def transform_fragments_data(raw_fragments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Optional transform before insertion (pass-through)."""
    return raw_fragments


# =============================================================================
# HELPERS
# =============================================================================
def _content_hash(content_text: str) -> str:
    """SHA-256 over whitespace-normalised text, for change detection."""
    normalised = re.sub(r"\s+", " ", content_text or "").strip()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()
