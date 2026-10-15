"""How a catalog response is laid out: the counts and id lists of a summary, minimal rows,
compact buckets capped with a hint, or every row with its aliases.

It formats rows a caller may already see and reads no package internals, so ordinary and
restricted (grant-scoped) catalogs share one response contract.
"""

from __future__ import annotations

from typing import Any

# Compact-view per-bucket row cap. Sized so a 7-kind catalog stays under
# ~5K tokens even with all buckets full. Callers asking for the full
# inventory request ``verbosity=full``.
_CATALOG_COMPACT_BUCKET_CAP: int = 200


_MINIMAL_CATALOG_ROW_KEYS: frozenset[str] = frozenset({"id", "kind", "name", "label", "available"})


def _minimal_catalog_row(row: dict[str, Any]) -> dict[str, Any]:
    return {key: row[key] for key in _MINIMAL_CATALOG_ROW_KEYS if key in row}


def format_catalog_payload(
    grouped: dict[str, Any],
    *,
    package: dict[str, Any],
    schema_version: Any,
    view: str,
    verbosity: str,
    supported_capabilities: list[dict[str, Any]],
    unsupported_capabilities: list[dict[str, Any]],
    aliases: dict[str, list[str]] | None = None,
    alias_index: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Format already-authorized rows without reading package internals.

    Shared by ordinary catalogs and restricted public projections so caps,
    counts, full retrieval, and verbosity keep the same response contract.
    """
    grouped["meta"] = {
        "package": package,
        "schema_version": schema_version,
        "view": view,
        "verbosity": verbosity,
    }
    verbosity_norm = str(verbosity or "").lower()
    if verbosity_norm == "summary":
        # Cold-start orientation: counts + flat ID list per kind. No row
        # dicts, no descriptions, no payload — agents inspect specific
        # ids via the ``inspect`` tool. Target envelope: under 10KB even
        # on a 300-object package. Capabilities ship here too so a single
        # ``catalog?verbosity=summary`` call gives an agent everything it
        # needs to choose its next move without paying for row metadata.
        # ``kind_map`` (kind→bucket) reversed gives bucket→singular kind
        # so id-list keys read naturally as ``measure_ids``, ``entity_ids``
        # etc. rather than tripping over irregular plurals.
        bucket_to_kind = {
            "entities": "entity",
            "dimensions": "dimension",
            "measures": "measure",
            "segments": "segment",
            "temporal_roles": "temporal_role",
            "relationships": "relationship",
            "value_domains": "value_domain",
            "metrics": "metric",
        }
        summary_payload: dict[str, Any] = {
            "meta": grouped["meta"],
            "counts": {
                bucket_key: len(grouped[bucket_key])
                for bucket_key in list(grouped)
                if isinstance(grouped[bucket_key], list)
            },
            "capabilities": supported_capabilities,
            "unsupported_capabilities": unsupported_capabilities,
        }
        for bucket_key in list(grouped):
            bucket = grouped[bucket_key]
            if not isinstance(bucket, list):
                continue
            singular = bucket_to_kind.get(bucket_key, bucket_key)
            summary_payload[f"{singular}_ids"] = [
                str(row.get("id", "")) for row in bucket if isinstance(row, dict)
            ]
        return summary_payload
    if verbosity_norm == "minimal":
        # Strip every row to {id, kind, name, label, available} and drop
        # the alias dictionaries — both can be rebuilt by inspecting
        # individual objects once the agent has narrowed its target.
        for bucket_key in list(grouped.keys()):
            bucket = grouped[bucket_key]
            if isinstance(bucket, list):
                grouped[bucket_key] = [
                    _minimal_catalog_row(row) for row in bucket if isinstance(row, dict)
                ]
        grouped["counts"] = {
            bucket_key: len(grouped[bucket_key])
            for bucket_key in grouped
            if isinstance(grouped[bucket_key], list)
        }
    elif verbosity_norm == "full":
        # Full verbosity is opt-in: keep the alias maps for typo-resolution
        # and emit every matching row uncapped.
        grouped["aliases"] = aliases or {}
        grouped["alias_index"] = alias_index or {}
    else:
        # Compact (the default). Round four dropped ``alias_index`` and
        # ``aliases`` here — both grew unbounded with the catalog and
        # drowned token budgets even with ``search=...``. Callers who
        # need typo-resolution request ``verbosity=full``; callers who
        # know an object id use ``inspect``. Compact also caps each
        # bucket at ``_CATALOG_COMPACT_BUCKET_CAP`` rows and reports the
        # pre-cap totals + a ``truncated`` flag when any bucket spilled.
        counts_total: dict[str, int] = {}
        truncated_buckets: list[str] = []
        for bucket_key, bucket in list(grouped.items()):
            if not isinstance(bucket, list):
                continue
            counts_total[bucket_key] = len(bucket)
            if len(bucket) > _CATALOG_COMPACT_BUCKET_CAP:
                grouped[bucket_key] = bucket[:_CATALOG_COMPACT_BUCKET_CAP]
                truncated_buckets.append(bucket_key)
        grouped["counts_total"] = counts_total
        grouped["counts"] = {bucket_key: len(grouped[bucket_key]) for bucket_key in counts_total}
        if truncated_buckets:
            grouped["truncated"] = True
            grouped["truncated_buckets"] = sorted(truncated_buckets)
            grouped["truncation_hint"] = (
                "Compact view caps each kind at "
                f"{_CATALOG_COMPACT_BUCKET_CAP} rows. Narrow with "
                "search=/kind=/entity= or request verbosity=full to see "
                "the full inventory."
            )
    grouped["capabilities"] = supported_capabilities
    grouped["unsupported_capabilities"] = unsupported_capabilities
    return grouped
