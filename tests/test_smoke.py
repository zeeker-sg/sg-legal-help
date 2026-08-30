"""Smoke tests for the sg-legal-help Zeeker build.

Guards the build surface a CI run can actually check without crawling:
every production entrypoint must remain importable in the SAME WAY the zeeker
build loop imports it, and every expected entrypoint must exist and be a
callable. This is the CI gate for the monthly sg-legal-help build: if an
adapter file is deleted/renamed, a fetch() is renamed away, or a dependency
breaks an import, CI fails here instead of the first of the month at 03:11
UTC (see RUNBOOK.md).
"""

from __future__ import annotations

import sys
import unittest
from contextlib import contextmanager
from importlib import import_module
from importlib import util as importlib_util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RESOURCES_DIR = REPO_ROOT / "resources"
SOURCES_DIR = RESOURCES_DIR / "sources"

# Every source adapter file that must exist in resources/sources/ and expose a
# callable fetch(). Keep in sync with resources/resources.py's ADAPTERS tuple
# and resources/sources/__init__.py's documented adapter list.
EXPECTED_ADAPTERS = ("lawgowhere", "lab", "probono", "manual")

# Shared-helpers module: every adapter imports it, so it must exist and keep
# its documented API importable — but it exposes helpers, not a fetch().
EXPECTED_HELPERS = {"get_soup", "strip_noise", "text_with_tables", "split_by_heading_text", "derive_title"}

# The single Zeeker resource — loaded by file path (no package context) by
# zeeker >= 0.9.0, with resources/ appended to sys.path so bare sibling
# imports (import build_state, from sources import lab, ...) resolve.
ORCHESTRATOR = REPO_ROOT / "resources" / "resources.py"


@contextmanager
def _resources_dir_on_path():
    """Put resources/ on sys.path exactly the way zeeker does during a build.

    Mirrors zeeker >= 0.9.0 (zeeker/core/database/processor.py): the resources
    dir is APPENDED — lowest precedence, so resources/*.py can never shadow
    stdlib or site-packages — and removed again afterwards. Bare sibling
    imports (import build_state / http_client, from sources import ...) only
    resolve while this is active, which is why production keeps them at module
    top level.
    """
    added = str(RESOURCES_DIR) not in sys.path
    if added:
        sys.path.append(str(RESOURCES_DIR))
    try:
        yield
    finally:
        if added:
            sys.path.remove(str(RESOURCES_DIR))


def _load_orchestrator():
    """Import resources/resources.py by file path — how zeeker loads it.

    zeeker never imports the resource as a package member; it loads the file
    with no package context. A plain ``import resources.resources`` from the
    repo root would NOT exercise the same import semantics.
    """
    assert ORCHESTRATOR.is_file(), f"missing file: {ORCHESTRATOR.relative_to(REPO_ROOT)}"
    name = "_smoke_orchestrator_resources"
    spec = importlib_util.spec_from_file_location(name, ORCHESTRATOR)
    assert spec is not None and spec.loader is not None, "no importable spec for orchestrator"
    module = importlib_util.module_from_spec(spec)
    sys.modules[name] = module
    with _resources_dir_on_path():
        spec.loader.exec_module(module)
    return module


def _load_adapter(name: str):
    """Import a source adapter the way production reaches it: via the package.

    Adapters are never by-path loads — the orchestrator's ``from sources
    import lawgowhere, lab, probono, manual`` brings them in as
    ``sources.*`` package modules, so that is what we verify here.
    """
    path = SOURCES_DIR / f"{name}.py"
    assert path.is_file(), f"missing file: {path.relative_to(REPO_ROOT)}"
    with _resources_dir_on_path():
        return import_module(f"sources.{name}")


class TestSourceAdapters(unittest.TestCase):
    """Every adapter in resources/sources/ imports and exposes callable fetch()."""

    def test_every_expected_adapter_file_is_present(self):
        expected_files = sorted(f"{name}.py" for name in (*EXPECTED_ADAPTERS, "_common"))
        missing = [
            fname for fname in expected_files if not (SOURCES_DIR / fname).is_file()
        ]
        self.assertEqual(
            missing,
            [],
            f"expected adapter file(s) missing from resources/sources/: {missing}",
        )

    def test_every_expected_adapter_imports_and_exposes_fetch(self):
        for adapter in EXPECTED_ADAPTERS:
            with self.subTest(adapter=adapter):
                module = _load_adapter(adapter)
                self.assertTrue(
                    callable(getattr(module, "fetch", None)),
                    f"resources/sources/{adapter}.py must expose a callable fetch()",
                )

    def test_common_helpers_import_and_expose_documented_api(self):
        module = _load_adapter("_common")
        missing = sorted(name for name in EXPECTED_HELPERS if not callable(getattr(module, name, None)))
        self.assertEqual(
            missing,
            [],
            f"resources/sources/_common.py is missing callable(s): {missing}",
        )


class TestOrchestrator(unittest.TestCase):
    """Resources/resources.py imports like production and exposes its API."""

    def test_orchestrator_imports_the_way_zeeker_loads_it(self):
        module = _load_orchestrator()
        self.assertTrue(
            callable(getattr(module, "fetch_data", None)),
            "orchestrator must expose a callable fetch_data (zeeker 0.9.0 "
            "single-fetch lifecycle calls it exactly once per build)",
        )
        self.assertTrue(
            callable(getattr(module, "fetch_fragments_data", None)),
            "orchestrator must expose a callable fetch_fragments_data "
            "(fragments resource phase)",
        )

    def test_orchestrator_adapter_registry_lists_expected_sources(self):
        module = _load_orchestrator()
        # Adapters enter ADAPTERS as package modules, so strip any qualification
        # (``sources.lab`` -> ``lab``) before comparing.
        registered = {
            getattr(a, "__name__", "").rpartition(".")[2] for a in getattr(module, "ADAPTERS", ())
        }
        self.assertEqual(
            registered,
            {"lawgowhere", "lab", "probono", "manual"},
            "ADAPTERS registry should list every live source module",
        )