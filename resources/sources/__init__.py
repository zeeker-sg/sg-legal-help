"""Source adapters for the sg-legal-help catalogue.

Each adapter exposes ``fetch() -> list[dict]`` returning one *page dict* per
resource. A page dict carries the content + curation + licensing fields and an
embedded ``fragments`` list; the orchestrating ``resources`` Zeeker resource
(resources/resources.py) fills the housekeeping fields (id, content_hash,
summary, timestamps, status) and splits ``fragments`` into the
``resources_fragments`` table. All sources land in one URL-keyed catalogue.

Page dict shape (all keys optional unless noted; the orchestrator supplies
sensible defaults)::

    {
        "source_url": str,            # REQUIRED — canonical URL (catalogue key)
        "title": str,
        "publisher": str,             # e.g. "LawGoWhere"
        "publisher_type": str,        # ministry / statutory_board / court / a2j_body / ...
        "source_last_updated": str,   # the page's own stated date, if any
        "topic": str,                 # controlled vocab
        "audience": str,              # individual / sme / both
        "resource_kind": str,         # rights_explainer / how_to / process_steps / ...
        "is_routing_destination": bool,
        "content_text": str,          # full extracted text (indexed; serving gated by can_store_fulltext)
        "license": str,
        "license_url": str,
        "attribution": str,
        "can_store_fulltext": bool,   # False for all-rights-reserved sources
        "volatility": int,            # 1 (structural) / 2 (numbers move) / 3 (default)
        "source_method": str,         # defaults to "crawled"
        "fragments": [                # optional; only long multi-section guides
            {"heading": str, "content_type": str, "content_text": str},
            ...
        ],
    }
"""
