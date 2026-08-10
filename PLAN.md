# Plan-of-Record: scrollback

**Library name**: `scrollback`. **Status**: 0.6.0 (alpha).
**Repo**: <https://github.com/a-attia/scrollback>. **Licence**: MIT.
**Audience**: developers who use AI coding agents and want their session
history to be searchable, exportable, and permanent.

> **Doc status**: Living plan-of-record. Date-stamp every revision.
> Humans wanting to *use* scrollback should read [`README.md`](README.md);
> AI agents working on it start at [`AGENTS.md`](AGENTS.md).

---

## Contents

- [Headline goal](#headline-goal)
- [Scope and non-scope](#scope-and-non-scope)
- [1. Architecture](#1-architecture)
- [2. Public surface](#2-public-surface)
- [3. Milestones](#3-milestones)
- [4. Design decisions log](#4-design-decisions-log)
- [5. Open questions](#5-open-questions)
- [6. Tracking and cadence](#6-tracking-and-cadence)

---

## Headline goal

AI coding agents each keep their own session history, in their own format,
in their own directory — and several of them delete it on a timer (Claude
Code prunes after roughly 30 days). scrollback makes that history *one*
searchable corpus across every agent you use, and makes it *permanent*: a
durable, user-owned vault that outlives the agents' retention policies.
Everything is local and strictly read-only with respect to the agents' own
data; the only thing scrollback ever writes is its own vault.

The bet is that an agent transcript is a research artefact worth keeping —
it records not just the code you ended up with but the reasoning, the
rejected alternatives, and the dead ends. Those are exactly the things that
are otherwise lost.

## Scope and non-scope

**In scope.**

- Reading agent session stores through a common adapter interface, so a new
  agent is one file and everything else works automatically.
- Three surfaces over the same model: a CLI, a local web app, and a native
  window. All read the same `Store`.
- A durable archive vault, with lossless round-trip, integrity
  verification, and export/import for moving between machines.
- Search: a lexical fallback that always works, plus an optional SQLite
  FTS5 index for speed.

**Out of scope**, deliberately:

- *Writing to, or resuming inside, an agent's own store.* scrollback prints
  the resume command; it never drives the agent. This keeps the read-only
  invariant absolute rather than conditional.
- *Any network service.* No telemetry, no cloud sync, no accounts. The web
  app binds loopback only. Multi-machine sync is file-based (export a vault
  zip, import it elsewhere) precisely so no server is needed.
- *Editing transcripts.* The vault is an archive, not a workspace.
- *A frontend build step.* Plain ES modules and CSS, served static.

## 1. Architecture

```text
        agents' own stores (read-only)
   opencode.db   ~/.claude   ~/.codex   .aider.chat.history.md
        │            │           │            │
        └────────────┴─────┬─────┴────────────┘
                           │   Source adapters (sources/*.py)
                           ▼   normalise to Session / Message / Part
                     ┌───────────┐
                     │   Store   │  dedup, filter, search, stats
                     └─────┬─────┘
              ┌────────────┼────────────┐
              ▼            ▼            ▼
            cli.py     web/app.py    fts.py
                           │
                     ArchiveStore  ──►  ~/.scrollback/archive
                                        manifest.sqlite + sessions/*.json
                                          ▲
                                          └── read back as ArchiveSource
```

Two properties carry most of the design weight:

**The archive is both a sink and a source.** `ArchiveStore` writes the
vault; `ArchiveSource` reads it back through the ordinary `Source`
interface, so browse, search, export, and stats work over archived
sessions with no special-casing — including sessions the agent has since
deleted. Because a vault-held session keeps its *original* source name,
a live copy and an archived copy share a `(source, id)` key and dedup to
one. Archive is therefore a browse **mode**, not a source.

**Change detection is a signature, not a diff.** Both the vault and the
FTS index key on `(updated, message_count)` read from a source's listing.
This keeps sync O(sessions) rather than O(bytes), which matters when a
single session can exceed 500 MB. The subtlety that has bitten this
codebase twice is recorded in [`AGENTS.md`](AGENTS.md) under "Signature
discipline"; read it before touching sync.

## 2. Public surface

| Surface | Entry point | Notes |
|:--|:--|:--|
| CLI | `scrollback` | Browse (`list`, `show`, `search`, `stats`), move data out (`export`, `copy`, `resume`), keep it (`archive`, `index`), and manage the install (`sources`, `doctor`, `install-launcher`, `uninstall`) |
| Web API | `scrollback web` | FastAPI JSON API, loopback only, Host-header guarded |
| Native window | `scrollback-app` | pywebview over the same local server |
| Python | `scrollback.store.Store` | Not yet a committed API; see Open question 3 |

The vault's on-disk format is a public contract in practice — users
back it up and move it between machines. It is versioned by
`archivefmt.SCHEMA_VERSION` so a future release can migrate old vaults.

## 3. Milestones

| Milestone | Status | Goal |
|:--|:--|:--|
| M1 — core model + opencode + CLI | done | Common `Session`/`Message`/`Part`; SQLite adapter; list/show/search/export |
| M2 — multi-source | done | Claude Code (incl. subagents), Codex, Aider adapters |
| M3 — web app + native window | done | FastAPI + static frontend; pywebview shell |
| M4 — FTS index | done | Optional SQLite FTS5 index with incremental sync |
| M5 — durable archive, phases 1–4 | done (0.4.0) | Lossless vault, CLI, read-back as a source, web integration |
| M6 — simpler install | done (0.5.0) | Web + native + rich are default deps; no extras to choose |
| M7 — archive correctness + scale | done (0.6.0) | Signature convergence, honest deleted-session reporting, O(index) request paths — see [CHANGELOG](CHANGELOG.md) |
| M8 — vault search | planned | Index the vault so archived sessions are fast to search, not just browse |
| M9 — auto-sync | planned | Opt-in background archiving, so keeping history needs no discipline |
| M10 — more adapters | planned | Gemini CLI and Zed are Tier 1; see [`ROADMAP.md`](ROADMAP.md) |

## 4. Design decisions log

Append-only. A reversed decision gets a **new** entry that supersedes the
old one; the old entry stays, so the reasoning trail survives.

**D1 — Read-only is an invariant, not a default** (2026-06).
Every adapter opens its store read-only (`mode=ro` for SQLite). *Rejected:*
a "safe write" mode for resuming or tagging sessions. Even a correct
implementation would make the safety claim conditional, and the claim is
the product. *Consequence:* resume is a printed command, never an action.

**D2 — Vault precedence is live-wins** (2026-07).
When a session exists both live and in the vault, the live copy shadows the
archived one during dedup, implemented by ordering the archive source last.
*Alternative:* prefer whichever has more messages. *Rejected:* it makes
precedence data-dependent and therefore unpredictable to explain.
*Consequence:* the archived copy is still reachable, and a stale vault copy
is flagged rather than silently shown.

**D3 — Overwrite on change, but never shrink** (2026-07).
A re-archive overwrites in place rather than keeping versions, but a read
yielding *fewer* messages than the archived copy is refused. *Rationale:* a
truncated or partial read must never destroy good archived data; full
version history would multiply an already multi-GB vault. *Consequence:*
`kept_shrunk` in sync output; the guard compares written-count to
written-count (`archived_message_count`).

**D4 — Deletion is one fact with one definition** (2026-08).
"Deleted from agent" means a `(source, id)` in the manifest absent from the
live key set. *Context:* it had two implementations — a timestamp
comparison for the aggregate count and a set difference for the per-session
badge — which disagreed, so a headline number contradicted the list it
linked to. *Consequence:* `ArchiveStore.stats(live_keys=...)` is the
authoritative form; `last_full_sync` is the fallback and is written only by
a full sync.

**D5 — Signatures are stored as compared** (2026-08).
The `(updated, message_count)` written to the manifest is the one taken from
the source's *listing*, never recomputed from the loaded session. *Context:*
adapters may legitimately count differently in the two paths; storing the
loaded count meant such sessions never matched their own signature and
re-archived on every sync forever. *Consequence:* the never-shrink guard
needs its own column, `archived_message_count`.

**D6 — Whole-vault work never runs inline** (2026-08).
Deep integrity verification parses every archived file, so it runs as a
background job; the interactive path does a presence check instead.
*Rationale:* the landing page blocked for ~18 s on a 3.4 GB vault.
*Consequence:* two verification depths, and `--quick` on the CLI.

**D7 — The manifest runs in WAL mode** (2026-08).
*Rationale:* it indexes gigabytes of session files, so corruption is real
data loss; WAL also lets the UI read during a long sync. *Consequence:*
anything that copies the vault must `checkpoint()` first and exclude the
`-wal`/`-shm` sidecars, which are only valid beside their own database.

## 5. Open questions

1. **Vault compression.** The vault is ~3.4 GB for ~8.8 GB of source data,
   stored as pretty-printed JSON. Gzip would cut it substantially, at the
   cost of making files no longer greppable by hand. Worth measuring before
   deciding.
2. **When does auto-sync become safe?** (M9) The failure mode to avoid is a
   background job holding the manifest while a user runs a CLI sync. The
   writer lock exists; the scheduling policy does not.
3. **Is `Store` a public Python API?** It is importable and stable in
   practice, but undocumented as a contract. Committing to it constrains
   refactoring; not committing leaves users guessing.
4. **Retention policy for the vault itself.** Currently: keep everything,
   forever. At some scale a user will want "drop archived-only sessions
   older than N years", which is in tension with the durability promise.

## 6. Tracking and cadence

| Artefact | Lives in |
|:--|:--|
| This plan-of-record | `PLAN.md` |
| Agent-facing rules + invariants | [`AGENTS.md`](AGENTS.md) |
| User documentation | [`README.md`](README.md) |
| Future work, with research | [`ROADMAP.md`](ROADMAP.md) |
| Per-feature design records | `docs/*.md` |
| Released + unreleased changes | [`CHANGELOG.md`](CHANGELOG.md) |
| Contributor workflow | [`CONTRIBUTING.md`](CONTRIBUTING.md) |

Design records under `docs/` are written *before* a large feature and
updated with as-built notes afterwards; they are historical records, so
prefer adding a note over rewriting one.

---

*Created 2026-08-09. Maintained by Ahmed Attia.*
