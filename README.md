# sg-legal-help

A curated, plain-language database of Singapore consumer and small-business legal **help
resources** — official guidance, "how-to" explainers, and where-to-go routing — built as a
[Zeeker](https://zeeker.sg) data source, served via Datasette on `data.zeeker.sg`, and consumed
by the **harry** legal assistant through the gateway-brokered Zeeker MCP connector.

> **One-line description** (for the repo's About field and `zeeker.toml`): *Curated, plain-language
> Singapore consumer/SME legal-help resources — official guidance, how-to explainers, and
> where-to-go routing — indexed for the harry assistant.*

---

## What this is

The existing Zeeker databases hold *primary and professional* material: court judgments
(`zeeker-judgements`), PDPC enforcement decisions and guidelines (`pdpc`), government newsroom
press releases (`sg-gov-newsrooms`), and curated legal commentary (`sglawwatch`). None of them
hold the plain-language explainer layer an ordinary person actually needs: *"how do I get my
deposit back,"* *"do I need a contract to hire a part-timer,"* *"I got a demand letter — what do
I do,"* *"who can help me for free."*

`sg-legal-help` fills that gap. It curates **official, authoritative, consumer-grade guidance**
mapped to the everyday situations people arrive with, so the harry assistant can ground
plain-language answers on plain-language sources — and point users to the right official body or
help scheme when a matter needs a human.

This is a **data pipeline, not an application**: a standalone Zeeker project that fetches,
extracts, summarizes, and publishes a searchable database. The harry app retrieves from it at
runtime; it is not part of the app's runtime.

---

## Scope

**Rule of thumb:** explainers and pointers go *in*; primary-source decisions, legislation, and
news stay in their own databases.

**In scope**

- Plain-language **rights explainers** and **how-to guides** from official / authoritative
  sources (ministries, statutory boards, courts and tribunals, recognised access-to-justice
  bodies).
- **Process steps** for the routes a layperson takes (e.g. filing at the Small Claims Tribunals,
  TADM mediation).
- **Where-to-go / who-to-contact** directories (legal aid, pro bono clinics, sector regulators).
  These double as the assistant's escalation / routing map.

**Out of scope** (these have homes elsewhere)

- Court judgments, tribunal / PDPC enforcement *decisions*, other primary-source rulings →
  decisions databases. *Example:* Strata Titles Board *decisions* belong in a decisions DB; only
  a plain-language "what the STB does / how to bring a strata claim" explainer belongs here.
- Raw legislation / statutes → a separate legislation corpus (different licensing regime).
- Ministry press releases / news → `sg-gov-newsrooms`.
- Academic commentary → `sglawwatch`.
- Third-party law-firm SEO / explainer content → excluded by default, link-only at most. The
  curated set stays official; that is its value.

Before ingesting any source, **de-duplicate against the sibling Zeeker databases** so the same
material does not appear twice.

---

## Data model

A single catalogue table, `resources` (one row per guide / page), with an optional fragments
table for long, multi-section guides — the catalogue-plus-fragments shape Zeeker already uses for
`about_singapore_law`. URL-keyed on the canonical source URL; opaque hash IDs.

| Group | Fields |
| --- | --- |
| Identity | `id`, `source_url` |
| Source metadata | `title`, `publisher`, `publisher_type`, `source_last_updated` (the page's own stated date) |
| Curation | `topic` (controlled vocabulary), `audience` (individual / sme / both), `resource_kind` (rights_explainer / how_to / process_steps / faq / contact_directory / form / calculator) |
| Routing | `is_routing_destination` (official "where to go" endpoint?) |
| Content | `summary` (AI, ~100 words — the safe-to-serve display + grounding field); `content_text` (full text, **indexed for search but not served** where licensing requires) |
| Licensing / provenance | `license`, `license_url`, `attribution`, `can_store_fulltext` |
| Freshness / housekeeping | `source_method` (crawled / manual), `volatility`, `last_checked`, `content_hash`, `status` (active / superseded / dead_link), `created_at` |

Long guides are split into a `resources_fragments` table on the page's own section headings
(*Eligibility* / *How to apply* / *Fees*) so the assistant retrieves the right slice rather than
a whole page.

The authoritative column definitions live in `zeeker.toml`; `CLAUDE.md` documents the
implementation and maintenance details.

---

## Sources

Curation prioritises the access-to-justice / routing layer first (it doubles as the escalation
map), then the high-volume topics.

- **Access to justice / routing** — LawGoWhere (`lawgowhere.sg`) ✅ *live*, Legal Aid Bureau
  (`lab.mlaw.gov.sg`) ✅ *live*, Pro Bono SG (`probono.sg`) ✅ *live*, Community Justice Centre
  (`cjc.org.sg`) ⏸ *deferred — site unreachable (Power Pages portal); candidate for the manual seed*.
- **Consumer & contracts** — CASE (`case.org.sg`).
- **Employment** — MOM employment practices (`mom.gov.sg/employment-practices`), TADM
  (`tal.sg/tadm`), State Courts / CJTS (`judiciary.gov.sg`).
- **Housing & money** (tied to the harry document templates) — HDB renting (`hdb.gov.sg`), IRAS
  stamp duty (`iras.gov.sg`), CPF (`cpf.gov.sg`).

---

## Licensing & redistribution posture

This determines how content is stored, and it is checked **per source**.

- Most Singapore government *guidance pages* are all-rights-reserved website content, **not**
  Open-Data-Licence datasets. Default handling: **index the full text for search, but serve only
  the AI summary plus a link to the source** — the same pattern `sg-gov-newsrooms` uses.
  `can_store_fulltext = false` unless a source is explicitly permissive. *(LawGoWhere is
  all-rights-reserved Pro Bono SG content, so every row is stored with `can_store_fulltext = 0`.)*
- Where a source is published under the **Singapore Open Data Licence** or a **Creative Commons**
  licence, fuller reuse is allowed with attribution; record the actual licence on the row.
- Every row carries its own `license` / `attribution`, so the project-level data licence is
  **mixed**.
- Content is presented as **information with a link to the official source — never as the
  official word, and never as legal advice.** Do not imply official status or endorsement.
- **Code** in this repository is MIT. The **data** licence is separate and per-source (above).

> Compliance posture only — not a legal conclusion. The licensing handling and not-advice framing
> should be reviewed by a Singapore-qualified lawyer before launch.

---

## Refresh / freshness

Freshness is treated as a feature: a stale official figure that the assistant grounds on is the
worst failure mode for a legal product.

- **Volatility-tiered cadence.** Most explainers re-check monthly (Tier 3); pages with numbers
  that move — claim caps, means-test thresholds, fee / stamp-duty schedules — weekly (Tier 2);
  structural background least often. Driven by the per-row `volatility` flag.
- **Hash-based change detection.** Each run recomputes `content_hash` over normalised text.
  Unchanged → bump `last_checked` only (no LLM cost). Changed → re-extract / re-summarize /
  re-fragment and bump the dates. *(Incremental re-summarise/link-rot is a later refinement; the
  first build inserts new pages and dedupes by canonical URL.)*
- **Liveness + `status`.** Dead or redirected links are marked `superseded` / `dead_link`;
  retrieval filters to `status = active`.
- **Staleness signalled to the product.** `last_checked` is carried through.
- **Incremental builds + a human glance.** Builds sync the existing DB and update only changed
  rows; the sanity gate (`scripts/sanity_checks.py`) blocks a regressed deploy.

---

## One-off / manual entries

Not every useful resource comes from a crawlable catalogue. Ad-hoc additions go through a
**manifest-backed `manual` resource** (`resources/sources/manual.py`), never a direct insert into
the served DB. *(Deferred: the manifest `resources/manual/seed.yaml` and the add-CLI are not built
yet; the adapter is a stub.)*

- Hand-curated entries live in a git-tracked manifest; the `manual` adapter reads it and emits
  rows into the same `resources` table with `source_method = manual`.
- A small **add CLI** takes a URL (plus optional curation fields), de-dupes by normalised URL,
  fetches once to auto-fill title / summary / dates, suggests a `topic` and `resource_kind`, and
  appends a validated entry.
- Because manual entries flow through the normal build, they inherit the full refresh pipeline.

---

## Project structure

```
sg-legal-help/
├── pyproject.toml            # dependencies (uv-managed)
├── zeeker.toml               # project + resource config, column docs
├── resources/
│   ├── resources.py          # the single `resources` catalogue (orchestrator)
│   ├── ids.py                # opaque ids + URL normalisation
│   ├── http_client.py        # polite client (delay/jitter/retry/circuit breaker)
│   ├── summary.py            # AI summaries (graceful skip)
│   ├── build_state.py        # build-DB handle (schema pre-creation)
│   └── sources/              # one adapter per source
│       ├── _common.py        # shared extraction helpers (newer adapters)
│       ├── lawgowhere.py     # LawGoWhere (live)
│       ├── lab.py            # Legal Aid Bureau (live)
│       ├── probono.py        # Pro Bono SG (live)
│       └── manual.py         # deferred stub
├── scripts/
│   ├── nightly.sh            # build -> sanity gate -> (opt-in) deploy
│   └── sanity_checks.py      # integrity gate
├── .env.example
├── CLAUDE.md                 # dev / maintenance notes
└── README.md
```

Database name: `sg-legal-help.db` (Zeeker derives it from the project name).

---

## Build & deploy

```
scripts/nightly.sh                                 # build + sanity gate, NO deploy
scripts/nightly.sh --deploy                        # build + sanity gate + deploy to S3
uv run zeeker build resources --setup-fts          # build one resource locally with FTS
uv run python scripts/sanity_checks.py             # run the integrity gate
```

Scheduling is owned by the user's personal agent (not GitHub Actions); the project provides the
runnable pipeline. Served through Datasette on `data.zeeker.sg`; the harry app reaches it through
the gateway-brokered Zeeker MCP connector, with the BFF Datasette client as a fallback.

**Environment** (`.env.example` → `.env`, never committed): `LLM_BASE_URL` / `LLM_API_KEY` /
`LLM_MODEL` (summaries), `S3_BUCKET` / `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` (deploy).
See `CLAUDE.md` for the full list and dev knobs.

---

## Conventions

- Python via **uv**; **black** formatting (line length 100, Python 3.12 target); **ruff** lint.
- HTTP via **httpx**, retries via **tenacity**; HTML via **beautifulsoup4** + **lxml**; AI
  summaries via any OpenAI-compatible LLM endpoint.
- Rate-limited, jittered, circuit-breakered fetches; respectful crawling with a descriptive
  ZeekerBot user agent.

---

## Status

Early. The project is scaffolded and the access-to-justice / routing layer is live across three
sources (LawGoWhere, Legal Aid Bureau, Pro Bono SG), building locally into `sg-legal-help.db`
(~72 resources + ~336 fragments, AI summaries via a local LLM) for human review before any
deploy. CJC is deferred (unreachable). Next: review, then the topic sources (consumer /
employment / housing-money) and the manual-seed manifest.
