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


# -- security: zip-slip + path traversal ------------------------------------


def test_import_rejects_zip_slip(tmp_path):
    """A malicious zip with a '../' member must not write outside the temp dir."""
    import zipfile

    a = archive.ArchiveStore(tmp_path / "A")
    a.sync(_store(_session("s1")))

    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("manifest.sqlite", "not a real db")
        zf.writestr("../../pwned.txt", "escaped!")

    # import_from opens the zip as a vault; the zip-slip member must be rejected
    # (raises), and crucially must NOT create the escaped file.
    import pytest
    with pytest.raises((ValueError, FileNotFoundError, Exception)):
        a.import_from(evil)
    assert not (tmp_path.parent / "pwned.txt").exists()
    assert not (tmp_path / "pwned.txt").exists()


def test_safe_path_rejects_traversal(tmp_path):
    """safe_path must refuse a manifest file_path that escapes the vault."""
    v = archive.ArchiveStore(tmp_path / "v")
    v.sync(_store(_session("s1")))
    # legit relative path resolves inside the vault
    assert v.safe_path("sessions/fake/s1.json") is not None
    # traversal + absolute escapes are refused
    assert v.safe_path("../../../../etc/passwd") is None
    assert v.safe_path("/etc/passwd") is None
    assert v.safe_path("") is None


def test_import_with_traversal_file_path_reads_nothing(tmp_path):
    """A crafted incoming manifest whose file_path points outside its vault must
    not let import read arbitrary files."""
    import sqlite3

    # Build a fake incoming vault whose manifest points file_path at a secret.
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET", encoding="utf-8")
    incoming = tmp_path / "incoming"
    (incoming / "sessions").mkdir(parents=True)
    conn = sqlite3.connect(incoming / "manifest.sqlite")
    conn.executescript(archive._SCHEMA)
    conn.execute(
        "INSERT INTO archived (source, session_id, updated, message_count, file_path) "
        "VALUES ('fake','evil','2026-01-01',1,'../secret.txt')")
    conn.commit()
    conn.close()

    dst = archive.ArchiveStore(tmp_path / "dst")
    dst.sync(_store(_session("keep")))
    dst.import_from(incoming)   # must not import the secret
    ids = {s.id for s in ArchiveSourceList(dst)}
    assert "evil" not in ids
    assert "keep" in ids


def ArchiveSourceList(store):
    from scrollback.sources.archive import ArchiveSource
    return list(ArchiveSource(store.path).list_sessions())


# -- security: never-shrink with unknown counts -----------------------------


def test_never_shrink_when_archived_count_null(tmp_path):
    """If the archived row has message_count NULL, a smaller re-read must still
    be refused (file-size guard), not silently clobber the good copy."""
    import sqlite3

    v = archive.ArchiveStore(tmp_path / "v")
    v.sync(_store(_session("s1", n_msgs=6)))
    # Force the archived row's message_count to NULL (simulating an old row).
    conn = sqlite3.connect(v.manifest_path)
    conn.execute("UPDATE archived SET message_count = NULL WHERE session_id='s1'")
    conn.commit()
    conn.close()

    # A smaller/newer re-read must be kept_shrunk (file-size guard).
    outcome = v.sync_one(
        _store(_session("s1", n_msgs=2, updated=_T0 + timedelta(hours=1))),
        "fake", "s1")
    assert outcome == "kept_shrunk"
    restored = archivefmt.from_archive_json(
        v._session_file("fake", "s1").read_text(encoding="utf-8"))
    assert len(restored.messages) == 6


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


# -- asymmetric adapters: list count != load count ---------------------------
#
# Every FakeSource above reports the same message_count when listing and when
# loading. Real adapters do not always: Claude Code counts every user/assistant
# turn while listing, but only *renderable* ones while loading (tool-result-only
# and empty turns drop out). The archive's change-detection signature straddles
# both paths, so this asymmetry is the exact shape that made sessions
# re-archive on every sync forever. These tests encode the invariant directly.


class AsymmetricSource(FakeSource):
    """A source whose listing count exceeds what `load_session` returns.

    Models Claude Code's meta/empty turns: `list_sessions` advertises
    `n_msgs + skew` messages, `load_session` yields `n_msgs`.
    """

    name = "asym"

    def __init__(self, sessions, *, skew=3):
        super().__init__(sessions)
        self._skew = skew

    def list_sessions(self):
        from dataclasses import replace
        return iter([
            replace(s, messages=(), message_count=(s.message_count or 0) + self._skew)
            for s in self._sessions.values()
        ])


def test_sync_converges_when_list_and_load_counts_differ(tmp_path):
    """An unchanged session must be 'unchanged' on EVERY subsequent sync.

    Regression: the stored signature was recomputed from the loaded session
    while the comparison used the listed session, so any adapter whose two
    counts differ re-archived every session on every run -- permanently
    "stale", and rewriting the whole vault each time.
    """
    src = AsymmetricSource([_session("s1", source="asym", n_msgs=4)])
    store = Store([src])
    vault = archive.ArchiveStore(tmp_path / "vault")

    assert vault.sync(store)["added"] == 1
    for _ in range(3):
        r = vault.sync(store)
        assert r == {"added": 0, "updated": 0, "unchanged": 1,
                     "kept_orphan": 0, "kept_shrunk": 0}


def test_asymmetric_source_never_reports_stale_after_sync(tmp_path):
    """The UI's 'needs updating' flag must clear once a session is archived."""
    src = AsymmetricSource([_session("s1", source="asym", n_msgs=4)])
    vault_path = tmp_path / "vault"
    archive.ArchiveStore(vault_path).sync(Store([src]))

    rows = Store([src]).with_archive(vault_path).list_sessions()
    assert [(s.raw or {}).get("archive_status") for s in rows] == ["archived"]


def test_never_shrink_guard_still_fires_for_asymmetric_source(tmp_path):
    """Comparing written-count to written-count must not disarm the guard."""
    src = AsymmetricSource([_session("s1", source="asym", n_msgs=6)])
    store = Store([src])
    vault = archive.ArchiveStore(tmp_path / "vault")
    vault.sync(store)

    # Degraded re-read: newer `updated`, but only 2 messages actually load.
    src._sessions["s1"] = _session("s1", source="asym", n_msgs=2,
                                   updated=_T0 + timedelta(hours=1))
    assert vault.sync(store)["kept_shrunk"] == 1
    restored = archivefmt.from_archive_json(
        vault._session_file("asym", "s1").read_text(encoding="utf-8"))
    assert len(restored.messages) == 6


# -- orphan counting ---------------------------------------------------------


def test_orphan_count_matches_archived_only_rows(tmp_path):
    """`stats()["orphans"]` must equal the number of `archived_only` sessions.

    Regression: orphans were counted as `last_seen_live < last_sync`, but
    `last_seen_live` was stamped per-row as the sync loop ran while `last_sync`
    was written at the end -- so nearly every row compared "older" and the
    headline count reported the whole vault as deleted, contradicting the
    drill-down list it linked to.
    """
    src = FakeSource([_session(f"s{i}") for i in range(5)])
    vault_path = tmp_path / "vault"
    vault = archive.ArchiveStore(vault_path)
    vault.sync(Store([src]))

    # Nothing deleted yet.
    assert vault.stats()["orphans"] == 0

    # The agent prunes two sessions.
    del src._sessions["s1"]
    del src._sessions["s3"]
    assert vault.sync(Store([src]))["kept_orphan"] == 2

    archived_only = {
        s.id for s in Store([src]).with_archive(vault_path).list_sessions()
        if (s.raw or {}).get("archived_only")
    }
    assert archived_only == {"s1", "s3"}
    assert vault.stats()["orphans"] == len(archived_only)
    # The explicit live-probe path must agree with the marker-based one.
    assert vault.stats(live_keys=Store([src]).live_keys())["orphans"] == 2


def test_partial_syncs_do_not_inflate_orphan_count(tmp_path):
    """`sync_one` / `sync_many` must not advance the full-sync marker.

    They only look at the keys they were given, so treating them as evidence
    about every other session would mark the untouched ones deleted.
    """
    src = FakeSource([_session("s1"), _session("s2"), _session("s3")])
    store = Store([src])
    vault = archive.ArchiveStore(tmp_path / "vault")
    vault.sync(store)
    assert vault.stats()["orphans"] == 0

    # Touch one session well after the full sync.
    src._sessions["s1"] = _session("s1", n_msgs=9, updated=_T0 + timedelta(hours=2))
    assert vault.sync_one(store, "fake", "s1") == "updated"
    assert vault.stats()["orphans"] == 0

    vault.sync_many(store, [("fake", "s2")])
    assert vault.stats()["orphans"] == 0


def test_sync_many_reports_not_found(tmp_path):
    """A key that no longer resolves is counted, not silently dropped."""
    store = _store(_session("s1"))
    vault = archive.ArchiveStore(tmp_path / "vault")
    r = vault.sync_many(store, [("fake", "s1"), ("fake", "ghost")])
    assert r["added"] == 1
    assert r["not_found"] == 1
    assert sum(r.values()) == 2  # every requested key accounted for


# -- vault must not read itself back -----------------------------------------


def test_sync_ignores_a_reader_for_its_own_vault(tmp_path):
    """Deleted sessions stay detectable even when the store carries the vault.

    The CLI attaches an `ArchiveSource` so deleted sessions remain browsable;
    feeding that back into a sync let the vault see itself as a live source, so
    `kept_orphan` was always 0 and orphans were re-stamped as live forever.
    """
    src = FakeSource([_session("s1"), _session("gone")])
    vault_path = tmp_path / "vault"
    vault = archive.ArchiveStore(vault_path)
    vault.sync(Store([src]))

    del src._sessions["gone"]
    # A browsing-shaped store: live sources PLUS this vault.
    composed = Store([src]).with_archive(vault_path)
    assert vault.sync(composed)["kept_orphan"] == 1
    assert vault.stats()["orphans"] == 1


def test_summary_json_omits_volatile_provenance(tmp_path):
    """Archive-status fields must not be frozen into the stored summary.

    They describe the vault relationship *now*; a cached value goes stale the
    moment the live copy changes.
    """
    import json

    vault = archive.ArchiveStore(tmp_path / "vault")
    vault.sync(_store(_session("s1")))
    with vault._connect(write=False) as conn:
        raw = conn.execute("SELECT meta_json FROM archived").fetchone()[0]
    stored = json.loads(raw)
    for volatile in ("archived", "archived_only", "archive_status"):
        assert volatile not in stored


# -- read-back adapter: windowed + indexed access ----------------------------
#
# An archive file is one JSON document, so a naive read-back parses the whole
# thing for every access. The base-class defaults did exactly that: showing a
# header, or paging a transcript, re-parsed the entire session each time --
# seconds per page on a large one. These tests pin the cheap paths.


def _archive_reader(tmp_path, *sessions):
    from scrollback.sources.archive import ArchiveSource

    vp = tmp_path / "vault"
    archive.ArchiveStore(vp).sync(_store(*sessions))
    return ArchiveSource(vp)


def test_archive_reader_meta_does_not_parse_the_file(tmp_path, monkeypatch):
    """`load_session_meta` must answer from the manifest, not the session file."""
    reader = _archive_reader(tmp_path, _session("s1", n_msgs=5))

    def boom(*_a, **_k):
        raise AssertionError("load_session_meta parsed the session file")

    monkeypatch.setattr(reader, "_read_file", boom)
    meta = reader.load_session_meta("s1")
    assert meta is not None
    assert meta.id == "s1" and meta.message_count == 5
    assert meta.messages == ()


def test_archive_reader_pages_parse_the_file_once(tmp_path, monkeypatch):
    """Paging a transcript must not re-parse the whole file per page.

    The JSON parse is what costs seconds on a large session, so that is what
    is counted here -- not the number of `_read_file` calls, which are cheap
    cache probes.
    """
    from scrollback.sources import archive as archive_src

    reader = _archive_reader(tmp_path, _session("s1", n_msgs=10))
    calls = {"n": 0}
    real = archive_src.archivefmt.from_archive_json

    def counting(text):
        calls["n"] += 1
        return real(text)

    monkeypatch.setattr(archive_src.archivefmt, "from_archive_json", counting)
    pages = [reader.load_messages("s1", offset=o, limit=4) for o in (0, 4, 8)]
    assert [len(p) for p in pages] == [4, 4, 2]
    assert [m.id for m in pages[0]] == [f"s1-m{i}" for i in range(4)]
    assert calls["n"] == 1  # parsed once, reused across pages


def test_archive_reader_cache_invalidates_on_change(tmp_path):
    """A refreshed archive copy must never be served from the stale cache."""
    src = FakeSource([_session("s1", n_msgs=2)])
    vp = tmp_path / "vault"
    vault = archive.ArchiveStore(vp)
    vault.sync(Store([src]))

    from scrollback.sources.archive import ArchiveSource
    reader = ArchiveSource(vp)
    assert len(reader.load_messages("s1")) == 2

    src._sessions["s1"] = _session("s1", n_msgs=7, updated=_T0 + timedelta(hours=1))
    vault.sync(Store([src]))
    assert len(reader.load_messages("s1")) == 7


def test_archive_reader_resolves_ids_and_prefixes(tmp_path):
    reader = _archive_reader(
        tmp_path,
        _session("abc123", updated=_T0),
        _session("abc999", updated=_T0 + timedelta(hours=1)),
        _session("zz", updated=_T0 - timedelta(hours=1)),
    )
    assert reader.resolve_session_id("abc123") == "abc123"
    assert reader.resolve_session_id("zz") == "zz"
    assert reader.resolve_session_id("abc") is None      # ambiguous prefix
    assert reader.resolve_session_id("abc9") == "abc999"  # unique prefix
    assert reader.resolve_session_id("nope") is None
    assert reader.resolve_session_id("latest") == "abc999"


def test_archive_reader_prefix_treats_underscore_literally(tmp_path):
    """`_` is a LIKE wildcard; ids contain it, so it must be escaped."""
    reader = _archive_reader(tmp_path, _session("ses_a1"), _session("sesXa1"))
    assert reader.resolve_session_id("ses_") == "ses_a1"


# -- integrity check: shallow vs deep ---------------------------------------


def test_verify_shallow_skips_parsing_but_finds_missing(tmp_path):
    vault = archive.ArchiveStore(tmp_path / "vault")
    vault.sync(_store(_session("s1"), _session("s2")))

    # Corrupt s1's contents; a shallow check cannot see it, a deep one must.
    vault._session_file("fake", "s1").write_text("{not json", encoding="utf-8")
    quick = vault.verify(deep=False)
    assert len(quick["ok"]) == 2 and quick["unreadable"] == []
    deep = vault.verify(deep=True)
    assert deep["unreadable"] == ["fake:s1"] and len(deep["ok"]) == 1

    # A truncated (empty) file counts as missing even in the shallow check.
    vault._session_file("fake", "s2").write_text("", encoding="utf-8")
    assert vault.verify(deep=False)["missing"] == ["fake:s2"]


def test_verify_reports_progress(tmp_path):
    vault = archive.ArchiveStore(tmp_path / "vault")
    vault.sync(_store(_session("s1"), _session("s2"), _session("s3")))
    seen = []
    vault.verify(deep=True, progress=lambda d, t: seen.append((d, t)))
    assert seen == [(1, 3), (2, 3), (3, 3)]


def test_backfill_meta_preserves_the_listing_signature(tmp_path):
    """Backfill must copy the manifest's signature, not the loaded counts.

    Otherwise a vault backfilled from an asymmetric adapter reads back as
    permanently stale -- the same defect as in `_archive_session`.
    """
    import json

    src = AsymmetricSource([_session("s1", source="asym", n_msgs=4)])
    vault_path = tmp_path / "vault"
    vault = archive.ArchiveStore(vault_path)
    vault.sync(Store([src]))

    # Simulate a vault archived before `meta_json` existed.
    with vault._connect(write=True) as conn:
        conn.execute("UPDATE archived SET meta_json = NULL")
    assert vault.backfill_meta() == 1

    with vault._connect(write=False) as conn:
        stored = json.loads(conn.execute("SELECT meta_json FROM archived").fetchone()[0])
    listed = next(iter(src.list_sessions()))
    assert stored["message_count"] == listed.message_count

    rows = Store([src]).with_archive(vault_path).list_sessions()
    assert [(s.raw or {}).get("archive_status") for s in rows] == ["archived"]


# -- WAL sidecars must never travel with an exported vault -------------------


def _dirty_wal(vault):
    """Leave a committed-but-not-checkpointed write in the manifest's WAL."""
    import sqlite3

    conn = sqlite3.connect(vault.manifest_path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('probe', 'x')")
        conn.commit()
    finally:
        conn.close()


def test_zip_export_excludes_sqlite_sidecars(tmp_path):
    """A `-wal`/`-shm` file is only valid beside the DB that wrote it.

    Shipping them inside a backup gives the recipient a manifest that is
    either stale or refuses to open.
    """
    import zipfile

    vault = archive.ArchiveStore(tmp_path / "vault")
    vault.sync(_store(_session("s1"), _session("s2")))
    _dirty_wal(vault)

    dest = tmp_path / "backup.zip"
    vault.export_to(dest)
    names = zipfile.ZipFile(dest).namelist()
    assert not [n for n in names if n.endswith(("-wal", "-shm", "-journal"))]
    assert "manifest.sqlite" in names

    # The checkpointed copy is complete: re-importing recovers both sessions.
    merged = archive.ArchiveStore(tmp_path / "restored")
    assert merged.import_from(dest)["added"] == 2


def test_dir_export_excludes_sqlite_sidecars(tmp_path):
    vault = archive.ArchiveStore(tmp_path / "vault")
    vault.sync(_store(_session("s1")))
    _dirty_wal(vault)

    dest = tmp_path / "copy"
    vault.export_to(dest)
    assert sorted(p.name for p in dest.iterdir()) == ["manifest.sqlite", "sessions"]
    assert archive.ArchiveStore(tmp_path / "restored").import_from(dest)["added"] == 1


def test_zip_export_refuses_to_overwrite(tmp_path):
    """The target of a backup is, by definition, precious."""
    import pytest

    vault = archive.ArchiveStore(tmp_path / "vault")
    vault.sync(_store(_session("s1")))
    dest = tmp_path / "backup.zip"
    dest.write_text("existing backup", encoding="utf-8")

    with pytest.raises(FileExistsError):
        vault.export_to(dest)
    assert dest.read_text(encoding="utf-8") == "existing backup"
