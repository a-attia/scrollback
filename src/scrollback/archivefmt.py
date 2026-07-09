"""Lossless serialize / deserialize for the durable archive.

Unlike `export.to_json` (which strips every `raw` blob for readability) and
`serialize.py` (API-shaped, lossy), this module is the *lossless* round-trip
the archive relies on: `from_archive_json(to_archive_json(s))` reconstructs a
`Session` equal to the original, `raw` blobs included.

The on-disk shape is a small envelope wrapping the full session dict::

    {
      "schema_version": 1,
      "archived_at": "<ISO-8601 UTC>",
      "scrollback_version": "<pkg version>",
      "session": { ...full asdict(session), raw kept, datetimes as ISO... }
    }

The envelope's `schema_version` lets a future scrollback migrate old archives.
See `docs/archive-plan.md` Component 1.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from . import __version__
from .models import Session

#: Bump when the archive dict shape changes in a non-backward-compatible way.
SCHEMA_VERSION = 1


def _json_default(o: object) -> object:
    """JSON encoder fallback: datetimes -> explicit ISO strings.

    Deliberately strict: anything that is not a datetime raises `TypeError`
    rather than being silently coerced via `str()` (the lossy behavior in
    `export.to_json`). A non-JSON-native value in `raw` is a fidelity bug we
    want to surface, not paper over.
    """
    if isinstance(o, datetime):
        return o.isoformat()
    raise TypeError(
        f"archive fidelity: non-JSON-native value of type {type(o).__name__!r} "
        f"in session data: {o!r}"
    )


def to_archive_dict(session: Session) -> dict[str, Any]:
    """Full, lossless dict for `session`, wrapped in the archive envelope.

    Keeps every `raw` blob (unlike `export.to_json`). Datetimes remain
    `datetime` objects here; JSON serialization converts them via
    `_json_default`. Computed properties (`short_id`, `is_subagent`) are not
    stored -- they are recomputed on `from_dict`.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "archived_at": datetime.now(timezone.utc).isoformat(),
        "scrollback_version": __version__,
        "session": asdict(session),
    }


def to_archive_json(session: Session, *, indent: int | None = None) -> str:
    """Serialize `session` to a lossless archive JSON string."""
    return json.dumps(
        to_archive_dict(session),
        default=_json_default,
        ensure_ascii=False,
        indent=indent,
    )


def from_archive_dict(d: dict[str, Any]) -> Session:
    """Reconstruct a `Session` from an archive envelope dict.

    Accepts either the enveloped form (`{"session": {...}, ...}`) or a bare
    session dict, so callers that have already unwrapped the envelope still
    work.
    """
    payload = d.get("session", d) if isinstance(d, dict) else d
    return Session.from_dict(payload)


def from_archive_json(text: str) -> Session:
    """Reconstruct a `Session` from a lossless archive JSON string."""
    return from_archive_dict(json.loads(text))


def schema_version_of(d: dict[str, Any]) -> int | None:
    """Read the `schema_version` from an archive envelope dict, if present."""
    return d.get("schema_version") if isinstance(d, dict) else None
