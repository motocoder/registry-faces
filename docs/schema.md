# Canonical Schema — `OffenderRecord`

The normalized shape every adapter produces. Defined as a Pydantic model in
[`src/registry_faces/schema.py`](../src/registry_faces/schema.py); persisted
as pretty-printed JSON at `records/<jurisdiction>/<source_id>/record.json`.

**Photo metadata is not in the record.** Photos live in `photos/manifest.json`
next to each record — see [storage-layout.md](storage-layout.md) for that
format.

## Design principles

- **Optional by default.** Different registries publish different subsets;
  the only required fields are the `source` triple and `identity.full_name`.
  Everything else is optional.
- **Always keep `raw`.** Every record stores the original source payload
  verbatim. Cheap to re-derive normalized fields later; expensive to re-fetch.
- **No cross-jurisdiction normalization of offense severity.** Tier I in
  one state ≠ Tier I in another. Store `tier_or_level_raw` and leave
  comparison out of scope.
- **No URL fetching at normalize time.** Photo URLs go to the manifest
  (via `extract_photos()`); geocoding is a separate step; downloads happen
  in `sync-photos`.

---

## Top-level shape

```
OffenderRecord
├── source: Source                      (required)
├── identity: Identity                  (required)
├── addresses: list[Address]            (default: [])
├── offenses: list[Offense]             (default: [])
├── registration: Registration          (default: Registration())
└── raw: dict | None                    (default: None — adapters should populate)
```

The model has `extra="allow"` during development so adapters can attach
extra fields without breaking validation.

---

## `Source` (required)

Where this record came from. The pair `(jurisdiction, source_id)` is the
primary key in the store and determines the on-disk folder.

| Field            | Type     | Required | Notes |
|------------------|----------|----------|-------|
| `jurisdiction`   | str      | yes      | ISO 3166-2 style: `"US-HI"`, `"US-FL"`. |
| `source_id`      | str      | yes      | The registry's own ID for this person. |
| `source_url`     | str?     | no       | Where the data was pulled from (the search/bulk endpoint). |
| `info_url`       | str?     | no       | Per-offender detail page on the source site, suitable for a human to click. |
| `first_seen_at`  | datetime?| no       | Set on first ingest; never changes thereafter. |
| `fetched_at`     | datetime | yes      | Updated on every re-ingest. |

`first_seen_at` is populated by the store layer when a record is first
written — adapters don't need to set it.

---

## `Identity` (required)

| Field            | Type     | Notes |
|------------------|----------|-------|
| `guid`           | str      | UUID4. Auto-generated on first ingest and **preserved across re-ingests** — never overwritten. Use this for stable cross-system references; the `(jurisdiction, source_id)` pair remains the on-disk primary key. |
| `full_name`      | str      | The only required identity field. |
| `aliases`        | list[str]| Empty if none. Merged as a case-insensitive set across re-ingests. |
| `dob`            | datetime?| Full date of birth if available. |
| `year_of_birth`  | int?     | Year only, when full DoB isn't published. |
| `sex`            | `"M"` \| `"F"` \| `"X"` \| `"unknown"` | Normalized in the adapter. |
| `race`           | str?     | Stored raw — different registries use different vocabularies. |
| `height_cm`      | float?   | Convert from feet/inches in the adapter. |
| `weight_kg`      | float?   | Convert from pounds in the adapter. |
| `eye_color`      | str?     | |
| `hair_color`     | str?     | |
| `description`    | str?     | Free-form text (scars, marks, tattoos, identifying features). |

---

## `Address`

A record can have multiple addresses (home, work, school, temporary).

| Field         | Type     | Notes |
|---------------|----------|-------|
| `type`        | `"home"` \| `"work"` \| `"school"` \| `"temporary"` \| `"other"` | Default `"home"`. |
| `street`      | str?     | |
| `city`        | str?     | |
| `state`       | str?     | Two-letter US state code. |
| `zip`         | str?     | String, not int — preserves leading zeros. |
| `country`     | str      | Default `"US"`. |
| `lat`         | float?   | Populated by a geocoding pass, not at ingest time. |
| `lon`         | float?   | |
| `verified_at` | datetime?| When the source last verified this address. |

Merge dedup key: `(type, street, city, state, zip)` lower-cased.

---

## `Offense`

A record can have multiple offenses.

| Field                 | Type     | Notes |
|-----------------------|----------|-------|
| `raw_code`            | str?     | The source's offense code. |
| `raw_description`     | str      | Human-readable offense as published. **Required.** |
| `normalized_category` | str?     | Optional cross-source bucket. Don't force it. |
| `conviction_date`     | datetime?| When the conviction occurred. |
| `jurisdiction`        | str?     | Conviction jurisdiction; may differ from registry jurisdiction. |
| `statute`             | str?     | Statute reference. |
| `tier_or_level_raw`   | str?     | The source's tier/level string verbatim. **Do not normalize.** |

Merge dedup key: `(raw_code, raw_description, conviction_date)`.

---

## `Registration`

Status fields about the registration itself. These are merged "latest wins"
since they represent current state.

| Field                | Type     | Notes |
|----------------------|----------|-------|
| `status`             | `"active"` \| `"absconder"` \| `"incarcerated"` \| `"deceased"` \| `"removed"` \| `"unknown"` | Default `"unknown"`. |
| `registered_since`   | datetime?| First registration date. |
| `next_verification`  | datetime?| When the offender must next verify with the registry. |
| `absconder`          | bool     | Convenience flag, mirrors `status == "absconder"`. |

---

## `raw`

The original source payload (dict). Always populate this. Two reasons:

1. **Re-derivation.** If the canonical schema gains a new field next month,
   you can re-derive it from `raw` without re-fetching.
2. **Debugging.** When a normalize step produces a surprising value, `raw`
   shows what the adapter started from.

---

## Example record

```json
{
  "source": {
    "jurisdiction": "US-HI",
    "source_id": "12345",
    "source_url": "https://hcjdc.ehawaii.gov/bulkcor/public/csv",
    "first_seen_at": "2026-05-20T16:32:00Z",
    "fetched_at": "2026-05-20T16:32:00Z"
  },
  "identity": {
    "guid": "8f7d0f8e-1f0a-4b6c-9e2d-7a3a1b5e6c10",
    "full_name": "John Q Public",
    "aliases": ["Johnny Public"],
    "year_of_birth": 1975,
    "sex": "M",
    "race": "WHITE",
    "eye_color": "BRO",
    "hair_color": "BRO"
  },
  "addresses": [
    {
      "type": "home",
      "street": "123 Main St",
      "city": "Honolulu",
      "state": "HI",
      "zip": "96813",
      "country": "US",
      "lat": null,
      "lon": null
    }
  ],
  "offenses": [
    {
      "raw_code": "707-730",
      "raw_description": "Sexual assault in the first degree",
      "jurisdiction": "US-HI",
      "statute": "HRS 707-730",
      "tier_or_level_raw": "Tier III"
    }
  ],
  "registration": {
    "status": "active",
    "absconder": false
  },
  "raw": {
    "first_name": "JOHN",
    "middle_name": "Q",
    "last_name": "PUBLIC",
    "...": "every field from the source, verbatim"
  }
}
```

---

## See also

- **[storage-layout.md](storage-layout.md)** — folder structure, photo
  manifest format, merge rules across re-ingests.
- **[usage.md](usage.md)** — install, configure, build, ingest, query.
