"""scripts/sanity_checks.py click CLI via click.testing.CliRunner (click + sqlite3)."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys

import pytest
from click.testing import CliRunner
from conftest import REPO_ROOT


@pytest.fixture(scope="module")
def sanity():
    spec = importlib.util.spec_from_file_location(
        "sanity_checks_cli", REPO_ROOT / "scripts" / "sanity_checks.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_db(path, resources=(), fragments=(), unique=True):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE resources (id TEXT PRIMARY KEY, source_url TEXT, status TEXT, "
        "content_text TEXT, summary TEXT)"
    )
    conn.execute(
        "CREATE TABLE resources_fragments (id TEXT PRIMARY KEY, item_id TEXT, fragment_order INTEGER)"
    )
    if unique:
        conn.execute("CREATE UNIQUE INDEX u1 ON resources(source_url)")
        conn.execute("CREATE UNIQUE INDEX u2 ON resources_fragments(item_id, fragment_order)")
    conn.executemany("INSERT INTO resources VALUES (?,?,?,?,?)", resources)
    conn.executemany("INSERT INTO resources_fragments VALUES (?,?,?)", fragments)
    conn.commit()
    conn.close()
    return path


def run(sanity, *args):
    return CliRunner().invoke(sanity.main, list(args))


GOOD = [("a", "https://x/a", "active", "some text", ""), ("b", "https://x/b", "dead_link", "", "")]


def test_clean_db_passes(sanity, tmp_path):
    db = make_db(tmp_path / "n.db", GOOD, [("f1", "a", 0)])
    res = run(sanity, "--db", str(db), "--previous", str(tmp_path / "none.db"))
    assert res.exit_code == 0, res.output
    assert "All sanity checks passed." in res.output
    assert "previous database" in res.output  # warned about missing previous


def test_missing_db_fails(sanity, tmp_path):
    res = run(sanity, "--db", str(tmp_path / "nope.db"))
    assert res.exit_code == 1 and "does not exist" in res.output


def test_row_count_decrease_fails(sanity, tmp_path):
    prev = make_db(tmp_path / "p.db", GOOD)
    new = make_db(tmp_path / "n.db", GOOD[:1])
    res = run(sanity, "--db", str(new), "--previous", str(prev))
    assert res.exit_code == 1 and "row count decreased 2 -> 1" in res.output


def test_row_count_growth_ok_and_fragment_shrink_exempt(sanity, tmp_path):
    prev = make_db(tmp_path / "p.db", GOOD[:1], [("f1", "a", 0), ("f2", "a", 1)])
    new = make_db(tmp_path / "n.db", GOOD, [("f1", "a", 0)])
    res = run(sanity, "--db", str(new), "--previous", str(prev))
    assert res.exit_code == 0, res.output
    assert "exempt" in res.output


def test_orphan_fragment_fails(sanity, tmp_path):
    db = make_db(tmp_path / "n.db", GOOD, [("f1", "ghost", 0)])
    res = run(sanity, "--db", str(db), "--previous", str(tmp_path / "none.db"))
    assert res.exit_code == 1 and "orphan" in res.output


def test_empty_active_row_fails(sanity, tmp_path):
    db = make_db(tmp_path / "n.db", [("a", "https://x/a", "active", "  ", None)])
    res = run(sanity, "--db", str(db), "--previous", str(tmp_path / "none.db"))
    assert res.exit_code == 1 and "empty content_text AND summary" in res.output


def test_summary_alone_satisfies_active_row(sanity, tmp_path):
    db = make_db(tmp_path / "n.db", [("a", "https://x/a", "active", "", "a summary")])
    assert run(sanity, "--db", str(db), "--previous", str(tmp_path / "none.db")).exit_code == 0


def test_missing_unique_index_fails(sanity, tmp_path):
    db = make_db(tmp_path / "n.db", GOOD, unique=False)
    res = run(sanity, "--db", str(db), "--previous", str(tmp_path / "none.db"))
    assert res.exit_code == 1
    assert "missing required unique index on (source_url)" in res.output
    assert "(fragment_order, item_id)" in res.output


def test_help_lists_options(sanity):
    res = run(sanity, "--help")
    assert res.exit_code == 0 and "--db" in res.output and "--previous" in res.output
