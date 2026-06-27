#!/usr/bin/env python3
"""Post-build sanity checks for the sg-legal-help database.

Run after ``zeeker build`` and before ``zeeker deploy`` (see scripts/nightly.sh).
Fails loudly (exit 1) when an invariant is violated so a broken build never
reaches S3:

1. Append-mostly invariant: the ``resources`` catalogue row count never
   decreases versus the previously deployed DB (rows are flagged
   superseded/dead_link, never deleted). ``resources_fragments`` is EXEMPT —
   re-extraction replaces a page's fragments wholesale (delete-then-reinsert),
   so a re-fragmented page legitimately shrinks the table.
2. Referential integrity: every ``resources_fragments.item_id`` points at a
   real ``resources.id`` (no orphan fragments) — counts ACTUAL rows, never a
   denormalised counter.
3. No empty active rows: every ``status='active'`` resource has non-empty
   ``content_text`` OR ``summary`` (an active row the assistant can't ground on
   or display is a silent failure).
4. Unique-index tripwires intact: ``resources(source_url)`` and
   ``resources_fragments(item_id, fragment_order)``. If key derivation drifts,
   fail loudly rather than insert near-duplicates.

It also prints (does not fail on) a fragment-coverage and status breakdown so a
curator can eyeball the build. Checks degrade gracefully (warn, pass) when a
table does not exist yet.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import click

KNOWN_TABLES = ("resources", "resources_fragments")
NEVER_DECREASE_TABLES = ("resources",)

REQUIRED_UNIQUE_INDEXES: dict[str, tuple[frozenset[str], ...]] = {
    "resources": (frozenset({"source_url"}),),
    "resources_fragments": (frozenset({"item_id", "fragment_order"}),),
}

# Active rows shorter than this with no fragments are reported (not failed) so a
# curator can sanity-check that long guides actually fragmented.
LONG_CONTENT_CHARS = 4000


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def row_count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def unique_index_column_sets(conn: sqlite3.Connection, table: str) -> list[frozenset[str]]:
    column_sets = []
    for index in conn.execute(f"PRAGMA index_list({table})"):
        if not index[2]:  # not unique
            continue
        columns = {
            info[2]
            for info in conn.execute(f"PRAGMA index_info({index[1]})")
            if info[2] is not None
        }
        if columns:
            column_sets.append(frozenset(columns))
    return column_sets


def check_row_counts(conn: sqlite3.Connection, previous_path: Path, violations: list[str]) -> None:
    if not previous_path.exists():
        click.echo(
            f"warn: previous database {previous_path} not found -- "
            "skipping row-count comparison (first run?)"
        )
        return
    previous = sqlite3.connect(f"file:{previous_path}?mode=ro", uri=True)
    try:
        for table in KNOWN_TABLES:
            if not table_exists(previous, table):
                continue
            previous_count = row_count(previous, table)
            if not table_exists(conn, table):
                violations.append(
                    f"{table}: present in previous DB ({previous_count} rows) "
                    "but missing from the new build"
                )
                continue
            new_count = row_count(conn, table)
            if table not in NEVER_DECREASE_TABLES:
                click.echo(
                    f"note: {table} row count {previous_count} -> {new_count} "
                    "(exempt from never-decrease: wholesale fragment replacement)"
                )
                continue
            if new_count < previous_count:
                violations.append(
                    f"{table}: row count decreased {previous_count} -> {new_count} "
                    "(append-mostly invariant: rows are flagged, never deleted)"
                )
            else:
                click.echo(f"ok: {table} row count {previous_count} -> {new_count}")
    finally:
        previous.close()


def check_no_orphan_fragments(conn: sqlite3.Connection, violations: list[str]) -> None:
    if not table_exists(conn, "resources_fragments"):
        click.echo("warn: resources_fragments table does not exist yet -- skipping orphan check")
        return
    if not table_exists(conn, "resources"):
        violations.append("resources_fragments exists but resources table does not")
        return
    orphans = conn.execute(
        "SELECT f.id FROM resources_fragments f "
        "WHERE NOT EXISTS (SELECT 1 FROM resources r WHERE r.id = f.item_id)"
    ).fetchall()
    if orphans:
        sample = ", ".join(str(row[0]) for row in orphans[:10])
        violations.append(
            f"resources_fragments: {len(orphans)} orphan row(s) with no parent "
            f"resources row (e.g. {sample})"
        )
    else:
        click.echo("ok: every resources_fragments row references a real resources row")


def check_no_empty_active_rows(conn: sqlite3.Connection, violations: list[str]) -> None:
    if not table_exists(conn, "resources"):
        click.echo("warn: resources table does not exist yet -- skipping empty-row check")
        return
    columns = table_columns(conn, "resources")
    if not {"status", "content_text", "summary"} <= columns:
        click.echo("warn: resources lacks status/content_text/summary -- skipping empty-row check")
        return
    empties = conn.execute(
        "SELECT source_url FROM resources WHERE status = 'active' "
        "AND COALESCE(NULLIF(TRIM(content_text), ''), NULLIF(TRIM(summary), '')) IS NULL"
    ).fetchall()
    if empties:
        sample = ", ".join(str(row[0]) for row in empties[:10])
        violations.append(
            f"resources: {len(empties)} active row(s) with empty content_text AND summary "
            f"(e.g. {sample})"
        )
    else:
        click.echo("ok: every active resources row has content_text or summary")


def check_unique_indexes(conn: sqlite3.Connection, violations: list[str]) -> None:
    for table, required_sets in REQUIRED_UNIQUE_INDEXES.items():
        if not table_exists(conn, table):
            click.echo(f"warn: table {table} does not exist yet -- skipping index tripwire")
            continue
        present = unique_index_column_sets(conn, table)
        for required in required_sets:
            pretty = ", ".join(sorted(required))
            if required in present:
                click.echo(f"ok: {table} has unique index on ({pretty})")
            else:
                violations.append(f"{table}: missing required unique index on ({pretty})")


def report_breakdown(conn: sqlite3.Connection) -> None:
    """Informational: status mix + long-but-unfragmented pages (no fail)."""
    if not table_exists(conn, "resources"):
        return
    columns = table_columns(conn, "resources")
    if "status" in columns:
        click.echo("--- status breakdown ---")
        for status, count in conn.execute(
            "SELECT status, COUNT(*) FROM resources GROUP BY status ORDER BY 2 DESC"
        ):
            click.echo(f"  {status}: {count}")
    if "content_text" in columns and table_exists(conn, "resources_fragments"):
        long_unfragmented = conn.execute(
            "SELECT COUNT(*) FROM resources r WHERE LENGTH(COALESCE(r.content_text, '')) > ? "
            "AND NOT EXISTS (SELECT 1 FROM resources_fragments f WHERE f.item_id = r.id)",
            (LONG_CONTENT_CHARS,),
        ).fetchone()[0]
        if long_unfragmented:
            click.echo(
                f"note: {long_unfragmented} active page(s) >{LONG_CONTENT_CHARS} chars have no "
                "fragments -- verify the fragmenter handled long guides"
            )


@click.command()
@click.option(
    "--db",
    "db_path",
    default="sg-legal-help.db",
    show_default=True,
    type=click.Path(path_type=Path),
    help="Freshly built database to validate.",
)
@click.option(
    "--previous",
    "previous_path",
    default="sg-legal-help.previous.db",
    show_default=True,
    type=click.Path(path_type=Path),
    help="Snapshot of the previously deployed database (optional).",
)
def main(db_path: Path, previous_path: Path) -> None:
    """Validate the freshly built sg-legal-help database before deployment."""
    if not db_path.exists():
        click.echo(f"FAIL: built database {db_path} does not exist")
        sys.exit(1)

    violations: list[str] = []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        check_row_counts(conn, previous_path, violations)
        check_no_orphan_fragments(conn, violations)
        check_no_empty_active_rows(conn, violations)
        check_unique_indexes(conn, violations)
        report_breakdown(conn)
    finally:
        conn.close()

    if violations:
        click.echo("")
        click.echo(f"SANITY CHECKS FAILED -- {len(violations)} violation(s):")
        for violation in violations:
            click.echo(f"  - {violation}")
        click.echo("Refusing to deploy a database that breaks build invariants.")
        sys.exit(1)
    click.echo("All sanity checks passed.")


if __name__ == "__main__":
    main()
