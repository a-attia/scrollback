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


def _summary_json(session) -> str:
    """A compact JSON metadata summary stored in the manifest, so the archive
    can be *listed* without parsing every full session file. Mirrors the
    lightweight list shape (`serialize.session_summary`) minus children/
    messages (a listing never needs those)."""
    import json as _json

    from .serialize import session_summary

    d = session_summary(session)
    d.pop("children", None)
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
    PRIMARY KEY (source, session_id)
);
"""


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
            conn.executescript(_SCHEMA)
            # Migrate older manifests that predate the meta_json column (added so
            # listing reads metadata from SQLite instead of parsing every JSON).
            cols = {r[1] for r in conn.execute("PRAGMA table_info(archived)")}
            if "meta_json" not in cols:
                conn.execute("ALTER TABLE archived ADD COLUMN meta_json TEXT")
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
        """
        stats = {"added": 0, "updated": 0, "unchanged": 0,
                 "kept_orphan": 0, "kept_shrunk": 0}
        now = datetime.now(timezone.utc).isoformat()

        with self._connect(write=True) as conn:
            have = {
                (r["source"], r["session_id"]): r
                for r in conn.execute(
                    "SELECT source, session_id, updated, message_count, "
                    "first_archived, file_path FROM archived"
                )
            }
            live = store.list_sessions(fold_subagents=False)
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

            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_sync', ?)",
                (now,),
            )
            conn.commit()
        return stats

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
                "first_archived, file_path FROM archived "
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
            new_count = sess.message_count
            if new_count is None:
                new_count = len(sess.messages)
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
        conn.execute(
            "INSERT OR REPLACE INTO archived (source, session_id, updated, "
            "message_count, first_archived, last_synced, last_seen_live, file_path, "
            "meta_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                meta.source, meta.id,
                sess.updated.isoformat() if sess.updated else None,
                sess.message_count,
                first_archived, now, now,
                str(dest.relative_to(self.path)),
                _summary_json(sess),
            ),
        )
        return "added" if prev is None else "updated"

    # -- stats --------------------------------------------------------------

    def stats(self) -> dict[str, int]:
        """Summary counts for the vault (per-source + orphan count).

        ``orphans`` counts archived sessions whose ``last_seen_live`` is older
        than the most recent sync -- i.e. they exist in the vault but were not
        seen live at the last sync (deleted from their agent).
        """
        if not self.exists():
            return {"sessions": 0, "orphans": 0, "per_source": {}}
        with self._connect(write=False) as conn:
            total = conn.execute("SELECT COUNT(*) FROM archived").fetchone()[0]
            last_sync_row = conn.execute(
                "SELECT value FROM meta WHERE key = 'last_sync'"
            ).fetchone()
            last_sync = last_sync_row[0] if last_sync_row else None
            orphans = 0
            if last_sync is not None:
                orphans = conn.execute(
                    "SELECT COUNT(*) FROM archived "
                    "WHERE last_seen_live IS NULL OR last_seen_live < ?",
                    (last_sync,),
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
                "SELECT source, session_id, file_path FROM archived "
                "WHERE meta_json IS NULL"
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
                conn.execute(
                    "UPDATE archived SET meta_json = ? WHERE source = ? AND session_id = ?",
                    (_summary_json(sess), r["source"], r["session_id"]),
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
        iterable of `(source, session_id)` tuples.
        """
        stats = {"added": 0, "updated": 0, "unchanged": 0, "kept_shrunk": 0}
        keys = list(keys)
        total = len(keys)
        for i, (source, sid) in enumerate(keys):
            outcome = self.sync_one(store, source, sid)
            if outcome in stats:
                stats[outcome] += 1
            if progress:
                progress(i + 1, total)
        return stats

    # -- integrity ----------------------------------------------------------

    def verify(self) -> dict[str, list[str]]:
        """Check every manifest row against its on-disk JSON.

        Returns ``{"ok": [...], "missing": [...], "unreadable": [...]}`` where
        each list holds ``"<source>:<id>"`` labels. ``missing`` = manifest row
        with no file; ``unreadable`` = file present but it fails to parse back
        via ``archivefmt.from_archive_json``.
        """
        result: dict[str, list[str]] = {"ok": [], "missing": [], "unreadable": []}
        if not self.exists():
            return result
        with self._connect(write=False) as conn:
            rows = list(conn.execute(
                "SELECT source, session_id, file_path FROM archived"
            ))
        for r in rows:
            label = f"{r['source']}:{r['session_id']}"
            path = self.safe_path(r["file_path"])
            if path is None or not path.is_file():
                result["missing"].append(label)
                continue
            try:
                archivefmt.from_archive_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, KeyError):
                result["unreadable"].append(label)
                continue
            result["ok"].append(label)
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

    def _export_vault(self, dest: Path) -> dict:
        import shutil

        n = self.stats().get("sessions", 0)
        if str(dest).endswith(".zip"):
            # Zip the vault directory; the archive root mirrors the vault layout.
            base = dest.with_suffix("")  # shutil adds .zip
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.make_archive(str(base), "zip", root_dir=str(self.path))
        else:
            if dest.exists():
                raise FileExistsError(f"destination already exists: {dest}")
            shutil.copytree(self.path, dest)
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
