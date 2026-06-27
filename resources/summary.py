"""AI summaries for sg-legal-help rows (any OpenAI-compatible endpoint).

The summary is the *safe-to-serve* field: a ~100-word plain-language gist the
harry assistant can display and ground on without redistributing all-rights-
reserved full text. It is generated from ``content_text`` at extraction time.

Graceful degradation: if ``LLM_BASE_URL`` is unset the build still runs and
rows are stored with an empty ``summary`` (a backfill pass can fill them later).
A TAILSCALE_PROXY (socks5h://...) routes to a local Ollama when set.
"""

from __future__ import annotations

import os
from typing import Optional

import click
import httpx

SYSTEM_PROMPT = """
You explain Singapore legal-help resources to ordinary people and small-business
owners in plain English. Given the text of an official guide, scheme, or
"where to go" page, write ONE narrative paragraph of at most 100 words that tells
the reader what help this page offers, who it is for, and what to do next (e.g.
eligibility, how to apply, who to contact). Be concrete and practical. Do NOT give
legal advice, do not imply official endorsement, and do not invent details that
are not in the text. This is information with a link to the source, never the
official word.
""".strip()


def get_summary(text: str) -> str:
    """Summarise ``text`` to ~100 plain-language words. Returns "" if no LLM set.

    Never raises: a summary failure must not abort a build (the row is still
    valuable with content_text + link). Failures are logged to stderr.
    """
    base_url = os.environ.get("LLM_BASE_URL", "")
    api_key = os.environ.get("LLM_API_KEY", "")
    model = os.environ.get("LLM_MODEL", "gpt-4.1-mini")
    tailscale_proxy = os.environ.get("TAILSCALE_PROXY", "")

    if not base_url:
        return ""
    if not text or not text.strip():
        return ""

    from openai import OpenAI

    http_client: Optional[httpx.Client] = None
    if tailscale_proxy:
        try:
            http_client = httpx.Client(proxy=httpx.Proxy(tailscale_proxy), timeout=120)
        except Exception as exc:
            click.echo(f"  → Tailscale proxy setup failed: {exc} — direct", err=True)

    client = OpenAI(
        base_url=base_url,
        api_key=api_key or "not-needed",
        max_retries=2,
        timeout=120,
        http_client=http_client,
    )
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Summarise this resource:\n\n{text[:4000]}"},
            ],
        )
        content = response.choices[0].message.content
        return content.strip() if content else ""
    except Exception as exc:
        click.echo(f"  → Summary generation failed: {exc}", err=True)
        return ""
    finally:
        if http_client:
            http_client.close()
