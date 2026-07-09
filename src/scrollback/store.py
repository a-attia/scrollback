"""Unified store: query across all (or selected) source adapters.

This is the single entry point the CLI and web app use. It composes the
individual adapters, applies cross-source filtering/sorting, and resolves
session selectors that may carry a `source:id` qualifier.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone

from .models import Message, Part, Session
from .sources import registry
from .sources.base import Source


def _sort_key(s: Session) -> datetime:
    return s.updated or s.created or datetime.min.replace(tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
class SearchHit:
    """A single search match within a session."""

    session: Session
    message: Message
    part: Part
    snippet: str


@dataclass(frozen=True, slots=True)
class SourceUsage:
    """Per-source rollup of sessions, messages, tokens, and cost.

    `cost` is None when the source does not report cost at all (e.g. Claude
    Code / Codex / Aider), distinguishing "unknown" from a real $0.00.
    """

    source: str
    sessions: int = 0
    messages: int = 0
    tokens_input: int = 0
    tokens_output: int = 0
    tokens_cache_read: int = 0
    tokens_cache_write: int = 0
    tokens_reasoning: int = 0
    cost: float | None = None


@dataclass(frozen=True, slots=True)
class Stats:
    """Aggregate counts across sessions (see `Store.stats`)."""

    sessions: int
    per_source: dict[str, int]
    per_project: dict[str, int]
    per_source_usage: dict[str, SourceUsage]
    total_messages: int
    total_tokens_input: int
    total_tokens_output: int
    total_tokens_cache_read: int
    total_tokens_cache_write: int
    total_tokens_reasoning: int
    total_cost: float
    oldest: datetime | None
    newest: datetime | None


class Store:
    """Facade over one or more source adapters."""

    def __init__(
        self, sources: list[Source] | None = None, *, live_probe=None,
        hide_archived_only=False,
    ) -> None:
        self._sources = sources if sources is not None else registry.available_sources()
        # Optional authoritative set of live `(source, id)` keys, used ONLY to
        # decide whether an archived session is "deleted" (archived_only) vs.
        # still live. Needed for the archive-only store (which has no live
        # sources of its own), so a still-live session is not mislabelled
        # "deleted". `None` means "infer from this store's own live sources".
        self._live_probe = live_probe
        # When True, drop archived-only (deleted-from-agent) sessions from
        # listings. Used by "live" mode, which composes with the archive purely
        # to ANNOTATE live sessions with their archive status (archived / stale)
        # -- so the provenance tag matches the other modes -- without surfacing
        # sessions the agent has deleted.
        self._hide_archived_only = hide_archived_only

    @property
    def sources(self) -> list[Source]:
        return self._sources

    def with_sources(self, names: list[str]) -> "Store":
        """Return a Store narrowed to the named sources.

        Filters THIS store's own sources rather than re-fetching from the
        global registry, so an injected store (tests, demos) stays isolated
        instead of silently picking up the machine's real adapters.
        """
        wanted = set(names)
        chosen = [s for s in self._sources if s.name in wanted]
        return Store(chosen, live_probe=self._live_probe,
                     hide_archived_only=self._hide_archived_only)

    def with_archive(self, vault_path, *, live_probe=None, hide_archived_only=False) -> "Store":
        """Return a Store that also reads a durable archive vault.

        The archive source is appended AFTER the live sources so that
        first-match resolution and (source, id) dedup both favour the fresher
        live copy (see `docs/archive-plan.md` §7.2). A session that exists only
        in the vault -- deleted from its agent -- still surfaces, carrying the
        ``archived_only`` badge. No-op if the vault does not exist.

        `live_probe`: an authoritative set of live `(source, id)` keys for the
        deleted-vs-live decision. Supply this when the resulting store has no
        live sources of its own (the archive-only "browse the vault" store), so
        a still-live session is labelled "archived", not "deleted".
        """
        from .sources.archive import ArchiveSource

        arc = ArchiveSource(vault_path)
        if not arc.is_available():
            return self
        return Store([*self._sources, arc],
                     live_probe=live_probe if live_probe is not None else self._live_probe,
                     hide_archived_only=hide_archived_only or self._hide_archived_only)

    def live_keys(self) -> set:
        """The set of `(source, id)` keys held by this store's LIVE (non-archive)
        sources -- used as a `live_probe` for an archive-only store."""
        from .sources.archive import ArchiveSource

        keys = set()
        for src in self._sources:
            if isinstance(src, ArchiveSource):
                continue
            for s in src.list_sessions():
                keys.add((s.source, s.id))
        return keys

    # -- aggregate stats ----------------------------------------------------

    def stats(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> "Stats":
        """Aggregate session metadata across sources (metadata-only; cheap-ish).

        Computes per-source and per-project session counts plus totals
        (messages, tokens, cost) and the overall date span. Uses list-level
        metadata only -- it does not load message bodies. `since`/`until`
        restrict the aggregation to sessions in that date window.
        """
        from collections import Counter

        per_source: Counter[str] = Counter()
        per_project: Counter[str] = Counter()
        total_messages = 0
        total_tokens_in = 0
        total_tokens_out = 0
        total_cache_read = 0
        total_cache_write = 0
        total_reasoning = 0
        total_cost = 0.0
        oldest: datetime | None = None
        newest: datetime | None = None
        n = 0

        # Per-source accumulators (mutable during the pass, frozen at the end).
        # `cost` starts at None so a source that never reports cost stays
        # "unknown" rather than showing a misleading $0.00.
        usage: dict[str, dict] = {}

        def _acc(src: str) -> dict:
            return usage.setdefault(src, {
                "sessions": 0, "messages": 0, "tokens_input": 0,
                "tokens_output": 0, "tokens_cache_read": 0,
                "tokens_cache_write": 0, "tokens_reasoning": 0, "cost": None,
            })

        for s in self.list_sessions(since=since, until=until, fold_subagents=False):
            n += 1
            per_source[s.source] += 1
            if s.directory:
                per_project[s.directory] += 1

            u = _acc(s.source)
            u["sessions"] += 1
            if s.message_count:
                total_messages += s.message_count
                u["messages"] += s.message_count
            if s.tokens_input:
                total_tokens_in += s.tokens_input
                u["tokens_input"] += s.tokens_input
            if s.tokens_output:
                total_tokens_out += s.tokens_output
                u["tokens_output"] += s.tokens_output
            if s.tokens_cache_read:
                total_cache_read += s.tokens_cache_read
                u["tokens_cache_read"] += s.tokens_cache_read
            if s.tokens_cache_write:
                total_cache_write += s.tokens_cache_write
                u["tokens_cache_write"] += s.tokens_cache_write
            if s.tokens_reasoning:
                total_reasoning += s.tokens_reasoning
                u["tokens_reasoning"] += s.tokens_reasoning
            # Presence check (not truthiness): a real reported $0.00 must count
            # as "known cost", keeping the None-vs-0.0 distinction the
            # SourceUsage docstring promises.
            if s.cost is not None:
                total_cost += s.cost
                u["cost"] = (u["cost"] or 0.0) + s.cost
            when = s.updated or s.created
            if when is not None:
                oldest = when if oldest is None or when < oldest else oldest
                newest = when if newest is None or when > newest else newest

        per_source_usage = {
            src: SourceUsage(source=src, **vals) for src, vals in usage.items()
        }

        return Stats(
            sessions=n,
            per_source=dict(per_source),
            per_project=dict(per_project),
            per_source_usage=per_source_usage,
            total_messages=total_messages,
            total_tokens_input=total_tokens_in,
            total_tokens_output=total_tokens_out,
            total_tokens_cache_read=total_cache_read,
            total_tokens_cache_write=total_cache_write,
            total_tokens_reasoning=total_reasoning,
            total_cost=total_cost,
            oldest=oldest,
            newest=newest,
        )

    # -- listing ------------------------------------------------------------

    def list_sessions(
        self,
        *,
        source: str | None = None,
        directory: str | None = None,
        query: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
        fold_subagents: bool = False,
    ) -> list[Session]:
        """List sessions across sources, newest first.

        Args:
          source: keep only sessions whose ORIGINAL source matches (filters on
            `sess.source`, not the adapter name -- so an archived opencode
            session is kept when `source="opencode"`, even though its holding
            adapter is the archive reader).
          directory: keep only sessions whose directory contains this substring.
          query: case-insensitive substring match on the title.
          since / until: keep sessions whose updated (or created) time falls
            within the range (inclusive).
          limit / offset: pagination over the filtered, sorted result.
          fold_subagents: nest subagent sessions under their parent (as
            `.children`) instead of listing them at the top level.
        """
        results: list[Session] = []
        for src in self._sources:
            for sess in src.list_sessions():
                if source and sess.source != source:
                    continue
                if directory and (sess.directory is None or directory not in sess.directory):
                    continue
                if query and query.lower() not in (sess.title or "").lower():
                    continue
                when = sess.updated or sess.created
                if since and (when is None or when < since):
                    continue
                if until and (when is None or when > until):
                    continue
                results.append(sess)
        results = _dedup(results, live_probe=self._live_probe)
        if self._hide_archived_only:
            # "live" mode: keep the archive annotation on live sessions but drop
            # sessions that exist ONLY in the vault (deleted from their agent).
            results = [s for s in results if not (s.raw or {}).get("archived_only")]
        results.sort(key=_sort_key, reverse=True)

        if fold_subagents:
            results = _fold(results)

        if offset:
            results = results[offset:]
        if limit is not None:
            results = results[:limit]
        return results

    # -- single session -----------------------------------------------------

    def _resolve(self, selector: str, source: str | None):
        """Return (Source, full_id) for a selector, or (None, None).

        When a `source` qualifier is given it selects by adapter name, but the
        injected archive reader is ALWAYS kept as a trailing fallback: an
        archived session keeps its ORIGINAL source name (e.g. "opencode"),
        which never equals the archive adapter's own name, so a `source=`
        filter would otherwise make deleted-but-archived sessions unresolvable.
        Ordering (live first, archive last) still makes live win.
        """
        from .sources.archive import ArchiveSource

        src_name, sel = _split_selector(selector, source)
        if src_name is None:
            candidates = self._sources
        else:
            candidates = [
                s for s in self._sources
                if s.name == src_name or isinstance(s, ArchiveSource)
            ]
        for src in candidates:
            full = src.resolve_session_id(sel)
            if full:
                return src, full
        return None, None

    def _archive_reader(self):
        """The injected ArchiveSource, if any (None when no vault is attached)."""
        from .sources.archive import ArchiveSource

        return next((s for s in self._sources if isinstance(s, ArchiveSource)), None)

    def _mark_archived_only(self, sess: Session | None, src) -> Session | None:
        """Tag a single-session load with archive provenance + status.

        The list path computes `archived` / `archived_only` / `archive_status`
        via `_dedup`; single-session loads bypass dedup, so this recomputes the
        same facts for a consistent badge + a working per-session sync button:

        * resolved from the vault, no live twin -> archived_only + status
          "archived";
        * resolved from a live source with a vault twin -> "archived" (up to
          date) or "stale" (signatures differ);
        * no vault copy -> status "none".
        """
        from dataclasses import replace

        from .sources.archive import ArchiveSource

        if sess is None:
            return sess

        def _sig(s):
            return (s.updated.isoformat() if s.updated else None, s.message_count)

        arc = self._archive_reader()

        if isinstance(src, ArchiveSource):
            live_has = any(
                not isinstance(s, ArchiveSource) and s.resolve_session_id(sess.id)
                for s in self._sources
            )
            raw = {**(sess.raw or {}), "archived": True}
            if not live_has:
                raw["archived_only"] = True
                raw["archive_status"] = "archived"
            return replace(sess, raw=raw)

        # Resolved from a live source: consult the vault reader for a twin.
        if arc is not None:
            twin = arc.load_session(sess.id)
            if twin is not None and twin.source == sess.source:
                status = "archived" if _sig(twin) == _sig(sess) else "stale"
                return replace(sess, raw={
                    **(sess.raw or {}), "archived": True, "archive_status": status,
                })
        return replace(sess, raw={**(sess.raw or {}), "archive_status": "none"})

    def load_session_meta(self, selector: str, *, source: str | None = None) -> Session | None:
        """Load only a session's metadata (no messages) -- cheap for huge ones."""
        src, full = self._resolve(selector, source)
        return self._mark_archived_only(src.load_session_meta(full), src) if src else None

    def load_messages(
        self, selector: str, *, source: str | None = None,
        offset: int = 0, limit: int | None = None,
    ) -> list[Message]:
        """Load a windowed slice of a session's messages."""
        src, full = self._resolve(selector, source)
        return src.load_messages(full, offset=offset, limit=limit) if src else []

    def load_session(self, selector: str, *, source: str | None = None) -> Session | None:
        """Load one session (with all messages) by selector.

        Selector may be `source:id`, a full id, a unique prefix, or 'latest'.
        """
        src, full = self._resolve(selector, source)
        return self._mark_archived_only(src.load_session(full), src) if src else None

    # -- search -------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        directory: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
        context: int = 80,
        use_index: bool = True,
    ) -> Iterator[SearchHit]:
        """Yield hits where `query` appears in any message part.

        If an FTS index exists (built via `scrollback index`) and
        `use_index` is True, the fast indexed path is used. Otherwise this
        falls back to a lexical scan over the live data -- zero setup, always
        correct, but O(corpus) per query.
        """
        if use_index:
            indexed = self._search_indexed(
                query, directory=directory, since=since, until=until, limit=limit
            )
            if indexed is not None:
                yield from indexed
                return
        yield from self._search_lexical(
            query, directory=directory, since=since, until=until,
            limit=limit, context=context,
        )

    def _search_indexed(
        self, query, *, directory, since, until, limit
    ) -> Iterator[SearchHit] | None:
        """Indexed search. Returns None if the index is unavailable (caller
        then falls back to lexical).

        The FTS query itself is ~instant; the only expensive thing is mapping
        a hit's session id back to session metadata. So we resolve metadata
        *lazily* per distinct session that actually appears in results
        (usually few), via the cheap per-adapter `load_session_meta`. We only
        pay for a full `list_sessions` when directory/date filters are given.
        """
        from . import fts

        index = fts.FtsIndex()
        if not index.exists():
            return None

        source_names = [s.name for s in self._sources]
        filtering = directory is not None or since is not None or until is not None
        allowed: set[tuple[str, str]] | None = None
        if filtering:
            allowed = {
                (s.source, s.id)
                for s in self.list_sessions(directory=directory, since=since, until=until)
            }

        meta_cache: dict[tuple[str, str], Session | None] = {}

        def meta_for(source: str, sid: str) -> Session | None:
            k = (source, sid)
            if k not in meta_cache:
                src = next((s for s in self._sources if s.name == source), None)
                meta_cache[k] = src.load_session_meta(sid) if src else None
            return meta_cache[k]

        def gen() -> Iterator[SearchHit]:
            count = 0
            for hit in index.search(query, sources=source_names):
                key = (hit.source, hit.session_id)
                if allowed is not None and key not in allowed:
                    continue
                meta = meta_for(hit.source, hit.session_id)
                if meta is None:
                    continue  # stale index entry (session deleted)
                part = Part(id="", type=hit.part_type, text="", tool_name=hit.tool_name)
                msg = Message(id=hit.message_id, role=hit.role, created=None, parts=(part,))
                yield SearchHit(
                    session=meta,
                    message=msg,
                    part=part,
                    snippet=_clean_snippet(hit.text),
                )
                count += 1
                if limit is not None and count >= limit:
                    return

        return gen()

    def _search_lexical(
        self, query, *, directory, since, until, limit, context
    ) -> Iterator[SearchHit]:
        ql = query.lower()
        count = 0
        for meta in self.list_sessions(directory=directory, since=since, until=until):
            # Route via the resolver so archive-only sessions (whose holding
            # adapter's name differs from their original source) are found too,
            # and so the loaded session carries the archived_only badge.
            sess = self.load_session(meta.id, source=meta.source)
            if sess is None:
                continue
            for msg in sess.messages:
                for part in msg.parts:
                    if not part.text:
                        continue
                    pos = part.text.lower().find(ql)
                    if pos == -1:
                        continue
                    yield SearchHit(
                        session=sess,
                        message=msg,
                        part=part,
                        snippet=_snippet(part.text, pos, len(query), context),
                    )
                    count += 1
                    if limit is not None and count >= limit:
                        return


def _dedup(sessions: list[Session], *, live_probe=None) -> list[Session]:
    """Collapse duplicate `(source, id)` sessions, keeping the first seen.

    Sources are gathered live-first with any injected `ArchiveSource` last
    (see `Store.with_archive`), so "first wins" means the fresher live copy
    shadows its archived twin -- the resolved precedence in
    `docs/archive-plan.md` §7.2. This is the single dedup chokepoint: `stats`,
    the lexical search filter, and the indexed search filter all funnel
    through `list_sessions`, so guarding here prevents the double-counting
    hazard those paths would otherwise have.

    Badge facts surfaced on the surviving copy's `raw`:

    * ``archived`` -- a copy of this session exists in the vault (true whether
      the survivor is the live copy or the archive copy).
    * ``archived_only`` -- the session exists ONLY in the vault, i.e. it was
      deleted from its agent (the survivor is the archive copy, with no live
      twin).
    * ``archive_status`` -- ``"none"`` (no vault copy), ``"archived"`` (vault
      copy is up to date), or ``"stale"`` (a live copy exists AND the vault
      copy is out of date -- fewer messages / older `updated`). Drives the
      per-session "archive / update" button in the web UI.
    """
    from dataclasses import replace

    def _sig(s: Session):
        return (s.updated.isoformat() if s.updated else None, s.message_count)

    seen: dict[tuple[str, str], Session] = {}
    order: list[tuple[str, str]] = []
    archived_sig: dict[tuple[str, str], tuple] = {}
    for s in sessions:
        key = (s.source, s.id)
        if (s.raw or {}).get("archived"):
            archived_sig[key] = _sig(s)
        if key not in seen:
            seen[key] = s  # first wins: live shadows archive (ordering)
            order.append(key)

    out: list[Session] = []
    for key in order:
        s = seen[key]
        survivor_is_archive = bool((s.raw or {}).get("archived"))
        if key not in archived_sig:
            # No vault copy at all.
            out.append(replace(s, raw={**(s.raw or {}), "archive_status": "none"}))
            continue
        # Is the session still live? With an explicit live_probe (the archive-
        # only store passes one), that set is authoritative. Otherwise infer it:
        # a live copy would have won dedup, so "survivor is the archive copy"
        # means no live twin was gathered.
        if live_probe is not None:
            is_live = key in live_probe
        else:
            is_live = not survivor_is_archive
        badge = {"archived": True}
        if not is_live:
            # Truly deleted: in the vault, gone from every live source.
            badge["archived_only"] = True
            badge["archive_status"] = "archived"
        else:
            # Still live + a vault twin: up to date iff signatures match.
            badge["archive_status"] = (
                "archived" if _sig(s) == archived_sig[key] else "stale"
            )
        out.append(replace(s, raw={**(s.raw or {}), **badge}))
    return out


def _fold(sessions: list[Session]) -> list[Session]:
    """Nest subagent sessions under their parent.

    A session with a `parent_id` that matches another session's id becomes a
    child of that parent (attached via `.children`). Subagents whose parent
    is not in the list stay at top level so nothing is lost. Order among
    top-level sessions is preserved; children keep newest-first order.
    """
    from dataclasses import replace

    # Key on (source, id): ids are only unique within a source, so keying on
    # the bare id could mis-link a parent in one source to a child in another.
    def key(source: str, sid: str) -> tuple[str, str]:
        return (source, sid)

    by_key = {key(s.source, s.id): s for s in sessions}
    children_of: dict[tuple[str, str], list[Session]] = {}
    top: list[Session] = []
    for s in sessions:
        parent_key = key(s.source, s.parent_id) if s.parent_id else None
        # Fold only when the parent exists AND isn't the session itself
        # (a self-referential parent_id would otherwise drop the session).
        if parent_key and parent_key != key(s.source, s.id) and parent_key in by_key:
            children_of.setdefault(parent_key, []).append(s)
        else:
            top.append(s)
    return [
        replace(s, children=tuple(children_of.get(key(s.source, s.id), ())))
        if key(s.source, s.id) in children_of
        else s
        for s in top
    ]


def _split_selector(selector: str, source: str | None) -> tuple[str | None, str]:
    if source:
        return source, selector
    if ":" in selector:
        head, _, tail = selector.partition(":")
        if registry.get_source(head) is not None:
            return head, tail
    return None, selector


def _snippet(text: str, pos: int, qlen: int, context: int) -> str:
    start = max(0, pos - context)
    end = min(len(text), pos + qlen + context)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""
    return (prefix + text[start:end] + suffix).replace("\n", " ")


def _clean_snippet(snip: str) -> str:
    """Normalize an FTS5 snippet for display.

    The index requests snippets with \\x02/\\x03 wrapping the matched term
    (so the frontend/CLI can re-highlight without re-searching). We strip the
    markers here and collapse newlines; the consumers do their own
    highlighting against the query, matching the lexical path's snippets.
    """
    return snip.replace("\x02", "").replace("\x03", "").replace("\n", " ").strip()
