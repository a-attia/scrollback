"""Plain-dict serializers for the API layer.

Kept separate from `export.py` (which renders human-facing documents) and
from `models.py` (which stays behavior-free). These produce the JSON
shapes the web frontend consumes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .models import Message, Part, Session
from .store import SearchHit


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def session_summary(s: Session) -> dict[str, Any]:
    """Lightweight session metadata for list views."""
    return {
        "id": s.id,
        "source": s.source,
        "short_id": s.short_id,
        "title": s.title,
        "directory": s.directory,
        "created": _iso(s.created),
        "updated": _iso(s.updated),
        "model": s.model,
        "agent": s.agent,
        "parent_id": s.parent_id,
        "is_subagent": s.is_subagent,
        "message_count": s.message_count,
        "cost": s.cost,
        "tokens_input": s.tokens_input,
        "tokens_output": s.tokens_output,
        "tokens_cache_read": s.tokens_cache_read,
        "tokens_cache_write": s.tokens_cache_write,
        "tokens_reasoning": s.tokens_reasoning,
        "git_branch": (s.raw or {}).get("git_branch"),
        # Archive provenance: `archived` = a copy exists in the vault;
        # `archived_only` = it exists ONLY in the vault (deleted from its agent);
        # `archive_status` = none | archived | stale (drives the sync button).
        "archived": bool((s.raw or {}).get("archived")),
        "archived_only": bool((s.raw or {}).get("archived_only")),
        "archive_status": (s.raw or {}).get("archive_status", "none"),
        "children": [session_summary(c) for c in s.children],
    }


def part_dict(p: Part) -> dict[str, Any]:
    return {
        "id": p.id,
        "type": p.type,
        "text": p.text,
        "tool_name": p.tool_name,
        "tool_status": p.tool_status,
    }


def message_dict(m: Message) -> dict[str, Any]:
    return {
        "id": m.id,
        "role": m.role,
        "created": _iso(m.created),
        "model": m.model,
        "parts": [part_dict(p) for p in m.parts],
    }


def session_detail(s: Session) -> dict[str, Any]:
    """Full session including messages/parts."""
    data = session_summary(s)
    data["messages"] = [message_dict(m) for m in s.messages]
    return data


def search_hit(h: SearchHit) -> dict[str, Any]:
    return {
        "source": h.session.source,
        "session_id": h.session.id,
        "short_id": h.session.short_id,
        "title": h.session.title,
        "directory": h.session.directory,
        "archived": bool((h.session.raw or {}).get("archived")),
        "archived_only": bool((h.session.raw or {}).get("archived_only")),
        "message_id": h.message.id,
        "role": h.message.role,
        "part_type": h.part.type,
        "tool_name": h.part.tool_name,
        "snippet": h.snippet,
    }
