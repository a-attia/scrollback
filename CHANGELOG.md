# Changelog

All notable changes to scrollback are documented here. The format is based
on [Keep a Changelog](https://keepachangelog.com/), and the project aims to
follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.5.0] - 2026-07-16

Simpler install: `pipx install scrollback` now ships the full experience
(CLI + web app + native window + colour) out of the box — no extras to
pick.

### Changed

- **Simpler install: no more extras to remember.** The web app (FastAPI +
  uvicorn + jinja2), the native app window (pywebview), and coloured
  terminal output (rich) are now default runtime dependencies. A plain
  `pipx install scrollback` (or `pip install scrollback`) gives you the
  full experience out of the box — CLI, web app, and a double-clickable
  launcher via `scrollback install-launcher` — with no `[web]` / `[app]`
  / `[all]` selection step. The `[web]`, `[app]`, `[rich]`, and `[all]`
  extras have been removed; `[dev]` is retained for test/lint tooling
  and `[screenshots]` for maintainers regenerating README images.
- **Docs updated to match.** README now opens with a TL;DR (two
  commands: `pipx install scrollback` + `scrollback install-launcher`)
  and the Install / Development sections and launcher error messages
  drop all references to the removed extras.

### Removed

- **Optional-dependency extras `[web]`, `[app]`, `[rich]`, and `[all]`.**
  Their contents are now default runtime dependencies. Old install
  commands like `pip install "scrollback[all]"` still succeed on most
  resolvers (unknown extras are ignored with a warning) but should be
  simplified to `pip install scrollback`.

## [0.4.1] - 2026-07-09

### Fixed

- **Web assets bust the cache on content change.** `style.css` / `app.js` URLs
  are now stamped with a short content hash (computed at server start) instead
  of just the version, so any change to them forces a fresh fetch — fixing
  stale-UI cases in the native app's WebView where an updated `app.js` (e.g.
  the search titles/contents toggle) wasn't being picked up until a full quit
  + relaunch.

## [0.4.0] - 2026-07-09

Durable session archive: keep your AI-agent sessions forever, browse and
manage them in the web UI, and back up / sync them across machines.

### Added

- **Durable session archive ("keep sessions forever").** `scrollback archive`
  copies the sessions scrollback reads into a user-owned vault at
  `~/.scrollback/archive` (override with `--dest` or `$SCROLLBACK_ARCHIVE`) and
  keeps them **forever**, surviving the agents' own auto-deletion (e.g. Claude
  Code's ~30-day cleanup). The sync is one-way, incremental, and lossless
  (every `raw` blob kept); your agent data is never modified.
  - Archived sessions are read back as a first-class source, so `list` / `show`
    / `search` / `stats` / `export` all work over them — **including sessions
    the agent has already deleted**.
  - A session that is both live and archived is shown once (live wins); one
    that exists only in the vault is marked as deleted.
  - **CLI:** `scrollback archive` (sync), `--stats` (counts + vault layout),
    `--verify` (integrity), `--source archive` (archive-only view); wired into
    `doctor`. Kept on `uninstall` unless `--purge-archive` is given.

- **Backup + cross-machine sync.** `scrollback archive --export <dest>` writes
  a **faithful, re-importable** copy of the vault to a directory or `.zip`
  (`--format rendered` instead writes readable transcripts — markdown / html /
  json / text — for sharing, not backup). `scrollback archive --import <vault>`
  **merges another vault** into yours (keyed on `(source, id)`, larger/newer
  copy wins, never loses messages) — run either direction to converge two
  machines.

- **Web UI: Live / Archive / All mode + web-driven archive management.** A
  top-level mode switch scopes the whole UI — session list, search, and stats —
  to live agents, the durable archive (incl. deleted sessions), or both merged.
  Each row carries a provenance tag (live / live+archived / stale / deleted).
  The Archive view is a dashboard: disk usage, integrity check, clickable stat
  cards (kept / deleted-from-agent / needs-updating), per-source drill-down,
  and **Export .zip / Import .zip / Archive all / Update stale** actions with a
  live progress bar (Server-Sent Events). Per-session "archive / update" button
  in the transcript header; a contextual "archive these" bulk action for the
  current filter/search; a "clear selection" control; UI-wide help tooltips.

- **On-disk footprint visibility + complete uninstall.** `scrollback doctor`
  lists every file scrollback created — search index, web-app browser profile,
  cache dir, launchers, macOS `.app`, launcher log, and the archive vault —
  each tagged by tier (disposable / installed / durable) with its size, so
  there are no ghost files. `scrollback uninstall` is driven by that same
  footprint, so it now also removes the web-app **browser profile** and cache
  directory (previously left behind); the durable vault is still kept unless
  `--purge-archive`, which reports how many kept sessions will be lost,
  suggests `scrollback archive --export` first, and requires a typed
  confirmation.

### Changed

- **Read-only posture, restated precisely.** scrollback still **never writes to
  your agents' data** (test-enforced). The web app is no longer "writes nothing
  at all": it can now write to your own durable vault via explicit sync
  actions. Only the vault is ever written.
- Faster archive listing: session metadata is stored in the manifest so the
  archive is listed from SQLite instead of parsing every session file (with a
  one-time background backfill for pre-existing vaults).

### Security

- **Import hardening.** Importing a vault `.zip` now validates every entry
  against zip-slip (path traversal / absolute paths / symlinks are rejected),
  and reading a session file validates the manifest's `file_path` stays inside
  the vault — so a maliciously-crafted vault or zip cannot read or write
  outside `~/.scrollback`.
- Archive-writing operations are serialized (single writer) with a SQLite busy
  timeout, preventing manifest corruption/lock errors under concurrent syncs.
- The never-shrink guard (a re-read with fewer messages can't overwrite a good
  archived copy) now also covers rows with an unknown message count.

### Fixed

- `scrollback doctor` no longer crashes on a malformed/old search index (it
  reports it as unreadable and suggests a rebuild).

## [0.3.2] - 2026-06-30

### Added

- An **About dialog** in the web UI (the header ⓘ button): app icon, version
  (from `/api/health`), description, and a clickable repository link. Works
  the same in a browser tab, a standalone window, and the native app — and on
  every platform, unlike the macOS-only native About panel.

### Fixed

- External links from the native app window now open in the user's real
  default browser (via the Python bridge, restricted to http/https) instead
  of being trapped in an in-app pywebview window.

## [0.3.1] - 2026-06-30

### Fixed

- The macOS native-app About panel now always shows the repository URL
  (via the copyright line), instead of relying on a menu rewire that could
  silently fail on some pywebview builds; the rewire (for a clickable link)
  is also made more tolerant, and its failures are logged under
  `SCROLLBACK_DEBUG`.

## [0.3.0] - 2026-06-30

### Added

- **Usage statistics page (web) + per-tool breakdown.** A new `stats` view
  aggregates usage by tool (opencode / Claude Code / Codex / Aider) plus an
  overall total: sessions, messages, input/output/cache tokens, and cost
  (shown only where the tool records it). `scrollback stats` gains the same
  per-tool table and a `--json` breakdown; a `/api/stats` endpoint backs the
  web page. Both honour `since`/`until` date filters (new `--since`/`--until`
  on the CLI `stats` command).
- **Redesigned top bar.** A radio-style `browse | stats` view switch (exactly
  one active) replaces the buried icon buttons; the brand mark resets to home;
  the theme toggle is pinned to the far right as a setting.
- **Responsive session drawer.** On narrow screens (e.g. split-screen) the
  session list becomes a slide-in drawer opened by a `sessions` button, with
  a backdrop; it closes on selection, backdrop click, or `Esc`. Previously the
  list was hidden with no way to reach it.
- The empty state now shows the full app icon.

### Fixed

- A genuine reported cost of `$0.00` is now kept distinct from "cost unknown"
  in the per-tool usage rollup (was collapsed to unknown by a truthiness check).

## [0.2.0] - 2026-06-30

### Added

- **Cache & reasoning token accounting.** Sessions now carry
  `tokens_cache_read`, `tokens_cache_write`, and `tokens_reasoning` in
  addition to input/output/cost. In agentic sessions cache reads often
  dominate total token volume, so this makes scrollback's usage numbers
  reconcilable with the agents' own reports:
  - **opencode** reads the corresponding SQLite columns (tolerant of older
    databases that lack them).
  - **Claude Code** now reports usage at all — summed per-turn from each
    assistant message's `usage` block (previously blank).
  - **Codex** parses token-count records where the rollout format includes
    them (best-effort; `None` when absent).
  - **Aider** has no token data on disk and stays `None`.
- `stats` shows a `cache` (read/write) line and a `reasoning` line; the web
  transcript header shows a cache figure; Markdown/HTML/JSON exports include
  a usage summary.

## [0.1.2] - 2026-06-30

### Added

- `scrollback uninstall`: removes the artifacts scrollback created (Desktop
  launcher, macOS `.app`, optional search index, launcher log) with a
  confirmation prompt (`--yes` / `--dry-run`). It never touches agent data
  and never self-removes the package; it prints the right `pip`/`pipx
  uninstall` command instead.

## [0.1.1] - 2026-06-30

### Fixed

- README images now render on PyPI: use absolute, release-pinned PNG URLs
  (PyPI does not resolve relative paths or display SVGs). Adds a PyPI-
  friendly `cli.png` alongside the GitHub SVG.

## [0.1.0] - 2026-06-30

The first release. scrollback reads AI coding-agent session history
(opencode + Claude Code) read-only and lets you browse, search, copy, and
export it from a CLI and a local web app.

### Added

- **CLI** (`scrollback`): `sources`, `list`, `show`, `search`, `export`
  (markdown / json / html / text), `copy`, `stats`, `resume`, `web`,
  `index`, `doctor`, and `install-launcher`.
- **Source adapters** (pluggable, read-only): opencode (SQLite), Claude Code
  (JSONL, with subagent sidechains folded under their parent), Codex
  (`rollout-*.jsonl`), and Aider (`.aider.chat.history.md`). More are queued
  in `ROADMAP.md`.
- `stats` aggregates session/message/token/cost totals plus top projects;
  `resume` prints the native command to continue a session in its own agent.
- Listing filters: `--source`, `--dir`, `--query`, `--since` / `--until`,
  pagination (`--offset` / `--page`), usage columns (`--usage`), and
  subagent folding (on by default; `--no-fold`). Optional coloured output
  via `rich`.
- **Web app** (`scrollback web`): local, read-only, served on
  `127.0.0.1`. Session list with source filters, date filters, and a
  `titles | contents` search scope; lazy, windowed transcript loading so
  very large sessions open instantly; in-transcript find; per-message and
  per-session copy; export and print; light/dark theme; keyboard
  navigation; a frozen session header with a scrolling message body.
- **Markdown rendering**: assistant/user text renders as Markdown with code
  highlighting -- in the browser (vendored marked + highlight.js) and in
  the static HTML export (a dependency-free Python renderer + highlighter).
- **Math / equation rendering**: delimited LaTeX (`$...$`, `$$...$$`,
  `\(...\)`, `\[...\]`) is detected and shielded from the Markdown pass so
  `\`, `_`, `*`, `^` survive intact in both renderers. A render mode --
  `raw` (verbatim source), `latex` (verbatim, never typeset, paste-ready),
  or `rendered` (typeset) -- is a toggle in the web transcript header
  (persisted like the theme) and an `--math {raw,latex,rendered}` flag on
  `scrollback export` / `copy`. Typesetting uses vendored KaTeX (no CDN);
  the self-contained HTML export embeds KaTeX with its fonts inlined so
  saved/printed files typeset offline. The single-`$` form is recognised
  conservatively so currency (`$5 to $10`) and code are left alone.
- **Optional full-text search index** (`scrollback index`): SQLite FTS5,
  incremental, stored in a disposable cache DB; the source data is never
  modified, and search falls back to a lexical scan without it.
- **Launching without the terminal**: `scrollback-web` / `scrollback-app`
  console entry points; `install-launcher` drops a double-clickable
  launcher (macOS `.command` / `.app`, Windows `.bat`, Linux `.desktop`);
  a native desktop window via pywebview that frees the port on close.
- App icon (macOS `.app` + web favicon) and macOS app identity (menu name,
  About panel with version and a clickable repo link).
- Configurable host/port via flags or `SCROLLBACK_HOST` / `SCROLLBACK_PORT`,
  with automatic free-port selection.

### Security

- Sanitize rendered Markdown (DOMPurify) to prevent transcript content from
  injecting scripts into the web UI.
- Host-header allowlist guarding against DNS-rebinding (loopback-only by
  default); loud warning on non-loopback binds.
- Path-traversal containment for Claude subagent id resolution.

### Performance

- Cache Claude Code metadata scans by file mtime (repeated listings go from
  ~1.2s to ~0.01s).
- Byte-offset paging index for Claude transcripts (deep pages on a
  31k-message session: ~1s to ~2ms).
- Lazy per-session metadata resolution on the indexed search path.

### Fixed

- Timezone-naive timestamps no longer crash session sorting.
- Subagent folding no longer drops self-referential or cross-source records.
- Reliable downloads and printing from the native desktop window.
- Negative pagination arguments are rejected; clearer errors for unknown
  sources, failed exports, and unavailable data sources.

[Unreleased]: https://github.com/a-attia/scrollback/compare/v0.3.2...HEAD
[0.3.2]: https://github.com/a-attia/scrollback/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/a-attia/scrollback/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/a-attia/scrollback/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/a-attia/scrollback/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/a-attia/scrollback/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/a-attia/scrollback/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/a-attia/scrollback/releases/tag/v0.1.0
