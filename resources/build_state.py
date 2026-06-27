"""Per-build state for the sg-legal-help resource (DB handle + build marker).

Two concerns, both consequences of how ``zeeker build`` drives resource
modules (see zeeker.core.database.builder/processor):

1. **Build-DB access for schema pre-creation.** On a from-scratch build zeeker
   passes ``fetch_data(existing_table=None)`` and only creates the table from
   the returned rows *after* the resource runs — so a resource that wants its
   unique-index tripwires present at the end of the very first build (the S3
   DB is the only durable state, so every night is potentially a "first
   build") must create the tables + indexes itself, up front, so zeeker's
   ``insert_all`` lands into the prepared table. :func:`connect_db` opens the
   build database the same way zeeker does: the ``[project] database`` file
   named in ``zeeker.toml`` (``zeeker build`` always runs from the project
   root), defaulting to ``sg-legal-help.db``.

2. **The per-build marker.** For fragments-enabled resources zeeker calls
   ``fetch_data`` a SECOND time (in a freshly reloaded module, so module
   globals do not survive) purely to obtain the ``main_data_context`` argument
   for ``fetch_fragments_data`` — that second return value is never inserted.
   Without a guard the second call would re-crawl the listing and re-drain the
   extraction queue, doubling the run's request budget. The first ``fetch_data``
   writes ``.cache/sg_legal_help_build_marker.json`` at the start of a real
   run; a *fresh* marker (< :data:`MARKER_FRESH_HOURS` old) tells the second
   call to skip crawling entirely. A stale/missing/corrupt marker always reads
   as "run normally", so corruption can only cost an extra crawl, never
   suppress a build.
"""

from __future__ import annotations

import json
import os
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import sqlite_utils

#: Marker written by the first ``fetch_data`` of a build; read by the second
#: (fragments-context) invocation. Lives under ``.cache/`` so ephemeral CI
#: disk wipes it between builds.
MARKER_PATH = Path(".cache/sg_legal_help_build_marker.json")

#: A marker younger than this is "the same build" (zeeker's two fetch_data
#: calls are seconds apart; nightly builds are ~24h apart).
MARKER_FRESH_HOURS = 6.0

DEFAULT_DB_FILENAME = "sg-legal-help.db"


def build_db_path() -> Path:
    """Path of the database ``zeeker build`` is writing, per ``zeeker.toml``."""
    toml_path = Path("zeeker.toml")
    if toml_path.exists():
        try:
            config = tomllib.loads(toml_path.read_text(encoding="utf-8"))
            database = (config.get("project") or {}).get("database")
            if database:
                return Path(database)
        except (OSError, tomllib.TOMLDecodeError):
            pass
    return Path(DEFAULT_DB_FILENAME)


def connect_db() -> sqlite_utils.Database:
    """Open the build database for schema pre-creation (see module docstring)."""
    return sqlite_utils.Database(str(build_db_path()))


def cache_write(path: Path, content: str) -> None:
    """Atomically write ``content`` to ``path`` (tmp file + ``os.replace``)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def write_marker(now: Optional[datetime] = None) -> None:
    """Record that a real run started now; refreshes any previous marker."""
    started_at = (now or datetime.now(timezone.utc)).isoformat()
    cache_write(MARKER_PATH, json.dumps({"started_at": started_at}))


def marker_is_fresh(now: Optional[datetime] = None) -> bool:
    """True when a marker exists and is younger than :data:`MARKER_FRESH_HOURS`.

    A missing, unreadable, malformed, or future-dated marker reads as stale —
    stale always means "run normally".
    """
    try:
        payload = json.loads(MARKER_PATH.read_text(encoding="utf-8"))
        started_at = datetime.fromisoformat(payload["started_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    if started_at.tzinfo is None:
        return False
    age = ((now or datetime.now(timezone.utc)) - started_at).total_seconds()
    return 0 <= age < MARKER_FRESH_HOURS * 3600


def clear_marker() -> None:
    """Remove the marker (test/maintenance helper; a real run refreshes it)."""
    try:
        os.remove(MARKER_PATH)
    except FileNotFoundError:
        pass
