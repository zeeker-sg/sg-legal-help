"""Deterministic id minting for the sg-legal-help tables.

Primary keys are opaque deterministic hashes — SHA-256 hexdigest, first 12
chars — of pinned, namespaced inputs. Because the PK is derived from a stable
source attribute (the canonical URL), re-running discovery mints the same id
and the upsert collapses duplicates structurally; nothing scans for dupes.
Content/natural identifiers (titles, citations) are content fields, never keys.

Pinned hash inputs (changing any of these is a migration — do not improvise):

==================  ============================  ==========================
table               input                         helper
==================  ============================  ==========================
resources           ``resource:{norm_url}``       :func:`resource_id`
resources_fragments ``{parent_id}:{order}``        :func:`fragment_id`
==================  ============================  ==========================

``parent_id`` is always the parent row's opaque 12-hex id, so the fragment
namespace can never collide with the catalogue namespace.
"""

from __future__ import annotations

import hashlib
from urllib.parse import urlsplit, urlunsplit


def hash_id(raw: str) -> str:
    """SHA-256 hexdigest of ``raw``, truncated to the first 12 hex chars."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def normalize_url(url: str) -> str:
    """Canonicalise a URL for keying and de-duplication.

    Lowercases scheme/host, drops a trailing slash on the path, strips the
    fragment, and discards the query string (LawGoWhere content pages are
    path-addressed; tracking params must not mint distinct rows). Two URLs
    that point at the same page collapse to one id.
    """
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower() or "https"
    netloc = parts.netloc.lower()
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((scheme, netloc, path, "", ""))


def resource_id(source_url: str) -> str:
    """Catalogue PK — opaque hash of the normalised canonical URL."""
    return hash_id(f"resource:{normalize_url(source_url)}")


def fragment_id(parent_id: str, fragment_order: int | str) -> str:
    """Fragment PK — unique within a resource; re-extraction replaces wholesale."""
    return hash_id(f"{parent_id}:{fragment_order}")
