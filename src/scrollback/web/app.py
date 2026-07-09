"""FastAPI application exposing a JSON API over the Store.

Design notes
------------
* **Never writes to your agents' data.** Reading is strictly read-only. The
  only writes are to scrollback's own durable vault (`~/.scrollback/archive`),
  and only via the explicit archive-sync endpoints below.
* Intended to bind to 127.0.0.1 only (enforced by the `web` CLI command).
* The frontend is static (HTML/CSS/JS) served from `web/static/`; the
  browser talks to the JSON endpoints below.
* Browsing has a top-level mode -- live / archive / all -- passed as a `mode`
  query param to the read endpoints (see `create_app`).

Endpoints
---------
GET  /api/sources                          -> available source adapters
GET  /api/sessions?source&mode&dir&q&limit -> session summaries (newest first)
GET  /api/sessions/{source}/{id}           -> full session with messages/parts
GET  /api/search?q&mode&dir&limit          -> search hits across sessions
GET  /api/export/{source}/{id}?format&...  -> rendered document
GET  /api/stats?mode                       -> aggregate usage for the mode
GET  /api/archive                          -> durable-vault overview
POST /api/archive/sync                     -> sync all live sessions -> vault
POST /api/archive/sync/{source}/{id}       -> archive/update one session
GET  /api/archive/jobs/{job_id}/events     -> SSE progress for a sync job
"""

from __future__ import annotations

from typing import Any

try:
    from fastapi import Body, FastAPI, HTTPException, Query, Request, Response
    from fastapi.staticfiles import StaticFiles
except ModuleNotFoundError as exc:  # pragma: no cover - guidance path
    raise SystemExit(
        "The web app needs FastAPI/uvicorn. Install with:\n"
        '    pip install "scrollback[web]"\n'
        "or:\n"
        "    pip install fastapi uvicorn"
    ) from exc

from pathlib import Path

from datetime import datetime, timezone

from .. import __version__, export
from ..serialize import message_dict, search_hit, session_detail, session_summary
from ..store import Store


class _SyncJob:
    """State for one in-flight archive sync, observable via SSE.

    Progress is pushed by `ArchiveStore.sync`'s `progress(done, total)`
    callback. The job runs to completion server-side regardless of whether any
    SSE client stays connected.
    """

    def __init__(self, job_id: str, kind: str) -> None:
        import threading

        self.id = job_id
        self.kind = kind  # "all" | "one"
        self.done = 0
        self.total = 0
        self.phase = "starting"
        self.result: dict | None = None
        self.error: str | None = None
        self.finished = threading.Event()

    def on_progress(self, done: int, total: int) -> None:
        self.done, self.total, self.phase = done, total, "syncing"

    def snapshot(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "done": self.done,
            "total": self.total, "phase": self.phase,
            "result": self.result, "error": self.error,
            "finished": self.finished.is_set(),
        }


class _JobRegistry:
    """In-process registry of sync jobs with single-flight for full syncs."""

    def __init__(self) -> None:
        import threading

        self._jobs: dict[str, _SyncJob] = {}
        self._lock = threading.Lock()          # guards _jobs / _active bookkeeping
        self._writer_lock = threading.Lock()   # only one manifest writer at a time
        self._active: str | None = None        # id of the currently-running job

    def get(self, job_id: str) -> "_SyncJob | None":
        return self._jobs.get(job_id)

    def start(self, kind: str, work) -> _SyncJob:
        """Register a job and run `work(job)` in a daemon thread.

        ALL sync jobs write the manifest (a single SQLite file), so they must
        not run concurrently. If any job is already active, a same-`kind`
        request returns the running job (single-flight); otherwise the new job
        waits on `_writer_lock` so writers are serialized (never two at once).
        """
        import threading
        import uuid

        with self._lock:
            if self._active:
                existing = self._jobs.get(self._active)
                if existing and not existing.finished.is_set() and existing.kind == kind:
                    return existing  # coalesce identical concurrent requests
            job = _SyncJob(uuid.uuid4().hex, kind)
            self._jobs[job.id] = job

        def run() -> None:
            # Serialize all manifest writers: one at a time, no concurrent
            # SQLite write handles on the same DB.
            with self._writer_lock:
                with self._lock:
                    self._active = job.id
                try:
                    work(job)
                except Exception as exc:  # keep the failure visible to the client
                    job.error = str(exc)
                finally:
                    job.phase = "done"
                    job.finished.set()
                    with self._lock:
                        if self._active == job.id:
                            self._active = None

        threading.Thread(target=run, daemon=True).start()
        return job


def _default_store() -> Store:
    """The production store: live sources plus the durable archive vault.

    Archived sessions -- including ones the agent has deleted -- become
    browsable/searchable in the web UI, deduped live-wins (see
    `Store.with_archive`). No-op when no vault exists. Tests inject their own
    store and bypass this.
    """
    from .. import archive

    return Store().with_archive(archive.default_archive_path())


def _parse_dt(s: str | None) -> datetime | None:
    """Parse an ISO date/datetime from a query param; None if blank/invalid."""
    if not s:
        return None
    raw = s.strip()
    try:
        if len(raw) == 10:
            return datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        iso = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        dt = datetime.fromisoformat(iso)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None

_STATIC_DIR = Path(__file__).parent / "static"

# Content types for the export endpoint.
_MEDIA = {
    "markdown": "text/markdown; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
    "json": "application/json; charset=utf-8",
    "html": "text/html; charset=utf-8",
    "text": "text/plain; charset=utf-8",
    "txt": "text/plain; charset=utf-8",
}
_EXT = {"markdown": "md", "md": "md", "json": "json", "html": "html",
        "text": "txt", "txt": "txt"}


def create_app(
    store: Store | None = None,
    *,
    on_idle=None,
    idle_timeout: float = 0.0,
    allowed_hosts: list[str] | None = None,
    archive_path=None,
):
    """Build the FastAPI app. A custom Store can be injected for tests.

    The injected (or default) `store` is treated as the **live base**. The
    top-level Live / Archive / All mode (a `mode` query param on the read
    endpoints) composes stores from this live base plus the durable vault at
    `archive_path` (default: `archive.default_archive_path()`):

      * live    -> the live base only (fastest);
      * archive -> the vault only (incl. deleted sessions);
      * all     -> live base + vault, deduped (live wins).

    If `idle_timeout` > 0 and `on_idle` is provided, the app runs a watchdog:
    the frontend pings `/api/heartbeat` periodically, and if no ping arrives
    for `idle_timeout` seconds (i.e. the window was closed), `on_idle()` is
    called -- used to auto-stop the server and free the port.

    `allowed_hosts` guards against DNS-rebinding: requests whose Host header's
    hostname is not in the allowlist are rejected. Defaults to loopback names
    (localhost / 127.0.0.1 / ::1). Pass an explicit list when binding to a
    non-loopback address. `None` => loopback-only; an empty list disables the
    check (not recommended).
    """
    from .. import archive as _archive_mod

    app = FastAPI(title="scrollback", version=__version__)
    _live_store = store if store is not None else Store()
    _vault_path = (
        archive_path if archive_path is not None
        else _archive_mod.default_archive_path()
    )

    # One-time, off the request path: backfill meta_json for vaults archived
    # before that column existed, so archive/all listings are pure-SQLite fast
    # instead of parsing every session file.
    def _backfill_bg() -> None:
        try:
            _archive_mod.ArchiveStore(_vault_path).backfill_meta()
        except Exception:
            pass
    import threading as _threading
    _threading.Thread(target=_backfill_bg, daemon=True).start()

    def _store_for(mode: str) -> Store:
        """Return the Store for a browsing mode (live | archive | all).

        Not cached across requests: the vault can be created (or grow) at
        runtime via the sync endpoints, and `with_archive` is a cheap
        existence-check + wrap, so we compose fresh each call to always reflect
        the current vault state.
        """
        mode = mode if mode in ("live", "archive", "all") else "all"
        if mode == "live":
            # Live is the default + hot path: keep it live-only so it never pays
            # the archive read cost. (Archive-status tags still show accurately
            # in All / Archive modes.)
            return _live_store
        if mode == "archive":
            # Live sources dropped; only the vault reader remains. Pass the live
            # session keys as a probe so a still-live archived session is
            # labelled "archived", not "deleted" (archived_only).
            return Store([], live_probe=_live_store.live_keys()).with_archive(_vault_path)
        return _live_store.with_archive(_vault_path)  # all

    # Back-compat alias: endpoints that don't take a mode use the "all" store
    # (the previous default behaviour).
    _store = _store_for("all")

    # Activity hook: a running sync should count as activity so the idle
    # auto-shutdown watchdog does not kill the server mid-sync. The watchdog
    # (when installed) replaces this with a real timestamp bump.
    _activity = {"bump": lambda: None}

    def _note_activity() -> None:
        _activity["bump"]()

    _install_host_guard(app, allowed_hosts)

    # Translate unexpected source/IO failures (locked/corrupt DB, unreadable
    # files) into a clean 503 instead of a leaked 500 + traceback.
    import sqlite3

    from fastapi.responses import JSONResponse

    @app.exception_handler(sqlite3.Error)
    async def _sqlite_error(_request, exc):  # pragma: no cover - error path
        return JSONResponse(status_code=503, content={"detail": "data source unavailable"})

    @app.exception_handler(OSError)
    async def _os_error(_request, exc):  # pragma: no cover - error path
        return JSONResponse(status_code=503, content={"detail": "data source unavailable"})

    watchdog_on = idle_timeout > 0 and on_idle is not None
    if watchdog_on:
        _install_heartbeat_watchdog(app, on_idle, idle_timeout, _activity)
    else:
        # Always expose the config endpoint so the frontend can ask once and
        # skip heartbeats when auto-shutdown is not in effect.
        @app.get("/api/heartbeat-config")
        def heartbeat_config_off() -> dict[str, float]:
            return {"interval": 0.0, "enabled": 0.0}

    # -- API ---------------------------------------------------------------

    @app.get("/api/sources")
    def api_sources() -> list[dict[str, Any]]:
        # Report every KNOWN adapter, marking which have data on this machine.
        # The store holds the available ones; we additionally surface any
        # registered-but-unavailable adapters so the UI can show them greyed.
        from ..sources import registry
        from ..sources.archive import ArchiveSource

        out: list[dict[str, Any]] = []
        available_names = set()
        # Report only real agent adapters. The archive reader is a browse MODE
        # (live/archive/all), not an agent source, so it must not appear as a
        # source-filter chip.
        for s in _store.sources:
            if isinstance(s, ArchiveSource):
                continue
            available_names.add(s.name)
            out.append({
                "name": s.name,
                "label": s.label,
                "available": True,
                "location": str(s.location()) if s.location() else None,
            })
        for s in registry.all_sources():
            if s.name in available_names:
                continue
            out.append({
                "name": s.name,
                "label": s.label,
                "available": False,
                "location": None,
            })
        return out

    @app.get("/api/sessions")
    def api_sessions(
        source: str | None = None,
        mode: str = "all",
        dir: str | None = None,
        q: str | None = None,
        since: str | None = None,
        until: str | None = None,
        fold: bool = True,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=60, ge=1, le=2000),
    ) -> dict[str, Any]:
        base = _store_for(mode)
        # `source` filters WITHIN the mode, on the session's ORIGINAL source
        # (so archived opencode copies are kept for source="opencode"). Mode is
        # the archive axis; there is no source="archive". Accept any known live
        # adapter name -- including ones only present in the injected live store
        # (tests) or the global registry.
        from ..sources import registry
        known = {s.name for s in _live_store.sources} | {s.name for s in registry.all_sources()}
        if source and source not in known:
            raise HTTPException(status_code=400, detail=f"unknown source: {source}")
        # Fetch one extra to tell the client whether more pages exist.
        rows = base.list_sessions(
            source=source, directory=dir, query=q,
            since=_parse_dt(since), until=_parse_dt(until),
            offset=offset, limit=limit + 1, fold_subagents=fold,
        )
        has_more = len(rows) > limit
        rows = rows[:limit]
        return {
            "sessions": [session_summary(s) for s in rows],
            "offset": offset,
            "limit": limit,
            "has_more": has_more,
        }

    @app.get("/api/sessions/{source}/{session_id}")
    def api_session_detail(source: str, session_id: str) -> dict[str, Any]:
        """Full session including all messages. For very large sessions the
        frontend should prefer the meta + windowed messages endpoints."""
        sess = _store_for("all").load_session(session_id, source=source)
        if sess is None:
            raise HTTPException(status_code=404, detail="session not found")
        return session_detail(sess)

    @app.get("/api/sessions/{source}/{session_id}/meta")
    def api_session_meta(source: str, session_id: str) -> dict[str, Any]:
        sess = _store_for("all").load_session_meta(session_id, source=source)
        if sess is None:
            raise HTTPException(status_code=404, detail="session not found")
        return session_summary(sess)

    @app.get("/api/sessions/{source}/{session_id}/messages")
    def api_session_messages(
        source: str,
        session_id: str,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=40, ge=1, le=500),
    ) -> dict[str, Any]:
        msgs = _store_for("all").load_messages(
            session_id, source=source, offset=offset, limit=limit + 1
        )
        has_more = len(msgs) > limit
        msgs = msgs[:limit]
        return {
            "messages": [message_dict(m) for m in msgs],
            "offset": offset,
            "limit": limit,
            "has_more": has_more,
        }

    @app.get("/api/search")
    def api_search(
        q: str,
        mode: str = "all",
        dir: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = Query(default=100, ge=1, le=2000),
    ) -> list[dict[str, Any]]:
        if not q.strip():
            return []
        hits = _store_for(mode).search(
            q, directory=dir, since=_parse_dt(since), until=_parse_dt(until), limit=limit
        )
        return [search_hit(h) for h in hits]

    @app.get("/api/export/{source}/{session_id}")
    def api_export(
        source: str,
        session_id: str,
        format: str = "markdown",
        reasoning: bool = True,
        tools: bool = True,
        math: str = "raw",
        download: bool = False,
    ) -> "Response":
        if format not in export.FORMATS:
            raise HTTPException(status_code=400, detail=f"bad format: {format}")
        if math not in export.MATH_MODES:
            raise HTTPException(status_code=400, detail=f"bad math mode: {math}")
        sess = _store_for("all").load_session(session_id, source=source)
        if sess is None:
            raise HTTPException(status_code=404, detail="session not found")
        kwargs: dict[str, Any] = {}
        if format != "json":
            kwargs = {"include_reasoning": reasoning, "include_tools": tools, "math": math}
        body = export.render(sess, format, **kwargs)
        headers = {}
        if download:
            ext = _EXT.get(format, "txt")
            fname = f"{sess.source}_{sess.short_id}.{ext}"
            headers["Content-Disposition"] = f'attachment; filename="{fname}"'
        return Response(content=body, media_type=_MEDIA.get(format, "text/plain"),
                        headers=headers)

    @app.get("/print/{source}/{session_id}")
    def print_view(
        source: str, session_id: str, reasoning: bool = True, tools: bool = True,
        math: str = "raw",
    ) -> "Response":
        """A print-friendly HTML page that auto-opens the print dialog.

        Used by the native-window 'print' action, which opens this URL in the
        user's real browser (where window.print() works)."""
        if math not in export.MATH_MODES:
            raise HTTPException(status_code=400, detail=f"bad math mode: {math}")
        sess = _store_for("all").load_session(session_id, source=source)
        if sess is None:
            raise HTTPException(status_code=404, detail="session not found")
        html = export.to_html(sess, include_reasoning=reasoning, include_tools=tools, math=math)
        # Inject an auto-print trigger before </body>.
        auto = "<script>window.addEventListener('load',()=>setTimeout(()=>window.print(),300));</script>"
        if "</body>" in html:
            html = html.replace("</body>", auto + "</body>", 1)
        else:
            html += auto
        return Response(content=html, media_type="text/html; charset=utf-8")

    @app.get("/api/stats")
    def api_stats(
        mode: str = "all", since: str | None = None, until: str | None = None
    ) -> dict[str, Any]:
        """Aggregate usage statistics: per-source breakdown plus overall totals.

        Honours the browsing `mode` (live / archive / all) so the stats page
        reflects the selected scope, and the same `since`/`until` window as the
        session list. Metadata-only (does not load message bodies), so it is
        cheap to compute on demand.
        """
        st = _store_for(mode).stats(since=_parse_dt(since), until=_parse_dt(until))

        def _src_row(u) -> dict[str, Any]:
            return {
                "source": u.source,
                "sessions": u.sessions,
                "messages": u.messages,
                "tokens_input": u.tokens_input,
                "tokens_output": u.tokens_output,
                "tokens_cache_read": u.tokens_cache_read,
                "tokens_cache_write": u.tokens_cache_write,
                "tokens_reasoning": u.tokens_reasoning,
                "cost": u.cost,
            }

        # Sort by total token volume (in + out + cache), busiest first.
        rows = sorted(
            (_src_row(u) for u in st.per_source_usage.values()),
            key=lambda r: (r["tokens_input"] + r["tokens_output"]
                           + r["tokens_cache_read"] + r["tokens_cache_write"]),
            reverse=True,
        )
        return {
            "sessions": st.sessions,
            "messages": st.total_messages,
            "per_source": rows,
            "totals": {
                "tokens_input": st.total_tokens_input,
                "tokens_output": st.total_tokens_output,
                "tokens_cache_read": st.total_tokens_cache_read,
                "tokens_cache_write": st.total_tokens_cache_write,
                "tokens_reasoning": st.total_tokens_reasoning,
                "cost": st.total_cost,
            },
            "oldest": st.oldest.isoformat() if st.oldest else None,
            "newest": st.newest.isoformat() if st.newest else None,
        }

    @app.get("/api/archive")
    def api_archive() -> dict[str, Any]:
        """Overview of the durable vault: path + per-source + orphan counts.

        Returns ``{"exists": false}`` when no vault has been created, so the
        frontend can show a "start archiving" prompt.
        """
        vault = _archive_mod.ArchiveStore(_vault_path)
        if not vault.exists():
            return {"exists": False, "path": str(_vault_path)}
        s = vault.stats()
        # Count stale sessions (still live but archived copy out of date) from
        # the deduped "all" view, so the landing can offer "update all stale".
        stale = sum(
            1 for x in _store_for("all").list_sessions()
            if (x.raw or {}).get("archive_status") == "stale"
        )
        return {
            "exists": True,
            "path": str(vault.path),
            "sessions": s["sessions"],
            "orphans": s["orphans"],
            "stale": stale,
            "per_source": s.get("per_source", {}),
            "bytes": vault.disk_usage(),
        }

    @app.get("/api/archive/verify")
    def api_archive_verify() -> dict[str, Any]:
        """Integrity check: counts of ok / missing / unreadable archived files."""
        vault = _archive_mod.ArchiveStore(_vault_path)
        if not vault.exists():
            return {"exists": False}
        v = vault.verify()
        return {"exists": True, "ok": len(v["ok"]),
                "missing": v["missing"], "unreadable": v["unreadable"]}

    @app.get("/api/archive/export")
    def api_archive_export():
        """Download the whole vault as a .zip backup (re-importable)."""
        import io
        import zipfile

        from starlette.responses import Response

        vault = _archive_mod.ArchiveStore(_vault_path)
        if not vault.exists():
            raise HTTPException(status_code=404, detail="no archive to export")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in vault.path.rglob("*"):
                if p.is_file():
                    zf.write(p, arcname=str(p.relative_to(vault.path)))
        _note_activity()
        return Response(
            content=buf.getvalue(), media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="scrollback-archive.zip"'},
        )

    _jobs = _JobRegistry()

    @app.post("/api/archive/sync")
    def api_archive_sync() -> dict[str, Any]:
        """Start a full incremental sync of live sessions into the vault.

        Returns a `job_id`; watch progress on the SSE events endpoint. Writes
        ONLY to the vault -- never to agent data. Single-flight: a concurrent
        request returns the running job.
        """
        vault = _archive_mod.ArchiveStore(_vault_path)

        def work(job: _SyncJob) -> None:
            _note_activity()
            job.result = vault.sync(_live_store, progress=job.on_progress)

        job = _jobs.start("all", work)
        return {"job_id": job.id, **job.snapshot()}

    @app.post("/api/archive/sync/{source}/{session_id}")
    def api_archive_sync_one(source: str, session_id: str) -> dict[str, Any]:
        """Archive/update a single live session. Writes ONLY to the vault."""
        vault = _archive_mod.ArchiveStore(_vault_path)

        def work(job: _SyncJob) -> None:
            _note_activity()
            job.total = 1
            outcome = vault.sync_one(_live_store, source, session_id)
            job.done = 1
            job.result = {"outcome": outcome}

        job = _jobs.start("one", work)
        return {"job_id": job.id, **job.snapshot()}

    @app.post("/api/archive/sync/stale")
    def api_archive_sync_stale() -> dict[str, Any]:
        """Update every archived session whose live copy has newer content."""
        vault = _archive_mod.ArchiveStore(_vault_path)

        def work(job: _SyncJob) -> None:
            _note_activity()
            stale_keys = [
                (s.source, s.id) for s in _store_for("all").list_sessions()
                if (s.raw or {}).get("archive_status") == "stale"
            ]
            job.result = vault.sync_many(_live_store, stale_keys, progress=job.on_progress)

        job = _jobs.start("all", work)
        return {"job_id": job.id, **job.snapshot()}

    @app.post("/api/archive/sync/batch")
    def api_archive_sync_batch(payload: dict = Body(...)) -> dict[str, Any]:
        """Archive/update a specific set of sessions (bulk action).

        Body: ``{"keys": [["opencode","ses_..."], ...]}`` -- the (source, id)
        pairs to archive (e.g. everything matching the current filter/search).
        """
        keys = [tuple(k) for k in payload.get("keys", []) if len(k) == 2]
        vault = _archive_mod.ArchiveStore(_vault_path)

        def work(job: _SyncJob) -> None:
            _note_activity()
            job.result = vault.sync_many(_live_store, keys, progress=job.on_progress)

        job = _jobs.start("all", work)
        return {"job_id": job.id, **job.snapshot()}

    @app.post("/api/archive/import")
    async def api_archive_import(request: "Request") -> dict[str, Any]:
        """Merge an uploaded vault .zip into the local vault (cross-machine).

        The .zip is sent as the raw request body (no multipart dependency).
        """
        import tempfile

        data = await request.body()
        if not data:
            raise HTTPException(status_code=400, detail="empty upload")
        tmp = Path(tempfile.mkdtemp(prefix="scrollback-upload-"))
        zip_path = tmp / "incoming.zip"
        zip_path.write_bytes(data)
        vault = _archive_mod.ArchiveStore(_vault_path)

        def work(job: _SyncJob) -> None:
            _note_activity()
            try:
                job.result = vault.import_from(zip_path, progress=job.on_progress)
            finally:
                import shutil
                shutil.rmtree(tmp, ignore_errors=True)

        job = _jobs.start("all", work)
        return {"job_id": job.id, **job.snapshot()}

    @app.get("/api/archive/jobs/{job_id}")
    def api_archive_job(job_id: str) -> dict[str, Any]:
        job = _jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown job")
        return job.snapshot()

    @app.get("/api/archive/jobs/{job_id}/events")
    def api_archive_job_events(job_id: str):
        """Server-Sent Events stream of a sync job's progress.

        Emits `{done,total,phase}` frames until the job finishes, then a final
        frame carrying the result summary. The sync itself continues
        server-side even if the client disconnects.
        """
        from starlette.responses import StreamingResponse

        job = _jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown job")

        def stream():
            import json as _json
            import time

            last = None
            while not job.finished.is_set():
                snap = job.snapshot()
                if snap != last:
                    yield f"data: {_json.dumps(snap)}\n\n"
                    last = snap
                time.sleep(0.15)
            _note_activity()
            yield f"data: {_json.dumps(job.snapshot())}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get("/api/health")
    def api_health() -> dict[str, Any]:
        # Live agent sources only (the archive reader is a mode, not a source).
        return {"status": "ok", "version": __version__,
                "sources": [s.name for s in _live_store.sources]}

    # -- static frontend ---------------------------------------------------

    if _STATIC_DIR.is_dir():
        # Serve the three app-owned files (index.html + style.css + app.js)
        # ourselves with `Cache-Control: no-cache`, so the native app's WebView
        # (WKWebView) MUST revalidate them every load instead of serving a stale
        # copy from its heuristic cache. index.html also gets the app version
        # stamped onto the CSS/JS URLs as a second cache-busting layer. Vendor
        # assets (which never change) stay on the normally-cached static mount.
        from fastapi.responses import HTMLResponse, Response

        _no_cache = {"Cache-Control": "no-cache, must-revalidate"}
        _index_html = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")
        _index_html = (
            _index_html
            .replace('href="/style.css"', f'href="/style.css?v={__version__}"')
            .replace('src="/app.js"', f'src="/app.js?v={__version__}"')
        )

        @app.get("/", include_in_schema=False)
        def index() -> "HTMLResponse":
            return HTMLResponse(_index_html, headers=_no_cache)

        @app.get("/style.css", include_in_schema=False)
        def style_css() -> "Response":
            return Response((_STATIC_DIR / "style.css").read_text(encoding="utf-8"),
                            media_type="text/css", headers=_no_cache)

        @app.get("/app.js", include_in_schema=False)
        def app_js() -> "Response":
            return Response((_STATIC_DIR / "app.js").read_text(encoding="utf-8"),
                            media_type="text/javascript", headers=_no_cache)

        app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")

    return app


_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", ""}


def _install_host_guard(app: "FastAPI", allowed_hosts: list[str] | None) -> None:
    """Reject requests whose Host header isn't an allowed hostname.

    Defends against DNS-rebinding: a malicious page can't point its own
    hostname at 127.0.0.1 and read local data, because the Host header would
    be that hostname, not a loopback name. The port portion is ignored (it
    can auto-change); only the hostname is checked.
    """
    if allowed_hosts is not None and not allowed_hosts:
        return  # explicitly disabled
    allow = set(_LOOPBACK_HOSTS)
    if allowed_hosts:
        allow.update(h.lower() for h in allowed_hosts)

    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import PlainTextResponse

    class _HostGuard(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            host = request.headers.get("host", "")
            # Strip the port; handle IPv6 [::1]:port form.
            hostname = host.rsplit(":", 1)[0] if ":" in host and not host.endswith("]") else host
            hostname = hostname.strip("[]").lower()
            if hostname not in allow:
                return PlainTextResponse("Forbidden: unexpected Host header", status_code=403)
            return await call_next(request)

    app.add_middleware(_HostGuard)


def _install_heartbeat_watchdog(
    app: "FastAPI", on_idle, idle_timeout: float, activity: dict | None = None
) -> None:
    """Auto-stop when the page stops sending heartbeats (window closed).

    The frontend POSTs /api/heartbeat on an interval. A background thread
    checks the last-seen time; if it exceeds `idle_timeout`, it calls
    `on_idle()` once. A grace period before the first heartbeat avoids
    shutting down during initial page load. `activity["bump"]` is wired so a
    running archive sync also counts as activity (won't be killed mid-sync).
    """
    import threading
    import time

    state = {"last": time.monotonic() + max(idle_timeout, 10.0), "fired": False}

    if activity is not None:
        activity["bump"] = lambda: state.__setitem__("last", time.monotonic())

    @app.post("/api/heartbeat")
    def heartbeat() -> dict[str, str]:
        state["last"] = time.monotonic()
        return {"status": "ok"}

    @app.get("/api/heartbeat-config")
    def heartbeat_config() -> dict[str, float]:
        # Tell the client how often to ping (a third of the timeout).
        return {"interval": max(idle_timeout / 3.0, 2.0), "enabled": 1.0}

    def watch() -> None:
        while not state["fired"]:
            time.sleep(1.0)
            if time.monotonic() - state["last"] > idle_timeout:
                state["fired"] = True
                on_idle()
                return

    threading.Thread(target=watch, daemon=True).start()
