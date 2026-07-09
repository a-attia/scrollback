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

    def _read_file(self, rel_path: str) -> Session | None:
        path = self._store.safe_path(rel_path)   # rejects traversal outside the vault
        if path is None:
            return None
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            return archivefmt.from_archive_json(text)
        except (ValueError, KeyError):
            return None

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

        for _source, sid, rel in self._manifest_rows():
            if sid == session_id:
                sess = self._read_file(rel)
                if sess is None:
                    return None
                raw = {**(sess.raw or {}), "archived": True}
                return replace(sess, raw=raw)
        return None
