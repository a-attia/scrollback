"""Common data model shared by all source adapters.

Every adapter normalizes its agent's on-disk representation into these
immutable dataclasses, so the rest of the program (CLI, search, export,
web) is agent-agnostic. Keeping these as plain data structures (rather
than behavior-rich classes) follows the "functions over data structures"
principle: many functions operate on these few shapes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

PartType = Literal[
    "text",
    "reasoning",
    "tool",
    "file",
    "patch",
    "step-start",
    "step-finish",
    "compaction",
    "unknown",
]

Role = Literal["user", "assistant", "system", "tool"]


def _to_dt(ms_or_iso: int | float | str | None) -> datetime | None:
    """Best-effort conversion of a timestamp to an aware UTC datetime.

    Accepts epoch milliseconds (opencode) or ISO-8601 strings (Claude Code).
    Returns None when the input is missing or unparseable.
    """
    if ms_or_iso is None:
        return None
    if isinstance(ms_or_iso, datetime):
        # Already a datetime (e.g. from dataclasses.asdict, which does not
        # stringify datetimes). Ensure tz-awareness for consistent sorting.
        return ms_or_iso if ms_or_iso.tzinfo else ms_or_iso.replace(tzinfo=timezone.utc)
    if isinstance(ms_or_iso, (int, float)):
        # opencode stores epoch milliseconds.
        return datetime.fromtimestamp(ms_or_iso / 1000.0, tz=timezone.utc)
    if isinstance(ms_or_iso, str):
        s = ms_or_iso.strip()
        if not s:
            return None
        try:
            # Handle trailing Z.
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        # Force tz-awareness: a timezone-less timestamp would otherwise be a
        # naive datetime, which raises TypeError when sorted alongside the
        # aware datetimes used elsewhere. Assume UTC when no offset is given.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    return None


@dataclass(frozen=True, slots=True)
class Part:
    """A single content block within a message.

    `text` holds a human-readable rendering of the part regardless of type
    (the message body, the reasoning text, a tool's input/output summary).
    `raw` preserves the adapter's original parsed object for fidelity.
    """

    id: str
    type: PartType
    text: str = ""
    tool_name: str | None = None
    tool_status: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Part":
        """Reconstruct a Part from its `asdict` form (archive round-trip)."""
        return cls(
            id=d["id"],
            type=d["type"],
            text=d.get("text", "") or "",
            tool_name=d.get("tool_name"),
            tool_status=d.get("tool_status"),
            raw=d.get("raw") or {},
        )


@dataclass(frozen=True, slots=True)
class Message:
    """One turn in a conversation, composed of ordered parts."""

    id: str
    role: Role
    created: datetime | None
    parts: tuple[Part, ...] = ()
    model: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def text(self) -> str:
        """Concatenated text of all textual parts (text + reasoning)."""
        return "\n".join(p.text for p in self.parts if p.text)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Message":
        """Reconstruct a Message (and its Parts) from its `asdict` form."""
        return cls(
            id=d["id"],
            role=d["role"],
            created=_to_dt(d.get("created")),
            parts=tuple(Part.from_dict(p) for p in d.get("parts", ())),
            model=d.get("model"),
            raw=d.get("raw") or {},
        )


@dataclass(frozen=True, slots=True)
class Session:
    """A whole conversation: metadata plus (optionally) its messages.

    Listing operations populate metadata only and leave `messages` empty
    for speed; loading a single session populates `messages`.
    """

    id: str
    source: str  # adapter name, e.g. "opencode" / "claudecode"
    title: str
    directory: str | None
    created: datetime | None
    updated: datetime | None
    model: str | None = None
    agent: str | None = None
    parent_id: str | None = None
    message_count: int | None = None
    # Usage accounting (None when the source does not report a given figure).
    # `tokens_input` / `tokens_output` are fresh (uncached) prompt + generated
    # tokens; the cache figures track prompt-cache reuse (large in agentic
    # sessions and priced very differently); `tokens_reasoning` is the portion
    # of output spent on hidden reasoning, where the source distinguishes it.
    cost: float | None = None
    tokens_input: int | None = None
    tokens_output: int | None = None
    tokens_cache_read: int | None = None
    tokens_cache_write: int | None = None
    tokens_reasoning: int | None = None
    # Children populated when subagent folding is enabled.
    children: tuple["Session", ...] = ()
    messages: tuple[Message, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def short_id(self) -> str:
        """A compact id suitable for display and prefix selection."""
        return self.id[:12]

    @property
    def is_subagent(self) -> bool:
        """True if this session was spawned by another (has a parent)."""
        return bool(self.parent_id)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Session":
        """Reconstruct a Session (with children + messages) from its `asdict`
        form. Inverse of `dataclasses.asdict(session)`; the missing half of
        the archive round-trip. Computed properties (`short_id`,
        `is_subagent`) are not stored and are recomputed on access."""
        return cls(
            id=d["id"],
            source=d["source"],
            title=d["title"],
            directory=d.get("directory"),
            created=_to_dt(d.get("created")),
            updated=_to_dt(d.get("updated")),
            model=d.get("model"),
            agent=d.get("agent"),
            parent_id=d.get("parent_id"),
            message_count=d.get("message_count"),
            cost=d.get("cost"),
            tokens_input=d.get("tokens_input"),
            tokens_output=d.get("tokens_output"),
            tokens_cache_read=d.get("tokens_cache_read"),
            tokens_cache_write=d.get("tokens_cache_write"),
            tokens_reasoning=d.get("tokens_reasoning"),
            children=tuple(Session.from_dict(c) for c in d.get("children", ())),
            messages=tuple(Message.from_dict(m) for m in d.get("messages", ())),
            raw=d.get("raw") or {},
        )


# Re-export the converter for adapters.
__all__ = ["Part", "Message", "Session", "PartType", "Role", "_to_dt"]
