"""Tests for the Aider source adapter (synthetic .aider.chat.history.md)."""

from pathlib import Path

from scrollback.sources.aider import AiderSource, _is_unsafe_root, _search_roots

_HISTORY = """# aider chat started at 2025-01-31 12:34:56

#### add a function to parse dates

Sure, here is a `parse_date` helper.

```python
def parse_date(s): ...
```

#### now add tests

Added tests in test_dates.py.

# aider chat started at 2025-02-01 09:00:00

#### second session

Second reply.
"""


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "myproject"
    proj.mkdir()
    (proj / ".aider.chat.history.md").write_text(_HISTORY)
    return proj


def test_aider_not_available_when_empty(tmp_path):
    assert AiderSource(roots=[tmp_path]).is_available() is False


def test_aider_does_not_scan_without_optin(monkeypatch):
    # No SCROLLBACK_AIDER_DIRS => no roots => Aider is unavailable and never
    # walks the filesystem (prevents macOS permission prompts on app launch).
    monkeypatch.delenv("SCROLLBACK_AIDER_DIRS", raising=False)
    assert _search_roots() == []
    assert AiderSource().is_available() is False


def test_aider_refuses_unsafe_roots():
    home = Path.home()
    assert _is_unsafe_root(Path("/")) is True
    assert _is_unsafe_root(home) is True
    assert _is_unsafe_root(home / "Pictures") is True
    assert _is_unsafe_root(home / "Library") is True
    assert _is_unsafe_root(home / "Documents") is True


def test_aider_optin_via_env(tmp_path, monkeypatch):
    proj = _project(tmp_path)
    monkeypatch.setenv("SCROLLBACK_AIDER_DIRS", str(proj.parent))
    src = AiderSource()  # picks up the env var
    assert src.is_available() is True
    assert len(list(src.list_sessions())) == 2


def test_aider_splits_runs_into_sessions(tmp_path):
    _project(tmp_path)
    src = AiderSource(roots=[tmp_path])
    assert src.is_available()
    sessions = list(src.list_sessions())
    assert len(sessions) == 2
    titles = sorted(s.title for s in sessions)
    assert titles == ["add a function to parse dates", "second session"]
    # created times parsed from the 'started at' markers
    assert all(s.created is not None for s in sessions)
    assert all(s.source == "aider" for s in sessions)


def test_aider_loads_messages(tmp_path):
    proj = _project(tmp_path)
    src = AiderSource(roots=[tmp_path])
    first = next(s for s in src.list_sessions() if s.title.startswith("add a function"))
    full = src.load_session(first.id)
    assert full is not None
    assert full.directory == str(proj)
    roles = [m.role for m in full.messages]
    assert roles == ["user", "assistant", "user", "assistant"]
    assert "parse_date" in full.messages[1].text


def test_last_block_updated_tracks_file_mtime(tmp_path):
    """The in-progress session must sort by recency, not by when it started.

    Aider's history format only timestamps the "chat started at" marker, so a
    block has no last-activity time of its own. Using the start time for the
    still-growing final block buried an actively-used session at the bottom of
    a date-sorted list.
    """
    _project(tmp_path)
    sessions = sorted(AiderSource(roots=[tmp_path]).list_sessions(),
                      key=lambda s: s.created)
    older, newest = sessions

    # The closed block (a later marker follows it) keeps its start time.
    assert older.updated == older.created

    # The final block is still being appended to -> mtime, which is "now".
    assert newest.updated > newest.created


def test_appending_to_history_changes_the_signature(tmp_path):
    """Growth must be detectable, or the archive can never refresh the copy."""
    proj = _project(tmp_path)
    hist = proj / ".aider.chat.history.md"

    def sig():
        s = sorted(AiderSource(roots=[tmp_path]).list_sessions(),
                   key=lambda x: x.created)[-1]
        return (s.updated, s.message_count)

    before = sig()
    hist.write_text(hist.read_text() + "\n#### another question\n\nAnother reply.\n",
                    encoding="utf-8")
    assert sig() != before
