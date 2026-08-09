# AGENTS.md — scrollback

Entry point for AI coding agents. Humans start at [`README.md`](README.md);
the plan-of-record is [`PLAN.md`](PLAN.md).

## What this project is

A local-first, **read-only** tool that reads AI coding-agent session
history (opencode, Claude Code, Codex, Aider) through a common adapter
interface and exposes it as a CLI, a web app, and a native window. It also
maintains a **durable archive vault** that outlives the agents' own
retention policies.

Python 3.10+. Package at `src/scrollback/`. Hatchling build. MIT.

## Hard invariants — never violate

1. **Never write to, lock for writing, or upload a user's agent data.**
   The opencode SQLite DB is opened `mode=ro`; JSONL/markdown sources are
   read-only opens. The ONLY writes scrollback performs are to its own
   vault (`~/.scrollback/archive`) and disposable cache
   (`~/.cache/scrollback/`). Tests assert this.
2. **The vault is durable user data.** It survives `uninstall` unless the
   user passes `--purge-archive`. Never delete or truncate it implicitly.
3. **A sync must never read the vault back into itself.** See "Signature
   discipline" below.
4. **Web binds loopback only**, with a Host-header guard against DNS
   rebinding (`web/app.py:_install_host_guard`).
5. **Untrusted input stays contained**: imported vault zips go through
   `_safe_extract_zip` (zip-slip + symlink rejection); manifest
   `file_path` values go through `ArchiveStore.safe_path` (traversal
   rejection). Never bypass either.

## Signature discipline (the subtlest thing in this codebase)

Change detection for both the archive and the FTS index keys on a
signature: `(updated_iso, message_count)`, taken from a source's
**listing** metadata (`list_sessions`).

The invariant: **whatever you compare against must be what you stored.**

- `list_sessions()` and `load_session()` may report *different*
  `message_count` for the same session. Claude Code is the live example:
  listing and loading previously applied different rules. Adapters should
  keep the two consistent, but the archive must not assume it.
- Therefore `_archive_session` stores the signature from `meta` (the
  listing copy), never recomputed from the loaded `sess`. Storing the
  loaded counts makes such sessions re-archive on *every* sync, forever
  "stale". This was a real, shipped bug — do not reintroduce it.
- The count of messages actually written is tracked separately in
  `archived_message_count` and is what the never-shrink guard compares.
- `backfill_meta` has the same constraint: take the signature from the
  manifest row, not from the parsed file.

Regression tests: `tests/test_archive.py`, the `AsymmetricSource` cases.
Any new adapter whose list/load counts can differ must be exercised
against them.

## Orphan (deleted-session) discipline

"Deleted from agent" must be **one fact with one definition**, or the UI
contradicts itself (a headline count that disagrees with the list it
links to — another real, shipped bug).

- Authoritative form: a `(source, id)` in the manifest that is absent
  from the live key set. Pass `live_keys=` to `ArchiveStore.stats()`
  whenever a live store is at hand.
- Fallback: rows whose `last_seen_live` predates `last_full_sync`.
- **Only a full `sync()` may write `last_full_sync`.** `sync_one` and
  `sync_many` look at a subset, so they prove nothing about the rest.
- The per-session flag (`raw["archived_only"]`, set in `store._dedup`)
  and the aggregate count must always agree.

## Layout

```text
src/scrollback/
├── models.py          Session / Message / Part — the common model
├── store.py           facade over adapters; dedup, filtering, search
├── archive.py         ArchiveStore — the durable vault (sync/verify/export)
├── archivefmt.py      lossless JSON round-trip for archived sessions
├── fts.py             optional SQLite FTS5 index
├── export.py          render a session to md / html / json / text
├── cli.py             argparse entry point (all subcommands)
├── sources/
│   ├── base.py        the Source contract
│   ├── registry.py    adapter registration
│   ├── opencode.py    SQLite adapter
│   ├── claudecode.py  JSONL adapter (+ subagent sidechains)
│   ├── codex.py       JSONL rollout adapter
│   ├── aider.py       markdown-history adapter
│   └── archive.py     reads the vault back as a Source
└── web/
    ├── app.py         FastAPI JSON API
    └── static/        app.js / style.css / index.html (no build step)
```

Docs: [`README.md`](README.md) (users), [`PLAN.md`](PLAN.md)
(plan-of-record), [`ROADMAP.md`](ROADMAP.md) (future work),
[`CONTRIBUTING.md`](CONTRIBUTING.md) (workflow),
[`CHANGELOG.md`](CHANGELOG.md), `docs/*.md` (per-feature design records).

## Conventions

- **Stdlib first.** A new runtime dependency for the core CLI is a hard
  sell. Optional features go behind extras.
- **Tolerant parsing.** Skip malformed records; degrade, never crash. A
  single bad line must not lose a session.
- **Platform-agnostic.** Guard OS-specific code with `sys.platform` and
  make it best-effort.
- **No build step for the frontend.** Plain ES modules + CSS, served
  static. Vendored libraries live in `web/static/vendor/`.
- **Comments explain *why*.** The codebase documents rationale and
  rejected alternatives, not restatements of the code. Match that.
- **Line length 100**, `ruff` enforced, target py310.

## Testing

```bash
pytest -q              # full suite, ~4s
ruff check src tests   # lint (CI runs exactly this)
```

Both must pass. `ruff format` is **not** used — do not reformat the repo.

Test discipline:

- Every bug fix needs a regression test.
- **Verify a new test actually fails against the unfixed code.**
  `git stash push -- src/ && pytest -q; git stash pop` is the cheap check.
  Several bugs here were invisible precisely because the existing fakes
  were too well-behaved to express them.
- Fakes must be able to express real-world asymmetry (see
  `AsymmetricSource`). A fake where `list_sessions().message_count`
  always equals `load_session().message_count` cannot catch the class of
  bug that has actually shipped.
- Tests use synthetic fixtures under `tmp_path`; tests touching real
  local data skip gracefully when it is absent.

## Performance constraints

Real vaults reach multiple GB across hundreds of sessions; a single
archived session can exceed 500 MB. Anything on a request path or a
render path must be O(index), not O(vault).

- Listings read `meta_json` from the manifest — never parse session files.
- `ArchiveSource` caches its most recent parse (keyed on mtime+size) so
  paging a transcript parses once, not once per page.
- Whole-vault work (deep integrity verification) belongs in a background
  job, never inline in a request.
- The manifest runs in WAL mode. Anything that **copies** the vault must
  call `ArchiveStore.checkpoint()` first and exclude `-wal`/`-shm`
  sidecars — they are only valid beside the database that wrote them.

## Adding a source adapter

Subclass `Source` (`sources/base.py`), register in `registry.py`. The
CLI, search, export, web, and index then work automatically. Checklist:

- read-only access only;
- tolerant parsing;
- override `load_session_meta` / `load_messages` so large sessions do not
  load in full;
- keep `list_sessions().message_count` consistent with what
  `load_session()` yields (see "Signature discipline");
- `resume_command` if the agent supports resume-by-id;
- synthetic-fixture tests — contributors will not have the agent's data.

## Gotchas

- Session ids are unique only **within** a source. Always key on
  `(source, id)`.
- Claude Code subagent ids embed `::`; the store's `source:id` selector
  splits on a single `:`, and `::` never collides with it. Path
  components are sanitised by `archive._safe_component`.
- `ArchiveSource.name` is `"archive"`, but the sessions it yields keep
  their **original** `source` (e.g. `"opencode"`). Archive is a browse
  *mode*, not a source. Never expose it as a source filter chip.
- Dedup is ordering-dependent: live sources first, archive last, so the
  fresher live copy wins (`store.with_archive`).
- Prompt-caching: do not edit this file mid-session.

---

*Created 2026-08-09. Maintained by Ahmed Attia.*
