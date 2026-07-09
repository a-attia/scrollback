"""Unit tests for pure model/conversion helpers (no I/O, deterministic)."""

from dataclasses import asdict
from datetime import datetime, timezone

from scrollback import archivefmt
from scrollback.models import Message, Part, Session, _to_dt


def test_to_dt_epoch_millis():
    # 1772993881154 ms == 2026-03-06T... UTC; verify round-trip to seconds.
    dt = _to_dt(1772993881154)
    assert dt is not None
    assert dt.tzinfo is timezone.utc
    # Expected value derived directly from the input: 1772993881.154 s.
    assert abs(dt.timestamp() - 1772993881.154) < 1e-3


def test_to_dt_iso_with_z():
    dt = _to_dt("2026-06-08T00:25:52.018Z")
    assert dt is not None
    assert dt.year == 2026 and dt.month == 6 and dt.day == 8
    assert dt.tzinfo is not None


def test_to_dt_none_and_garbage():
    assert _to_dt(None) is None
    assert _to_dt("") is None
    assert _to_dt("not-a-date") is None


def test_to_dt_tzless_iso_is_made_aware():
    # Regression: a timezone-less ISO timestamp must become tz-aware (UTC),
    # otherwise sorting it alongside aware datetimes raises TypeError.
    dt = _to_dt("2024-01-01T00:00:00")  # no Z / offset
    assert dt is not None
    assert dt.tzinfo is not None
    # And it must be comparable with an aware datetime (no crash).
    aware = _to_dt("2024-01-02T00:00:00Z")
    assert sorted([aware, dt]) == [dt, aware]


def test_message_text_concatenates_textual_parts():
    parts = (
        Part(id="1", type="text", text="hello"),
        Part(id="2", type="reasoning", text="thinking"),
        Part(id="3", type="tool", text="$ ls", tool_name="bash"),
    )
    msg = Message(id="m1", role="assistant", created=None, parts=parts)
    # `.text` includes text + reasoning (both carry .text); tool also has text.
    assert "hello" in msg.text
    assert "thinking" in msg.text


def test_session_short_id():
    s = Session(
        id="ses_0123456789abcdef",
        source="opencode",
        title="t",
        directory=None,
        created=None,
        updated=None,
    )
    assert s.short_id == "ses_01234567"
    assert len(s.short_id) == 12


# -- archive round-trip (Component 1: lossless serialize/deserialize) --------


def _rich_session(sid="ses_abc123", source="opencode", *, with_child=True):
    """A synthetic session exercising every field that must round-trip:
    datetimes, raw blobs at every level, tool parts, None fields, children."""
    created = datetime(2026, 3, 6, 12, 0, 0, tzinfo=timezone.utc)
    updated = datetime(2026, 3, 6, 12, 30, 15, 123000, tzinfo=timezone.utc)
    parts = (
        Part(id=f"{sid}-p0", type="text", text="hello", raw={"type": "text", "text": "hello"}),
        Part(id=f"{sid}-p1", type="reasoning", text="thinking", raw={"nested": {"a": [1, 2, 3]}}),
        Part(
            id=f"{sid}-p2",
            type="tool",
            text="$ ls",
            tool_name="bash",
            tool_status="completed",
            raw={"state": {"status": "completed"}, "tool": "bash"},
        ),
    )
    msg = Message(
        id=f"{sid}-m0",
        role="assistant",
        created=created,
        parts=parts,
        model="claude-sonnet",
        raw={"provider": "anthropic"},
    )
    child = None
    if with_child:
        child = _rich_session(sid=f"{sid}::sub", source=source, with_child=False)
        child = Session(  # give the child a parent_id pointing at us
            **{**asdict(child), "parent_id": sid,
               "created": child.created, "updated": child.updated,
               "children": (), "messages": child.messages}
        )
    return Session(
        id=sid,
        source=source,
        title="A rich session",
        directory="/tmp/proj",
        created=created,
        updated=updated,
        model="claude-sonnet",
        agent="build",
        parent_id=None,
        message_count=1,
        cost=0.0123,
        tokens_input=100,
        tokens_output=None,  # keep a None field in the round-trip
        tokens_cache_read=50,
        tokens_cache_write=0,
        tokens_reasoning=None,
        children=(child,) if child else (),
        messages=(msg,),
        raw={},  # opencode leaves session raw empty
    )


def test_part_from_dict_round_trip():
    p = Part(id="p", type="tool", text="x", tool_name="bash",
             tool_status="ok", raw={"k": "v"})
    assert Part.from_dict(asdict(p)) == p


def test_message_from_dict_round_trip():
    created = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    m = Message(id="m", role="user", created=created,
                parts=(Part(id="p", type="text", text="hi"),),
                model="gpt", raw={"x": 1})
    assert Message.from_dict(asdict(m)) == m


def test_session_from_dict_round_trip_asdict():
    """Linchpin: from_dict(asdict(s)) == s, children + raw + datetimes intact."""
    s = _rich_session()
    assert Session.from_dict(asdict(s)) == s


def test_archive_json_round_trip():
    """Full lossless path: from_archive_json(to_archive_json(s)) == s."""
    s = _rich_session()
    restored = archivefmt.from_archive_json(archivefmt.to_archive_json(s))
    assert restored == s


def test_archive_envelope_shape():
    s = _rich_session()
    d = archivefmt.to_archive_dict(s)
    assert d["schema_version"] == archivefmt.SCHEMA_VERSION
    assert "archived_at" in d and "scrollback_version" in d
    assert d["session"]["id"] == s.id
    # raw blobs are kept (unlike export.to_json)
    assert d["session"]["messages"][0]["parts"][0]["raw"] == {"type": "text", "text": "hello"}


def test_archive_json_datetimes_are_iso_strings():
    s = _rich_session()
    import json

    raw = json.loads(archivefmt.to_archive_json(s))
    assert raw["session"]["created"] == s.created.isoformat()
    assert raw["session"]["messages"][0]["created"] == s.messages[0].created.isoformat()


def test_archive_json_rejects_non_json_native_raw():
    """Fidelity guard: a non-JSON-native value in raw raises, not str()-coerced."""
    import pytest

    bad = Session(
        id="s", source="x", title="t", directory=None,
        created=None, updated=None,
        messages=(Message(id="m", role="user", created=None,
                          parts=(Part(id="p", type="text", raw={"obj": object()}),)),),
    )
    with pytest.raises(TypeError):
        archivefmt.to_archive_json(bad)


def test_from_dict_tolerates_missing_optional_keys():
    """A minimal dict (only required keys) reconstructs with defaults."""
    s = Session.from_dict({
        "id": "s", "source": "x", "title": "t",
        "directory": None, "created": None, "updated": None,
    })
    assert s.id == "s"
    assert s.messages == ()
    assert s.children == ()
    assert s.raw == {}
