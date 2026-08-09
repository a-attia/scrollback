"""Durable session archive: a one-way, lossless local vault.

scrollback reads your agents read-only; `ArchiveStore` copies the sessions it
reads into a user-owned vault and keeps them **forever**, surviving the
agents' own auto-deletion (e.g. Claude Code's ~30-day cleanup). See
`docs/archive-plan.md` (design of record), Component 2.

Design
------
* The vault lives at ``~/.scrollback/archive`` (override with ``--dest`` or
  ``$SCROLLBACK_ARCHIVE``). Unlike the disposable ``~/.cache/scrollback/``
  index, this is **durable, user-owned** data that survives ``uninstall``.
* ``manifest.sqlite`` records a per-session signature
  ``(updated_iso, message_count)`` plus provenance (first_archived,
  last_synced, last_seen_live, file_path), so :meth:`sync` only re-serializes
  new/changed sessions.
* ``sessions/<source>/<id>.json`` holds one lossless JSON per session
  (via :mod:`scrollback.archivefmt`, keeping every ``raw`` blob).

This mirrors :class:`scrollback.fts.FtsIndex` with two deliberate
differences (see the plan, §2 + §7.3):

* **The prune step is inverted.** A session that vanishes from its live
  source is **kept** in the vault (counted ``kept_orphan``) -- that is the
  entire point of a durable archive.
* **Never-shrink guard.** If a re-sync reads back a session with *fewer*
  messages than the archived copy (corruption / partial read / truncation),
  the write is skipped (counted ``kept_shrunk``) rather than clobbering good
  archived data with a degraded read.
"""

from __future__ import annotations

import contextlib
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from . import archivefmt

if TYPE_CHECKING:
    from .store import Store


def default_archive_path() -> Path:
    """Resolve the vault path: ``$SCROLLBACK_ARCHIVE`` else ``~/.scrollback/archive``.

    The ``--dest`` flag is applied by the caller (it takes precedence over the
    env var); a future ``~/.scrollback/config.json`` slots between the env var
    and the default (Component 6, not v1).
    """
    override = os.environ.get("SCROLLBACK_ARCHIVE")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".scrollback" / "archive"


_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def _safe_component(name: str) -> str:
    """Sanitize a source name or session id into a safe path component.

    Claude Code subagent ids contain ``::`` (claudecode.py); opencode ids are
    already safe. Anything outside ``[A-Za-z0-9._-]`` is replaced with ``_``.
    Collisions are avoided by appending a short hash of the original when
    sanitization actually changed the string.
    """
    safe = _UNSAFE.sub("_", name)
    if safe != name:
        import hashlib

        digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
        safe = f"{safe}-{digest}"
    return safe


def _safe_extract_zip(zip_path: "Path", dest: "Path") -> None:
    """Extract a zip, rejecting entries that would escape `dest` (zip-slip).

    Imported zips are untrusted (uploaded via the web import endpoint), so an
    entry named ``../../foo``, an absolute path, or a symlink must never write
    outside the temp extraction dir. Each member's resolved target is checked
    to stay within `dest`; symlink entries are skipped.
    """
    import stat
    import zipfile

    dest = dest.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            # Skip symlinks (their "content" is a path that could point anywhere).
            mode = (info.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                continue
            target = (dest / info.filename).resolve()
            if target != dest and dest not in target.parents:
                raise ValueError(f"unsafe path in zip archive: {info.filename!r}")
            zf.extract(info, dest)


class _SigOnly:
    """Carries just the `(updated, message_count)` signature pair.

    Lets a manifest row stand in for a `Session` where `_summary_json` only
    needs those two fields (see `backfill_meta`).
    """

    __slots__ = ("updated", "message_count")

    def __init__(self, updated, message_count) -> None:
        self.updated = updated
        self.message_count = message_count


def _row_get(row, key: str):
    """Read a column from a sqlite3.Row, returning None when it is absent.

    Rows selected before a migration ran (or by an older query) may not carry
    every column; treat "not selected" the same as "NULL".
    """
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def _summary_json(session, meta=None) -> str:
    """A compact JSON metadata summary stored in the manifest, so the archive
    can be *listed* without parsing every full session file. Mirrors the
    lightweight list shape (`serialize.session_summary`) minus children/
    messages (a listing never needs those).

    Archive-provenance fields are stripped: `archived` / `archived_only` /
    `archive_status` describe the session's relationship to the vault *right
    now*, so a value frozen at archive time would be stale the moment the live
    copy changes. They are recomputed on read (`ArchiveSource`, `Store._dedup`).

    `meta`, when given, is the LISTING metadata this session was archived from.
    Its `updated` / `message_count` overwrite the loaded values, because those
    two fields form the signature `Store._dedup` compares a live session
    against to decide "archived" vs "stale". Storing the loaded counts instead
    made asymmetric adapters (Claude Code) read back as permanently stale.
    """
    import json as _json

    from .serialize import session_summary

    d = session_summary(session)
    for volatile in ("children", "archived", "archived_only", "archive_status"):
        d.pop(volatile, None)
    if meta is not None:
        upd = meta.updated
        # Accept either a datetime (a live Session) or an already-ISO string
        # (a signature read back out of the manifest).
        d["updated"] = upd.isoformat() if hasattr(upd, "isoformat") else upd
        d["message_count"] = meta.message_count
    return _json.dumps(d, ensure_ascii=False)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS archived (
    source TEXT NOT NULL,
    session_id TEXT NOT NULL,
    updated TEXT,
    message_count INTEGER,
    first_archived TEXT,
    last_synced TEXT,
    last_seen_live TEXT,
    file_path TEXT,
    meta_json TEXT,
    archived_message_count INTEGER,
    PRIMARY KEY (source, session_id)
);
"""

#: Columns added after the initial release, applied by `_connect(write=True)`.
#: Each is nullable so an old manifest migrates by a bare ALTER TABLE.
_ADDED_COLUMNS = {
    # Metadata summary, so listings read SQLite instead of parsing every file.
    "meta_json": "TEXT",
    # Number of messages actually WRITTEN to the archive file. Distinct from
    # `message_count`, which is the *listing* count forming the change-detection
    # signature. The two differ for adapters whose list count and load count use
    # different rules (Claude Code counts every user/assistant turn when
    # listing, but only renderable ones when loading), and conflating them made
    # such sessions re-archive on every single sync. See `sync`.
    "archived_message_count": "INTEGER",
}


class ArchiveStore:
    """Read/write wrapper around a durable session vault."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_archive_path()

    # -- paths --------------------------------------------------------------

    @property
    def manifest_path(self) -> Path:
        return self.path / "manifest.sqlite"

    @property
    def sessions_dir(self) -> Path:
        return self.path / "sessions"

    def _session_file(self, source: str, session_id: str) -> Path:
        return (
            self.sessions_dir
            / _safe_component(source)
            / f"{_safe_component(session_id)}.json"
        )

    def safe_path(self, rel_path: str) -> Path | None:
        """Resolve a manifest `file_path` to an absolute path, but ONLY if it
        stays inside this vault. Returns None for anything that would escape
        (absolute paths, ``..`` traversal) -- manifests from an imported vault
        are untrusted, so a crafted `file_path` must never read outside here."""
        if not rel_path:
            return None
        base = self.path.resolve()
        target = (self.path / rel_path).resolve()
        if target == base or base in target.parents:
            return target
        return None

    def exists(self) -> bool:
        return self.manifest_path.is_file()

    # -- connection ---------------------------------------------------------

    @contextlib.contextmanager
    def _connect(self, *, write: bool):
        """Context manager yielding a manifest connection that is always CLOSED
        on exit (and committed on the write path). `with sqlite3.connect(...)`
        alone only commits -- it leaks the connection -- so we manage it here."""
        if write:
            self.path.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.manifest_path, timeout=30.0)
            # Wait up to 30s for a competing writer instead of erroring instantly
            # (defense in depth; the web layer already serializes its writers).
            conn.execute("PRAGMA busy_timeout = 30000")
            # WAL: the manifest indexes gigabytes of session files, so a crash
            # mid-sync corrupting it is a genuine data-loss event. WAL also lets
            # readers (listing, the web UI) proceed during a long sync.
            with contextlib.suppress(sqlite3.Error):
                conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(_SCHEMA)
            # Migrate older manifests that predate later columns.
            cols = {r[1] for r in conn.execute("PRAGMA table_info(archived)")}
            missing = [c for c in _ADDED_COLUMNS if c not in cols]
            for col in missing:
                conn.execute(
                    f"ALTER TABLE archived ADD COLUMN {col} {_ADDED_COLUMNS[col]}"
                )
            if missing:
                conn.commit()
        else:
            uri = f"file:{self.manifest_path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            if write:
                conn.commit()
        finally:
            conn.close()

    # -- sync ---------------------------------------------------------------

    def sync(self, store: "Store", *, progress=None) -> dict[str, int]:
        """Incrementally copy live sessions into the vault.

        Returns counts ``{"added", "updated", "unchanged", "kept_orphan",
        "kept_shrunk"}``. ``progress(done, total)`` is called per live session
        if provided.

        Reads *live* sources only (never an injected archive source), so it can
        never archive its own archive. Writes only to the vault -- the
        read-only invariant on source stores is preserved.

        If `store` happens to carry a reader for THIS vault (the CLI composes
        one so deleted sessions stay browsable), it is dropped here: reading
        our own archive back would make every orphan look still-present and
        defeat deleted-session detection entirely. A reader for a *different*
        vault is kept -- that is exactly how :meth:`import_from` merges.
        """
        stats = {"added": 0, "updated": 0, "unchanged": 0,
                 "kept_orphan": 0, "kept_shrunk": 0}
        now = datetime.now(timezone.utc).isoformat()

        with self._connect(write=True) as conn:
            have = {
                (r["source"], r["session_id"]): r
                for r in conn.execute(
                    "SELECT source, session_id, updated, message_count, "
                    "first_archived, file_path, archived_message_count FROM archived"
                )
            }
            live = self._live_only(store).list_sessions(fold_subagents=False)
            live_keys = {(s.source, s.id) for s in live}
            total = len(live)

            for i, meta in enumerate(live):
                key = (meta.source, meta.id)
                sig = (
                    meta.updated.isoformat() if meta.updated else None,
                    meta.message_count,
                )
                prev = have.get(key)
                if prev is not None and (prev["updated"], prev["message_count"]) == sig:
                    # Unchanged: refresh last_seen_live only.
                    conn.execute(
                        "UPDATE archived SET last_seen_live = ? "
                        "WHERE source = ? AND session_id = ?",
                        (now, meta.source, meta.id),
                    )
                    stats["unchanged"] += 1
                else:
                    outcome = self._archive_session(conn, store, meta, prev, now)
                    stats[outcome] += 1
                if progress:
                    progress(i + 1, total)

            # Inverted prune: sessions no longer live are KEPT (the durability
            # guarantee), only counted.
            stats["kept_orphan"] = len(set(have) - live_keys)

            # `last_full_sync` marks a run that enumerated EVERY live session,
            # so `last_seen_live < last_full_sync` is exactly "not live any
            # more". Only full syncs may write it -- a single-session or batch
            # sync proves nothing about the sessions it did not look at. Every
            # row seen above was stamped with this same `now`, so the
            # comparison is exact rather than racing per-row timestamps.
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_full_sync', ?)",
                (now,),
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_sync', ?)",
                (now,),
            )
            conn.commit()
        return stats

    def _live_only(self, store: "Store") -> "Store":
        """`store` minus any reader pointed at THIS vault.

        The CLI attaches an `ArchiveSource` for this vault so deleted sessions
        stay browsable; feeding that back into a sync would let the vault see
        itself as a live source, so no session could ever be detected as gone.
        Readers for other vaults are left alone -- `import_from` relies on
        syncing from exactly such a reader.
        """
        from .sources.archive import ArchiveSource
        from .store import Store as _Store

        mine = self.path.expanduser().resolve()

        def _is_self(src) -> bool:
            if not isinstance(src, ArchiveSource):
                return False
            try:
                return src._store.path.expanduser().resolve() == mine
            except OSError:
                return False

        kept = [s for s in store.sources if not _is_self(s)]
        if len(kept) == len(store.sources):
            return store
        return _Store(kept)

    def sync_one(self, store: "Store", source: str, session_id: str) -> str:
        """Archive/update a single live session by ``(source, id)``.

        Returns the outcome bucket: ``"added"`` | ``"updated"`` |
        ``"unchanged"`` | ``"kept_shrunk"`` | ``"not_found"``. Same fidelity and
        never-shrink guarantees as :meth:`sync`; writes only to the vault. Used
        by the web per-session "archive / update" action so it does not have to
        re-enumerate every session.
        """
        meta = store.load_session_meta(session_id, source=source)
        if meta is None:
            return "not_found"
        now = datetime.now(timezone.utc).isoformat()
        with self._connect(write=True) as conn:
            row = conn.execute(
                "SELECT source, session_id, updated, message_count, "
                "first_archived, file_path, archived_message_count FROM archived "
                "WHERE source = ? AND session_id = ?",
                (source, session_id),
            ).fetchone()
            sig = (meta.updated.isoformat() if meta.updated else None,
                   meta.message_count)
            if row is not None and (row["updated"], row["message_count"]) == sig:
                conn.execute(
                    "UPDATE archived SET last_seen_live = ? "
                    "WHERE source = ? AND session_id = ?",
                    (now, source, session_id),
                )
                outcome = "unchanged"
            else:
                outcome = self._archive_session(conn, store, meta, row, now)
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_sync', ?)",
                (now,),
            )
            conn.commit()
        return outcome

    def _archive_session(self, conn, store, meta, prev, now: str) -> str:
        """Serialize one session to the vault; return the stat bucket name.

        Returns one of ``"added"``, ``"updated"``, ``"kept_shrunk"``.
        """
        sess = store.load_session(meta.id, source=meta.source)
        if sess is None:
            # Could not load (transient); leave any existing copy untouched.
            return "unchanged"

        # Never-shrink guard: refuse to overwrite a larger archived copy with a
        # smaller freshly-read one (corruption / partial read / truncation).
        # Compare like-for-like: `message_count` if the source reports it, else
        # the actual number of loaded messages -- on BOTH the archived and the
        # new copy, so the comparison is consistent. If the archived count is
        # unknown (NULL) we fall back to the byte size of the stored file, so a
        # truncated re-read still can't silently clobber a good copy.
        if prev is not None:
            new_count = len(sess.messages)
            # Compare against what was actually WRITTEN last time. Falling back
            # to the listing `message_count` would compare a load-count against
            # a list-count, which differ for some adapters (see _ADDED_COLUMNS).
            prev_count = _row_get(prev, "archived_message_count")
            if prev_count is None:
                prev_count = prev["message_count"]
            if prev_count is not None:
                if new_count < prev_count:
                    conn.execute(
                        "UPDATE archived SET last_seen_live = ? "
                        "WHERE source = ? AND session_id = ?",
                        (now, meta.source, meta.id),
                    )
                    return "kept_shrunk"
            else:
                # Archived count unknown: guard on file size as a proxy -- never
                # replace a non-empty archived file with a strictly smaller one.
                existing = self._session_file(meta.source, meta.id)
                new_bytes = len(archivefmt.to_archive_json(sess, indent=2).encode("utf-8"))
                try:
                    old_bytes = existing.stat().st_size if existing.is_file() else 0
                except OSError:
                    old_bytes = 0
                if old_bytes and new_bytes < old_bytes:
                    conn.execute(
                        "UPDATE archived SET last_seen_live = ? "
                        "WHERE source = ? AND session_id = ?",
                        (now, meta.source, meta.id),
                    )
                    return "kept_shrunk"

        dest = self._session_file(meta.source, meta.id)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(archivefmt.to_archive_json(sess, indent=2), encoding="utf-8")

        first_archived = prev["first_archived"] if prev is not None else now
        # Store the signature taken from `meta` -- the SAME listing metadata the
        # next sync will compare against. Recomputing it from `sess` (the loaded
        # copy) is what made list-vs-load count mismatches re-archive forever.
        conn.execute(
            "INSERT OR REPLACE INTO archived (source, session_id, updated, "
            "message_count, first_archived, last_synced, last_seen_live, file_path, "
            "meta_json, archived_message_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                meta.source, meta.id,
                meta.updated.isoformat() if meta.updated else None,
                meta.message_count,
                first_archived, now, now,
                str(dest.relative_to(self.path)),
                _summary_json(sess, meta),
                len(sess.messages),
            ),
        )
        return "added" if prev is None else "updated"

    # -- stats --------------------------------------------------------------

    def stats(self, *, live_keys=None) -> dict[str, int]:
        """Summary counts for the vault (per-source + orphan count).

        ``orphans`` counts archived sessions that are no longer live -- i.e.
        deleted from their agent, and therefore only surviving here.

        Two ways to determine that, in order of preference:

        * ``live_keys`` -- an authoritative set of live ``(source, id)`` tuples
          (e.g. ``Store.live_keys()``). Exact, and correct even if a sync has
          never run. Callers that already hold a live store should pass this.
        * otherwise, fall back to the ``last_full_sync`` marker: a row whose
          ``last_seen_live`` predates the last run that enumerated *everything*
          was not seen live by that run. Note this is only meaningful after a
          full :meth:`sync`; single-session and batch syncs deliberately do not
          advance the marker, since they prove nothing about sessions they
          never looked at.

        The count returned here must agree with the ``archived_only`` flag that
        :func:`scrollback.store._dedup` puts on individual sessions -- they are
        the same fact, and a UI that shows a total from one and a drill-down
        list from the other will contradict itself.
        """
        if not self.exists():
            return {"sessions": 0, "orphans": 0, "per_source": {}}
        with self._connect(write=False) as conn:
            total = conn.execute("SELECT COUNT(*) FROM archived").fetchone()[0]
            if live_keys is not None:
                live = set(live_keys)
                orphans = sum(
                    1
                    for r in conn.execute("SELECT source, session_id FROM archived")
                    if (r["source"], r["session_id"]) not in live
                )
            else:
                row = conn.execute(
                    "SELECT value FROM meta WHERE key = 'last_full_sync'"
                ).fetchone()
                last_full_sync = row[0] if row else None
                orphans = 0
                if last_full_sync is not None:
                    orphans = conn.execute(
                        "SELECT COUNT(*) FROM archived "
                        "WHERE last_seen_live IS NULL OR last_seen_live < ?",
                        (last_full_sync,),
                    ).fetchone()[0]
            per_source = {
                r["source"]: r["n"]
                for r in conn.execute(
                    "SELECT source, COUNT(*) AS n FROM archived GROUP BY source"
                )
            }
        return {"sessions": total, "orphans": orphans, "per_source": per_source}

    def backfill_meta(self) -> int:
        """One-time: populate `meta_json` for rows that lack it (vaults archived
        before the column existed), so listings become pure-SQLite fast. Parses
        each such file ONCE. Returns the count backfilled; no-op once complete."""
        if not self.exists():
            return 0
        with self._connect(write=True) as conn:
            rows = list(conn.execute(
                "SELECT source, session_id, file_path, updated, message_count "
                "FROM archived WHERE meta_json IS NULL"
            ))
            n = 0
            for r in rows:
                path = self.safe_path(r["file_path"])
                if path is None or not path.is_file():
                    continue
                try:
                    sess = archivefmt.from_archive_json(path.read_text(encoding="utf-8"))
                except (OSError, ValueError, KeyError):
                    continue
                # Take the signature fields from the MANIFEST, not the parsed
                # file: the manifest holds the listing-derived signature that
                # staleness is judged against. Using the loaded session's own
                # counts would make asymmetric-adapter sessions read back as
                # permanently stale (the same defect fixed in `_archive_session`).
                summary = _summary_json(sess, _SigOnly(r["updated"], r["message_count"]))
                conn.execute(
                    "UPDATE archived SET meta_json = ? WHERE source = ? AND session_id = ?",
                    (summary, r["source"], r["session_id"]),
                )
                n += 1
            if n:
                conn.commit()
        return n

    def disk_usage(self) -> int:
        """Total bytes on disk for the whole vault (manifest + session files)."""
        if not self.exists():
            return 0
        total = 0
        for p in self.path.rglob("*"):
            if p.is_file():
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
        return total

    def sync_many(self, store: "Store", keys, *, progress=None) -> dict[str, int]:
        """Archive/update a specific set of `(source, id)` sessions.

        Used by the web UI's bulk actions ("update all stale", "archive all
        matching this filter"). Same per-session semantics as :meth:`sync_one`
        (signature skip + never-shrink guard); does not prune. `keys` is an
        iterable of `(source, session_id)` tuples. Returns counts keyed by
        outcome, including ``not_found`` for keys that no longer resolve.
        """
        stats = {"added": 0, "updated": 0, "unchanged": 0, "kept_shrunk": 0,
                 "not_found": 0}
        keys = list(keys)
        total = len(keys)
        for i, (source, sid) in enumerate(keys):
            outcome = self.sync_one(store, source, sid)
            # Count every outcome, `not_found` included: dropping it made the
            # reported total silently smaller than the requested batch, with
            # nothing explaining the gap.
            stats[outcome] = stats.get(outcome, 0) + 1
            if progress:
                progress(i + 1, total)
        return stats

    # -- integrity ----------------------------------------------------------

    def verify(self, *, deep: bool = True, progress=None) -> dict[str, list[str]]:
        """Check every manifest row against its on-disk JSON.

        Returns ``{"ok": [...], "missing": [...], "unreadable": [...]}`` where
        each list holds ``"<source>:<id>"`` labels. ``missing`` = manifest row
        with no file; ``unreadable`` = file present but it fails to parse back
        via ``archivefmt.from_archive_json``.

        `deep=True` (the default) parses every file, which is the only way to
        detect corruption -- but it reads the entire vault, so on a multi-
        gigabyte archive it takes tens of seconds and belongs in a background
        job, never on a page load. `deep=False` only stats each file, catching
        the common failure (a missing or truncated-to-empty file) in
        milliseconds; nothing is reported as ``unreadable`` in that mode.

        `progress(done, total)` is called per row if provided.
        """
        result: dict[str, list[str]] = {"ok": [], "missing": [], "unreadable": []}
        if not self.exists():
            return result
        with self._connect(write=False) as conn:
            rows = list(conn.execute(
                "SELECT source, session_id, file_path FROM archived"
            ))
        total = len(rows)
        for i, r in enumerate(rows):
            label = f"{r['source']}:{r['session_id']}"
            path = self.safe_path(r["file_path"])
            try:
                ok_stat = path is not None and path.is_file() and path.stat().st_size > 0
            except OSError:
                ok_stat = False
            if not ok_stat:
                result["missing"].append(label)
            elif not deep:
                result["ok"].append(label)
            else:
                try:
                    archivefmt.from_archive_json(path.read_text(encoding="utf-8"))
                except (OSError, ValueError, KeyError):
                    result["unreadable"].append(label)
                else:
                    result["ok"].append(label)
            if progress:
                progress(i + 1, total)
        return result

    # -- export / backup ----------------------------------------------------

    def export_to(self, dest, *, fmt: str = "vault", doc_format: str = "markdown") -> dict:
        """Export the vault to `dest` for backup or moving between machines.

        Two formats:

        * ``fmt="vault"`` (default) -- a **faithful, re-importable copy** of the
          whole vault (``manifest.sqlite`` + every ``sessions/.../*.json``). If
          `dest` ends in ``.zip`` a zip archive is written; otherwise a
          directory. Point ``$SCROLLBACK_ARCHIVE`` (or ``--dest``) at the copy
          to use it directly as a vault. This is the backup/sync format.
        * ``fmt="rendered"`` -- render each archived session to a human-readable
          document (``doc_format``: markdown / html / json / text) at
          ``<dest>/<source>/<id>.<ext>``. Browsable/shareable, but NOT a vault
          (lossy; no manifest) -- a viewing export, not a backup.

        Returns ``{"format", "dest", "sessions"}`` (count exported).
        """
        dest = Path(dest).expanduser()
        if not self.exists():
            return {"format": fmt, "dest": str(dest), "sessions": 0}

        if fmt == "vault":
            return self._export_vault(dest)
        if fmt == "rendered":
            return self._export_rendered(dest, doc_format)
        raise ValueError(f"unknown export format: {fmt!r} (use 'vault' or 'rendered')")

    def checkpoint(self) -> None:
        """Fold any write-ahead log back into the manifest file.

        The manifest runs in WAL mode, so recent commits can live in a
        ``-wal`` sidecar rather than in ``manifest.sqlite`` itself. Anything
        that copies the vault (export, backup, moving machines) must run this
        first, or the copy can silently lose the newest rows -- or, if the
        sidecars are copied too, be read against a mismatched WAL.
        """
        if not self.exists():
            return
        with contextlib.suppress(sqlite3.Error, OSError):
            conn = sqlite3.connect(self.manifest_path, timeout=30.0)
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                conn.close()

    #: SQLite sidecars that must never be copied into an exported vault: they
    #: are only meaningful next to the exact database that produced them.
    _SIDECARS = ("-wal", "-shm", "-journal")

    def _is_sidecar(self, p: Path) -> bool:
        return any(p.name.endswith(sfx) for sfx in self._SIDECARS)

    def _export_vault(self, dest: Path) -> dict:
        import shutil

        n = self.stats().get("sessions", 0)
        # Fold the WAL in first, then exclude the sidecars, so the exported
        # manifest is self-contained.
        self.checkpoint()
        if str(dest).endswith(".zip"):
            # Zip the vault directory; the archive root mirrors the vault layout.
            import zipfile

            # Refuse to clobber, matching the directory path below. Silently
            # overwriting is especially bad here: the target is by definition
            # someone's backup.
            if dest.exists():
                raise FileExistsError(f"destination already exists: {dest}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
                for p in sorted(self.path.rglob("*")):
                    if p.is_file() and not self._is_sidecar(p):
                        zf.write(p, arcname=str(p.relative_to(self.path)))
        else:
            if dest.exists():
                raise FileExistsError(f"destination already exists: {dest}")
            shutil.copytree(
                self.path, dest,
                ignore=lambda _d, names: [n for n in names
                                          if self._is_sidecar(Path(n))],
            )
        return {"format": "vault", "dest": str(dest), "sessions": n}

    # -- import / merge (cross-machine sync) --------------------------------

    def import_from(self, other, *, progress=None) -> dict[str, int]:
        """Merge another vault into this one (cross-machine sync).

        `other` is another vault -- a directory or a ``.zip`` produced by
        :meth:`export_to`. Its sessions are folded into this vault by the SAME
        rules as :meth:`sync`: keyed on ``(source, id)``, unchanged sessions are
        skipped, and the never-shrink guard (§7.3) means the larger/newer copy
        wins so a merge can never lose messages. Run it in either direction to
        converge two machines.

        Returns the same counts as :meth:`sync`
        (``added / updated / unchanged / kept_orphan / kept_shrunk``).
        ``kept_orphan`` here is always 0 -- import never prunes.
        """
        from .sources.archive import ArchiveSource
        from .store import Store

        other = Path(other).expanduser()
        with self._maybe_unzip(other) as other_dir:
            src = ArchiveSource(other_dir)
            if not src.is_available():
                raise FileNotFoundError(f"no vault found at {other}")
            # A merge is just a sync whose "live" source is the other vault.
            return self.sync(Store([src]), progress=progress)

    @staticmethod
    def _maybe_unzip(path: Path):
        """Context manager yielding a vault directory. If `path` is a .zip, it is
        extracted to a temp dir (cleaned up on exit); otherwise yielded as-is."""
        import contextlib
        import tempfile

        @contextlib.contextmanager
        def _cm():
            if path.is_file() and str(path).endswith(".zip"):
                tmp = tempfile.mkdtemp(prefix="scrollback-import-")
                try:
                    _safe_extract_zip(path, Path(tmp))
                    yield Path(tmp)
                finally:
                    import shutil
                    shutil.rmtree(tmp, ignore_errors=True)
            else:
                yield path

        return _cm()

    def _export_rendered(self, dest: Path, doc_format: str) -> dict:
        from . import export as _export

        ext = {"markdown": "md", "md": "md", "html": "html",
               "json": "json", "text": "txt", "txt": "txt"}.get(doc_format, "txt")
        dest.mkdir(parents=True, exist_ok=True)
        count = 0
        with self._connect(write=False) as conn:
            rows = list(conn.execute(
                "SELECT source, session_id, file_path FROM archived"
            ))
        for r in rows:
            path = self.safe_path(r["file_path"])
            if path is None or not path.is_file():
                continue
            try:
                sess = archivefmt.from_archive_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, KeyError):
                continue
            body = _export.render(sess, doc_format)
            out = dest / _safe_component(r["source"]) / f"{_safe_component(r['session_id'])}.{ext}"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(body, encoding="utf-8")
            count += 1
        return {"format": "rendered", "dest": str(dest), "sessions": count}
