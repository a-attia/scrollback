"""scrollback command-line interface.

Subcommands:
  sources                 list detected agents and where they read from
  list                    list sessions (newest first), with filters
  show <selector>         print a session transcript to the terminal
  search <query>          search across sessions
  export <selector>       render a session to markdown/json/html/text
  copy <selector>         copy a rendered session to the clipboard

Selectors accept a full id, a unique prefix, `source:id`, or `latest`.
All output is plain and pipe-friendly. Reads are strictly read-only.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

from . import __version__, clipboard, export, serverconfig
from .models import Session
from .sources import registry
from .store import Stats, Store


def _fmt_dt(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M") if dt else "?"


def _eprint(*a: object) -> None:
    print(*a, file=sys.stderr)


def _dbg(*a: object) -> None:
    """Diagnostic to stderr, only when SCROLLBACK_DEBUG is set.

    Used for best-effort, silently-degrading paths (e.g. the macOS About
    panel) so failures are diagnosable without spamming normal runs.
    """
    import os

    if os.environ.get("SCROLLBACK_DEBUG"):
        print(*a, file=sys.stderr)


def _nonneg_int(s: str) -> int:
    """argparse type: a non-negative integer (rejects negatives)."""
    try:
        v = int(s)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected an integer, got {s!r}") from exc
    if v < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0, got {v}")
    return v


def _positive_int(s: str) -> int:
    """argparse type: a positive integer (>= 1)."""
    v = _nonneg_int(s)
    if v < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {v}")
    return v


def _parse_date(s: str | None) -> datetime | None:
    """Parse a CLI date/datetime into an aware UTC datetime.

    Accepts YYYY-MM-DD or full ISO-8601. Naive values are treated as UTC.
    Raises argparse.ArgumentTypeError on bad input so the CLI reports it.
    """
    if not s:
        return None
    raw = s.strip()
    try:
        if len(raw) == 10:  # YYYY-MM-DD
            dt = datetime.strptime(raw, "%Y-%m-%d")
        else:
            iso = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
            dt = datetime.fromisoformat(iso)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid date {s!r}; use YYYY-MM-DD or ISO-8601"
        ) from exc
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _fmt_tokens(n: int | None) -> str:
    """Compact token count: 12345 -> '12.3k', 2100000 -> '2.1M'."""
    if n is None:
        return ""
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k"
    return f"{n / 1_000_000:.1f}M"


def _fmt_cost(c: float | None) -> str:
    return f"${c:.2f}" if c else ""


# -- subcommand implementations -------------------------------------------


def cmd_sources(args: argparse.Namespace) -> int:
    any_found = False
    for src in registry.all_sources():
        avail = src.is_available()
        any_found = any_found or avail
        loc = src.location()
        status = "available" if avail else "not found"
        print(f"{src.name:12} {status:12} {loc if loc else ''}")
    return 0 if any_found else 1


def cmd_doctor(args: argparse.Namespace) -> int:
    """Print a diagnostics summary: sources, index, optional features, env."""
    import platform

    from . import __version__, fts

    print(f"scrollback {__version__}")
    print(f"python {platform.python_version()}  ({platform.system()} {platform.machine()})")
    import sqlite3

    print(f"sqlite {sqlite3.sqlite_version}")
    print()

    print("sources:")
    store = Store()
    any_avail = False
    for src in registry.all_sources():
        avail = src.is_available()
        any_avail = any_avail or avail
        loc = src.location()
        if avail:
            try:
                n = len(list(src.list_sessions()))
            except Exception:
                n = "?"
            print(f"  {src.name:12} available   {n} sessions   {loc}")
        else:
            print(f"  {src.name:12} not found   (looked in default location)")
    if not any_avail:
        print("  (none detected -- set SCROLLBACK_OPENCODE_DB / SCROLLBACK_CLAUDE_DIR")
        print("   if your data lives outside the default locations)")
    print()

    print("optional features:")
    print(f"  full-text search (FTS5): {'yes' if fts.fts5_available() else 'no'}")
    index = fts.FtsIndex()
    if index.exists():
        try:
            s = index.stats()
            stale = "stale" if index.is_stale(store) else "fresh"
            print(f"  search index: built ({s['sessions']} sessions, {s['parts']} parts, {stale})")
        except sqlite3.Error:
            # A malformed/old index shouldn't crash the diagnostics command.
            print("  search index: present but unreadable (rebuild: 'scrollback index --clear')")
        print(f"                {index.path}")
    else:
        print("  search index: not built (run 'scrollback index' for faster search)")
    from . import archive

    vault = archive.ArchiveStore()
    if vault.exists():
        vs = vault.stats()
        print(f"  archive vault: {vs['sessions']} sessions "
              f"({vs['orphans']} no longer live)")
        print(f"                 {vault.path}")
    else:
        print("  archive vault: none (run 'scrollback archive' to keep sessions forever)")
    print(f"  native window (pywebview): {'yes' if _pywebview_available() else 'no'}")
    print(f"  rich terminal output: {'yes' if _rich_available() else 'no'}")
    print(f"  web app (fastapi/uvicorn): {'yes' if _web_available() else 'no'}")
    print()

    # -- on-disk footprint: everything scrollback created, so nothing is a
    #    surprise. Agent data is never listed (scrollback only reads it).
    from . import launcher_install

    print("on-disk footprint (scrollback's own files; your agent data is never here):")
    entries = launcher_install.footprint()
    if not entries:
        print("  (nothing created yet)")
    else:
        _TIER = {"disposable": "disposable", "artifact": "installed", "durable": "DURABLE"}
        for e in entries:
            size = launcher_install._dir_size(e.path)
            print(f"  [{_TIER[e.tier]:10}] {_fmt_bytes(size):>8}  {e.path}")
            print(f"               {e.description}")
        print("  disposable + installed are removed by 'scrollback uninstall';")
        print("  DURABLE (your archive) is kept unless you pass --purge-archive.")

    return 0 if any_avail else 1


def _fmt_bytes(n: int) -> str:
    if not n:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    f = float(n)
    while f >= 1024 and i < len(units) - 1:
        f /= 1024
        i += 1
    return f"{f:.1f} {units[i]}" if i else f"{int(f)} B"


def _rich_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("rich") is not None


def _web_available() -> bool:
    import importlib.util

    return (
        importlib.util.find_spec("fastapi") is not None
        and importlib.util.find_spec("uvicorn") is not None
    )


def cmd_resume(args: argparse.Namespace) -> int:
    store = _make_store(args)
    src, full = store._resolve(args.selector, getattr(args, "source", None))
    if src is None or full is None:
        _eprint(f"session not found: {args.selector}")
        return 1
    sess = src.load_session_meta(full)
    if sess is None:
        _eprint(f"session not found: {args.selector}")
        return 1
    cmd = src.resume_command(sess)
    if not cmd:
        _eprint(f"{src.name} has no by-id resume command; open the project and "
                "start the agent there:")
        if sess.directory:
            _eprint(f"  cd {sess.directory!r} && {src.name}")
        return 1
    if args.copy:
        if clipboard.copy(cmd):
            _eprint("resume command copied to clipboard")
        else:
            _eprint("clipboard unavailable; printing instead")
            print(cmd)
    else:
        print(cmd)
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    store = _make_store(args)
    if not store.sources:
        _no_sessions_help(store)
        return 1
    st = store.stats(since=args.since, until=args.until)
    if args.json:
        import json

        print(json.dumps({
            "sessions": st.sessions,
            "per_source": st.per_source,
            "per_source_usage": {
                src: {
                    "sessions": u.sessions,
                    "messages": u.messages,
                    "tokens_input": u.tokens_input,
                    "tokens_output": u.tokens_output,
                    "tokens_cache_read": u.tokens_cache_read,
                    "tokens_cache_write": u.tokens_cache_write,
                    "tokens_reasoning": u.tokens_reasoning,
                    "cost": u.cost,
                }
                for src, u in st.per_source_usage.items()
            },
            "total_messages": st.total_messages,
            "total_tokens_input": st.total_tokens_input,
            "total_tokens_output": st.total_tokens_output,
            "total_tokens_cache_read": st.total_tokens_cache_read,
            "total_tokens_cache_write": st.total_tokens_cache_write,
            "total_tokens_reasoning": st.total_tokens_reasoning,
            "total_cost": st.total_cost,
            "oldest": st.oldest.isoformat() if st.oldest else None,
            "newest": st.newest.isoformat() if st.newest else None,
            "top_projects": sorted(
                st.per_project.items(), key=lambda kv: kv[1], reverse=True
            )[:args.top],
        }, indent=2, ensure_ascii=False))
        return 0

    span = ""
    if st.oldest and st.newest:
        span = f"  ({_fmt_dt(st.oldest)} -> {_fmt_dt(st.newest)})"
    print(f"sessions: {st.sessions}{span}")
    print(f"messages: {st.total_messages}")
    if st.total_tokens_input or st.total_tokens_output:
        print(f"tokens:   {_fmt_tokens(st.total_tokens_input)} in / "
              f"{_fmt_tokens(st.total_tokens_output)} out")
    if st.total_tokens_cache_read or st.total_tokens_cache_write:
        print(f"cache:    {_fmt_tokens(st.total_tokens_cache_read)} read / "
              f"{_fmt_tokens(st.total_tokens_cache_write)} write")
    if st.total_tokens_reasoning:
        print(f"reasoning:{_fmt_tokens(st.total_tokens_reasoning)} tokens")
    if st.total_cost:
        print(f"cost:     ${st.total_cost:.2f}")
    print()
    _print_usage_by_tool(st)
    if st.per_project:
        print()
        print(f"top {args.top} projects:")
        top = sorted(st.per_project.items(), key=lambda kv: kv[1], reverse=True)[:args.top]
        for path, count in top:
            base = path.rstrip("/").split("/")[-1] or path
            print(f"  {count:>5}  {base}")
    return 0


def _print_usage_by_tool(st: Stats) -> None:
    """Print a per-tool usage table (sessions/messages/tokens/cost) + totals."""
    rows = sorted(
        st.per_source_usage.values(),
        key=lambda u: (u.tokens_input + u.tokens_output
                       + u.tokens_cache_read + u.tokens_cache_write),
        reverse=True,
    )
    header = ("tool", "sess", "msgs", "in", "out", "cache r", "cache w", "cost")
    widths = (11, 6, 7, 8, 8, 9, 9, 9)

    def fmt_row(cells: tuple) -> str:
        out = []
        for i, c in enumerate(cells):
            out.append(f"{c:<{widths[i]}}" if i == 0 else f"{c:>{widths[i]}}")
        return "  ".join(out)

    def cost_str(c) -> str:
        return "-" if c is None else f"${c:.2f}"

    print("usage by tool:")
    print("  " + fmt_row(header))
    for u in rows:
        print("  " + fmt_row((
            u.source, str(u.sessions), _fmt_tokens(u.messages),
            _fmt_tokens(u.tokens_input), _fmt_tokens(u.tokens_output),
            _fmt_tokens(u.tokens_cache_read), _fmt_tokens(u.tokens_cache_write),
            cost_str(u.cost),
        )))
    # Totals row.
    print("  " + fmt_row((
        "all", str(st.sessions), _fmt_tokens(st.total_messages),
        _fmt_tokens(st.total_tokens_input), _fmt_tokens(st.total_tokens_output),
        _fmt_tokens(st.total_tokens_cache_read), _fmt_tokens(st.total_tokens_cache_write),
        cost_str(st.total_cost or None),
    )))


def cmd_index(args: argparse.Namespace) -> int:
    from . import fts

    index = fts.FtsIndex()
    if args.clear:
        if index.path.exists():
            index.path.unlink()
            _eprint(f"removed index {index.path}")
        else:
            _eprint("no index to remove")
        return 0
    if args.stats:
        if not index.exists():
            _eprint("no index built yet; run 'scrollback index' to build one")
            return 1
        s = index.stats()
        print(f"index: {index.path}")
        print(f"sessions: {s['sessions']}   parts: {s['parts']}")
        return 0
    # Build / update.
    if not fts.fts5_available():
        _eprint(
            "full-text search needs SQLite FTS5, which this Python's SQLite "
            "was not built with. Search still works without an index (lexical "
            "scan); no action needed."
        )
        return 1
    store = Store()
    if not store.sources:
        _eprint("no sources available to index")
        return 1
    _eprint(f"building index at {index.path} ...")

    def progress(done: int, total: int) -> None:
        if done == total or done % 25 == 0:
            _eprint(f"  {done}/{total} sessions", )

    stats = index.sync(store, progress=progress)
    _eprint(
        f"done: +{stats['added']} added, {stats['updated']} updated, "
        f"{stats['removed']} removed, {stats['unchanged']} unchanged"
    )
    return 0


def cmd_archive(args: argparse.Namespace) -> int:
    from pathlib import Path

    from . import archive

    dest = Path(args.dest).expanduser() if args.dest else archive.default_archive_path()
    store = archive.ArchiveStore(dest)

    if args.stats:
        if not store.exists():
            _eprint(f"no archive at {store.path}; run 'scrollback archive' to create one")
            return 1
        s = store.stats()
        print(f"archive: {store.path}")
        print(f"sessions: {s['sessions']}   no longer live: {s['orphans']}")
        for src, n in sorted(s.get("per_source", {}).items()):
            print(f"  {src:12} {n}")
        print()
        print("layout (durable, user-owned; survives 'uninstall'):")
        print(f"  {store.path}/")
        print("  ├── manifest.sqlite      index: (source,id) -> signature, path")
        print("  └── sessions/<source>/<id>.json   one lossless JSON per session")
        print("back up or move to another machine with:")
        print("  scrollback archive --export <dest>            (copy the vault)")
        print("  scrollback archive --export bundle.zip        (zip it)")
        return 0

    if args.verify:
        if not store.exists():
            _eprint(f"no archive at {store.path}; nothing to verify")
            return 1
        v = store.verify()
        print(f"archive: {store.path}")
        print(f"ok: {len(v['ok'])}   missing: {len(v['missing'])}   "
              f"unreadable: {len(v['unreadable'])}")
        for label in v["missing"]:
            _eprint(f"  missing file: {label}")
        for label in v["unreadable"]:
            _eprint(f"  unreadable:   {label}")
        return 0 if not v["missing"] and not v["unreadable"] else 1

    if args.export:
        if not store.exists():
            _eprint(f"no archive at {store.path}; nothing to export")
            return 1
        fmt = args.format
        try:
            r = store.export_to(args.export, fmt=fmt, doc_format=args.doc_format)
        except FileExistsError as exc:
            _eprint(str(exc))
            return 1
        if fmt == "vault":
            _eprint(f"exported {r['sessions']} sessions -> {r['dest']}")
            _eprint("this is a full, re-importable copy of your vault. To use it:")
            _eprint(f"    SCROLLBACK_ARCHIVE={r['dest']} scrollback archive --stats")
        else:
            _eprint(f"rendered {r['sessions']} sessions ({args.doc_format}) -> {r['dest']}")
            _eprint("note: rendered transcripts are for reading/sharing, not a backup "
                    "(cannot be re-imported as a vault).")
        return 0

    if getattr(args, "import_from", None):
        _eprint(f"merging {args.import_from} into {store.path} ...")

        def iprogress(done: int, total: int) -> None:
            if done == total or done % 25 == 0:
                _eprint(f"  {done}/{total} sessions")

        try:
            r = store.import_from(args.import_from, progress=iprogress)
        except FileNotFoundError as exc:
            _eprint(str(exc))
            return 1
        msg = (f"done: +{r['added']} added, {r['updated']} updated, "
               f"{r['unchanged']} unchanged")
        if r["kept_shrunk"]:
            msg += f", {r['kept_shrunk']} kept (skipped shrunk)"
        _eprint(msg)
        return 0

    live = _make_store(args)
    if not live.sources:
        _eprint("no sources available to archive")
        return 1
    _eprint(f"archiving to {store.path} ...")

    def progress(done: int, total: int) -> None:
        if done == total or done % 25 == 0:
            _eprint(f"  {done}/{total} sessions")

    r = store.sync(live, progress=progress)
    msg = (
        f"done: +{r['added']} added, {r['updated']} updated, "
        f"{r['unchanged']} unchanged, {r['kept_orphan']} kept (no longer live)"
    )
    if r["kept_shrunk"]:
        msg += f", {r['kept_shrunk']} kept (skipped shrunk read)"
    _eprint(msg)
    return 0


class _BadSource(Exception):
    """Raised when an unknown --source name is given."""


def _make_store(args: argparse.Namespace) -> Store:
    store = Store()
    name = getattr(args, "source", None)
    if name:
        known = {s.name for s in registry.all_sources()} | {"archive"}
        if name not in known:
            raise _BadSource(
                f"unknown source {name!r}; available: {', '.join(sorted(known))}"
            )
        # "archive" is not a live adapter; with_sources([]) drops live sources
        # and the archive reader is added below, giving an archive-only view.
        store = store.with_sources([] if name == "archive" else [name])
    # Make archived sessions -- including ones the agent has deleted -- a
    # first-class readable source when a vault exists (no-op otherwise).
    # Deduped live-wins, so this never changes what a live session shows.
    from . import archive

    store = store.with_archive(archive.default_archive_path())
    return store


def cmd_list(args: argparse.Namespace) -> int:
    store = _make_store(args)
    offset = args.offset
    if args.page and args.page > 1:
        offset = (args.page - 1) * args.limit
    sessions = store.list_sessions(
        directory=args.dir,
        query=args.query,
        since=args.since,
        until=args.until,
        limit=args.limit,
        offset=offset,
        fold_subagents=not args.no_fold,
    )
    if not sessions:
        _no_sessions_help(store)
        return 1
    if args.json:
        import json

        def row(s: Session) -> dict[str, object]:
            return {
                "id": s.id,
                "source": s.source,
                "title": s.title,
                "directory": s.directory,
                "updated": s.updated.isoformat() if s.updated else None,
                "model": s.model,
                "agent": s.agent,
                "messages": s.message_count,
                "cost": s.cost,
                "tokens_input": s.tokens_input,
                "tokens_output": s.tokens_output,
                "tokens_cache_read": s.tokens_cache_read,
                "tokens_cache_write": s.tokens_cache_write,
                "tokens_reasoning": s.tokens_reasoning,
                "parent_id": s.parent_id,
                "children": [row(c) for c in s.children],
            }

        print(json.dumps([row(s) for s in sessions], indent=2, ensure_ascii=False))
        return 0

    from . import termrender

    if termrender.available(force=_color_force(args)):
        termrender.render_list(sessions, show_usage=args.usage)
    else:
        if args.usage:
            _eprint(
                f"{'source':10} {'id':13} {'updated':16} {'msgs':>9} "
                f"{'cost':>7} {'tok in/out':>14}  title"
            )
        _print_list(sessions, show_usage=args.usage)
    if offset:
        _eprint(f"(offset {offset})")
    return 0


def _color_force(args: argparse.Namespace) -> bool | None:
    """Translate --plain into a force flag for termrender.available()."""
    if getattr(args, "plain", False):
        return False
    return None


def _print_list(sessions: list[Session], *, show_usage: bool, indent: str = "") -> None:
    for s in sessions:
        msgs = f"{s.message_count:>4}" if s.message_count is not None else "   ?"
        usage = ""
        if show_usage:
            toks = f"{_fmt_tokens(s.tokens_input)}/{_fmt_tokens(s.tokens_output)}"
            cost = _fmt_cost(s.cost)
            usage = f" {cost:>7} {toks:>14}"
        marker = "\u2514 " if indent else ""
        print(
            f"{indent}{marker}{s.source:10} {s.short_id:13} {_fmt_dt(s.updated):16} "
            f"{msgs} msgs{usage}  {s.title}"
        )
        if s.children:
            _print_list(list(s.children), show_usage=show_usage, indent=indent + "  ")


def _resolve(store: Store, args: argparse.Namespace) -> Session | None:
    return store.load_session(args.selector, source=getattr(args, "source", None))


def cmd_show(args: argparse.Namespace) -> int:
    store = _make_store(args)
    sess = _resolve(store, args)
    if sess is None:
        _eprint(f"session not found: {args.selector}")
        return 1
    from . import termrender

    if termrender.available(force=_color_force(args)):
        termrender.render_transcript(
            sess,
            include_reasoning=args.reasoning,
            include_tools=not args.no_tools,
            markdown=not args.no_markdown,
        )
        return 0
    text = export.to_text(
        sess,
        include_reasoning=args.reasoning,
        include_tools=not args.no_tools,
    )
    print(text)
    return 0


def _no_sessions_help(store: Store) -> None:
    """Explain why a list/search is empty: no sources vs. just no matches."""
    if not store.sources:
        _eprint("no AI-agent sessions found -- no supported sources detected.")
        _eprint("scrollback reads, by default:")
        _eprint("  opencode    ~/.local/share/opencode/opencode.db")
        _eprint("  claudecode  ~/.claude/projects/")
        _eprint("Override with SCROLLBACK_OPENCODE_DB / SCROLLBACK_CLAUDE_DIR.")
        _eprint("Run 'scrollback doctor' to see what was detected.")
    else:
        _eprint("no sessions matched.")


def _maybe_warn_stale_index(store: Store) -> None:
    """Hint (once) that the FTS index is stale, so results may miss new
    sessions. Cheap mtime check; no-op when there's no index."""
    try:
        from . import fts

        index = fts.FtsIndex()
        if index.exists() and index.is_stale(store):
            _eprint("note: search index looks out of date; run 'scrollback index' to refresh")
    except Exception:  # never let a hint break search
        pass


def cmd_search(args: argparse.Namespace) -> int:
    store = _make_store(args)
    hits = list(
        store.search(
            args.query,
            directory=args.dir,
            since=args.since,
            until=args.until,
            limit=args.limit,
        )
    )
    _maybe_warn_stale_index(store)
    if not hits:
        _eprint("no matches")
        return 1
    if args.json:
        import json

        rows = [
            {
                "source": h.session.source,
                "session_id": h.session.id,
                "title": h.session.title,
                "message_id": h.message.id,
                "role": h.message.role,
                "part_type": h.part.type,
                "snippet": h.snippet,
            }
            for h in hits
        ]
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    from . import termrender

    if termrender.available(force=_color_force(args)):
        termrender.render_search(hits, args.query)
    else:
        for h in hits:
            print(f"{h.session.source}:{h.session.short_id} [{h.message.role}] {h.snippet}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    store = _make_store(args)
    sess = _resolve(store, args)
    if sess is None:
        _eprint(f"session not found: {args.selector}")
        return 1
    kwargs = _render_kwargs(args.format, args)
    rendered = export.render(sess, args.format, **kwargs)
    if args.output:
        try:
            with open(args.output, "w", encoding="utf-8") as fh:
                fh.write(rendered)
        except OSError as exc:
            _eprint(f"could not write {args.output}: {exc}")
            return 1
        _eprint(f"wrote {args.output}")
    else:
        print(rendered)
    return 0


def cmd_copy(args: argparse.Namespace) -> int:
    store = _make_store(args)
    sess = _resolve(store, args)
    if sess is None:
        _eprint(f"session not found: {args.selector}")
        return 1
    kwargs = _render_kwargs(args.format, args)
    rendered = export.render(sess, args.format, **kwargs)
    if clipboard.copy(rendered):
        _eprint(f"copied {len(rendered)} chars ({args.format}) to clipboard")
        return 0
    _eprint("clipboard unavailable; printing instead")
    print(rendered)
    return 1


def _render_kwargs(fmt: str, args: argparse.Namespace) -> dict[str, object]:
    if fmt == "json":
        return {}
    return {
        "include_reasoning": args.reasoning,
        "include_tools": not args.no_tools,
        "math": getattr(args, "math", "raw"),
    }


def cmd_web(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ModuleNotFoundError:
        _eprint(
            "the web app needs fastapi/uvicorn, which normally ship with scrollback.\n"
            "if they're missing (e.g. a broken environment), reinstall with:\n"
            "    pip install --force-reinstall scrollback\n"
            "or install the packages directly:\n"
            "    pip install fastapi uvicorn"
        )
        return 1
    from .web.app import create_app

    # Resolve the actual port to bind: honour the requested one, but fall back
    # to the next free port if it's taken (unless --strict-port). This keeps a
    # single source of truth and avoids "started on a different port than the
    # URL we opened" bugs -- the resolved port is used everywhere below.
    try:
        port = serverconfig.resolve_port(args.host, args.port, strict=args.strict_port)
    except OSError as exc:
        _eprint(str(exc))
        return 1
    if port != args.port:
        _eprint(f"port {args.port} busy; using {port} instead")
    args.port = port  # so downstream (app-window mode) sees the real port

    url = f"http://{args.host}:{port}"

    # If binding to a non-loopback address, warn loudly: the API is
    # unauthenticated and would expose all local AI history to the network.
    # Add the chosen host to the Host-guard allowlist so it can be reached.
    loopback = {"127.0.0.1", "localhost", "::1", "0.0.0.0", ""}
    allowed_hosts = None
    if args.host not in loopback:
        _eprint(f"WARNING: binding to non-loopback host {args.host!r}; the read-only "
                "API will be reachable from the network with no authentication.")
        allowed_hosts = [args.host]
    elif args.host == "0.0.0.0":
        _eprint("WARNING: binding to 0.0.0.0 exposes the API on all interfaces.")
        allowed_hosts = []  # can't know the external hostname; disable host guard

    # Desktop "app window" mode: a true native window via pywebview. Closing
    # the window quits the process -> server stops -> port is freed, and there
    # is no terminal. If pywebview isn't available, fall back to a browser
    # window (with heartbeat auto-shutdown) instead of failing.
    if getattr(args, "app", False):
        if _pywebview_available():
            return _run_app_window(create_app(allowed_hosts=allowed_hosts), args, url)
        _eprint("native window unavailable (pywebview not installed/usable); "
                "opening a browser window with auto-shutdown instead")
        args.window = True
        args.auto_shutdown = True  # browser fallback: stop server when window closes

    # Optional heartbeat auto-shutdown: stop the server shortly after the
    # browser window/tab is closed (so the port is freed without Ctrl-C).
    server_holder: dict[str, object] = {}

    def _on_idle() -> None:
        srv = server_holder.get("server")
        if srv is not None:
            srv.should_exit = True

    if getattr(args, "auto_shutdown", False):
        app = create_app(on_idle=_on_idle, idle_timeout=10.0, allowed_hosts=allowed_hosts)
    else:
        app = create_app(allowed_hosts=allowed_hosts)

    _eprint(f"scrollback web -> {url}  (read-only; Ctrl-C to stop)")
    _maybe_launcher_hint(args)
    if not args.no_browser:
        import threading

        from . import webopen

        # Open after a short delay so the server is accepting connections.
        # `--window` asks for a standalone window; default opens a tab.
        opener = webopen.open_window if args.window else _open_tab
        threading.Timer(0.8, lambda: opener(url)).start()

    _background_index_refresh()

    server = uvicorn.Server(
        uvicorn.Config(app, host=args.host, port=port, log_level="warning")
    )
    server_holder["server"] = server
    server.run()
    return 0


def _maybe_launcher_hint(args: argparse.Namespace) -> None:
    """Print a one-time tip about the double-clickable launcher.

    Shown only on an interactive terminal and only for the plain `web`
    command -- not the native `--app` / `--window` modes (already launched
    that way) -- so it never gets in the way of scripted or app usage.
    """
    import sys

    if getattr(args, "app", False) or getattr(args, "window", False):
        return
    if not sys.stderr.isatty():
        return
    _eprint(
        "tip: 'scrollback install-launcher --app-bundle' adds a "
        "double-clickable app so you can skip the terminal next time."
    )


def _background_index_refresh() -> None:
    """If an FTS index exists and is stale, refresh it in a daemon thread.

    Opt-in by virtue of an index existing; runs off the request path so the
    UI is usable immediately and shutdown isn't blocked.
    """
    import threading

    def work() -> None:
        try:
            from . import fts

            index = fts.FtsIndex()
            store = Store()
            if index.exists() and index.is_stale(store):
                index.sync(store)
        except Exception:
            pass  # best-effort; search still works via the existing index

    threading.Thread(target=work, daemon=True).start()


def _pywebview_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("webview") is not None


def _app_icon_path() -> str | None:
    """Extract the bundled PNG window icon to a temp file and return its path.

    Used cross-platform: pywebview's `icon` wants a filesystem path (Windows
    taskbar, GTK/Qt window icon, macOS Dock). Our icon ships as package data,
    so we materialize it once per run.
    """
    import tempfile
    from importlib import resources

    try:
        data = resources.files("scrollback.assets").joinpath("icon-256.png").read_bytes()
    except (OSError, ModuleNotFoundError, FileNotFoundError):
        return None
    path = os.path.join(tempfile.gettempdir(), "scrollback-icon.png")
    try:
        with open(path, "wb") as fh:
            fh.write(data)
    except OSError:
        return None
    return path


def _brand_macos_app() -> None:
    """Brand the macOS app: menu name 'scrollback', a rich standard About
    panel (version + description), and the Dock icon.

    The menu-bar name and the standard About panel both read from the running
    process's bundle info dict. When we run unbundled (or after the .app
    runner exec's python), that's 'Python' with an empty About. We patch the
    main bundle's info dict via PyObjC (already a pywebview dep on macOS) so
    pywebview's *default* app menu -- the one it always creates -- gets the
    right name and a useful About, instead of adding a second custom menu.
    """
    if sys.platform != "darwin":
        return
    from . import __version__

    try:
        from Foundation import NSBundle  # type: ignore

        bundle = NSBundle.mainBundle()
        info = bundle.localizedInfoDictionary() or bundle.infoDictionary()
        if info is not None:
            info["CFBundleName"] = "scrollback"
            info["CFBundleDisplayName"] = "scrollback"
            # Fields the standard About panel reads:
            info["CFBundleShortVersionString"] = __version__
            info["CFBundleVersion"] = __version__
            # Shown as the small grey text in the standard About panel. Include
            # the repo URL here so it is ALWAYS visible, independent of whether
            # the clickable-link menu rewire (below) succeeds on this pywebview
            # / macOS build.
            info["NSHumanReadableCopyright"] = (
                "Navigate, search, copy, and export your AI coding-agent "
                "sessions. Local-first and read-only.\n"
                "github.com/a-attia/scrollback"
            )
    except Exception:
        pass  # best-effort cosmetic; never fail the launch
    # Dock icon (independent of the menu name).
    icon = _app_icon_path()
    if icon:
        try:
            from AppKit import NSApplication, NSImage  # type: ignore

            img = NSImage.alloc().initByReferencingFile_(icon)
            if img is not None:
                NSApplication.sharedApplication().setApplicationIconImage_(img)
        except Exception:
            pass


# Keep a reference so the Objective-C handler isn't garbage-collected while the
# menu item points at it.
_about_handler = None


def _install_macos_about_link() -> None:
    """Re-point the standard 'About' menu item to a panel that includes a
    clickable link to the project repo.

    Runs after the Cocoa menu exists (via webview.start(func=...)). Replaces
    the About item's action with one that calls
    orderFrontStandardAboutPanelWithOptions: and passes a Credits attributed
    string containing a real hyperlink.
    """
    global _about_handler
    if sys.platform != "darwin":
        return
    try:
        from AppKit import (  # type: ignore
            NSApplication,
            NSAttributedString,
            NSFont,
            NSFontAttributeName,
        )
        from Foundation import NSObject, NSURL  # type: ignore

        from . import __version__

        repo = "https://github.com/a-attia/scrollback"
        credits = NSAttributedString.alloc().initWithString_attributes_(
            "Navigate, search, copy, and export your AI coding-agent sessions.\n"
            "Local-first and read-only.\n\nRepository:  ",
            {NSFontAttributeName: NSFont.systemFontOfSize_(11)},
        )
        link = NSAttributedString.alloc().initWithString_attributes_(
            "github.com/a-attia/scrollback",
            {
                "NSLink": NSURL.URLWithString_(repo),
                NSFontAttributeName: NSFont.systemFontOfSize_(11),
            },
        )
        full = credits.mutableCopy()
        full.appendAttributedString_(link)

        class _AboutHandler(NSObject):
            def showAbout_(self, _sender):
                opts = {
                    "Credits": full,
                    "ApplicationName": "scrollback",
                    "Version": __version__,
                    "ApplicationVersion": __version__,
                }
                NSApplication.sharedApplication().orderFrontStandardAboutPanelWithOptions_(opts)

        _about_handler = _AboutHandler.alloc().init()

        # Find the standard About item in the app menu (first menu) and rewire
        # it to our rich panel (with the clickable repo link). If no such item
        # exists (some pywebview builds don't add one), the plain panel still
        # shows the repo URL via NSHumanReadableCopyright set in _brand_macos_app.
        app = NSApplication.sharedApplication()
        main_menu = app.mainMenu()
        if main_menu is None or main_menu.numberOfItems() == 0:
            return
        app_menu = main_menu.itemAtIndex_(0).submenu()
        rewired = False
        for i in range(app_menu.numberOfItems()):
            item = app_menu.itemAtIndex_(i)
            action = item.action()
            title = str(item.title() or "")
            if (action is not None and str(action) == "orderFrontStandardAboutPanel:") \
                    or title.startswith("About"):
                item.setTarget_(_about_handler)
                item.setAction_(b"showAbout:")
                rewired = True
                break
        if not rewired:
            _dbg("about-link: no standard About menu item found to rewire")
    except Exception as exc:  # best-effort; the plain About still works
        _dbg(f"about-link: {exc!r}")


def _open_tab(url: str) -> str:
    import webbrowser

    return "tab" if webbrowser.open(url) else "failed"


def cmd_install_launcher(args: argparse.Namespace) -> int:
    from . import launcher_install

    dest = None
    if args.dest:
        import pathlib

        dest = pathlib.Path(args.dest).expanduser()
    try:
        created = launcher_install.install(
            dest, desktop=args.desktop, app_bundle=args.app_bundle
        )
    except OSError as exc:
        _eprint(f"could not install launcher: {exc}")
        return 1
    if not created:
        _eprint("nothing was installed")
        return 1
    _eprint("installed launcher(s):")
    for p in created:
        _eprint(f"  {p}")
    if sys.platform == "darwin":
        made_app = any(p.name.endswith(".app") for p in created)
        made_cmd = any(p.name.endswith(".command") for p in created)
        if made_cmd:
            _eprint("tip: double-click it (first time: right-click -> Open).")
        if made_cmd and not made_app:
            _eprint("     for an app icon in ~/Applications, add --app-bundle")
    return 0


def _detect_install_tool() -> str:
    """Best-effort guess of how scrollback was installed, for the hint.

    Returns the command the user should run to remove the package itself.
    We never run it: a process cannot reliably uninstall the package it is
    executing from, and the right tool (pip / pipx / conda) depends on how
    it was installed.
    """
    exe = (sys.executable or "").replace("\\", "/")
    if "/pipx/" in exe or "/.local/pipx/" in exe:
        return "pipx uninstall scrollback"
    return "pip uninstall scrollback"


def cmd_uninstall(args: argparse.Namespace) -> int:
    """Remove scrollback-created artifacts; explain how to remove the package.

    Removes only files scrollback itself created (launchers, the macOS .app,
    the optional search index, the launcher log). It never touches your agent
    data, and it never tries to uninstall the Python package -- that is left
    to pip/pipx, with the exact command printed at the end.
    """
    from . import archive, launcher_install

    entries = launcher_install.footprint()
    # Remove disposable (cache/index/browser) + installed artifacts by default.
    # The durable vault is user data: kept unless --purge-archive.
    removable = [e for e in entries if e.tier in ("disposable", "artifact")]
    vault_entry = next((e for e in entries if e.tier == "durable"), None)
    purge_vault = bool(getattr(args, "purge_archive", False)) and vault_entry is not None

    if vault_entry is not None and not purge_vault:
        _eprint(f"keeping your durable archive vault ({vault_entry.path}).")
        _eprint("  it holds sessions you chose to keep; pass --purge-archive to remove it.")

    if not removable and not purge_vault:
        _eprint("no scrollback-created files to remove.")
        _eprint(f"to remove the package itself, run:\n    {_detect_install_tool()}")
        return 0

    label = "would remove" if args.dry_run else "about to remove"
    _eprint(f"{label}:")
    for e in removable:
        _eprint(f"  {e.path}   ({e.description})")

    targets = [e.path for e in removable]

    if purge_vault:
        # Extra caution for durable data: state the loss, suggest a backup.
        vault = archive.ArchiveStore()
        n = vault.stats().get("sessions", 0) if vault.exists() else 0
        _eprint("")
        _eprint(f"  {vault_entry.path}")
        _eprint(f"  \u26a0 DURABLE ARCHIVE -- {n} kept session(s) will be PERMANENTLY deleted.")
        _eprint("    Back up first with:  scrollback archive --export <dest>")
        targets.append(vault_entry.path)

    if args.dry_run:
        return 0

    if not args.yes:
        try:
            reply = input("remove these? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            reply = ""
        if reply not in ("y", "yes"):
            _eprint("aborted; nothing removed.")
            return 1

    # Purging the vault is irreversible: require typing the word, even with -y.
    if purge_vault:
        try:
            confirm = input(
                "type 'delete archive' to confirm permanent deletion of your vault: "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            confirm = ""
        if confirm != "delete archive":
            _eprint("archive NOT deleted (confirmation did not match).")
            targets = [p for p in targets if p != vault_entry.path]

    # Remove deepest paths first so a child (e.g. the index / browser profile)
    # is gone before its parent cache dir; skip anything already removed.
    removed, failed = 0, 0
    for p in sorted(targets, key=lambda x: len(str(x)), reverse=True):
        if not p.exists():
            continue
        try:
            launcher_install.remove_path(p)
            _eprint(f"removed {p}")
            removed += 1
        except OSError as exc:
            _eprint(f"could not remove {p}: {exc}")
            failed += 1

    _eprint(f"\nremoved {removed} item(s)" + (f", {failed} failed" if failed else ""))
    _eprint(f"to remove the package itself, run:\n    {_detect_install_tool()}")
    return 1 if failed else 0


class _AppBridge:
    """JS<->Python API exposed to the pywebview window.

    In a native webview the browser's own download/print plumbing isn't
    available, so the frontend calls these methods (via window.pywebview.api)
    to save a file through a native dialog and to print via the user's real
    browser. Each method returns a small status string the JS can toast.
    """

    def __init__(self, url: str) -> None:
        self._url = url
        self.window = None  # set after the window is created

    def is_native(self) -> bool:
        return True

    def save_file(self, suggested_name: str, content: str) -> str:
        """Show a native Save dialog and write `content` to the chosen path."""
        import webview

        try:
            result = self.window.create_file_dialog(
                webview.SAVE_DIALOG, save_filename=suggested_name
            )
        except Exception as exc:  # pragma: no cover - GUI path
            return f"error: {exc}"
        if not result:
            return "cancelled"
        dest = result if isinstance(result, str) else result[0]
        try:
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write(content)
        except OSError as exc:  # pragma: no cover - GUI path
            return f"error: {exc}"
        return f"saved: {dest}"

    def open_external(self, path_and_query: str) -> str:
        """Open a URL on this server in the user's real browser (for printing,
        which the native webview can't do reliably)."""
        from . import webopen

        full = self._url + path_and_query
        webopen.open_window(full)
        return "opened"

    def open_link(self, url: str) -> str:
        """Open an absolute external URL (e.g. the repo) in the real browser.

        The native webview traps `target="_blank"` in an in-app window, so the
        frontend routes external links through here. Restricted to http(s) so
        the bridge can't be coaxed into opening arbitrary schemes.
        """
        import webbrowser

        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            return "rejected"
        webbrowser.open(url)
        return "opened"


def _run_app_window(app: object, args: argparse.Namespace, url: str) -> int:
    try:
        import webview  # pywebview
    except ModuleNotFoundError:
        _eprint(
            "the desktop app window needs pywebview, which normally ships with\n"
            "scrollback. if it's missing (e.g. a broken environment), install it\n"
            "with:\n"
            "    pip install pywebview\n"
            "or just run without --app to use your browser."
        )
        return 1
    import threading

    import uvicorn

    server = uvicorn.Server(
        uvicorn.Config(app, host=args.host, port=args.port, log_level="warning")
    )
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    _background_index_refresh()
    _eprint(f"scrollback app -> {url}  (read-only; close the window to quit)")
    # macOS only: fix the menu-bar app name (an unbundled python process shows
    # up as "Python"). No-op on other platforms, where the window title + icon
    # below are what the OS uses.
    _brand_macos_app()
    bridge = _AppBridge(url)
    # Window title is used by all backends (Windows/Linux taskbar + title bar).
    window = webview.create_window("scrollback", url, width=1280, height=860, js_api=bridge)
    bridge.window = window
    icon = _app_icon_path()           # cross-platform window/taskbar/Dock icon
    start_kwargs: dict[str, object] = {}
    if icon:
        start_kwargs["icon"] = icon
    # Run once the Cocoa menu exists to add a clickable repo link to the
    # standard About panel (macOS only; no-op elsewhere).
    webview.start(_install_macos_about_link, **start_kwargs)  # blocks until window closed
    # Window closed: stop the server and wait for the port to be released so
    # an immediate relaunch can reuse it.
    server.should_exit = True
    t.join(timeout=5)
    return 0


# -- argument parser -------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="scrollback",
        description="Navigate, search, copy, and export AI coding-agent sessions.",
    )
    p.add_argument("--version", action="version", version=f"scrollback {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    # sources
    sp = sub.add_parser("sources", help="list detected agents")
    sp.set_defaults(func=cmd_sources)

    # doctor
    sp = sub.add_parser("doctor", help="diagnostics: sources, index, features, env")
    sp.set_defaults(func=cmd_doctor)

    # index
    sp = sub.add_parser(
        "index", help="build/update the full-text search index (optional, faster search)"
    )
    sp.add_argument("--stats", action="store_true", help="show index stats and exit")
    sp.add_argument("--clear", action="store_true", help="delete the index and exit")
    sp.set_defaults(func=cmd_index)

    # common filters
    def add_source_flag(sp_: argparse.ArgumentParser) -> None:
        sp_.add_argument("--source", help="restrict to one source (e.g. opencode)")

    # archive
    sp = sub.add_parser(
        "archive",
        help="copy sessions into a durable local vault (kept forever)",
        description="Incrementally copy the sessions scrollback reads into a "
                    "user-owned vault that survives the agents' own auto-"
                    "deletion. One-way and lossless; your agent data is never "
                    "modified. Default vault: ~/.scrollback/archive "
                    "(override with --dest or $SCROLLBACK_ARCHIVE).",
    )
    add_source_flag(sp)
    sp.add_argument("--dest", help="vault path (default: ~/.scrollback/archive)")
    sp.add_argument("--stats", action="store_true",
                    help="show vault stats + layout and exit (no sync)")
    sp.add_argument("--verify", action="store_true",
                    help="check archived files exist and parse; exit (no sync)")
    sp.add_argument("--export", metavar="DEST",
                    help="export the vault for backup / another machine, to a "
                         "directory or a .zip (no sync)")
    sp.add_argument("--import", dest="import_from", metavar="VAULT",
                    help="merge another vault (directory or .zip, e.g. one made "
                         "with --export on another machine) into this one; "
                         "larger/newer copy wins, never loses messages (no sync)")
    sp.add_argument("--format", choices=["vault", "rendered"], default="vault",
                    help="export format: 'vault' (faithful, re-importable copy; "
                         "default) or 'rendered' (readable transcripts, not a backup)")
    sp.add_argument("--doc-format", dest="doc_format", default="markdown",
                    choices=["markdown", "html", "json", "text"],
                    help="document format when --format rendered (default markdown)")
    sp.set_defaults(func=cmd_archive)

    # list
    sp = sub.add_parser("list", help="list sessions (newest first)")
    add_source_flag(sp)
    sp.add_argument("--dir", help="filter by directory substring")
    sp.add_argument("-q", "--query", help="filter by title substring")
    sp.add_argument("--since", type=_parse_date, metavar="DATE",
                    help="only sessions updated on/after DATE (YYYY-MM-DD or ISO)")
    sp.add_argument("--until", type=_parse_date, metavar="DATE",
                    help="only sessions updated on/before DATE")
    sp.add_argument("-n", "--limit", type=_positive_int, default=30, help="max rows (default 30)")
    sp.add_argument("--offset", type=_nonneg_int, default=0, help="skip N rows (pagination)")
    sp.add_argument("--page", type=_positive_int, help="page number (uses --limit as page size)")
    sp.add_argument("--usage", action="store_true", help="show cost + token columns")
    sp.add_argument("--no-fold", action="store_true",
                    help="do not nest subagent sessions under their parent")
    sp.add_argument("--plain", action="store_true", help="disable colour output")
    sp.add_argument("--json", action="store_true", help="JSON output")
    sp.set_defaults(func=cmd_list)

    # show
    sp = sub.add_parser("show", help="print a session transcript")
    add_source_flag(sp)
    sp.add_argument("selector", help="session id / prefix / source:id / latest")
    sp.add_argument("--reasoning", action="store_true", help="include reasoning blocks")
    sp.add_argument("--no-tools", action="store_true", help="hide tool calls/outputs")
    sp.add_argument("--no-markdown", action="store_true",
                    help="render text as plain (no markdown formatting)")
    sp.add_argument("--plain", action="store_true", help="disable colour output")
    sp.set_defaults(func=cmd_show)

    # search
    sp = sub.add_parser("search", help="search across sessions")
    add_source_flag(sp)
    sp.add_argument("query", help="text to search for (case-insensitive)")
    sp.add_argument("--dir", help="filter by directory substring")
    sp.add_argument("--since", type=_parse_date, metavar="DATE",
                    help="only sessions updated on/after DATE")
    sp.add_argument("--until", type=_parse_date, metavar="DATE",
                    help="only sessions updated on/before DATE")
    sp.add_argument("-n", "--limit", type=_positive_int, default=50, help="max hits (default 50)")
    sp.add_argument("--plain", action="store_true", help="disable colour output")
    sp.add_argument("--json", action="store_true", help="JSON output")
    sp.set_defaults(func=cmd_search)

    # stats
    sp = sub.add_parser("stats", help="aggregate counts across your sessions")
    add_source_flag(sp)
    sp.add_argument("--since", type=_parse_date, metavar="DATE",
                    help="only count sessions updated on/after DATE (YYYY-MM-DD)")
    sp.add_argument("--until", type=_parse_date, metavar="DATE",
                    help="only count sessions updated on/before DATE (YYYY-MM-DD)")
    sp.add_argument("--top", type=_positive_int, default=10,
                    help="how many top projects to show (default 10)")
    sp.add_argument("--json", action="store_true", help="JSON output")
    sp.set_defaults(func=cmd_stats)

    # resume
    sp = sub.add_parser(
        "resume", help="print the command to resume a session in its native agent"
    )
    add_source_flag(sp)
    sp.add_argument("selector", help="session id / prefix / source:id / latest")
    sp.add_argument("--copy", action="store_true", help="copy the command to the clipboard")
    sp.set_defaults(func=cmd_resume)

    # export
    sp = sub.add_parser("export", help="render a session to a file/stdout")
    add_source_flag(sp)
    sp.add_argument("selector", help="session id / prefix / source:id / latest")
    sp.add_argument(
        "-f", "--format", default="markdown",
        choices=sorted(set(export.FORMATS)), help="output format",
    )
    sp.add_argument("-o", "--output", help="write to file instead of stdout")
    sp.add_argument("--reasoning", action="store_true", help="include reasoning blocks")
    sp.add_argument("--no-tools", action="store_true", help="hide tool calls/outputs")
    sp.add_argument(
        "--math", default="raw", choices=list(export.MATH_MODES),
        help="LaTeX handling: raw (verbatim), latex (verbatim, never typeset), "
             "rendered (typeset with KaTeX in html export)",
    )
    sp.set_defaults(func=cmd_export)

    # copy
    sp = sub.add_parser("copy", help="copy a rendered session to the clipboard")
    add_source_flag(sp)
    sp.add_argument("selector", help="session id / prefix / source:id / latest")
    sp.add_argument(
        "-f", "--format", default="markdown",
        choices=sorted(set(export.FORMATS)), help="render format",
    )
    sp.add_argument("--reasoning", action="store_true", help="include reasoning blocks")
    sp.add_argument("--no-tools", action="store_true", help="hide tool calls/outputs")
    sp.add_argument(
        "--math", default="raw", choices=list(export.MATH_MODES),
        help="LaTeX handling: raw (verbatim), latex (verbatim, never typeset), "
             "rendered (typeset with KaTeX in html export)",
    )
    sp.set_defaults(func=cmd_copy)

    # web
    sp = sub.add_parser("web", help="launch the local web app (read-only)")
    sp.add_argument("--host", default=serverconfig.default_host(),
                    help="bind host (default localhost; or $SCROLLBACK_HOST)")
    sp.add_argument("-p", "--port", type=int, default=serverconfig.default_port(),
                    help=f"port (default {serverconfig.DEFAULT_PORT}; or $SCROLLBACK_PORT)")
    sp.add_argument("--strict-port", action="store_true",
                    help="fail if the port is busy instead of picking the next free one")
    sp.add_argument("--no-browser", action="store_true", help="do not open a browser")
    sp.add_argument("--window", action="store_true",
                    help="open in a standalone browser window instead of a tab")
    sp.add_argument("--app", action="store_true",
                    help="open in a native desktop window (auto-closes; needs pywebview)")
    sp.add_argument("--auto-shutdown", action="store_true",
                    help="stop the server shortly after the browser window is closed")
    sp.set_defaults(func=cmd_web)

    # install-launcher
    sp = sub.add_parser(
        "install-launcher",
        help="install a double-clickable launcher for the web app",
        description="Install a launcher for the web app. With no flags, "
                    "creates both the Desktop launcher and (on macOS) the .app "
                    "bundle. Use --desktop or --app-bundle to create just one.",
    )
    sp.add_argument("--dest", help="where to place the launcher (default: Desktop)")
    sp.add_argument("--desktop", action="store_true",
                    help="only the Desktop launcher (.command / .bat / .desktop)")
    sp.add_argument("--app-bundle", action="store_true",
                    help="only the scrollback.app in ~/Applications (macOS; "
                         "falls back to the Desktop launcher elsewhere)")
    sp.set_defaults(func=cmd_install_launcher)

    # uninstall
    sp = sub.add_parser(
        "uninstall",
        help="remove scrollback-created files (see 'doctor' for the full list)",
        description="Remove every file scrollback created (search index, "
                    "web-app browser profile, cache dir, Desktop launcher, "
                    "macOS .app, launcher log) -- the same footprint "
                    "'scrollback doctor' lists. Your agent data is never "
                    "touched, and the durable archive vault is kept unless "
                    "--purge-archive is given (which asks for a typed "
                    "confirmation). The Python package itself is removed with "
                    "pip/pipx -- the exact command is printed at the end.",
    )
    sp.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
    sp.add_argument("--dry-run", action="store_true",
                    help="show what would be removed, then exit")
    sp.add_argument("--purge-archive", action="store_true",
                    help="also permanently delete the durable archive vault "
                         "(kept by default -- it is your data)")
    sp.set_defaults(func=cmd_uninstall)

    return p


def main(argv: list[str] | None = None) -> int:
    # Make stdout tolerant of non-UTF-8 locales (e.g. LANG=C) so transcripts
    # full of emoji/CJK don't crash with UnicodeEncodeError when piped/redirected.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except _BadSource as exc:
        _eprint(str(exc))
        return 2
    except BrokenPipeError:
        # Avoid a second BrokenPipeError + "Exception ignored" noise when Python
        # flushes stdout at shutdown (the classic `| head` case): redirect the
        # stdout fd to devnull before returning.
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except OSError:
            pass
        return 0
    except KeyboardInterrupt:
        return 130


def main_web(argv: list[str] | None = None) -> int:
    """Console entry point: `scrollback-web [options]` == `scrollback web`."""
    return main(["web", *(argv if argv is not None else sys.argv[1:])])


def main_app(argv: list[str] | None = None) -> int:
    """Console entry point: `scrollback-app` == `scrollback web --app`."""
    return main(["web", "--app", *(argv if argv is not None else sys.argv[1:])])


if __name__ == "__main__":
    raise SystemExit(main())
