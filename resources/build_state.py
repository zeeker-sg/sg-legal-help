"""Build-DB access for the sg-legal-help resource.

One concern, a consequence of how ``zeeker build`` drives resource modules
(see zeeker.core.database.builder/processor):

**Build-DB access for schema pre-creation.** On a from-scratch build zeeker
passes ``fetch_data(existing_table=None)`` and only creates the table from
the returned rows *after* the resource runs — so a resource that wants its
unique-index tripwires present at the end of the very first build (the S3
DB is the only durable state, so every night is potentially a "first
build") must create the tables + indexes itself, up front, so zeeker's
``insert_all`` lands into the prepared table. :func:`connect_db` opens the
build database the same way zeeker does: the ``[project] database`` file
named in ``zeeker.toml`` (``zeeker build`` always runs from the project
root), defaulting to ``sg-legal-help.db``.

Historical note: this module used to also own a per-build marker
(``.cache/sg_legal_help_build_marker.json``) that made zeeker's second
``fetch_data`` call a no-op. zeeker >= 0.9.0 calls ``fetch_data`` exactly
once per build (single-fetch lifecycle), so the marker is gone.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import sqlite_utils

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
