# Plan: Web UI redesign — Live / Archive / All + web-driven archiving

Status: **planned** (not started); design decisions **resolved** in an
interactive session (as of 2026-07-08). Design of record for the web UI's
information-architecture redesign that gives the durable archive its own
first-class place *and* lets users archive/update sessions from the web with
live progress feedback.

See also: [`docs/archive-plan.md`](archive-plan.md) (the archive feature this
builds on — Phases 1–4 shipped), [`ROADMAP.md`](../ROADMAP.md) ("Web UI
redesign" entry).

---

## 1. Problem

The durable archive (archive-plan §Phase 4) currently surfaces in the web UI
as two small additions bolted onto the existing single-list layout:

- a tri-state archive filter **chip** mixed in with the source chips
  (`static/index.html:56-65`), and
- per-row **badges** (`archived` / `deleted`) in `static/app.js`
  (`archivedBadge`).

This does not give the archive its own identity, and it offers no way to
*act* on the archive from the web:

- Deleted-but-kept sessions are interleaved with live ones; the only
  distinction is a small badge that is easy to miss.
- There is no "browse my archive" overview — no per-source vault counts, no
  landing view.
- Archiving is CLI-only (`scrollback archive`), so a user browsing the web UI
  cannot keep a session they are looking at, nor update a stale archived copy.
- No per-session archived **status** is shown, so the user cannot tell which
  live sessions are safely kept.

## 2. Goal & scope

Two things:

1. A **top-level mode switch — Live / Archive / All —** that is the outer
   browsing axis and drives the whole UI, **including the statistics viewer**.
2. **Web-driven archiving**: per-session and global "archive / update" actions
   that write to the vault, each with a **live progress bar**.

### Read-only posture — restated precisely

The earlier "web is strictly read-only" decision (archive-plan §7.1) is
**revised**. The invariant that matters is preserved and re-stated:

> **scrollback never writes to your agents' data.**

The web app may now write to **your own durable vault** (`~/.scrollback/`)
via explicit user action (sync buttons). It still never touches an agent's
session store. README + Safety copy + the app empty-state must be updated to
say "never writes to your agents" rather than "the web app writes nothing".

**In scope:**

- Header-level 3-way mode toggle (Live / Archive / All), default **Live**.
- Per-mode session list, search, and **stats** (the mode drives which store
  each aggregates over).
- Per-row **provenance tag** (live / archived / deleted) — always visible, and
  the primary way the archive is discovered in the default Live mode.
- Per-session **archived status** (not archived / archived / stale) with an
  archive-or-update button in the transcript header.
- A global **"sync all"** action (full incremental sync) on the Archive
  landing view.
- **Live progress bar** for every sync action (per-session and sync-all).
- **UI-wide help affordances** (hover/tooltip + small help icons) explaining
  terms — not archive-specific.
- Retiring the tri-state archive chip in favour of the mode switch.

**Out of scope (for this redesign):**

- Writing to agent data (never; the core invariant).
- Multi-machine / cloud sync (archive-plan Phase 5).
- Fast indexed search over the vault (tracked separately as a follow-up).
- Auto-sync (all sync remains explicit user action).

## 3. The three modes

| Mode | Sessions shown | Store construction | Stats aggregate over |
|:-----|:---------------|:-------------------|:---------------------|
| **Live** (default) | live adapters only | `Store()` | live only |
| **Archive** | the vault only (incl. deleted) | `Store([]).with_archive(path)` | vault only |
| **All** | live + vault, deduped (live wins) | `Store().with_archive(path)` | deduped union |

The existing dedup + badge machinery (`_dedup`, `archived` / `archived_only`
in `serialize.py`) already produces the right data for **All** and
**Archive**; the redesign is presentation + store selection + the new sync
endpoints.

### 3.1 Resolved decisions

- **Store selection = per-request `mode` param** (not a single merged store
  filtered client-side). Reasons: Live mode must skip the archive read cost;
  Archive mode must skip enumerating live sources; and the **stats viewer**
  must re-aggregate per mode (the double-count guard lives in `Store`, not the
  frontend, so client-side filtering cannot produce correct stats).
- **Default mode = Live.** Fastest; the per-row provenance tag becomes the
  discovery path for the archive.
- **Source chips filter *within* the selected mode** (mode is the outer axis;
  e.g. Archive + opencode = archived opencode sessions).
- **Archive landing = minimal first** (vault path, totals, per-source counts,
  N deleted) + the "sync all" button. Richer views (verify status, disk
  usage, dedicated deleted list) iterate later.

## 4. Backend changes

### 4.1 Mode-aware store selection

Accept a `mode` query param (`live` | `archive` | `all`, default per §3.1) on
the read endpoints `/api/sessions`, `/api/search`, `/api/stats`. Build the
three stores lazily and cache them on the app; `_default_store()` remains the
`all` case. `/api/sessions` and `/api/search` already thread most filters;
`mode` is an additional axis.

### 4.2 New endpoint: archive overview

`GET /api/archive` → vault landing data, backed by `ArchiveStore.stats()`:

```json
{
  "exists": true,
  "path": "~/.scrollback/archive",
  "sessions": 128,
  "orphans": 12,
  "per_source": {"opencode": 90, "claudecode": 38}
}
```

Returns `{"exists": false}` when no vault exists (frontend renders an
empty-state with the `scrollback archive` hint / a "start archiving" button).

### 4.3 New: web-driven sync (writes to the vault only)

Two mutating endpoints. Both write **only** under `~/.scrollback/` and never
touch agent data.

- `POST /api/archive/sync` — full incremental sync (the `scrollback archive`
  equivalent). Kicks off a background job; returns a `job_id`.
- `POST /api/archive/sync/{source}/{session_id}` — archive/update **one**
  live session. Fast (one session); still reports progress for consistency.

**Single-session archive path.** `ArchiveStore.sync` today does full
incremental sync only. Add a method to archive one session (reuse
`_archive_session` incl. the never-shrink guard, archive-plan §7.3) so the
per-session endpoint does not re-enumerate everything.

### 4.4 Progress via Server-Sent Events (SSE)

Every sync reports **live progress**; nothing syncs silently.

- The sync runs in a background job holding `{done, total, phase, result}`
  state, updated via `ArchiveStore.sync`'s existing `progress(done, total)`
  callback (built in Phase 2).
- `GET /api/archive/sync/{job_id}/events` — an SSE stream emitting
  `{done, total, phase}` events and a terminal `done` event carrying the
  sync summary (`added/updated/unchanged/kept_orphan/kept_shrunk`).
- FastAPI streams SSE via `StreamingResponse` (`text/event-stream`); **no new
  dependency**.
- Loopback + Host-guard + same read-only-to-agents guarantees apply; these are
  the first mutating endpoints, so they must be behind the existing Host guard
  and only ever write the vault.

### 4.5 Serialization: per-session archived status

`session_summary` already exposes `archived` / `archived_only`. Add
`archive_status`: one of `"none"` | `"archived"` | `"stale"`, computed by
comparing the live session's signature `(updated.isoformat(),
message_count)` against the vault manifest row (same signature the sync uses,
archive-plan Component 2). `"stale"` = a vault copy exists but the live
session has newer content, so the update button is meaningful. This lookup
needs the store to consult the `ArchiveStore` manifest; keep it cheap
(single manifest read, not a per-session file open).

## 5. Frontend changes

Vanilla JS, no build step (consistent with the current app).

### 5.1 Mode switch + state

- **Mode switch** in the header: three segmented buttons (Live / Archive /
  All). Persist in `localStorage` like the theme.
- **State:** add `state.mode` (default `"live"`); thread into `loadSessions`
  / `loadSearch` / the stats loader as the `mode` param. Remove the
  client-side `passesArchiveFilter` chip logic.
- **Retire** `archiveChip` / `cycleArchiveFilter` + `.arc-toggle` CSS.

### 5.2 Per-row provenance tag (always on)

Every session row shows a clear tag identifying its origin — **live**,
**archived** (kept + still live), or **deleted** (archive-only). This is
especially important in **All** mode where the two are interleaved, and is
the discovery path in the default Live mode. Supersedes today's easy-to-miss
badges; reuse `archived` / `archived_only` from the summary.

### 5.3 Stats viewer tied to the mode

The statistics viewer re-queries `/api/stats?mode=<mode>` when the mode
changes, so totals reflect the selected scope (live-only vs. vault-only vs.
deduped union). This is why stats must be a server-side per-mode aggregate
(§3.1).

### 5.4 Per-session status + sync button

- Each live session row shows its `archive_status` (not archived / archived /
  stale) as a small status indicator.
- The open transcript's header/action bar (where resume/export live) gets an
  **"Archive this" / "Update archive"** button, enabled per `archive_status`
  (archive when `none`, update when `stale`, no-op/disabled when `archived`).
- Clicking triggers `POST /api/archive/sync/{source}/{id}` and shows the
  progress bar (§5.6).

### 5.5 Archive landing view

When mode = Archive and no session is selected, the reader pane shows a
minimal overview (vault path, totals, per-source counts, N deleted) plus the
global **"Sync all"** button. Mirror the existing `stats-view` pattern
(`index.html:85`). Empty-state when no vault: a "start archiving" prompt.

### 5.6 Progress bar (all sync actions)

A shared progress-bar component consumes the SSE stream: shows `done/total`
and phase, then a completion summary (e.g. "+5 added, 2 updated"). Used by
both per-session and sync-all actions; per-session is fast but uses the same
component for consistency. Errors surface in the bar, not silently.

### 5.7 UI-wide help affordances

A general (not archive-specific) pattern for explaining terms: small help
icons and/or `title`/tooltip hovers on ambiguous labels across the whole UI
(e.g. token buckets, cache read/write, "stale", "deleted", mode meanings). A
single reusable helper (e.g. `helpIcon(text)` / consistent `title=`
convention) applied broadly.

## 6. Testing strategy

- **Mode selection (backend):** `/api/sessions?mode=live|archive|all` returns
  the expected set for a fixture with one live+archived session and one
  deleted-but-archived session. Live excludes deleted; Archive includes it;
  All dedups (the existing double-count regression still holds).
- **Stats per mode:** `/api/stats?mode=…` aggregates over the right store; the
  live+archived session counts once in All.
- **Archive overview:** `/api/archive` returns counts from a synced fixture
  vault, and `{"exists": false}` with no vault.
- **Web-driven sync:** `POST /api/archive/sync` and the per-session variant
  write only under the vault (assert no agent-data mtime change, mirroring
  `test_sources_live.py`); the per-session never-shrink guard still holds.
- **SSE progress:** the events stream emits monotonic `done` up to `total`
  and a terminal summary (drive `ArchiveStore.sync` with a synthetic store).
- **archive_status:** `none` / `archived` / `stale` computed correctly as the
  live signature diverges from the manifest.
- **Frontend:** keep logic thin + backend-tested; a lightweight check that the
  mode switch changes the `mode` param and that a sync click hits the POST
  endpoint (no JS test harness today).

## 7. Open questions to resolve at implementation time

1. **Job lifecycle** — where does sync-job state live (in-process dict keyed
   by `job_id`)? What happens to an in-flight job if the browser disconnects
   (SSE closes)? Leaning: in-process job registry; sync continues to
   completion server-side regardless of SSE connection.
2. **Concurrency** — guard against two overlapping sync-all jobs (a manifest
   is SQLite; concurrent writers need care). Leaning: a single-flight lock;
   a second sync request returns the running job's `job_id`.
3. **archive_status cost at scale** — a manifest read per `list_sessions`
   call is one query; confirm it stays cheap for large lists, else cache the
   manifest signatures per request.
4. **Mutating endpoints + auto-shutdown watchdog** — ensure a long sync does
   not get killed by the idle watchdog (`cli.py` `_background_index_refresh`
   neighbourhood); a running job should count as activity.

## 8. Key code references (grounding)

- Current single-layout markup: `static/index.html:56-87` (filters bar +
  rail + reader).
- Archive chip to retire: `static/app.js` (`archiveChip`,
  `cycleArchiveFilter`, `passesArchiveFilter`); CSS `.arc-toggle` in
  `static/style.css`.
- Store construction to make mode-aware: `web/app.py` `_default_store()` +
  `create_app` (`_store` assignment, ~line 94).
- Read endpoints to thread `mode` through: `/api/sessions`, `/api/search`,
  `/api/stats` in `web/app.py`.
- Sync engine + progress callback: `ArchiveStore.sync(..., progress=)` and
  `_archive_session` (never-shrink guard) in `archive.py`; vault stats
  `ArchiveStore.stats()`.
- Dedup + badges already shipped: `store.py` `_dedup`; `serialize.py`
  `session_summary` / `search_hit`.
- Transcript header/action bar (per-session button home): `renderHeader` /
  `actionBar` in `static/app.js`.
- Web test pattern to follow: `tests/test_web_api.py` (fixture + TestClient;
  the Phase-4 archive tests `_archive_client`).
