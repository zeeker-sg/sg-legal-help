"""Every module under resources/ (and scripts/sanity_checks.py) must import.

This is the cheapest dependency-bump tripwire: a Dependabot PR that removes or
renames anything we import at module top level (bs4, lxml, httpx, tenacity,
click, openai, sqlite_utils, zeeker) fails here first.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys

import pytest
from conftest import REPO_ROOT, RESOURCES_DIR

BARE_MODULES = [
    "build_state",
    "http_client",
    "ids",
    "summary",
    "sources",
    "sources._common",
    "sources.lab",
    "sources.lawgowhere",
    "sources.probono",
    "sources.manual",
]


@pytest.mark.parametrize("name", BARE_MODULES)
def test_bare_module_imports(name):
    # Bare names are how zeeker (>= 0.9.0) makes siblings importable.
    assert importlib.import_module(name) is not None


def test_orchestrator_loads_by_path_like_zeeker():
    path = RESOURCES_DIR / "resources.py"
    spec = importlib.util.spec_from_file_location("_imports_orchestrator", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    assert callable(module.fetch_data) and callable(module.fetch_fragments_data)
    assert callable(module.transform_data) and callable(module.transform_fragments_data)


def test_sanity_checks_script_imports_and_exposes_click_command():
    path = REPO_ROOT / "scripts" / "sanity_checks.py"
    spec = importlib.util.spec_from_file_location("_sanity_checks", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    import click

    assert isinstance(module.main, click.Command)


def test_every_py_file_under_resources_is_covered():
    on_disk = {
        p.relative_to(RESOURCES_DIR).with_suffix("").as_posix().replace("/", ".")
        for p in RESOURCES_DIR.rglob("*.py")
    }
    on_disk -= {"resources", "__init__", "sources.__init__"}
    covered = set(BARE_MODULES)
    assert on_disk <= covered, f"add new module(s) to BARE_MODULES: {sorted(on_disk - covered)}"
