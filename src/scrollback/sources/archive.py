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

    def _read_file(self, rel_path: str) -> Session | None:
        path = self._store.path / rel_path
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
        """Yield metadata-only sessions (messages stripped) for the whole vault.

        Each session keeps its original ``source`` and carries
        ``raw["archived"] = True`` so the read-back path is distinguishable.
        """
        from dataclasses import replace

        for _source, _sid, rel in self._manifest_rows():
            sess = self._read_file(rel)
            if sess is None:
                continue
            raw = {**(sess.raw or {}), "archived": True}
            yield replace(sess, messages=(), raw=raw)

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
