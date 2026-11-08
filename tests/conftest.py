"""Shared offline test plumbing.

Everything here exists to guarantee the suite never touches the network:
polite delays/backoff are disabled, LLM/OpenAI env vars are scrubbed, and HTTP
is served from ``httpx.MockTransport`` backed by saved fixtures.
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path
from typing import Callable, Dict, Optional, Union

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RESOURCES_DIR = REPO_ROOT / "resources"
FIXTURES = Path(__file__).parent / "fixtures"

# zeeker >= 0.9.0 appends resources/ to sys.path while loading the resource, so
# sibling modules import bare (``import http_client``, ``from sources import``).
if str(RESOURCES_DIR) not in sys.path:
    sys.path.append(str(RESOURCES_DIR))


@pytest.fixture(autouse=True)
def offline_env(monkeypatch, tmp_path):
    """No sleeping, no LLM, no cache writes into the repo, no ambient config."""
    monkeypatch.setenv("SGLEGALHELP_NO_DELAY", "1")
    for var in (
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "LLM_BASE_URL",
        "LLM_API_KEY",
        "LLM_MODEL",
        "TAILSCALE_PROXY",
        "SGLEGALHELP_HTML_CACHE",
    ):
        monkeypatch.delenv(var, raising=False)

    # Belt and braces: any real socket connection is a test bug.
    def no_network(self, *args, **kwargs):
        raise RuntimeError("network access attempted in an offline test")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    # Relative paths (.cache/, zeeker.toml, sg-legal-help.db) land in tmp.
    monkeypatch.chdir(tmp_path)


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


Route = Union[str, tuple]  # fixture filename | (status, body)


def mock_transport(routes: Dict[str, Route], seen: Optional[list] = None) -> httpx.MockTransport:
    """Serve ``routes`` keyed by URL path. ``"x.html"`` -> that fixture (200);
    ``(status, body)`` -> literal response; unknown paths -> 404."""

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        route = routes.get(request.url.path)
        if route is None:
            return httpx.Response(404, text="not found")
        if isinstance(route, tuple):
            return httpx.Response(route[0], text=route[1])
        return httpx.Response(200, text=fixture_text(route))

    return httpx.MockTransport(handler)


@pytest.fixture
def make_client():
    """Factory: ``make_client(routes) -> PoliteClient`` over a MockTransport."""
    import http_client

    clients = []

    def factory(routes: Dict[str, Route], seen: Optional[list] = None, **kwargs):
        client = http_client.PoliteClient(transport=mock_transport(routes, seen), **kwargs)
        clients.append(client)
        return client

    yield factory
    for c in clients:
        c.close()


@pytest.fixture
def patch_polite_client(monkeypatch) -> Callable[[Dict[str, Route]], list]:
    """Make adapters' ``http_client.PoliteClient()`` use a MockTransport.

    Returns the list that records every request, so tests can assert on the
    crawl footprint.
    """
    import http_client

    real = http_client.PoliteClient

    def install(routes: Dict[str, Route]) -> list:
        seen: list = []
        transport = mock_transport(routes, seen)
        monkeypatch.setattr(
            http_client, "PoliteClient", lambda **kw: real(transport=transport, **kw)
        )
        return seen

    return install
