"""Manual (hand-curated) source adapter — DEFERRED stub.

Planned (see README "One-off / manual entries"): hand-curated entries live in a
git-tracked manifest ``resources/manual/seed.yaml``; this adapter reads it and
emits page dicts with ``source_method = "manual"`` into the same ``resources``
table, so manual entries inherit the full refresh pipeline (re-checked,
hash-diffed, link-rot-caught). A small add-CLI de-dupes by normalised URL and
auto-fills title/summary/dates.

Not implemented yet — returns nothing so the build is unaffected.
"""

from __future__ import annotations

from typing import Dict, List


def fetch() -> List[Dict]:
    """Return page dicts. Empty until the manifest + add-CLI land."""
    return []
