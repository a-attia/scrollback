"""API tests for the Live/Archive/All mode switch + web-driven archive sync.

Covers the web-redesign backend: mode-aware store selection, the archive
overview endpoint, the sync endpoints (write ONLY to the vault), the SSE
progress stream, and per-session `archive_status`.
"""

import time
from dataclasses import replace
from datetime import datetime, timezone

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from scrollback.models import Message, Part, Session  # noqa: E402
from scrollback.sources.base import Source  # noqa: E402
from scrollback.store import Store  # noqa: E402
from scrollback.web.app import create_app  # noqa: E402

_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _session(sid, *, n_msgs=1, updated=_T0):
    msgs = tuple(
        Message(id=f"{sid}-m{i}", role="user", created=updated,
                parts=(Part(id=f"{sid}-m{i}-p", type="text",
                            text=f"body {i} conversation"),))
        for i in range(n_msgs)
    )
    return Session(id=sid, source="fake", title=f"S {sid}", directory="/tmp/p",
                   created=_T0, updated=updated, message_count=n_msgs, messages=msgs)


class FakeSource(Source):
    name = "fake"
    label = "Fake"

    def __init__(self, sessions):
        self._sessions = {s.id: s for s in sessions}

    def is_available(self):
        return True

    def location(self):
        from pathlib import Path
        return Path("/tmp/fake")

    def list_sessions(self):
        return iter([replace(s, messages=()) for s in self._sessions.values()])

    def load_session(self, sid):
        return self._sessions.get(sid)


def _client(tmp_path, src):
    return TestClient(create_app(Store([src]), allowed_hosts=[],
                                 archive_path=tmp_path / "vault"))


def _sync_all(client):
    r = client.post("/api/archive/sync").json()
    jid = r["job_id"]
    for _ in range(100):
        j = client.get(f"/api/archive/jobs/{jid}").json()
        if j["finished"]:
            return j
        time.sleep(0.02)
    raise AssertionError("sync did not finish")


# -- mode selection ---------------------------------------------------------


def test_modes_scope_sessions(tmp_path):
    src = FakeSource([_session("s1"), _session("gone")])
    c = _client(tmp_path, src)
    _sync_all(c)
    del src._sessions["gone"]  # deleted from live, kept in vault

    live = {s["id"] for s in c.get("/api/sessions?mode=live").json()["sessions"]}
    arch = {s["id"] for s in c.get("/api/sessions?mode=archive").json()["sessions"]}
    all_ = {s["id"] for s in c.get("/api/sessions?mode=all").json()["sessions"]}

    assert live == {"s1"}                 # live only, no deleted
    assert arch == {"s1", "gone"}         # vault, incl. deleted
    assert all_ == {"s1", "gone"}         # merged


def test_stats_respect_mode_no_double_count(tmp_path):
    src = FakeSource([_session("s1", n_msgs=4)])
    c = _client(tmp_path, src)
    _sync_all(c)  # s1 now live + archived

    live = c.get("/api/stats?mode=live").json()
    all_ = c.get("/api/stats?mode=all").json()
    assert live["sessions"] == 1 and live["messages"] == 4
    # All must dedup: 1 session / 4 messages, NOT 2 / 8.
    assert all_["sessions"] == 1 and all_["messages"] == 4


def test_source_filter_within_mode(tmp_path):
    src = FakeSource([_session("s1")])
    c = _client(tmp_path, src)
    assert c.get("/api/sessions?mode=live&source=fake").status_code == 200
    assert c.get("/api/sessions?mode=live&source=bogus").status_code == 400


# -- archive overview -------------------------------------------------------


def test_archive_mode_still_live_session_is_archived_not_deleted(tmp_path):
    """Regression: a still-live session that has been archived must read as
    'archived' (not 'deleted') even in Archive mode, where the store has no
    live sources of its own. archived_only means gone-from-agent only."""
    src = FakeSource([_session("s1"), _session("s2")])
    c = _client(tmp_path, src)
    _sync_all(c)  # both live + archived

    arch = {s["id"]: s for s in c.get("/api/sessions?mode=archive").json()["sessions"]}
    assert arch["s1"]["archive_status"] == "archived"
    assert arch["s1"]["archived_only"] is False   # still live -> NOT deleted

    # Now actually delete s1 from the live source: it becomes archive-only.
    del src._sessions["s1"]
    arch2 = {s["id"]: s for s in c.get("/api/sessions?mode=archive").json()["sessions"]}
    assert arch2["s1"]["archived_only"] is True    # gone from agent -> deleted


def test_archive_status_tag_is_consistent_across_modes(tmp_path):
    """A live+archived session reads as 'archived' in All and Archive modes.
    Live mode is intentionally live-only (no archive read) for performance, so
    it always shows every live session; the archive tag appears in All/Archive.
    """
    src = FakeSource([_session("s1"), _session("s2")])
    c = _client(tmp_path, src)
    # Archive only s1; s2 stays live-only.
    r = c.post("/api/archive/sync/fake/s1").json()
    jid = r["job_id"]
    for _ in range(100):
        if c.get(f"/api/archive/jobs/{jid}").json()["finished"]:
            break
        time.sleep(0.02)

    def s1_of(mode):
        rows = {s["id"]: s for s in c.get(f"/api/sessions?mode={mode}").json()["sessions"]}
        return rows["s1"]

    # All + Archive modes show the archive status.
    for mode in ("all", "archive"):
        s1 = s1_of(mode)
        assert s1["archived"] is True, mode
        assert s1["archive_status"] == "archived", mode
        assert s1["archived_only"] is False, mode  # still live -> not deleted

    # Live mode is live-only: both sessions present, no archive read (status
    # reflects live-only knowledge).
    live_ids = {s["id"] for s in c.get("/api/sessions?mode=live").json()["sessions"]}
    assert live_ids == {"s1", "s2"}

    # Deleting s1 from live: gone from live, still browsable via the archive.
    del src._sessions["s1"]
    live_ids = {s["id"] for s in c.get("/api/sessions?mode=live").json()["sessions"]}
    all_ids = {s["id"] for s in c.get("/api/sessions?mode=all").json()["sessions"]}
    assert "s1" not in live_ids
    assert "s1" in all_ids


def test_archive_overview_has_disk_and_stale(tmp_path):
    src = FakeSource([_session("s1"), _session("s2")])
    c = _client(tmp_path, src)
    _sync_all(c)
    ov = c.get("/api/archive").json()
    assert ov["bytes"] > 0
    assert ov["stale"] == 0
    # make s1 stale
    src._sessions["s1"] = _session("s1", n_msgs=9, updated=_T0.replace(hour=2))
    assert c.get("/api/archive").json()["stale"] == 1


def test_orphan_count_matches_deleted_drilldown(tmp_path):
    """The 'deleted from agent' card and the list it links to must agree.

    Regression: the card counted rows whose `last_seen_live` predated the last
    sync (nearly all of them, since those timestamps were written per-row as
    the sync ran), while the list showed `archived_only` rows. The headline
    said hundreds; the list showed a handful.
    """
    src = FakeSource([_session(f"s{i}") for i in range(6)])
    c = _client(tmp_path, src)
    _sync_all(c)
    assert c.get("/api/archive").json()["orphans"] == 0

    # The agent prunes two sessions.
    del src._sessions["s2"]
    del src._sessions["s4"]

    orphans = c.get("/api/archive").json()["orphans"]
    listed = {
        s["id"]
        for s in c.get("/api/sessions?mode=archive&deleted=true&limit=2000").json()["sessions"]
    }
    assert listed == {"s2", "s4"}
    assert orphans == len(listed)


def test_deleted_filter_is_applied_server_side(tmp_path):
    """`deleted=true` must find deleted sessions beyond the first page.

    They are interleaved by date with everything else, so a client-side filter
    over one page of results would show an empty list.
    """
    # 40 sessions; the deleted one is the OLDEST, so it sorts last.
    sessions = [
        _session(f"s{i:02d}", updated=_T0.replace(minute=i)) for i in range(40)
    ]
    src = FakeSource(sessions)
    c = _client(tmp_path, src)
    _sync_all(c)
    del src._sessions["s00"]

    first_page = c.get("/api/sessions?mode=archive&limit=10").json()["sessions"]
    assert "s00" not in {s["id"] for s in first_page}   # not on page 1

    data = c.get("/api/sessions?mode=archive&deleted=true&limit=10").json()
    assert [s["id"] for s in data["sessions"]] == ["s00"]
    assert data["has_more"] is False


def test_archive_verify_endpoint(tmp_path):
    c = _client(tmp_path, FakeSource([_session("s1")]))
    assert c.get("/api/archive/verify").json() == {"exists": False}
    _sync_all(c)
    v = c.get("/api/archive/verify").json()
    assert v["exists"] and v["ok"] == 1 and v["missing"] == [] and v["unreadable"] == []


def test_archive_export_download(tmp_path):
    c = _client(tmp_path, FakeSource([_session("s1"), _session("s2")]))
    assert c.get("/api/archive/export").status_code == 404   # no vault yet
    _sync_all(c)
    r = c.get("/api/archive/export")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert r.content[:2] == b"PK"   # zip magic


def test_archive_update_stale_endpoint(tmp_path):
    src = FakeSource([_session("s1", n_msgs=2)])
    c = _client(tmp_path, src)
    _sync_all(c)
    src._sessions["s1"] = _session("s1", n_msgs=5, updated=_T0.replace(hour=3))
    assert c.get("/api/archive").json()["stale"] == 1
    j = c.post("/api/archive/sync/stale").json()
    for _ in range(100):
        if c.get(f"/api/archive/jobs/{j['job_id']}").json()["finished"]:
            break
        time.sleep(0.02)
    assert c.get("/api/archive").json()["stale"] == 0


def test_archive_batch_endpoint(tmp_path):
    c = _client(tmp_path, FakeSource([_session("s1"), _session("s2"), _session("s3")]))
    j = c.post("/api/archive/sync/batch", json={"keys": [["fake", "s1"], ["fake", "s3"]]}).json()
    for _ in range(100):
        if c.get(f"/api/archive/jobs/{j['job_id']}").json()["finished"]:
            break
        time.sleep(0.02)
    res = c.get(f"/api/archive/jobs/{j['job_id']}").json()["result"]
    assert res["added"] == 2
    ids = {s["id"] for s in c.get("/api/sessions?mode=archive").json()["sessions"]}
    assert ids == {"s1", "s3"}   # s2 not archived


def test_archive_import_endpoint(tmp_path):
    # Build a source vault, export it, then import the zip via the endpoint.
    src_c = _client(tmp_path, FakeSource([_session("x1"), _session("x2")]))
    _sync_all(src_c)
    zip_bytes = src_c.get("/api/archive/export").content

    dst = TestClient(create_app(Store([FakeSource([_session("y1")])]),
                               allowed_hosts=[], archive_path=tmp_path / "dst"))
    dst.post("/api/archive/sync")  # seed y1
    for _ in range(100):
        if not dst.get("/api/archive").json().get("exists"):
            time.sleep(0.02)
        else:
            break
    j = dst.post("/api/archive/import", content=zip_bytes).json()
    for _ in range(100):
        if dst.get(f"/api/archive/jobs/{j['job_id']}").json()["finished"]:
            break
        time.sleep(0.02)
    ids = {s["id"] for s in dst.get("/api/sessions?mode=archive").json()["sessions"]}
    assert {"x1", "x2"} <= ids   # merged in


def test_archive_source_not_listed_as_a_filter_chip(tmp_path):
    """The archive reader is a browse MODE, not an agent -- it must never show
    up in /api/sources (which drives the source-filter chips)."""
    src = FakeSource([_session("s1")])
    c = _client(tmp_path, src)
    _sync_all(c)  # create the vault so the archive source becomes active
    names = [s["name"] for s in c.get("/api/sources").json()]
    assert "archive" not in names
    assert "fake" in names


def test_archive_overview_empty_then_populated(tmp_path):
    src = FakeSource([_session("s1"), _session("s2")])
    c = _client(tmp_path, src)
    before = c.get("/api/archive").json()
    assert before["exists"] is False

    _sync_all(c)
    after = c.get("/api/archive").json()
    assert after["exists"] is True
    assert after["sessions"] == 2
    assert after["per_source"] == {"fake": 2}


# -- sync endpoints + progress ---------------------------------------------


def test_sync_all_writes_only_to_vault(tmp_path):
    src = FakeSource([_session("s1")])
    c = _client(tmp_path, src)
    j = _sync_all(c)
    assert j["result"]["added"] == 1
    # Everything created is under the vault dir.
    assert [p.name for p in tmp_path.iterdir()] == ["vault"]


def test_sync_one_and_status_lifecycle(tmp_path):
    src = FakeSource([_session("s1", n_msgs=2)])
    c = _client(tmp_path, src)
    _sync_all(c)

    d = c.get("/api/sessions/fake/s1").json()
    assert d["archive_status"] == "archived"

    # Live session grows -> stale.
    src._sessions["s1"] = _session("s1", n_msgs=5, updated=_T0.replace(hour=1))
    assert c.get("/api/sessions/fake/s1").json()["archive_status"] == "stale"

    # Update just that session.
    r = c.post("/api/archive/sync/fake/s1").json()
    jid = r["job_id"]
    for _ in range(100):
        if c.get(f"/api/archive/jobs/{jid}").json()["finished"]:
            break
        time.sleep(0.02)
    assert c.get("/api/archive/jobs/" + jid).json()["result"]["outcome"] == "updated"
    assert c.get("/api/sessions/fake/s1").json()["archive_status"] == "archived"


def test_sse_progress_stream_completes(tmp_path):
    src = FakeSource([_session("s1"), _session("s2")])
    c = _client(tmp_path, src)
    r = c.post("/api/archive/sync").json()
    jid = r["job_id"]
    # The SSE stream yields frames and ends with a finished snapshot.
    with c.stream("GET", f"/api/archive/jobs/{jid}/events") as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        body = "".join(resp.iter_text())
    assert "data:" in body  # at least one SSE frame
    assert '"finished": true' in body  # terminal frame present


def test_sync_all_single_flight(tmp_path):
    src = FakeSource([_session(f"s{i}") for i in range(3)])
    c = _client(tmp_path, src)
    j1 = c.post("/api/archive/sync").json()
    j2 = c.post("/api/archive/sync").json()
    # A concurrent request returns the same running job (or a finished one).
    assert "job_id" in j1 and "job_id" in j2


def test_job_events_unknown_id_404(tmp_path):
    c = _client(tmp_path, FakeSource([_session("s1")]))
    assert c.get("/api/archive/jobs/nope/events").status_code == 404
    assert c.get("/api/archive/jobs/nope").status_code == 404


# -- integrity endpoint: cheap by default, deep on demand --------------------


def test_verify_endpoint_is_shallow_by_default(tmp_path):
    """The landing's integrity line must not parse the whole vault.

    Deep verification reads every archived file; doing that inline on each
    archive-landing render blocked the page for as long as it took.
    """
    c = _client(tmp_path, FakeSource([_session("s1"), _session("s2")]))
    _sync_all(c)

    from scrollback.archive import ArchiveStore
    vault = ArchiveStore(tmp_path / "vault")
    vault._session_file("fake", "s1").write_text("{corrupt", encoding="utf-8")

    shallow = c.get("/api/archive/verify").json()
    assert shallow["deep"] is False
    assert shallow["ok"] == 2 and shallow["unreadable"] == []

    deep = c.get("/api/archive/verify?deep=true").json()
    assert deep["deep"] is True and deep["unreadable"] == ["fake:s1"]


def test_deep_verify_runs_as_a_background_job(tmp_path):
    c = _client(tmp_path, FakeSource([_session("s1"), _session("s2")]))
    _sync_all(c)
    j = c.post("/api/archive/verify").json()
    for _ in range(100):
        snap = c.get(f"/api/archive/jobs/{j['job_id']}").json()
        if snap["finished"]:
            break
        time.sleep(0.02)
    assert snap["finished"] and snap["error"] is None
    assert snap["result"] == {"ok": 2, "missing": 0, "unreadable": 0}


# -- live mode still shows archive provenance --------------------------------


def test_live_mode_annotates_archive_status(tmp_path):
    """Live is the default mode; a session archived there must say so.

    It must NOT, however, surface vault-only sessions -- those are gone from
    the agent, so they do not belong in a view of live sessions.
    """
    src = FakeSource([_session("s1"), _session("gone")])
    c = _client(tmp_path, src)

    before = c.get("/api/sessions?mode=live").json()["sessions"]
    assert {s["archive_status"] for s in before} == {"none"}

    _sync_all(c)
    after = {s["id"]: s for s in c.get("/api/sessions?mode=live").json()["sessions"]}
    assert after["s1"]["archive_status"] == "archived"

    # A live session that has since grown reads back as stale, not archived.
    src._sessions["s1"] = _session("s1", n_msgs=9, updated=_T0.replace(hour=5))
    live = {s["id"]: s for s in c.get("/api/sessions?mode=live").json()["sessions"]}
    assert live["s1"]["archive_status"] == "stale"

    # Deleted-from-agent sessions stay out of live mode, but remain in all.
    del src._sessions["gone"]
    ids = {s["id"] for s in c.get("/api/sessions?mode=live").json()["sessions"]}
    assert "gone" not in ids
    assert "gone" in {s["id"] for s in c.get("/api/sessions?mode=all").json()["sessions"]}
