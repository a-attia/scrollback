# Plan: Durable Session Archive ("keep sessions forever")

Status: **Phases 1–4 shipped** (as of 2026-07-08); Phase 5 (multi-machine /
cloud sync) remains out of v1 scope. The five design questions are resolved
(see [§7](#7-resolved-design-decisions)). This is the design of record for
the durable-archive feature; it now doubles as the as-built reference. See
the per-phase status in [§6](#6-phasing-each-phase-independently-releasable).

See also: [`ROADMAP.md`](../ROADMAP.md) (index of planned work),
[`CONTRIBUTING.md`](../CONTRIBUTING.md) (the read-only invariant and
adapter conventions).

---

## 1. Goal & scope

A one-way, durable **local vault**: scrollback copies the sessions it reads
into a user-owned archive and keeps them **forever**, surviving the agents'
own auto-deletion (e.g. Claude Code's ~30-day `cleanupPeriodDays`). The
archive is **lossless** and **re-readable** as a first-class source, so
browse / search / export / stats work over archived sessions — including
ones the agent has already deleted.

**In scope (v1):** local durable vault, incremental one-way sync
(agents -> vault), lossless format, read-back as a source.

**Out of scope (v1):** multi-machine sync, cloud/remote push. A user may
point the vault at a synced folder (Dropbox / iCloud / git) themselves.

### Decisions locked in (from the planning discussion)

- **Extend scrollback; do not fork.** The feature is built entirely on the
  existing engine (`Source` adapters, `Store`, the
  `list_sessions(fold_subagents=False)` drive loop, `(source, id)` keying,
  the incremental-sync pattern proven in `fts.py`). A separate tool would
  duplicate or depend on all of that. Framing: scrollback **reads** your
  agents (read-only, always) and can **archive** what it reads to your own
  vault — two verbs, one clear boundary.
- **Sync scope:** durable local vault, one-way.
- **Format:** lossless normalized (keeps `raw`), re-readable as a source.
- **Read-back:** yes — the archive is a first-class readable source.
- **Vault home:** dot-namespaced `~/.scrollback/` for durable, user-owned
  scrollback state (archive + future config), distinct from the disposable
  `~/.cache/scrollback/`.

---

## 2. Guiding principles

1. **The read-only invariant is untouched.** scrollback writes only to its
   own vault, never back to source stores. Add tests asserting source files
   are unmodified after a sync (mirroring
   `tests/test_sources_live.py:24-46`).
2. **Reuse the proven `FtsIndex.sync` pattern** (`fts.py:116-163`) —
   signature-based incremental sync keyed on `(source, id)` — **with the
   prune step inverted**: the archive never deletes a session that vanished
   from the source (that is the entire point).
3. **Everything keys on `(source, id)`.** Session ids are unique only within
   a source, not globally (`store.py:405-408`, `fts.py:76`).
4. **Three storage tiers, cleanly separated:**
   - `~/.cache/scrollback/` — **disposable** (FTS index, browser profile).
     Removed by `scrollback uninstall`.
   - `~/.scrollback/` — **durable, user-owned** (the vault + future config).
     **Survives `uninstall`** unless explicitly purged.
   - Source stores (`~/.claude/...`, the opencode DB, ...) — **read-only,
     never touched.**

---

## 3. Storage layout (`~/.scrollback/`)

```text
~/.scrollback/
├── config.json                 # future scrollback settings (archive path, ...)
└── archive/                    # the vault (override: --dest / SCROLLBACK_ARCHIVE / config)
    ├── manifest.sqlite         # index: (source,id) -> signature, path, first/last-seen
    └── sessions/
        └── <source>/<id>.json  # one lossless JSON per session (shard if needed)
```

- **Path resolution order:** `--dest` flag -> `SCROLLBACK_ARCHIVE` env ->
  `~/.scrollback/config.json` -> default `~/.scrollback/archive`.
- Filenames must be sanitized: Claude Code subagent ids contain `::`
  (`claudecode.py:30`); opencode ids are safe. Shard by a hash prefix if a
  `<source>/` directory would grow too large.

---

## 4. Components

### Component 1 — Lossless serialize / deserialize (`models.py` + new `archivefmt.py`)

The current JSON export is **lossy** — it strips every `raw` blob
(`export.py:137-144`) — and there is **no deserializer** anywhere in the
codebase. The archive needs both directions, losslessly.

- `archivefmt.to_archive_json(session)`: full `asdict(session)` **keeping**
  all `raw` blobs, wrapped in a small envelope (`schema_version`,
  `archived_at`, `scrollback_version`). Must be JSON-native: datetimes
  serialized as explicit ISO strings (not via the lossy `str()` fallback in
  `export.to_json`'s `default=`). Each adapter's `raw` is already parsed
  JSON / JSON-safe objects — verify during implementation.
- `Session.from_dict` / `Message.from_dict` / `Part.from_dict` (new, in
  `models.py`): reconstruct the frozen dataclasses from the archive dict,
  including `raw`, `children`, `messages`, and datetime parsing. This is the
  missing round-trip half.
- `schema_version` lets a future scrollback migrate old archives.

**Linchpin test:** `from_dict(to_archive_json(s)) == s` for real sessions
from every adapter.

### Component 2 — Archive store + incremental sync (`archive.py`)

Modeled on `FtsIndex` (`fts.py`):

- `ArchiveStore(path)` owns `manifest.sqlite` with a table like
  `archived(source, session_id, updated, message_count, first_archived,
  last_synced, file_path, PRIMARY KEY(source, session_id))`.
- `sync(store, *, sources=None, progress=None) -> {"added", "updated",
  "unchanged", "kept_orphan", "kept_shrunk"}`:
  1. Enumerate live via `store.list_sessions(fold_subagents=False)` (the
     `fts.py:133` pattern — every session incl. subagents).
  2. Signature `(updated.isoformat(), message_count)` (`fts.py:139-142`).
     Unchanged -> skip; else `load_session(id, source=...)` fully and write
     the lossless JSON + upsert the manifest.
  3. **Never-shrink guard** (see [§7.3](#73-versioning-on-change--overwrite-but-never-shrink-)).
     Before overwriting, if the freshly-loaded session has **fewer**
     messages than the archived copy, skip the write and count it
     `kept_shrunk` — never clobber good archived data with a degraded read.
  4. **No prune.** Sessions in the manifest but absent from live are
     **kept** (counted `kept_orphan`) — the durability guarantee. Optionally
     record `last_seen_live` so we can report "N archived sessions no longer
     exist in their agent."
- Incrementality keeps re-syncs cheap; only new/changed sessions are
  re-serialized.

### Component 3 — Archive as a readable source (`sources/archive.py`, `ArchiveSource`)

- Reads `sessions/<source>/<id>.json` back via `from_dict`, exposing them
  through the normal `Source` interface, so browse / search / export / stats
  work over archived sessions **including ones the agent deleted**.
- **Preserves the original `(source, id)`**: a session archived from
  opencode still reports `source="opencode"`, so dedup on `(source, id)`
  works and provenance is kept.
- **Registration problem to solve:** `registry.py:27` instantiates adapters
  with `cls()` (no args); `ArchiveSource` needs a vault path and must be
  inactive when no vault exists. Plan: do **not** add it to `ALL_SOURCES`;
  instead have `Store` inject an archive source when a vault exists (e.g.
  `Store.with_archive(path)` or a constructor flag). It must not appear as a
  "known but unavailable" chip when no vault exists.
- **Loop-safety:** archive sync reads *live* sources only, never the
  `ArchiveSource`, so it can never archive its own archive.
- **Dedup is cross-cutting, not free** (see [§7.2](#72-dedup-precedence--live-wins-)).
  `Store` deduplicates nothing today; `list_sessions` (`store.py:215-228`)
  simply concatenates all sources. Read-back therefore needs a net-new
  `Store`-level dedup layer keyed on `(source, id)` with **live winning**.
  It must cover `list_sessions`, `search`, and `_resolve` (ordering-
  dependent), and **especially `stats` (`store.py:134`), which will
  double-count sessions/messages/tokens/cost** if a live+archived session
  is counted twice. Order live sources before the injected `ArchiveSource`
  so first-match precedence in `_resolve` picks the live copy.

### Component 4 — CLI `archive` command (`cli.py`)

Model on `cmd_index` (`cli.py:316`); slot near it (`cli.py:1122`).

- `scrollback archive` — incremental sync (the main verb); `--source`,
  `--dest`, `--since/--until`, progress output.
- `scrollback archive --stats` — vault size, per-source counts, how many
  archived sessions no longer exist live.
- Integrate with `list` (e.g. `list --source archive`) once Component 3
  lands.
- Later: `scrollback archive --verify` (integrity), `--export <dir>` (bulk
  export from the vault).
- Wire into `cmd_doctor` (`cli.py:151`) — show vault path + count. Wire into
  `cmd_uninstall` (`cli.py:947`) — **offer** to remove the vault but
  **default to keeping it** (durable user data, unlike the cache/index that
  `uninstall` deletes today at `cli.py:958`).

### Component 5 — Web integration (later phase)

A "sync now" affordance, archived-session badges, and an archive filter chip
in the web UI. Deferred to keep v1 focused on the CLI engine.

### Component 6 — Config file (`~/.scrollback/config.json`)

Minimal for now: the archive path override + a schema version. Establishes
the durable-config home for future scrollback behavior. Read during path
resolution; written by a future `scrollback config` command (not v1).

---

## 5. Testing strategy

- **Round-trip fidelity** (Component 1): `from_dict(to_archive_json(s)) == s`
  per adapter, using synthetic fixtures / the demo-data builders.
- **Incremental sync**: synthetic store — first sync archives all; second
  sync with one changed `message_count` re-archives only that one; a session
  removed from the live store stays in the vault (`kept_orphan`); a live
  session that reads back with **fewer** messages than the archived copy is
  skipped and counted `kept_shrunk` (never-shrink guard, §7.3).
- **Read-back**: `ArchiveSource` over a temp vault returns sessions equal to
  the originals; a session deleted from its source is still readable.
- **Read-only invariant**: source mtimes unchanged after a sync (parallels
  `test_sources_live.py:46`).
- **Dedup**: a live+archived `(source, id)` appears once in `list_sessions`
  and in `search`, with **live winning** (§7.2); and — critically —
  `stats` counts it **once** (regression test against the double-count
  hazard: sessions/messages/tokens/cost must not inflate when the same
  `(source, id)` is present live and archived).
- All using `tmp_path`; no real user data.

---

## 6. Phasing (each phase independently releasable)

Status as of 2026-07-08. Phases 1–4 are **shipped**; Phase 5 is future work.

- **Phase 1 — Lossless core — SHIPPED.** `Session/Message/Part.from_dict`
  (`models.py`) + `archivefmt.py` (`to_archive_json` / `from_archive_json`,
  strict JSON-native encoder) + round-trip tests (`test_models.py`). Also
  fixed `_to_dt` to pass through existing `datetime` objects (caught by the
  linchpin `from_dict(asdict(s)) == s` test). No user-facing change.
- **Phase 2 — Archive engine + CLI — SHIPPED.** `archive.py`
  (`ArchiveStore.sync` with inverted prune + never-shrink guard; path
  sanitization; `stats`), `scrollback archive` (`--source`, `--dest`,
  `--stats`), doctor wiring, uninstall keep-by-default + `--purge-archive`,
  tests (`test_archive.py`). Ships the durable local vault.
- **Phase 3 — Read-back — SHIPPED.** `sources/archive.py` (`ArchiveSource`,
  preserving original `(source, id)`), `Store.with_archive` injection, the
  cross-cutting `_dedup` layer (live wins; `archived` / `archived_only`
  badges), `_resolve` + `_search_lexical` archive fallbacks, and
  `_make_store` wiring (`--source archive`). Archived sessions — including
  deleted ones — are browsable / searchable / exportable.
- **Phase 4 — Web + polish — SHIPPED (read-only scope).** Archive injected
  into the web store (`_default_store`), `archived` / `archived_only` in
  `serialize.py`, list-row + header badges and a tri-state archive filter
  chip in the web UI, and `scrollback archive --verify`. **Deviation from
  the original plan:** the "sync now" web button was intentionally dropped —
  it would require a mutating endpoint, breaking the read-only web
  invariant. Sync stays a CLI verb. Opt-in auto-sync-on-web-launch (like
  `_background_index_refresh`, `cli.py:713`) was also deferred.
- **Phase 5 (future, out of v1):** multi-machine / cloud sync.

### Known follow-ups (post-Phase-4)

- **Indexed search does not cover archived-only sessions.** The FTS index
  syncs live sources only, so a deleted-but-archived session is found via the
  lexical path but not the fast indexed path. A future enhancement could
  index the vault.
- **Web UI redesign (view vs. archive split).** The current UI bolts archive
  badges + a filter chip onto the existing single list. A dedicated
  browse-live vs. browse-archive information architecture is planned; see
  [`ROADMAP.md`](../ROADMAP.md).
- **Opt-in auto-sync-on-web-launch** (deferred from Phase 4).

---

## 7. Resolved design decisions

The five questions deferred to the implementation session were resolved in
the planning re-audit (see the code-grounded rationale under each). All
five kept their original leaning; two picked up a small addition, flagged
**[+]** below.

### 7.1 Auto-sync vs. manual — **manual only in v1**

`scrollback archive` is an explicit verb; no opportunistic auto-sync in
v1. Rationale: the FTS background refresh (`cli.py:713`) exists because a
*stale search returns wrong answers* — a correctness concern. A stale
archive only means "not yet captured", never a wrong answer served, so
silent auto-sync would be an unrequested write side-effect that muddies the
clean "read vs. archive" boundary. Phase 4 may add **opt-in**
auto-sync-on-web-launch mirroring `_background_index_refresh`.

### 7.2 Dedup precedence — **live wins** [+]

When a `(source, id)` exists both live and archived, the live copy wins (it
is definitionally at least as fresh; sync only ever copies live → vault). A
**badge** marks sessions that exist only in the vault (deleted from the
agent).

**[+] Cost correction from the re-audit.** This is *not* a small precedence
tweak. `Store` performs **no deduplication anywhere today** — `list_sessions`
(`store.py:215-228`) flattens every source's sessions and sorts, trusting
that `(source, id)` is globally unique across the source list. Injecting an
`ArchiveSource` therefore requires a **net-new, cross-cutting dedup layer**
at the `Store` level, affecting four independent iteration paths:

- `list_sessions` (`store.py:215-228`) — would emit literal duplicates.
- `stats` (`store.py:134`) — iterates `list_sessions`, so **would
  silently double-count** sessions, messages, tokens, and cost. This is the
  sharpest hazard; naive read-back corrupts every aggregate stat.
- `search` — iterates sources; would return duplicate hits (verify shape
  during Phase 3).
- `_resolve` (`store.py:247-250`) — first-match-wins, so single-session
  precedence is determined by **source ordering**; live sources must be
  ordered before the injected `ArchiveSource`.

This is the plan's heaviest hidden lift and belongs in Phase 3. The **badge**
is surfaced via `Session.raw["archived_only"] = True` (fed from the manifest's
`last_seen_live`) rather than a new `models.py` field, keeping the model
behavior-free and adapter-agnostic.

### 7.3 Versioning on change — **overwrite, but never shrink** [+]

An archived session is overwritten with the latest canonical copy; no
historical versions are kept. The premise "a session only ever
appends/grows" holds for append-only agents, so overwrite normally loses
nothing, and versioning adds large complexity (multi-file per session, GC
policy, read-back ambiguity) for a rare payoff.

**[+] Never-shrink guard.** If a re-sync produces a session with **fewer**
messages than the archived copy (corruption, partial read, agent
truncation), that is a signal, not a normal overwrite. `ArchiveStore.sync`
must **skip** such a write and count it (e.g. `kept_shrunk`) rather than
clobbering good archived data with a degraded read. This preserves the
durability guarantee against bad reads; it is self-contained in `sync` with
no ripple into other components.

### 7.4 opencode fidelity — **normalized-only, accepted**

opencode's `Session.raw` is empty (`{}`) and it is a shared SQLite DB, so
there is no per-session file to byte-copy. "Lossless" here means lossless
*relative to scrollback's normalized model* (the `messages`/`parts` and
their `raw`), not byte-identical to the source — scrollback never held the
raw opencode blob. The round-trip invariant
`from_dict(to_archive_json(s)) == s` still holds on the normalized object.

**Phase-1 verification item (not a decision):** confirm opencode's
message/part `raw` fields *are* populated even though session `raw` is
empty, so we are not silently dropping fidelity that is in fact available.

### 7.5 Uninstall default — **keep the vault; `--purge-archive` opt-in**

`scrollback uninstall` keeps the vault by default; a new `--purge-archive`
flag is required to remove it. This is the point of the three-tier split:
the cache/index is disposable (`uninstall` deletes it today at
`cli.py:958`), but the vault is durable user data and deleting it on
uninstall would be a data-loss footgun.

**Net-new code, not a modification.** No `--purge-archive` flag exists
anywhere yet, and `cmd_uninstall`'s `targets` list (`cli.py:957-960`) only
ever appends the FTS index today. The vault entry is entirely new: add it to
`targets` **only** when `args.purge_archive` is set, and list it distinctly
in the confirmation prompt (`cli.py:967-970`) as "durable archive — will be
permanently deleted".

---

## 8. Key code references (grounding for implementation)

- Incremental sync pattern to mirror: `fts.py:116-163` (`FtsIndex.sync`),
  staleness `fts.py:165-187`.
- Session id non-uniqueness / `(source,id)` keying: `store.py:405-408`,
  `fts.py:66-88`.
- Lossy JSON export (to invert for the archive): `export.py:131-144`.
- Where `raw` / on-disk paths live: `models.py:62-128`; adapters set
  `Session.raw["path"]` for JSONL/markdown
  (`claudecode.py:230`, `codex.py:120`, `aider.py:243`); opencode leaves
  `Session.raw` empty.
- Source contract: `sources/base.py:19-111`.
- Registry (instantiates `cls()` with no args): `sources/registry.py:17-32`.
- CLI subcommand registration + `cmd_index` template: `cli.py:316`,
  `cli.py:1122`; uninstall artifact handling `cli.py:947-996`.
- Read-only test to parallel: `tests/test_sources_live.py:24-46`.
- Durable-vs-disposable path precedent: `fts.default_index_path()`
  (`fts.py:33-37`), `~/.cache/scrollback/` in `webopen.py:78`.
