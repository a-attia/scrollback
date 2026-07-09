"""Tests for the durable archive vault (ArchiveStore.sync + read-back format).

Uses a mutable in-memory FakeSource so a session can be changed, shrunk, or
removed between syncs to exercise the incremental / inverted-prune /
never-shrink behaviours. All I/O is under tmp_path; no real user data.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from scrollback import archive, archivefmt
from scrollback.models import Message, Part, Session
from scrollback.sources.base import Source
from scrollback.store import Store

_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _session(sid, source="fake", *, n_msgs=1, updated=_T0):
    msgs = tuple(
        Message(
            id=f"{sid}-m{i}",
            role="assistant",
            created=updated,
            parts=(Part(id=f"{sid}-m{i}-p0", type="text", text=f"msg {i}",
                        raw={"type": "text"}),),
            raw={"seq": i},
        )
        for i in range(n_msgs)
    )
    return Session(id=sid, source=source, title=f"Session {sid}",
                   directory="/tmp/proj", created=_T0, updated=updated,
                   message_count=n_msgs, messages=msgs)


class FakeSource(Source):
    name = "fake"
    label = "Fake"

    def __init__(self, sessions):
        self._sessions = {s.id: s for s in sessions}

    def is_available(self):
        return True

    def location(self):
        return Path("/tmp/fake")

    def list_sessions(self):
        # yield metadata-only copies (messages stripped), like real adapters
        from dataclasses import replace
        return iter([replace(s, messages=()) for s in self._sessions.values()])

    def load_session(self, session_id):
        return self._sessions.get(session_id)


def _store(*sessions):
    return Store([FakeSource(list(sessions))])


# -- first sync + incrementality --------------------------------------------


def test_first_sync_archives_all(tmp_path):
    store = _store(_session("s1"), _session("s2"))
    vault = archive.ArchiveStore(tmp_path / "vault")
    r = vault.sync(store)
    assert r["added"] == 2
    assert r["updated"] == 0 and r["unchanged"] == 0 and r["kept_orphan"] == 0
    assert vault.exists()
    # One JSON file per session, lossless (round-trips back to an equal Session).
    f = vault._session_file("fake", "s1")
    assert f.is_file()
    restored = archivefmt.from_archive_json(f.read_text(encoding="utf-8"))
    assert restored == store.load_session("s1", source="fake")


def test_second_sync_only_rearchives_changed(tmp_path):
    src = FakeSource([_session("s1"), _session("s2")])
    store = Store([src])
    vault = archive.ArchiveStore(tmp_path / "vault")
    vault.sync(store)

    # Change only s2 (new updated + more messages).
    src._sessions["s2"] = _session("s2", n_msgs=3, updated=_T0 + timedelta(hours=1))
    r = vault.sync(store)
    assert r["updated"] == 1
    assert r["unchanged"] == 1
    assert r["added"] == 0


# -- inverted prune (durability guarantee) ----------------------------------


def test_removed_session_is_kept_as_orphan(tmp_path):
    src = FakeSource([_session("s1"), _session("s2")])
    store = Store([src])
    vault = archive.ArchiveStore(tmp_path / "vault")
    vault.sync(store)

    # Delete s2 from the live source (simulating the agent's auto-cleanup).
    del src._sessions["s2"]
    r = vault.sync(store)
    assert r["kept_orphan"] == 1
    # The archived copy survives on disk.
    assert vault._session_file("fake", "s2").is_file()
    # And it is reported as no-longer-live in stats.
    assert vault.stats()["orphans"] == 1


# -- never-shrink guard (§7.3) ----------------------------------------------


def test_never_shrink_guard_skips_smaller_reread(tmp_path):
    src = FakeSource([_session("s1", n_msgs=5, updated=_T0)])
    store = Store([src])
    vault = archive.ArchiveStore(tmp_path / "vault")
    vault.sync(store)

    # A degraded re-read: same id, NEWER updated (so signature differs and it is
    # re-processed), but FEWER messages -> must be skipped, not clobbered.
    src._sessions["s1"] = _session("s1", n_msgs=2, updated=_T0 + timedelta(hours=1))
    r = vault.sync(store)
    assert r["kept_shrunk"] == 1
    assert r["updated"] == 0

    # The good 5-message copy is still on disk.
    restored = archivefmt.from_archive_json(
        vault._session_file("fake", "s1").read_text(encoding="utf-8")
    )
    assert len(restored.messages) == 5


def test_growth_is_allowed(tmp_path):
    src = FakeSource([_session("s1", n_msgs=2, updated=_T0)])
    store = Store([src])
    vault = archive.ArchiveStore(tmp_path / "vault")
    vault.sync(store)

    src._sessions["s1"] = _session("s1", n_msgs=6, updated=_T0 + timedelta(hours=1))
    r = vault.sync(store)
    assert r["updated"] == 1 and r["kept_shrunk"] == 0
    restored = archivefmt.from_archive_json(
        vault._session_file("fake", "s1").read_text(encoding="utf-8")
    )
    assert len(restored.messages) == 6


# -- path sanitization ------------------------------------------------------


def test_unsafe_ids_are_sanitized(tmp_path):
    # Claude Code subagent ids contain "::".
    class ClaudeLike(FakeSource):
        name = "claudecode"

    store = Store([ClaudeLike([_session("parent::sub", source="claudecode")])])
    vault = archive.ArchiveStore(tmp_path / "vault")
    vault.sync(store)
    f = vault._session_file("claudecode", "parent::sub")
    assert f.is_file()
    assert "::" not in f.name  # sanitized on disk
    # ...but the original id survives inside the JSON (provenance preserved).
    restored = archivefmt.from_archive_json(f.read_text(encoding="utf-8"))
    assert restored.id == "parent::sub"


# -- read-only invariant ----------------------------------------------------


def test_sync_writes_only_to_vault(tmp_path):
    """A sync must not create anything outside the vault directory."""
    store = _store(_session("s1"))
    vault_dir = tmp_path / "vault"
    vault = archive.ArchiveStore(vault_dir)
    vault.sync(store)
    # Everything created lives under the vault; tmp_path has no other children.
    assert [p.name for p in tmp_path.iterdir()] == ["vault"]


# -- path resolution --------------------------------------------------------


def test_sync_one_single_session(tmp_path):
    src = FakeSource([_session("s1", n_msgs=2), _session("s2")])
    store = Store([src])
    vault = archive.ArchiveStore(tmp_path / "vault")

    # Archive just s1; s2 is untouched.
    assert vault.sync_one(store, "fake", "s1") == "added"
    assert vault._session_file("fake", "s1").is_file()
    assert not vault._session_file("fake", "s2").is_file()

    # Re-archiving unchanged s1 is a no-op.
    assert vault.sync_one(store, "fake", "s1") == "unchanged"

    # Growing s1 then re-archiving updates it.
    src._sessions["s1"] = _session("s1", n_msgs=5, updated=_T0 + timedelta(hours=1))
    assert vault.sync_one(store, "fake", "s1") == "updated"

    # Unknown session.
    assert vault.sync_one(store, "fake", "nope") == "not_found"


def test_sync_one_never_shrink(tmp_path):
    src = FakeSource([_session("s1", n_msgs=5)])
    store = Store([src])
    vault = archive.ArchiveStore(tmp_path / "vault")
    vault.sync_one(store, "fake", "s1")
    # Degraded re-read: newer updated, fewer messages -> kept_shrunk (skipped).
    src._sessions["s1"] = _session("s1", n_msgs=2, updated=_T0 + timedelta(hours=1))
    assert vault.sync_one(store, "fake", "s1") == "kept_shrunk"
    restored = archivefmt.from_archive_json(
        vault._session_file("fake", "s1").read_text(encoding="utf-8"))
    assert len(restored.messages) == 5


def test_verify_reports_ok_missing_and_unreadable(tmp_path):
    vp = _vault_with(tmp_path, _session("s1"), _session("s2"))
    vault = archive.ArchiveStore(vp)

    # All good initially.
    v = vault.verify()
    assert sorted(v["ok"]) == ["fake:s1", "fake:s2"]
    assert v["missing"] == [] and v["unreadable"] == []

    # Delete one file -> missing; corrupt the other -> unreadable.
    vault._session_file("fake", "s1").unlink()
    vault._session_file("fake", "s2").write_text("{ not json", encoding="utf-8")
    v = vault.verify()
    assert v["ok"] == []
    assert v["missing"] == ["fake:s1"]
    assert v["unreadable"] == ["fake:s2"]


def test_export_vault_copy_is_reimportable(tmp_path):
    """A 'vault' export is a faithful copy that works as a vault itself."""
    vp = _vault_with(tmp_path, _session("s1", n_msgs=2), _session("s2"))
    src_vault = archive.ArchiveStore(vp)

    r = src_vault.export_to(tmp_path / "backup", fmt="vault")
    assert r["format"] == "vault" and r["sessions"] == 2

    # The copy re-imports as a usable vault with the same contents.
    copy = archive.ArchiveStore(tmp_path / "backup")
    assert copy.exists()
    assert copy.stats()["sessions"] == 2
    restored = archivefmt.from_archive_json(
        copy._session_file("fake", "s1").read_text(encoding="utf-8"))
    assert len(restored.messages) == 2


def test_export_vault_zip(tmp_path):
    vp = _vault_with(tmp_path, _session("s1"))
    r = archive.ArchiveStore(vp).export_to(tmp_path / "bundle.zip", fmt="vault")
    assert (tmp_path / "bundle.zip").is_file()
    assert r["sessions"] == 1


def test_export_vault_refuses_existing_dir(tmp_path):
    vp = _vault_with(tmp_path, _session("s1"))
    (tmp_path / "backup").mkdir()
    import pytest
    with pytest.raises(FileExistsError):
        archive.ArchiveStore(vp).export_to(tmp_path / "backup", fmt="vault")


def test_export_rendered_writes_readable_files(tmp_path):
    vp = _vault_with(tmp_path, _session("s1"), _session("s2"))
    r = archive.ArchiveStore(vp).export_to(tmp_path / "readable",
                                           fmt="rendered", doc_format="markdown")
    assert r["format"] == "rendered" and r["sessions"] == 2
    assert (tmp_path / "readable" / "fake" / "s1.md").is_file()
    assert (tmp_path / "readable" / "fake" / "s2.md").is_file()


def test_export_unknown_format_raises(tmp_path):
    vp = _vault_with(tmp_path, _session("s1"))
    import pytest
    with pytest.raises(ValueError):
        archive.ArchiveStore(vp).export_to(tmp_path / "x", fmt="bogus")


def test_export_no_vault_is_noop(tmp_path):
    r = archive.ArchiveStore(tmp_path / "nope").export_to(tmp_path / "out", fmt="vault")
    assert r["sessions"] == 0


# -- import / merge (cross-machine sync) ------------------------------------


def test_import_merges_disjoint_and_overlapping(tmp_path):
    a = archive.ArchiveStore(tmp_path / "A")
    a.sync(_store(_session("s1", n_msgs=2), _session("s2")))
    b = archive.ArchiveStore(tmp_path / "B")
    b.sync(_store(
        _session("s1", n_msgs=3, updated=_T0 + timedelta(hours=1)),  # grown + newer
        _session("s2"),                                              # identical
        _session("s3"),                                             # only on B
    ))

    r = a.import_from(tmp_path / "B")
    assert r["added"] == 1        # s3
    assert r["updated"] == 1      # s1 (newer copy from B)
    assert r["unchanged"] == 1    # s2
    assert a.stats()["sessions"] == 3
    # s1 grew to B's 3 messages (newer/larger won).
    s1 = archivefmt.from_archive_json(
        a._session_file("fake", "s1").read_text(encoding="utf-8"))
    assert len(s1.messages) == 3


def test_import_never_shrinks(tmp_path):
    a = archive.ArchiveStore(tmp_path / "A")
    a.sync(_store(_session("s1", n_msgs=5)))          # A has the big copy
    b = archive.ArchiveStore(tmp_path / "B")
    b.sync(_store(_session("s1", n_msgs=2, updated=_T0 + timedelta(hours=1))))

    r = a.import_from(tmp_path / "B")
    assert r["kept_shrunk"] == 1 and r["updated"] == 0
    s1 = archivefmt.from_archive_json(
        a._session_file("fake", "s1").read_text(encoding="utf-8"))
    assert len(s1.messages) == 5                      # protected


def test_import_from_zip(tmp_path):
    b = archive.ArchiveStore(tmp_path / "B")
    b.sync(_store(_session("s1"), _session("s2")))
    b.export_to(tmp_path / "b.zip", fmt="vault")

    a = archive.ArchiveStore(tmp_path / "A")
    a.sync(_store(_session("s9")))
    r = a.import_from(tmp_path / "b.zip")
    assert r["added"] == 2
    assert a.stats()["sessions"] == 3               # s9 + s1 + s2


def test_import_missing_vault_raises(tmp_path):
    a = archive.ArchiveStore(tmp_path / "A")
    a.sync(_store(_session("s1")))
    import pytest
    with pytest.raises(FileNotFoundError):
        a.import_from(tmp_path / "does-not-exist")


def test_import_is_symmetric_convergence(tmp_path):
    """Merging both directions converges both vaults to the same union."""
    a = archive.ArchiveStore(tmp_path / "A")
    a.sync(_store(_session("s1"), _session("s2")))
    b = archive.ArchiveStore(tmp_path / "B")
    b.sync(_store(_session("s2"), _session("s3")))

    a.import_from(tmp_path / "B")
    b.import_from(tmp_path / "A")
    assert a.stats()["sessions"] == 3
    assert b.stats()["sessions"] == 3


def test_default_archive_path_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("SCROLLBACK_ARCHIVE", str(tmp_path / "custom"))
    assert archive.default_archive_path() == tmp_path / "custom"


def test_default_archive_path_default(monkeypatch):
    monkeypatch.delenv("SCROLLBACK_ARCHIVE", raising=False)
    assert archive.default_archive_path() == Path.home() / ".scrollback" / "archive"


# -- Phase 3: read-back via ArchiveSource -----------------------------------


def _vault_with(tmp_path, *sessions):
    """Sync `sessions` into a fresh vault and return its path."""
    store = _store(*sessions)
    vault = archive.ArchiveStore(tmp_path / "vault")
    vault.sync(store)
    return tmp_path / "vault"


def test_archive_source_reads_back_sessions(tmp_path):
    from scrollback.sources.archive import ArchiveSource

    vp = _vault_with(tmp_path, _session("s1", n_msgs=2), _session("s2"))
    arc = ArchiveSource(vp)
    assert arc.is_available()

    listed = list(arc.list_sessions())
    assert {s.id for s in listed} == {"s1", "s2"}
    assert all(s.source == "fake" for s in listed)  # original source preserved
    assert all(s.raw.get("archived") for s in listed)

    full = arc.load_session("s1")
    assert full is not None
    assert len(full.messages) == 2


def test_deleted_session_still_readable_via_archive(tmp_path):
    """A session removed from its live source is still browsable from the vault."""
    src = FakeSource([_session("s1"), _session("gone")])
    live = Store([src])
    vault = archive.ArchiveStore(tmp_path / "vault")
    vault.sync(live)

    del src._sessions["gone"]  # agent auto-deletes it

    merged = live.with_archive(tmp_path / "vault")
    ids = {s.id for s in merged.list_sessions()}
    assert "gone" in ids  # survives via the archive
    # ...and it is loadable in full.
    restored = merged.load_session("gone", source="fake")
    assert restored is not None and restored.id == "gone"


def test_with_archive_noop_when_no_vault(tmp_path):
    live = _store(_session("s1"))
    same = live.with_archive(tmp_path / "does-not-exist")
    assert same is live


# -- Phase 3: dedup (live wins) + no double-count ---------------------------


def test_dedup_live_wins_over_archive(tmp_path):
    # Archive an OLD copy (1 msg), then present a NEWER live copy (3 msgs).
    _vault_with(tmp_path, _session("s1", n_msgs=1, updated=_T0))
    live = Store([FakeSource([
        _session("s1", n_msgs=3, updated=_T0 + timedelta(hours=1)),
    ])])
    merged = live.with_archive(tmp_path / "vault")

    listed = merged.list_sessions()
    s1 = [s for s in listed if s.id == "s1"]
    assert len(s1) == 1  # deduped
    assert s1[0].message_count == 3  # the LIVE (fresher) copy won
    assert not s1[0].raw.get("archived_only")  # live copy carries no badge


def test_stats_does_not_double_count(tmp_path):
    """Regression (§7.2): a live+archived session must count ONCE in stats."""
    _vault_with(tmp_path, _session("s1", n_msgs=4, updated=_T0))
    live = Store([FakeSource([_session("s1", n_msgs=4, updated=_T0)])])
    merged = live.with_archive(tmp_path / "vault")

    st = merged.stats()
    assert st.sessions == 1
    assert st.total_messages == 4  # not 8
    assert st.per_source["fake"] == 1


def test_archived_only_session_carries_badge(tmp_path):
    src = FakeSource([_session("s1"), _session("gone")])
    live = Store([src])
    vault = archive.ArchiveStore(tmp_path / "vault")
    vault.sync(live)
    del src._sessions["gone"]

    merged = live.with_archive(tmp_path / "vault")
    by_id = {s.id: s for s in merged.list_sessions()}
    assert by_id["gone"].raw.get("archived_only") is True
    assert not by_id["s1"].raw.get("archived_only")  # live one is not badged


# -- Phase 3: CLI integration (_make_store injects the vault) ----------------


def _args(**kw):
    kw.setdefault("source", None)
    return type("Args", (), kw)()


def _patch_live_store(monkeypatch, live_sources):
    """Make `cli.Store()` (no args) return a store over `live_sources`, while
    still honouring `.with_sources` / `.with_archive` (which pass a list)."""
    from scrollback import cli
    from scrollback.store import Store as RealStore

    def factory(sources=None):
        return RealStore(list(live_sources) if sources is None else sources)

    monkeypatch.setattr(cli, "Store", factory)


def test_make_store_injects_archive(monkeypatch, tmp_path):
    """`_make_store` surfaces deleted-but-archived sessions when a vault exists."""
    from scrollback import cli
    from scrollback.store import Store

    src = FakeSource([_session("s1"), _session("gone")])
    archive.ArchiveStore(tmp_path / "vault").sync(Store([src]))
    del src._sessions["gone"]

    monkeypatch.setenv("SCROLLBACK_ARCHIVE", str(tmp_path / "vault"))
    _patch_live_store(monkeypatch, [src])

    store = cli._make_store(_args())
    ids = {s.id for s in store.list_sessions()}
    assert {"s1", "gone"} <= ids


def test_make_store_source_archive_is_valid(monkeypatch, tmp_path):
    """`--source archive` is accepted and yields an archive-only view."""
    from scrollback import cli
    from scrollback.store import Store

    src = FakeSource([_session("s1")])
    archive.ArchiveStore(tmp_path / "vault").sync(Store([src]))
    monkeypatch.setenv("SCROLLBACK_ARCHIVE", str(tmp_path / "vault"))
    _patch_live_store(monkeypatch, [src])

    # Should not raise _BadSource.
    store = cli._make_store(_args(source="archive"))
    ids = {s.id for s in store.list_sessions()}
    assert ids == {"s1"}  # only the archived copy (live sources filtered out)
