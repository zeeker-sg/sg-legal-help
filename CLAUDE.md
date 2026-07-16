# CLAUDE.md — sg-legal-help development guide

Curated, plain-language Singapore consumer/SME legal-**help** resources (routing, how-to,
rights explainers) published as a Zeeker data source and consumed by the harry assistant. See
`README.md` for scope/data-model intent; this file is the maintenance guide.

## Architecture

- **One Zeeker resource, `resources`** (fragments-enabled → tables `resources` +
  `resources_fragments`). ALL sources land in this single URL-keyed catalogue; `source_method`
  distinguishes `crawled` vs `manual`. Do NOT add one Zeeker resource per website.
- **Source adapters** live in `resources/sources/`. Each exposes `fetch() -> list[page dict]`
  (page dict shape documented in `resources/sources/__init__.py`). The orchestrator
  `resources/resources.py` calls every adapter, fills housekeeping (id, content_hash, summary,
  timestamps, status), and splits each page's `fragments` into `resources_fragments`.
  - `sources/lawgowhere.py` — LawGoWhere (live). Has its own extraction helpers (predates
    `_common.py`; verified — leave untouched).
  - `sources/lab.py` — Legal Aid Bureau (live). Sitemap-driven; uses `_common.py`.
  - `sources/probono.py` — Pro Bono SG (live). Fixed seed set of net-new pages; uses `_common.py`.
  - `sources/manual.py` — deferred stub (manifest + add-CLI not built yet).
  - `sources/_common.py` — shared helpers for the newer adapters: `get_soup` (HTML/XML, dev
    cache), `strip_noise`, `text_with_tables` (linearises `<table>` to `a | b | c`),
    `split_by_heading_text` (slices flattened content at section headings), `derive_title`.
  - **CJC is DEFERRED** — `cjc.org.sg` is unreachable (NXDOMAIN / connection failure on all hosts;
    the only live candidate is a Microsoft Power Pages UAT portal behind auth). Revisit, or
    hand-curate its programmes (OSLAS/GPS/FLiP/HELP/PJP) via the manual-seed manifest.
- **Helpers** (`resources/`): `ids.py` (opaque SHA-256 ids + URL normalisation), `http_client.py`
  (polite client: UA, delay+jitter, tenacity backoff floored at the delay, circuit breaker, atomic
  `.cache/` writes), `summary.py` (OpenAI-compatible summaries, graceful skip), `build_state.py`
  (build DB handle for schema pre-creation).

## Zeeker build-loop notes (requires zeeker >= 0.9.0 — keep them intact)

- **Single-fetch lifecycle.** zeeker 0.9.0 loads the resource module ONCE per build and calls
  `fetch_data` ONCE (no reload between the main and fragments phases). The fragments bridge is
  the in-memory module global `_pending_fragment_pages` — the pre-0.9.0 per-build marker and
  fragments disk cache are GONE; do not reintroduce them. Crash recovery is `--sync-from-s3`
  (an undeployed partial build is discarded and re-crawled next run).
- **First build has no tables / unique indexes.** `ensure_schema()` pre-creates both tables + the
  unique-index tripwires up front (zeeker only creates tables from returned rows *after* the
  resource runs). Preserve this and the tripwires.
- **Sibling imports must stay top-level.** zeeker puts `resources/` on `sys.path` ONLY while the
  resource module loads — the old `sys.path.insert` shims were removed, and any lazy in-function
  import of a sibling module would fail at call time. Ruff knows the siblings as first-party
  (`[tool.ruff.lint.isort] known-first-party` in pyproject.toml).
- **Skip semantics.** `fetch_data` raises `Skip(kind="blocked")` only when EVERY adapter raises
  (total outage — the freshness marker must not advance). Partial adapter failure continues with
  the healthy adapters' rows.
- **`__zeeker_report__` counters** (`pages_crawled`, `new_rows`, `unchanged`, `adapters_failed`,
  optional `notes`) surface on the build status line and in `--json` — keep them accurate when
  touching `fetch_data`.
- **`fragments_on_skip` stays UNSET in zeeker.toml** — see RUNBOOK.md ("What happens during a
  run") for the unique-index rationale.

## Build / refresh / deploy

This project is run by the user's personal agent (NOT GitHub Actions). The deliverable is the
entrypoint script:

```
scripts/nightly.sh              # build --sync-from-s3 --setup-fts -> sanity gate -> NO deploy
scripts/nightly.sh --deploy     # ... -> zeeker deploy (S3, data.zeeker.sg)
```

`scripts/sanity_checks.py` gates the deploy: append-mostly row count on `resources`
(fragments exempt), no orphan fragments, no empty active rows, unique-index tripwires intact.
Any non-zero exit blocks deploy.

**Operations & monitoring live in `RUNBOOK.md`** — run narrative, healthy-log examples,
zeeker 0.9.0 status contract (Skip kinds, report counters, exit codes), cadence/yield
expectations, failure modes + recovery, and backlog SQL. This file keeps only what a
developer changing code needs.

**Cadence:** LawGoWhere explainers/schemes are low-volatility — **Tier 3 (monthly)**
re-check (per-row `volatility = 3`). No source ToU extraction window; no robots.txt (the site
serves none), so the polite 2s default delay is the floor.

## Environment variables (`.env`)

- `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` — AI summaries. **Unset ⇒ summaries skipped, build
  still runs** (rows stored with empty `summary`; backfill once configured). `TAILSCALE_PROXY`
  routes to a local Ollama.
- `SGLEGALHELP_DELAY_SECONDS` / `SGLEGALHELP_JITTER_SECONDS` — crawl pacing overrides.
- `SGLEGALHELP_HTML_CACHE=1` — dev only: cache raw HTML under `.cache/lawgowhere_html` so repeated
  builds don't re-hit the site. OFF in production (live crawl keeps change-detection honest).
- `S3_BUCKET` / `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `S3_ENDPOINT_URL` — deploy only.

## Source notes

### `lawgowhere.py`

- **Source:** https://lawgowhere.sg — MinLaw-supported portal operated by Pro Bono SG.
  Server-rendered static HTML (IIS/ASP.NET); httpx + BeautifulSoup, no headless browser.
- **Licensing:** all-rights-reserved (Pro Bono SG). Full `content_text` is stored for FTS but
  every row sets `can_store_fulltext = 0` so the serving layer shows only summary + link +
  attribution. `attribution` credits Pro Bono SG + MinLaw.
- **What it catalogues (3 groups):** `get-information` leaf explainer articles (rights_explainer,
  accordion Q&A → fragments; ~18 pages, discovered from the category grids — indexes are used for
  discovery only, not stored), `get-help` (the legal-aid scheme directory; contact_directory,
  `is_routing_destination`; the 27 hidden `div.form-html` blocks → fragments + tel:/mailto:
  contacts), and `contact-us` (contact_directory, routing).
- **Title trap:** every page ships an empty `<title>` — titles are derived from on-page H1/H2.
- **DEFERRED (TODO):** multimedia (past-webinars / podcasts / events) and about/privacy pages.

### `lab.py` — Legal Aid Bureau (lab.mlaw.gov.sg)
- Static Isomer (Jekyll) site. **Sitemap-driven** discovery (`/sitemap.xml`): drops PDFs
  (`/files/`, `.pdf`) and empty template stubs (`/faq/`, `/search/`, `/resource-room*`,
  `/about-us/permalink/`); a runtime empty-body guard (< 120 chars) skips unbuilt shells.
- Title from `<title>` (LAB has real titles, unlike LawGoWhere). `source_last_updated` from the
  sitemap `<lastmod>` (the footer "Last Updated" is a site-wide build string — do NOT use it).
- `resource_kind`/`topic`/`is_routing_destination` classified by URL prefix in `_classify`.
- **STRICT licensing:** MLAW ToU forbids reproduction without written approval → every row
  `can_store_fulltext = 0`. Routing pages (useful-links, contact-us, clinic lists) are kept as
  thin routing stubs (they point to destinations already in the catalogue).

### `probono.py` — Pro Bono SG (probono.sg)
- SAME publisher as LawGoWhere → most navigation prose is duplicative. Ingests only a **fixed
  seed set** of net-new/substantive pages (legal-representation, CLAS FAQ, legal-clinics-in-
  singapore, the 4 audience-guidance pages); DROPS the LawGoWhere-equivalent hubs/contact-us.
- WordPress/Elementor; `_best_container` picks the richest content region. CLAS FAQ uses a
  `_faq_fragments` fallback (Q&A render as `<li><strong>Q</strong> A</li>`, no headings).
  `source_last_updated` from `meta[article:modified_time]`. All-rights-reserved → `can_store_fulltext=0`.

## Next phases (out of scope)

Manual-seed manifest (`resources/manual/seed.yaml`) + add-CLI (could hold CJC's programmes);
remaining sources (consumer: CASE; employment: MOM/TADM; housing/money: HDB/IRAS/CPF);
content-hash-based incremental re-summarise + link-rot status updates; enabling deploy after
human review.
