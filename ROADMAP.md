# Roadmap

Planned work for scrollback, with the research behind it so the next
contributor doesn't have to re-derive it. This is a living document; the
current state of what *is* built lives in [`CHANGELOG.md`](CHANGELOG.md).

## Planned source adapters

scrollback currently reads **opencode**, **Claude Code**, **Codex**, and
**Aider**. The agents below were researched (formats, locations,
feasibility) but not yet implemented. The table records the verdict so the
effort and risk are known up front.

| Agent | Local store | Format | Transcripts? | Feasibility | Verdict |
|:------|:------------|:-------|:-------------|:------------|:--------|
| **Gemini CLI** | `~/.gemini/tmp/<project_hash>/chats/` (checkpoints under `checkpoints/`) | JSON per session | Yes — full (prompts, responses, tool I/O, tokens) | Easy–Medium | **Tier 1 — do next.** Best-documented; closest analog to the Claude Code JSONL adapter. Caveats: 30-day rolling retention by default; `<project_hash>` must map back to a project root. |
| **Zed** | `<data_dir>/threads/threads.db` (macOS `~/Library/Application Support/Zed`) | SQLite; `data` BLOB = zstd-compressed JSON | Yes — roles, text, thinking, tools, tokens | Medium | **Tier 1.** Adds an editor surface. Needs a zstd dependency (optional extra) and dual-version JSON handling (`0.3.0` + legacy). Schema is open-source and re-verifiable per release. |
| **VS Code Copilot Chat** | `<vscode-user>/workspaceStorage/<hash>/chatSessions/*.json` | JSON per session | Yes — requests/responses, parts, tool calls | Medium | **Tier 2 — best-effort.** High reach, but the schema is internal and churns across releases. Budget for version tolerance + workspace-hash→project mapping. |
| **GitHub Copilot CLI** | `~/.copilot/session-state/` | JSON state files | Partial→Yes (auto-compaction can lose detail) | Medium | **Tier 2 — best-effort.** Undocumented on disk; confirm the shape on a current build first. Exclude the legacy `gh copilot` Suggest/Explain extension (no transcripts). |
| **Cursor** | `state.vscdb` SQLite (VS Code-style app data) | SQLite key-value blobs | Partial | Hard | **Tier 2/3.** Chat is buried in a key-value DB; brittle to reverse-engineer and version-sensitive. |
| **Windsurf / Cascade** | `~/.codeium/windsurf/memories/` (rules/memories only) | Markdown | No (transcripts are cloud-side) | Not feasible | **Skip.** Conversations are not a documented local artifact; only distilled memories/rules live on disk. |

### Implementing one

Each adapter is a `Source` subclass in `src/scrollback/sources/` registered
in `registry.py`; see [`CONTRIBUTING.md`](CONTRIBUTING.md) and the existing
JSONL (`codex.py`, `claudecode.py`) and SQLite (`opencode.py`) adapters as
references. The same checklist applies to every new adapter:

- read-only access only (no writes, no write-locks);
- tolerant parsing (skip malformed records; degrade, don't crash);
- a `resume_command` override if the agent supports by-id resume;
- synthetic-fixture tests, since contributor machines won't all have the
  agent's data.

## Math / equation rendering

**Done** (steps 1–4 below shipped; see [`CHANGELOG.md`](CHANGELOG.md)).
Delimited LaTeX (`$...$`, `$$...$$`, `\(...\)`, `\[...\]`) is detected and
shielded from the Markdown pass in both renderers; a `raw` / `latex` /
`rendered` mode is exposed as a web header toggle (persisted like the theme)
and an `--math` flag on `export` / `copy`; typesetting uses vendored KaTeX,
inlined (fonts and all) into the self-contained HTML export so it works
offline. The single-`$` form is detected conservatively to avoid currency
and code false positives.

Math appears in three forms, only the first of which is unambiguous:
delimited LaTeX (handled), Unicode math (`∇·u = 0`, `x²`), and plain ASCII
(`x^2 + y^2`). The latter two remain **out of scope** — detecting them means
guessing, with false positives in ordinary prose and code.

Remaining open ideas (not planned):

- Unicode/ASCII → LaTeX normalization — likely too lossy to be worth it.
- A `tex` export format that emits a ready-to-`\input` LaTeX document
  (distinct from the current verbatim-preservation `--math latex`).
- Per-equation copy-the-LaTeX affordance in the rendered web view.

## Durable session archive ("keep sessions forever")

Move scrollback beyond read-only *viewing* to durable *keeping*: a one-way
sync that copies sessions into a user-owned vault (`~/.scrollback/archive/`)
and retains them forever, surviving the agents' own auto-deletion (e.g.
Claude Code's ~30-day cleanup). Lossless and re-readable as a first-class
source, so browse/search/export/stats work over archived — even deleted —
sessions. Reading stays strictly read-only; scrollback only ever writes to
its own vault.

Full design of record, phasing, and as-built notes:
[`docs/archive-plan.md`](docs/archive-plan.md). **Phases 1–4 shipped**
(as of 2026-07-08): the durable vault (`scrollback archive`), lossless
round-trip, read-back as a first-class source (deduped live-wins), and the
web badges + archive filter chip. File-based multi-machine sync (export a
vault, import it on the other machine, larger/newer copy wins) also
shipped; **cloud sync remains out of scope by design** (see
[`PLAN.md`](PLAN.md) "Scope and non-scope").

Remaining follow-ups, tracked as milestones M8–M9 in [`PLAN.md`](PLAN.md):

- **Index the vault for fast search.** Archived sessions are browsable and
  searchable today, but the FTS index covers live sources only, so
  searching the vault falls back to a lexical scan.
- **Opt-in auto-sync**, so keeping history does not depend on remembering
  to run the command. Blocked on a scheduling policy that cannot contend
  with an interactive sync — see PLAN.md Open question 2.

## Web UI redesign: browse-live vs. browse-archive

The durable-archive feature (above) currently surfaces in the web UI as
badges plus a tri-state filter chip bolted onto the single session list.
That was the minimal integration; it does not give the archive its own
first-class place, and archiving is CLI-only. Planned:

- a top-level **Live / Archive / All** mode switch (default Live) that drives
  the whole UI — session list, search, and the **statistics viewer**;
- a per-row **provenance tag** (live / archived / deleted), important in All
  mode and the discovery path in Live mode;
- **web-driven archiving**: a per-session "archive / update" button (with a
  per-session status: not archived / archived / stale) plus a global "sync
  all", each with a **live progress bar** (Server-Sent Events);
- a minimal **archive landing view** (vault path, per-source counts, deleted
  count);
- **UI-wide help affordances** (tooltips / help icons).

This revises the earlier "web is strictly read-only" posture: the web app may
now write to **your own vault** via explicit action, but **never to your
agents' data** (the invariant that matters). Design of record:
[`docs/web-redesign.md`](docs/web-redesign.md). **Shipped in 0.4.0**
(as of 2026-08-09) — every bullet above is in place. The Live mode also
carries archive provenance tags, so the discovery path works without
switching modes.

## Other ideas

- **`tail` / `watch`**: live-follow the most recently active session as it
  grows.
- **Per-source counts on the web filter chips** (deferred: needs a cheap
  count; opencode's full enumeration is currently too slow per page load).
- **Disk-usage reporting** for each session store (read-only).

---

*Living document. Verdicts above reflect a format-feasibility review; the
on-disk formats of the Tier 2/3 agents are internal and may have changed —
re-verify against a current build before implementing.*
