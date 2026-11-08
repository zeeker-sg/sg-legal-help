"""The zeeker contract: zeeker.toml <-> resources.py schema <-> built SQLite.

Covers zeeker (Skip, ZeekerProjectManager.build_database, FTS setup),
sqlite-utils (table creation, unique indexes, existing_table) and the
orchestrator's row shape. Adapters are stubbed; no crawling.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
import tomllib

import pytest
import sqlite_utils
from conftest import REPO_ROOT, RESOURCES_DIR
from zeeker import Skip, ZeekerProject, ZeekerProjectManager

from sources import lab, lawgowhere, manual, probono


@pytest.fixture
def orch():
    """Fresh load of resources/resources.py by file path (as zeeker does)."""
    spec = importlib.util.spec_from_file_location(
        "_contract_orchestrator", RESOURCES_DIR / "resources.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def page(url="https://x.test/a/", **over):
    base = {
        "source_url": url,
        "title": "T",
        "publisher": "P",
        "content_text": "alpha beta gamma " * 10,
        "fragments": [
            {"heading": "H1", "content_type": "faq", "content_text": " first fragment "},
            {"heading": "H2", "content_text": "second fragment"},
            {"heading": "empty", "content_text": "   "},
        ],
    }
    base.update(over)
    return base


def stub_adapters(monkeypatch, orch, by_name):
    for mod in (lawgowhere, lab, probono, manual):
        short = mod.__name__.rpartition(".")[2]
        val = by_name.get(short, [])
        if isinstance(val, Exception):
            monkeypatch.setattr(mod, "fetch", lambda v=val: (_ for _ in ()).throw(v))
        else:
            monkeypatch.setattr(mod, "fetch", lambda v=val: v)


# ------------------------------------------------------------------- toml/schema
def test_zeeker_toml_loads_with_zeeker_and_matches_schema(orch):
    project = ZeekerProject.from_toml(REPO_ROOT / "zeeker.toml")
    assert project.database == "sg-legal-help.db"
    assert "resources" in project.resources
    toml = tomllib.loads((REPO_ROOT / "zeeker.toml").read_text())
    documented = set(toml["resource"]["resources"]["columns"])
    assert documented == set(
        orch.RESOURCES_SCHEMA
    ), "zeeker.toml columns drifted from RESOURCES_SCHEMA"
    cfg = toml["resource"]["resources"]
    assert set(cfg["facets"]) <= set(orch.RESOURCES_SCHEMA)
    assert set(cfg["fts_fields"]) <= set(orch.RESOURCES_SCHEMA)
    assert set(cfg["fragments_fts_fields"]) <= set(orch.FRAGMENTS_SCHEMA)


def test_ensure_schema_creates_tables_and_unique_indexes_idempotently(orch):
    db = sqlite_utils.Database(memory=True)
    orch.ensure_schema(db)
    orch.ensure_schema(db)
    assert set(db["resources"].columns_dict) == set(orch.RESOURCES_SCHEMA)
    assert db["resources"].pks == ["id"]
    uniques = {
        tuple(i.columns)
        for t in ("resources", "resources_fragments")
        for i in db[t].indexes
        if i.unique
    }
    assert ("source_url",) in uniques and ("item_id", "fragment_order") in uniques


# --------------------------------------------------------------------- fetch_data
def test_fetch_data_row_shape_and_defaults(orch, monkeypatch):
    stub_adapters(monkeypatch, orch, {"lawgowhere": [page()]})
    rows = orch.fetch_data(None)
    assert len(rows) == 1
    row = rows[0]
    assert set(row) == set(orch.RESOURCES_SCHEMA)
    import ids

    assert row["id"] == ids.resource_id("https://x.test/a/")
    assert row["summary"] == ""  # no LLM configured -> graceful empty summary
    assert row["is_routing_destination"] == 0 and row["can_store_fulltext"] == 0
    assert (
        row["source_method"] == "crawled" and row["status"] == "active" and row["volatility"] == 3
    )
    assert len(row["content_hash"]) == 64
    assert row["last_checked"] == row["created_at"]
    for key, typ in orch.RESOURCES_SCHEMA.items():
        if row[key] is not None:
            assert isinstance(row[key], typ), key
    assert orch.__zeeker_report__["new_rows"] == 1


def test_fetch_data_dedupes_within_run_and_blank_urls(orch, monkeypatch):
    stub_adapters(
        monkeypatch,
        orch,
        {
            "lawgowhere": [
                page("https://x.test/a/"),
                page("https://X.test/a?utm=1"),
                {"title": "no url"},
            ],
            "lab": [page("https://x.test/b/")],
        },
    )
    rows = orch.fetch_data(None)
    # "/a/" and "X.test/a?utm=1" normalise to the same key; the url-less page is ignored.
    assert [r["source_url"] for r in rows] == ["https://x.test/a/", "https://x.test/b/"]
    report = orch.__zeeker_report__
    assert report["pages_crawled"] == 4 and report["new_rows"] == 2 and report["unchanged"] == 2


def test_fetch_data_skips_urls_already_in_catalogue(orch, monkeypatch, tmp_path):
    db = sqlite_utils.Database(tmp_path / "sg-legal-help.db")
    orch.ensure_schema(db)
    db["resources"].insert({"id": "old", "source_url": "https://x.test/a"}, pk="id", alter=True)
    stub_adapters(
        monkeypatch, orch, {"lawgowhere": [page("https://x.test/a/"), page("https://x.test/new/")]}
    )
    rows = orch.fetch_data(db["resources"])
    assert [r["source_url"] for r in rows] == ["https://x.test/new/"]
    assert orch.__zeeker_report__["unchanged"] == 1


def test_partial_adapter_failure_continues_and_reports(orch, monkeypatch):
    stub_adapters(monkeypatch, orch, {"lawgowhere": RuntimeError("site down"), "lab": [page()]})
    rows = orch.fetch_data(None)
    assert len(rows) == 1
    report = orch.__zeeker_report__
    assert report["adapters_failed"] == 1 and "site down" in report["notes"]


def test_all_adapters_failing_raises_blocked_skip(orch, monkeypatch):
    boom = RuntimeError("everything down")
    stub_adapters(monkeypatch, orch, {n: boom for n in ("lawgowhere", "lab", "probono", "manual")})
    with pytest.raises(Skip) as err:
        orch.fetch_data(None)
    assert err.value.kind == "blocked"


def test_summary_is_generated_from_content_when_llm_configured(orch, monkeypatch):
    import summary

    monkeypatch.setattr(summary, "get_summary", lambda text: "SUMMARY:" + text[:5])
    stub_adapters(
        monkeypatch, orch, {"lawgowhere": [page(), page("https://x.test/b/", summary="given")]}
    )
    rows = {r["source_url"]: r for r in orch.fetch_data(None)}
    assert rows["https://x.test/a/"]["summary"] == "SUMMARY:alpha"
    assert rows["https://x.test/b/"]["summary"] == "given"


# ----------------------------------------------------------------------- fragments
def test_fragments_follow_main_phase_and_clear_bridge(orch, monkeypatch):
    stub_adapters(monkeypatch, orch, {"lawgowhere": [page()]})
    (row,) = orch.fetch_data(None)
    frags = orch.fetch_fragments_data(None, [row])
    assert [f["fragment_order"] for f in frags] == [0, 1]  # blank fragment dropped, no gaps
    assert frags[0]["content_text"] == "first fragment" and frags[0]["char_count"] == len(
        "first fragment"
    )
    assert frags[1]["content_type"] == "paragraph"  # default
    assert all(f["item_id"] == row["id"] for f in frags)
    assert set(frags[0]) == set(orch.FRAGMENTS_SCHEMA)
    assert orch.fetch_fragments_data(None, [row]) == []  # bridge consumed


def test_fragments_replace_stale_rows_for_recrawled_page(orch, monkeypatch, tmp_path):
    db = sqlite_utils.Database(tmp_path / "sg-legal-help.db")
    orch.ensure_schema(db)
    db["resources_fragments"].insert(
        {
            "id": "stale",
            "item_id": "abc",
            "fragment_order": 0,
            "heading": "",
            "content_type": "p",
            "content_text": "old",
            "char_count": 3,
        }
    )
    orch._pending_fragment_pages = [{"item_id": "abc", "fragments": [{"content_text": "new text"}]}]
    out = orch.fetch_fragments_data(db["resources_fragments"])
    assert [f["content_text"] for f in out] == ["new text"]
    assert db["resources_fragments"].count == 0  # old rows deleted; zeeker inserts the new ones


# ------------------------------------------------------- real zeeker build, e2e
@pytest.fixture
def project(tmp_path_factory):
    root = tmp_path_factory.mktemp("zproject")
    shutil.copy(REPO_ROOT / "zeeker.toml", root / "zeeker.toml")
    shutil.copytree(RESOURCES_DIR, root / "resources", ignore=shutil.ignore_patterns("__pycache__"))
    return root


def test_real_zeeker_build_produces_valid_database_and_passes_sanity_checks(
    project, monkeypatch, orch
):
    stub_adapters(
        monkeypatch, orch, {"lawgowhere": [page(), page("https://x.test/b/", fragments=[])]}
    )
    monkeypatch.chdir(project)
    result = ZeekerProjectManager(project).build_database(setup_fts=True)
    assert result.is_valid, result.errors

    db = sqlite_utils.Database(project / "sg-legal-help.db")
    assert db["resources"].count == 2 and db["resources_fragments"].count == 2
    assert {"resources_fts", "resources_fragments_fts"} <= set(db.table_names())
    hits = list(db["resources"].search("alpha"))
    assert len(hits) == 2
    uniq = {tuple(i.columns) for i in db["resources"].indexes if i.unique}
    assert ("source_url",) in uniq

    # The sanity-check CLI accepts what the build just produced.
    from click.testing import CliRunner

    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        import sanity_checks
    finally:
        sys.path.remove(str(REPO_ROOT / "scripts"))
    res = CliRunner().invoke(sanity_checks.main, ["--db", str(project / "sg-legal-help.db")])
    assert res.exit_code == 0, res.output
    assert "All sanity checks passed." in res.output


def test_real_zeeker_build_with_all_adapters_down_does_not_write_rows(project, monkeypatch, orch):
    boom = RuntimeError("down")
    stub_adapters(monkeypatch, orch, {n: boom for n in ("lawgowhere", "lab", "probono", "manual")})
    monkeypatch.chdir(project)
    ZeekerProjectManager(project).build_database()
    db = sqlite_utils.Database(project / "sg-legal-help.db")
    assert not db["resources"].exists() or db["resources"].count == 0
