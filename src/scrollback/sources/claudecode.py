"""Claude Code source adapter (read-only JSONL).

Claude Code stores one directory per project under ~/.claude/projects/,
each containing one `<session-uuid>.jsonl` file per session (plus, for
subagents, `<uuid>` directories / sidechain files). Each line is a JSON
object with a top-level `type`. The lines we care about have
`type in {"user", "assistant"}` and carry a `message` object whose
`content` is either a plain string or a list of typed blocks
(text / thinking / tool_use / tool_result).

All reads are read-only file reads; we never modify the JSONL files.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..models import Message, Part, Session, _to_dt
from .base import Source

_DEFAULT_ROOT = Path.home() / ".claude" / "projects"

# Separator embedding a subagent's agent id within a synthetic child session
# id: "<parent_uuid>::agent-<agentId>". Uses "::" so it never collides with
# the store's "source:id" selector parsing (which splits on a single ":").
_CHILD_SEP = "::"


def _child_id(parent_id: str, agent_stem: str) -> str:
    return f"{parent_id}{_CHILD_SEP}{agent_stem}"


# Cache for _scan_metadata results, keyed by (path, mtime_ns, size). The same
# transcript is scanned by list_sessions, then again when a session is loaded
# or searched; caching on the file's mtime+size avoids repeated full-file scans
# while staying correct (any change to the file invalidates the entry).
_META_CACHE: dict[str, tuple[tuple[float, int], dict[str, Any] | None]] = {}
_META_CACHE_MAX = 4096


# Cache of byte offsets for the content-bearing message lines of a transcript,
# keyed by (path, mtime_ns, size). Lets load_messages seek directly to the Nth
# message instead of re-scanning the file from the top for every page, turning
# K-page traversal from O(K*n) into O(n + K*page).
_OFFSET_CACHE: dict[str, tuple[tuple[int, int], list[int]]] = {}
_OFFSET_CACHE_MAX = 512


def _content_line_offsets(path: Path) -> list[int]:
    """Byte offsets of each content-bearing user/assistant line in `path`.

    "Content-bearing" matches load_messages' own filter (user/assistant,
    not meta, with at least one renderable part). Built in a single pass and
    cached on the file's mtime+size.
    """
    try:
        st = path.stat()
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        return []
    key = str(path)
    hit = _OFFSET_CACHE.get(key)
    if hit is not None and hit[0] == sig:
        return hit[1]

    offsets: list[int] = []
    try:
        # Read in binary to track exact byte offsets; decode per line.
        with path.open("rb") as fh:
            pos = 0
            for raw in fh:
                line_start = pos
                pos += len(raw)
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line.decode("utf-8", "replace"))
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                t = obj.get("type")
                if t not in ("user", "assistant") or obj.get("isMeta"):
                    continue
                m = obj.get("message")
                if not isinstance(m, dict):
                    continue
                if _content_to_parts("probe", m.get("content")):
                    offsets.append(line_start)
    except OSError:
        return []

    if len(_OFFSET_CACHE) >= _OFFSET_CACHE_MAX:
        _OFFSET_CACHE.clear()
    _OFFSET_CACHE[key] = (sig, offsets)
    return offsets


def _read_line_at(path: Path, byte_offset: int) -> dict[str, Any] | None:
    """Parse the single JSONL record starting at `byte_offset`."""
    try:
        with path.open("rb") as fh:
            fh.seek(byte_offset)
            raw = fh.readline()
        obj = json.loads(raw.decode("utf-8", "replace"))
        return obj if isinstance(obj, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _cached_scan_metadata(path: Path) -> dict[str, Any] | None:
    try:
        st = path.stat()
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        return None
    key = str(path)
    hit = _META_CACHE.get(key)
    if hit is not None and hit[0] == sig:
        return hit[1]
    result = _scan_metadata(path)
    if len(_META_CACHE) >= _META_CACHE_MAX:
        _META_CACHE.clear()  # simple bound; correctness over LRU sophistication
    _META_CACHE[key] = (sig, result)
    return result


def _read_meta_json(sub_path: Path) -> dict[str, Any]:
    """Read the sibling `<agent>.meta.json` (agentType, description)."""
    meta_path = sub_path.with_suffix(".meta.json")
    try:
        with meta_path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _env_root() -> Path:
    override = os.environ.get("SCROLLBACK_CLAUDE_DIR")
    if override:
        p = Path(override).expanduser()
        # Accept either ~/.claude or ~/.claude/projects.
        return p / "projects" if (p / "projects").is_dir() else p
    return _DEFAULT_ROOT


class ClaudeCodeSource(Source):
    name = "claudecode"
    label = "Claude Code"

    def __init__(self, root: Path | None = None) -> None:
        self._root = root or _env_root()

    def resume_command(self, session) -> str | None:
        # `claude --resume <id>` resumes a conversation (verified via --help).
        # Subagent sidechains aren't separately resumable -> use the parent id.
        import shlex

        sid = session.parent_id or session.id
        if _CHILD_SEP in sid:
            sid = sid.split(_CHILD_SEP, 1)[0]
        cmd = f"claude --resume {sid}"
        if session.directory:
            return f"cd {shlex.quote(session.directory)} && {cmd}"
        return cmd

    # -- availability / location -------------------------------------------

    def is_available(self) -> bool:
        return self._root.is_dir()

    def location(self) -> Path | None:
        return self._root if self.is_available() else None

    # -- discovery ----------------------------------------------------------

    def _session_files(self) -> Iterator[Path]:
        # Top-level <uuid>.jsonl files are the primary sessions; nested
        # subagent sidechains are folded under their parent as children.
        for project_dir in sorted(self._root.iterdir()):
            if not project_dir.is_dir():
                continue
            for f in sorted(project_dir.glob("*.jsonl")):
                yield f

    def _subagent_files(self, parent_path: Path) -> list[Path]:
        """Return the subagent transcript files for a parent session.

        Claude Code stores them in `<parent_dir>/<uuid>/subagents/agent-*.jsonl`
        (a sibling directory next to the `<uuid>.jsonl` transcript).
        """
        sub_dir = parent_path.with_suffix("") / "subagents"
        if not sub_dir.is_dir():
            return []
        return sorted(sub_dir.glob("agent-*.jsonl"))

    # -- listing ------------------------------------------------------------

    def list_sessions(self) -> Iterator[Session]:
        if not self.is_available():
            return iter(())
        return self._list_sessions()

    def _list_sessions(self) -> Iterator[Session]:
        for f in self._session_files():
            meta = _cached_scan_metadata(f)
            if meta is None:
                continue
            children = tuple(
                self._subagent_summary(f, sub) for sub in self._subagent_files(f)
            )
            yield Session(
                id=meta["session_id"],
                source=self.name,
                title=meta["title"],
                directory=meta["cwd"],
                created=_to_dt(meta["first_ts"]),
                updated=_to_dt(meta["last_ts"]),
                model=meta["model"],
                agent=None,
                parent_id=None,
                message_count=meta["msg_count"],
                children=children,
                raw={"path": str(f), "git_branch": meta["git_branch"]},
                **_usage_from_meta(meta),
            )

    def _subagent_summary(self, parent_path: Path, sub_path: Path) -> Session:
        """Build a lightweight child Session for a subagent transcript."""
        parent_id = parent_path.stem
        agent_id = sub_path.stem  # e.g. "agent-a04011b25b0a152ee"
        info = _read_meta_json(sub_path)
        title = info.get("description") or agent_id
        agent_type = info.get("agentType")
        if agent_type:
            title = f"{title} (@{agent_type})"
        sm = _cached_scan_metadata(sub_path)
        return Session(
            id=_child_id(parent_id, agent_id),
            source=self.name,
            title=title,
            directory=(sm or {}).get("cwd"),
            created=_to_dt((sm or {}).get("first_ts")),
            updated=_to_dt((sm or {}).get("last_ts")),
            model=(sm or {}).get("model"),
            agent=agent_type,
            parent_id=parent_id,
            message_count=(sm or {}).get("msg_count", 0),
            raw={"path": str(sub_path)},
            **_usage_from_meta(sm or {}),
        )

    # -- single session -----------------------------------------------------

    def load_session(self, session_id: str) -> Session | None:
        if not self.is_available():
            return None
        path = self._find_path(session_id)
        if path is None:
            return None
        if _CHILD_SEP in session_id:
            return _parse_session(
                path, self.name, override=self._child_override(session_id, path)
            )
        return _parse_session(path, self.name)

    def _child_override(self, child_id: str, sub_path: Path) -> dict[str, Any]:
        """Title/id/parent override so a loaded subagent keeps its child id."""
        # rsplit to match _find_path: agent segment is the tail.
        parent_id = child_id.rsplit(_CHILD_SEP, 1)[0]
        info = _read_meta_json(sub_path)
        title = info.get("description") or sub_path.stem
        if info.get("agentType"):
            title = f"{title} (@{info['agentType']})"
        return {"id": child_id, "title": title,
                "parent_id": parent_id, "agent": info.get("agentType")}

    def resolve_session_id(self, selector: str) -> str | None:
        # Child (subagent) ids are self-describing; resolve directly.
        if _CHILD_SEP in selector and self._find_path(selector) is not None:
            return selector
        return super().resolve_session_id(selector)

    def _find_path(self, session_id: str) -> Path | None:
        # Subagent child id: "<parent>::agent-<id>" -> nested subagents file.
        # rsplit on the LAST separator: the agent segment is always the tail,
        # and this is robust even if a parent id ever contained the separator.
        if _CHILD_SEP in session_id:
            parent_id, agent_id = session_id.rsplit(_CHILD_SEP, 1)
            # Reject path-escaping agent ids before touching the filesystem.
            if not agent_id or "/" in agent_id or "\\" in agent_id or ".." in agent_id:
                return None
            parent = self._find_path(parent_id)
            if parent is None:
                return None
            sub_dir = (parent.with_suffix("") / "subagents").resolve()
            cand = (sub_dir / f"{agent_id}.jsonl").resolve()
            # Containment check: the resolved candidate must live inside the
            # parent's subagents directory (defends against traversal).
            try:
                cand.relative_to(sub_dir)
            except ValueError:
                return None
            return cand if cand.is_file() else None
        for f in self._session_files():
            if f.stem == session_id:
                return f
        # prefix match
        candidates = [f for f in self._session_files() if f.stem.startswith(session_id)]
        return candidates[0] if len(candidates) == 1 else None

    # -- windowed loading ---------------------------------------------------

    def load_session_meta(self, session_id: str) -> Session | None:
        if not self.is_available():
            return None
        path = self._find_path(session_id)
        if path is None:
            return None
        meta = _cached_scan_metadata(path)
        if meta is None:
            return None
        ovr = self._child_override(session_id, path) if _CHILD_SEP in session_id else {}
        return Session(
            id=ovr.get("id", meta["session_id"]),
            source=self.name,
            title=ovr.get("title", meta["title"]),
            directory=meta["cwd"],
            created=_to_dt(meta["first_ts"]),
            updated=_to_dt(meta["last_ts"]),
            model=meta["model"],
            agent=ovr.get("agent"),
            parent_id=ovr.get("parent_id"),
            message_count=meta["msg_count"],
            messages=(),
            raw={"path": str(path), "git_branch": meta["git_branch"]},
            **_usage_from_meta(meta),
        )

    def load_messages(
        self, session_id: str, *, offset: int = 0, limit: int | None = None
    ) -> list[Message]:
        if not self.is_available():
            return []
        path = self._find_path(session_id)
        if path is None:
            return []
        # Use the cached byte-offset index to seek directly to the requested
        # window instead of re-scanning from the top of the file each page.
        offsets = _content_line_offsets(path)
        window = offsets[offset:] if limit is None else offsets[offset : offset + limit]
        out: list[Message] = []
        for i, byte_off in enumerate(window):
            obj = _read_line_at(path, byte_off)
            if obj is None:
                continue
            m = obj.get("message", {})
            if not isinstance(m, dict):
                continue
            uuid = obj.get("uuid") or f"{path.stem}:{offset + i}"
            parts = _content_to_parts(uuid, m.get("content"))
            if not parts:
                continue
            out.append(
                Message(
                    id=uuid,
                    role=m.get("role", obj.get("type")),
                    created=_to_dt(obj.get("timestamp")),
                    parts=tuple(parts),
                    model=_clean_model(m.get("model")),
                    raw=obj,
                )
            )
        return out


# -- parsing helpers -------------------------------------------------------


def _iter_lines(path: Path) -> Iterator[dict[str, Any]]:
    try:
        # errors="replace": a single invalid UTF-8 byte must not abort the
        # whole-file iteration (it would silently truncate a session).
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError:
        return


def _scan_metadata(path: Path) -> dict[str, Any] | None:
    """Single pass over a transcript collecting just the metadata fields."""
    session_id = path.stem
    cwd: str | None = None
    git_branch: str | None = None
    model: str | None = None
    title: str | None = None
    first_ts: str | None = None
    last_ts: str | None = None
    first_user_text: str | None = None
    msg_count = 0
    seen = False
    # Usage accumulators. Claude Code records per-turn `usage` on assistant
    # messages; summing across turns matches the agent's own session totals.
    tok_in = tok_out = tok_cache_read = tok_cache_write = 0
    any_usage = False

    for obj in _iter_lines(path):
        seen = True
        t = obj.get("type")
        if obj.get("sessionId"):
            session_id = obj["sessionId"]
        if cwd is None and obj.get("cwd"):
            cwd = obj["cwd"]
        if git_branch is None and obj.get("gitBranch"):
            git_branch = obj["gitBranch"]
        if t == "ai-title":
            # Claude Code writes the title under `aiTitle` (newer) and may
            # also use `title`; the last one in the file wins.
            new_title = obj.get("aiTitle") or obj.get("title")
            if new_title:
                title = new_title
        if t in ("user", "assistant"):
            ts = obj.get("timestamp")
            if ts:
                if first_ts is None:
                    first_ts = ts
                last_ts = ts
            m = obj.get("message", {})
            if model is None and isinstance(m, dict):
                mv = m.get("model")
                if mv and mv != "<synthetic>":
                    model = mv
            if isinstance(m, dict):
                u = m.get("usage")
                if isinstance(u, dict):
                    any_usage = True
                    tok_in += _int(u.get("input_tokens"))
                    tok_out += _int(u.get("output_tokens"))
                    tok_cache_read += _int(u.get("cache_read_input_tokens"))
                    tok_cache_write += _int(u.get("cache_creation_input_tokens"))
            # Count exactly the turns `_parse_session` / `load_messages` will
            # actually yield: not meta, and carrying at least one renderable
            # part. Counting every user/assistant line instead made the listing
            # count disagree with the loaded count on ~40% of real transcripts
            # (tool-result-only turns, empty content), which in turn made the
            # archive's change-detection signature never converge.
            if not obj.get("isMeta") and isinstance(m, dict):
                if _content_to_parts("probe", m.get("content")):
                    msg_count += 1
            if (
                first_user_text is None
                and t == "user"
                and not obj.get("isMeta")
                and isinstance(m, dict)
            ):
                first_user_text = _first_text(m.get("content"))

    if not seen:
        return None
    return {
        "session_id": session_id,
        "cwd": cwd,
        "git_branch": git_branch,
        "model": model,
        "title": title or _fallback_title(path, cwd, first_user_text),
        "first_ts": first_ts,
        "last_ts": last_ts,
        "msg_count": msg_count,
        # Only report usage when the transcript actually carried any, so
        # sessions with no usage records stay None (not a misleading 0).
        "tokens_input": tok_in if any_usage else None,
        "tokens_output": tok_out if any_usage else None,
        "tokens_cache_read": tok_cache_read if any_usage else None,
        "tokens_cache_write": tok_cache_write if any_usage else None,
    }


def _int(v: Any) -> int:
    """Coerce a usage value to int, treating missing/garbage as 0."""
    return v if isinstance(v, int) else 0


def _usage_from_meta(meta: dict[str, Any]) -> dict[str, Any]:
    """Pull the usage fields out of a scan-metadata dict (all may be None)."""
    return {
        "tokens_input": meta.get("tokens_input"),
        "tokens_output": meta.get("tokens_output"),
        "tokens_cache_read": meta.get("tokens_cache_read"),
        "tokens_cache_write": meta.get("tokens_cache_write"),
    }


def _clean_model(model: Any) -> str | None:
    """Drop Claude Code's '<synthetic>' placeholder used on system turns."""
    if not model or model == "<synthetic>":
        return None
    return model


def _first_text(content: Any) -> str | None:
    """Extract the first human-readable text from a message content field."""
    if isinstance(content, str):
        s = content.strip()
        return s or None
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                s = (block.get("text") or "").strip()
                if s:
                    return s
            elif isinstance(block, str) and block.strip():
                return block.strip()
    return None


def _fallback_title(path: Path, cwd: str | None, first_user_text: str | None) -> str:
    """Build a readable title when the transcript has no ai-title.

    Prefer the first user line (trimmed), prefixed by the project basename
    for context; fall back to the directory basename, then the UUID prefix.
    """
    project = ""
    if cwd:
        project = cwd.rstrip("/").split("/")[-1]
    if first_user_text:
        snippet = " ".join(first_user_text.split())
        if len(snippet) > 60:
            snippet = snippet[:57] + "..."
        return f"{project}: {snippet}" if project else snippet
    if project:
        return project
    return path.stem[:8]


def _parse_session(
    path: Path, source_name: str, *, override: dict[str, Any] | None = None
) -> Session:
    override = override or {}
    meta = _cached_scan_metadata(path) or {
        "session_id": path.stem,
        "cwd": None,
        "git_branch": None,
        "model": None,
        "title": path.stem[:8],
        "first_ts": None,
        "last_ts": None,
        "msg_count": 0,
    }

    messages: list[Message] = []
    idx = 0
    for obj in _iter_lines(path):
        t = obj.get("type")
        if t not in ("user", "assistant"):
            continue
        if obj.get("isMeta"):
            # Skip local-command caveats and similar meta turns.
            continue
        m = obj.get("message", {})
        if not isinstance(m, dict):
            continue
        role = m.get("role", t)
        uuid = obj.get("uuid") or f"{path.stem}:{idx}"
        idx += 1
        parts = _content_to_parts(uuid, m.get("content"))
        if not parts:
            continue
        messages.append(
            Message(
                id=uuid,
                role=role,
                created=_to_dt(obj.get("timestamp")),
                parts=tuple(parts),
                model=_clean_model(m.get("model")),
                raw=obj,
            )
        )

    return Session(
        id=override.get("id", meta["session_id"]),
        source=source_name,
        title=override.get("title", meta["title"]),
        directory=meta["cwd"],
        created=_to_dt(meta["first_ts"]),
        updated=_to_dt(meta["last_ts"]),
        model=meta["model"],
        agent=override.get("agent"),
        parent_id=override.get("parent_id"),
        message_count=len(messages),
        messages=tuple(messages),
        raw={"path": str(path), "git_branch": meta["git_branch"]},
        **_usage_from_meta(meta),
    )


def _content_to_parts(msg_uuid: str, content: Any) -> list[Part]:
    """Normalize Claude Code message content into Parts."""
    parts: list[Part] = []
    if content is None:
        return parts
    if isinstance(content, str):
        if content.strip():
            parts.append(Part(id=f"{msg_uuid}:0", type="text", text=content))
        return parts
    if isinstance(content, list):
        for i, block in enumerate(content):
            part = _block_to_part(f"{msg_uuid}:{i}", block)
            if part is not None:
                parts.append(part)
    return parts


def _block_to_part(pid: str, block: Any) -> Part | None:
    if not isinstance(block, dict):
        if isinstance(block, str) and block.strip():
            return Part(id=pid, type="text", text=block)
        return None
    btype = block.get("type")
    if btype == "text":
        text = block.get("text", "")
        return Part(id=pid, type="text", text=text, raw=block) if text else None
    if btype == "thinking":
        text = block.get("thinking", "")
        return Part(id=pid, type="reasoning", text=text, raw=block) if text else None
    if btype == "tool_use":
        name = block.get("name")
        inp = block.get("input")
        text = f"$ {name} {json.dumps(inp, ensure_ascii=False)}" if inp is not None else f"$ {name}"
        return Part(id=pid, type="tool", text=text, tool_name=name, tool_status="call", raw=block)
    if btype == "tool_result":
        content = block.get("content")
        text = _stringify_tool_result(content)
        is_err = bool(block.get("is_error"))
        return Part(
            id=pid,
            type="tool",
            text=("[error] " + text) if is_err else text,
            tool_status="error" if is_err else "result",
            raw=block,
        )
    if btype == "image":
        return Part(id=pid, type="file", text="[image]", raw=block)
    return Part(id=pid, type="unknown", raw=block)


def _stringify_tool_result(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: list[str] = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                out.append(b.get("text", ""))
            elif isinstance(b, str):
                out.append(b)
            else:
                out.append(json.dumps(b, ensure_ascii=False))
        return "\n".join(out)
    return json.dumps(content, ensure_ascii=False)
