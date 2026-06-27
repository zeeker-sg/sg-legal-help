"""The ``resources`` catalogue — the single Zeeker resource for sg-legal-help.

Per the README data model, ALL sources land in ONE URL-keyed catalogue
(``resources``) plus an optional section-level ``resources_fragments`` table.
This module is the orchestrator: it calls each source adapter in
``resources/sources/``, fills the housekeeping fields, generates the safe-to-
serve AI ``summary``, and splits long guides into fragments. Adapters own the
site-specific crawling + curation; this module owns schema, identity, and the
zeeker build-loop plumbing.

Zeeker build-loop notes (see resources/build_state.py):
- ``fetch_data`` is called a SECOND time during the fragments phase purely to
  rebuild ``main_data_context``; a per-build marker makes that second call a
  no-op so the crawl runs once. Fragments do NOT depend on ``main_data_context``
  — ``fetch_fragments_data`` reads the on-disk extraction cache the first
  ``fetch_data`` wrote (the module is reloaded between phases, so in-memory
  state is gone; disk is the bridge).
- ``ensure_schema`` pre-creates both tables + unique-index tripwires up front,
  because zeeker only creates tables from returned rows AFTER the resource runs.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import click
from sqlite_utils.db import Table

# Zeeker loads resource files via importlib.util.spec_from_file_location, which
# bypasses package imports. Add this dir to sys.path so sibling modules and the
# sources/ sub-package import cleanly at build time.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_state  # noqa: E402
import ids as ids_mod  # noqa: E402
import summary as summary_mod  # noqa: E402
from sources import lab, lawgowhere, manual, probono  # noqa: E402

# Adapters run in order. Add new sources here.
ADAPTERS = (lawgowhere, lab, probono, manual)

#: Bridges the main-table and fragments phases (zeeker reloads the module
#: between them, killing in-memory state). Written by the real ``fetch_data``.
FRAGMENT_CACHE_PATH = Path(".cache/sg_legal_help_fragments.json")

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

    Side-effect-free on the fragments-phase re-invocation (build marker guard).
    """
    if build_state.marker_is_fresh():
        click.echo("resources: fresh build marker — fragments-context call, skipping crawl")
        return []
    build_state.write_marker()

    db = existing_table.db if existing_table is not None else build_state.connect_db()
    ensure_schema(db)

    existing_urls = set()
    if existing_table is not None:
        existing_urls = {ids_mod.normalize_url(r["source_url"]) for r in existing_table.rows}

    # Gather page dicts from every adapter (one adapter failing must not sink
    # the whole build).
    pages: List[Dict[str, Any]] = []
    for adapter in ADAPTERS:
        name = getattr(adapter, "__name__", str(adapter))
        try:
            adapter_pages = adapter.fetch() or []
            click.echo(f"resources: adapter {name} returned {len(adapter_pages)} pages")
            pages.extend(adapter_pages)
        except Exception as exc:  # noqa: BLE001 — isolate adapter failures
            click.echo(f"resources: adapter {name} failed: {exc}", err=True)

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

    _write_fragment_cache(cache_pages)
    click.echo(f"resources: {len(rows)} new rows (from {len(pages)} crawled pages)")
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

    Reads the on-disk cache written by ``fetch_data`` (NOT ``main_data_context``
    — see module docstring). Each page's fragments are replaced wholesale
    (delete-then-reinsert) so a re-fragmented page can't accumulate stale rows.
    """
    cache_pages = _read_fragment_cache()
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


def _write_fragment_cache(cache_pages: List[Dict[str, Any]]) -> None:
    build_state.cache_write(FRAGMENT_CACHE_PATH, json.dumps({"pages": cache_pages}))


def _read_fragment_cache() -> List[Dict[str, Any]]:
    try:
        payload = json.loads(FRAGMENT_CACHE_PATH.read_text(encoding="utf-8"))
        return payload.get("pages", [])
    except (OSError, ValueError):
        return []
