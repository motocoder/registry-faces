# Storage Layout

The registry is a plain directory tree. Browse it in Finder, diff it in git,
copy it with `rsync`. Nothing is hidden in a database.

## Tree

```
registry/                                ← root (override with --registry / -r)
├── records/
│   ├── US-HI/
│   │   ├── 12345/                       ← one folder per person
│   │   │   ├── record.json              ← canonical OffenderRecord
│   │   │   └── photos/
│   │   │       ├── manifest.json        ← authoritative photo metadata
│   │   │       ├── 001-registry.jpg
│   │   │       └── 002-registry.jpg
│   │   ├── 12346/
│   │   │   └── record.json              ← no photos folder if none published
│   ├── US-FL/
│   │   └── 987654/
│   │       └── ...
├── indexes/
│   ├── index.jsonl                      ← one line per record (search index)
│   └── manifest.json                    ← totals + per-jurisdiction counts
└── README.md                            ← brief layout note
```

The per-person folder name is `<source_id>`, sanitized to alphanumerics +
`._-` (other characters become `_`). The jurisdiction folder name matches
`Source.jurisdiction` exactly (`US-HI`, `US-FL`, etc.).

## record.json

The canonical [`OffenderRecord`](./schema.md) serialized as pretty-printed
JSON. Contains everything about the person *except* photos. See
[`schema.md`](./schema.md) for the field reference.

`Source.first_seen_at` is set on the first ingest of a record and never
changes thereafter. `Source.fetched_at` updates on every re-ingest.

## photos/manifest.json

The authoritative source for what photos belong to this person. **Invariant:**
every file in `photos/` (other than `manifest.json` itself) has exactly one
entry, and every entry with `local_filename` set points to a file that exists.
`registry-faces verify` checks this; `registry-faces sync-photos` maintains it.

```json
{
  "person": {
    "jurisdiction": "US-HI",
    "source_id": "12345"
  },
  "last_synced_at": "2026-05-20T16:32:01Z",
  "photos": [
    {
      "url": "https://hcjdc.ehawaii.gov/photos/12345.jpg",
      "source_type": "registry",
      "source_name": "Hawaii Criminal Justice Data Center",
      "local_filename": "001-registry.jpg",
      "sha256": "ab12cd34ef56...",
      "content_type": "image/jpeg",
      "size_bytes": 45231,
      "fetched_at": "2026-05-20T16:32:01Z"
    },
    {
      "url": "https://hcjdc.ehawaii.gov/photos/12345-side.jpg",
      "source_type": "registry",
      "source_name": "Hawaii Criminal Justice Data Center",
      "local_filename": null,
      "sha256": null,
      "content_type": null,
      "size_bytes": null,
      "fetched_at": null
    }
  ]
}
```

`local_filename: null` = pending. The URL was discovered at ingest but
`sync-photos` hasn't downloaded it yet. After sync, all fields are populated.

If a person has no photos, the `photos/` directory and `manifest.json`
simply don't exist.

## indexes/index.jsonl

One JSON object per line, lightweight projection of `record.json` for fast
search without loading every full record:

```json
{"jurisdiction":"US-HI","source_id":"12345","full_name":"John Q Public","addresses":[{"city":"Honolulu","state":"HI","zip":"96813","lat":21.3,"lon":-157.85}],"path":"records/US-HI/12345"}
```

Rebuildable from `records/` at any time:

```sh
registry-faces rebuild-index
```

## indexes/manifest.json

Bookkeeping for the registry as a whole:

```json
{
  "generated_at": "2026-05-20T16:32:01Z",
  "total_records": 2147,
  "by_jurisdiction": {
    "US-HI": 2147
  }
}
```

---

## Merge / idempotency rules

Re-running `registry-faces ingest <adapter>` is always safe. It merges new
data into existing records and never deletes anything.

| Field type | Rule |
|---|---|
| Identity scalars (name, DOB, sex, race, height, weight, etc.) | New value wins **only if non-null and not "unknown"**. Null never overwrites a value. |
| `identity.aliases` | Union, case-insensitive dedup. |
| `addresses` | Union by `(type, street, city, state, zip)` lower-cased. Existing entries refresh `verified_at` to latest seen; missing `lat`/`lon` get filled in. |
| `offenses` | Union by `(raw_code, raw_description, conviction_date)`. |
| `registration` | Latest wins (it's a status — it's supposed to change). |
| `source.first_seen_at` | Set on first ingest, never changes. |
| `source.fetched_at` | Always updated to current run. |
| `source.source_url` | New value wins if non-null. |
| `raw` | Replaced with the latest source payload. |
| `photos/manifest.json` entries | Union by `url`. Existing entries with a successful download (`sha256` set) are never re-fetched unless `--refresh`. New URLs append as pending. |
| Photo files on disk | Never deleted. `--refresh` re-downloads; if sha256 differs, the entry's metadata is updated in place. |
| People who disappear from the source feed | Folder stays untouched. |

The key invariant: **null never overwrites a value, and a list never shrinks
on re-ingest**.

---

## Commands that touch the layout

| Command | What it does |
|---|---|
| `ingest <adapter>` | Writes/merges `record.json` and pending photo entries into `photos/manifest.json`. Updates the index. |
| `sync-photos [--jurisdiction X] [--refresh]` | Walks each person's manifest, downloads pending entries, fills in file metadata. |
| `verify` | Reports any drift between `photos/` files and manifest entries. Read-only. |
| `rebuild-index` | Walks all `record.json` files and regenerates `indexes/index.jsonl` from scratch. Use this if the index file ever gets corrupted. |
| `lookup` / `near` / `stats` | Read-only — uses the index, loads full records only for matches. |

---

## Backup / portability

The whole tree is plain JSON + image files. Reasonable approaches:

- `git init` the registry root. The pretty-printed JSON diffs cleanly.
  Photo binaries should probably be `.gitignore`d or stored via git-lfs.
- `rsync -av registry/ backup/` to mirror.
- `tar czf registry-2026-05-20.tar.gz registry/` for snapshots.

Nothing in the project assumes the registry lives anywhere specific or that
SQLite is available.
