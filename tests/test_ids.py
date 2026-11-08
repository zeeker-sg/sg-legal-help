"""Deterministic id minting (hashlib + urllib)."""

from __future__ import annotations

import hashlib
import re

import ids


def test_hash_id_is_first_12_hex_of_sha256():
    expected = hashlib.sha256(b"abc").hexdigest()[:12]
    assert ids.hash_id("abc") == expected
    assert re.fullmatch(r"[0-9a-f]{12}", ids.hash_id("anything"))


def test_resource_id_is_stable_and_pinned():
    url = "https://lawgowhere.sg/get-help/"
    assert ids.resource_id(url) == ids.resource_id(url)
    # Pinned recipe: changing it is a migration (see ids.py docstring).
    assert ids.resource_id(url) == ids.hash_id("resource:https://lawgowhere.sg/get-help")


def test_equivalent_urls_collapse_to_one_id():
    variants = [
        "https://LawGoWhere.sg/get-help/",
        "https://lawgowhere.sg/get-help",
        "  https://lawgowhere.sg/get-help/?utm_source=x#top ",
    ]
    assert len({ids.resource_id(v) for v in variants}) == 1


def test_different_paths_get_different_ids():
    assert ids.resource_id("https://a.test/x") != ids.resource_id("https://a.test/y")


def test_normalize_url_rules():
    assert ids.normalize_url("HTTPS://Example.COM/A/b/?q=1#f") == "https://example.com/A/b"
    assert ids.normalize_url("https://example.com") == "https://example.com/"
    assert ids.normalize_url("//example.com/x") == "https://example.com/x"


def test_fragment_id_stable_and_order_sensitive():
    parent = ids.resource_id("https://a.test/x")
    assert ids.fragment_id(parent, 0) == ids.fragment_id(parent, "0")
    assert ids.fragment_id(parent, 0) != ids.fragment_id(parent, 1)
    assert ids.fragment_id(parent, 0) == ids.hash_id(f"{parent}:0")
