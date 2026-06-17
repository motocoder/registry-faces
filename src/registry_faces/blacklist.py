"""Single source of truth for jurisdictions we never ingest, build, or ship.

New York, California, and Massachusetts prohibit redistribution of registry
data, so they must be excluded everywhere — adapter ingest, shard packaging,
and upload alike. Import ``BLACKLIST`` here rather than re-listing the codes,
so adding/removing a jurisdiction is a one-line change with no drift.

Keyed by jurisdiction code (the ``records/<code>/`` dir name); value is the
human name used in log/report messages.
"""

from __future__ import annotations

BLACKLIST: dict[str, str] = {
    "US-NY": "New York",
    "US-CA": "California",
    "US-MA": "Massachusetts",
}


def is_blacklisted(jurisdiction: str) -> bool:
    """True if this jurisdiction code must never be ingested/built/shipped."""
    return jurisdiction in BLACKLIST
