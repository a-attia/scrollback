"""Read-back adapter over the durable archive vault.

`ArchiveSource` exposes the sessions stored in a vault (by
:class:`scrollback.archive.ArchiveStore`) through the normal `Source`
interface, so browse / search / export / stats work over archived sessions --
**including ones the agent has already deleted**.

Two properties make dedup and provenance work (see `docs/archive-plan.md`
Component 3):

* **The original ``(source, id)`` is preserved.** A session archived from
  opencode still reports ``source="opencode"``. `ArchiveSource` is a *reader*
  of the vault, not a new ``source="archive"`` namespace, so a live + archived
  copy of the same session share a key and dedup to one (live wins).
* **Registration is by injection, not the global registry.** `ArchiveSource`
  needs a vault path and must be inactive when no vault exists, so it is NOT in
  ``ALL_SOURCES``; `Store.with_archive(path)` injects it after the live
  sources (so first-match precedence favours live).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from .. import archivefmt
from ..models import Session
from .base import Source


def _like_prefix(selector: str) -> str:
    """Build a LIKE pattern matching `selector` as a literal prefix.

    Session ids can legitimately contain `_`, which is a LIKE wildcard, so the
    selector is escaped before the trailing `%` is appended.
    """
    escaped = selector.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "%"


class ArchiveSource(Source):
    """Read-only adapter over a vault directory.

    Unlike other adapters, ``name`` is descriptive only: the sessions this
    adapter yields keep their ORIGINAL ``source`` (e.g. ``"opencode"``), which
    is what dedup and provenance rely on.
    """

    name = "archive"
    label = "Archive"

    def __init__(self, vault_path: Path | str) -> None:
        from ..archive import ArchiveStore

        self._store = ArchiveStore(Path(vault_path))
        # Most-recently-parsed session: ((path, mtime_ns, size), Session).
        self._cache: tuple[tuple[str, int, int], Session] | None = None

    # -- discovery ----------------------------------------------------------

    def is_available(self) -> bool:
        return self._store.exists()

    def location(self) -> Path | None:
        return self._store.path

    def _manifest_rows(self) -> list[tuple[str, str, str]]:
        """Return (source, session_id, file_path) for every archived session."""
        if not self._store.exists():
            return []
        with self._store._connect(write=False) as conn:
            return [
                (r["source"], r["session_id"], r["file_path"])
                for r in conn.execute(
                    "SELECT source, session_id, file_path FROM archived"
                )
            ]

    def _manifest_meta_rows(self):
        """Return (source, session_id, file_path, meta_json) for the whole vault.

        `meta_json` is the compact metadata summary written at archive time, so
        listing does NOT need to open and parse every session file."""
        if not self._store.exists():
            return []
        with self._store._connect(write=False) as conn:
            return [
                (r["source"], r["session_id"], r["file_path"], r["meta_json"])
                for r in conn.execute(
                    "SELECT source, session_id, file_path, meta_json FROM archived"
                )
            ]

    def _row_for(self, session_id: str) -> tuple[str, str, str] | None:
        """Look up one manifest row by session id, or None.

        Queried directly rather than scanning every row: the manifest is keyed
        on ``(source, id)`` and a vault holds thousands of sessions. Ids are
        unique within a source, so a bare id may match rows from more than one
        source; the first is returned, mirroring the base-class resolver.
        """
        if not self._store.exists():
            return None
        with self._store._connect(write=False) as conn:
            r = conn.execute(
                "SELECT source, session_id, file_path FROM archived "
                "WHERE session_id = ? LIMIT 1",
                (session_id,),
            ).fetchone()
        return (r["source"], r["session_id"], r["file_path"]) if r else None

    def _read_file(self, rel_path: str) -> Session | None:
        """Parse one archived session file, with a one-entry cache.

        Archived sessions are single JSON documents, so any read costs a full
        parse -- and the web UI pages through a transcript, calling this once
        per page. Caching the most recent parse turns K pages over one session
        from K full parses into one (a 583 MB session took ~3 s *per page*
        without it). One entry is enough: reads are overwhelmingly repeated
        against the session currently open. The cache is keyed on the file's
        mtime+size, so an updated archive copy is never served stale.
        """
        path = self._store.safe_path(rel_path)   # rejects traversal outside the vault
        if path is None:
            return None
        try:
            st = path.stat()
            sig = (str(path), st.st_mtime_ns, st.st_size)
        except OSError:
            return None
        if self._cache is not None and self._cache[0] == sig:
            return self._cache[1]
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            sess = archivefmt.from_archive_json(text)
        except (ValueError, KeyError):
            return None
        self._cache = (sig, sess)
        return sess

    # -- Source contract ----------------------------------------------------

    def list_sessions(self) -> Iterator[Session]:
        """Yield metadata-only sessions for the whole vault.

        Reads metadata from the manifest's ``meta_json`` (fast; no per-file
        parsing). Each session keeps its original ``source`` and carries
        ``raw["archived"] = True``. Rows written before ``meta_json`` existed
        fall back to parsing the file (and will get a meta_json on next sync).
        """
        import json as _json
        from dataclasses import replace

        for _source, _sid, rel, meta_json in self._manifest_meta_rows():
            if meta_json:
                try:
                    d = _json.loads(meta_json)
                except ValueError:
                    d = None
                if d is not None:
                    yield self._session_from_summary(d)
                    continue
            # Fallback for pre-meta_json rows: parse the file (slow path).
            sess = self._read_file(rel)
            if sess is None:
                continue
            raw = {**(sess.raw or {}), "archived": True}
            yield replace(sess, messages=(), raw=raw)

    @staticmethod
    def _session_from_summary(d: dict) -> Session:
        """Build a metadata-only Session from a stored summary dict."""
        from ..models import _to_dt

        return Session(
            id=d["id"], source=d["source"], title=d.get("title", ""),
            directory=d.get("directory"),
            created=_to_dt(d.get("created")), updated=_to_dt(d.get("updated")),
            model=d.get("model"), agent=d.get("agent"),
            parent_id=d.get("parent_id"), message_count=d.get("message_count"),
            cost=d.get("cost"),
            tokens_input=d.get("tokens_input"), tokens_output=d.get("tokens_output"),
            tokens_cache_read=d.get("tokens_cache_read"),
            tokens_cache_write=d.get("tokens_cache_write"),
            tokens_reasoning=d.get("tokens_reasoning"),
            raw={"archived": True,
                 **({"git_branch": d["git_branch"]} if d.get("git_branch") else {})},
        )

    def load_session(self, session_id: str) -> Session | None:
        """Load one archived session fully by its (original) id.

        Ids are unique within a source; the manifest keys on
        ``(source, id)``. When only the bare id is given we return the first
        match, mirroring the base-class resolver.
        """
        from dataclasses import replace

        row = self._row_for(session_id)
        if row is None:
            return None
        sess = self._read_file(row[2])
        if sess is None:
            return None
        return replace(sess, raw={**(sess.raw or {}), "archived": True})

    def load_session_meta(self, session_id: str) -> Session | None:
        """Metadata only, read from the manifest's stored summary.

        Overridden so a header render costs one indexed SQLite lookup instead
        of the base class's parse-everything-then-discard-messages -- which on
        a large archived session meant parsing hundreds of megabytes to show a
        title. Falls back to the file for rows predating `meta_json`.
        """
        import json as _json
        from dataclasses import replace

        if not self._store.exists():
            return None
        with self._store._connect(write=False) as conn:
            r = conn.execute(
                "SELECT file_path, meta_json FROM archived "
                "WHERE session_id = ? LIMIT 1",
                (session_id,),
            ).fetchone()
        if r is None:
            return None
        if r["meta_json"]:
            try:
                return self._session_from_summary(_json.loads(r["meta_json"]))
            except (ValueError, KeyError):
                pass
        sess = self._read_file(r["file_path"])
        if sess is None:
            return None
        return replace(sess, messages=(), raw={**(sess.raw or {}), "archived": True})

    def load_messages(self, session_id: str, *, offset: int = 0, limit=None):
        """A window of one archived session's messages.

        An archive file is a single JSON document, so unlike the JSONL adapters
        there is no way to seek to the Nth message -- the whole file must be
        parsed. The override still matters: it goes through `_read_file`'s
        cache, so paging through a transcript parses once rather than once per
        page.
        """
        sess = self.load_session(session_id)
        if sess is None:
            return []
        msgs = list(sess.messages)
        if offset:
            msgs = msgs[offset:]
        if limit is not None:
            msgs = msgs[:limit]
        return msgs

    def resolve_session_id(self, selector: str) -> str | None:
        """Resolve an id / unique prefix / 'latest' against the manifest.

        Overridden to query SQLite instead of the base class's full
        `list_sessions()` scan, which would materialise every archived session
        just to match one id.
        """
        selector = selector.strip()
        if not self._store.exists():
            return None
        with self._store._connect(write=False) as conn:
            if selector == "latest":
                r = conn.execute(
                    "SELECT session_id FROM archived "
                    "WHERE updated IS NOT NULL ORDER BY updated DESC LIMIT 1"
                ).fetchone()
                return r["session_id"] if r else None
            r = conn.execute(
                "SELECT session_id FROM archived WHERE session_id = ? LIMIT 1",
                (selector,),
            ).fetchone()
            if r is not None:
                return r["session_id"]
            # Unique-prefix match. Fetch two: more than one hit is ambiguous.
            rows = conn.execute(
                "SELECT DISTINCT session_id FROM archived "
                "WHERE session_id LIKE ? ESCAPE '\\' LIMIT 2",
                (_like_prefix(selector),),
            ).fetchall()
        return rows[0]["session_id"] if len(rows) == 1 else None
