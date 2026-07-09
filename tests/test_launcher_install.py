"""Tests for the bundled-launcher installer.

These assert that the launcher templates are packaged (readable via
importlib.resources) and that `install()` writes the expected files into a
temp destination -- without touching the real Desktop/Applications.
"""

import sys
from importlib import resources

import pytest

from scrollback import launcher_install


def test_launcher_templates_are_packaged():
    # All four templates must be importable as package data (i.e. shipped).
    for name in (
        "scrollback.command",
        "scrollback.bat",
        "scrollback.sh",
        "scrollback.desktop",
    ):
        text = resources.files("scrollback.launchers").joinpath(name).read_text()
        assert "scrollback" in text


def test_install_writes_into_dest(tmp_path):
    created = launcher_install.install(tmp_path)
    assert created, "install should create at least one file"
    for p in created:
        assert p.exists()
    # The platform-appropriate primary launcher should be present.
    names = {p.name for p in created}
    if sys.platform == "darwin":
        assert "scrollback.command" in names
    elif sys.platform == "win32":
        assert "scrollback.bat" in names
    else:
        assert "scrollback.desktop" in names


def test_install_no_flags_creates_everything_on_macos(tmp_path):
    # Bare install (neither selector) = give me everything. On macOS that is
    # the Desktop launcher AND the .app bundle.
    created = launcher_install.install(tmp_path)
    names = {p.name for p in created}
    if sys.platform == "darwin":
        assert "scrollback.command" in names
        assert "scrollback.app" in names


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-specific selectors")
def test_desktop_only_skips_app(tmp_path):
    created = launcher_install.install(tmp_path, desktop=True)
    names = {p.name for p in created}
    assert "scrollback.command" in names
    assert "scrollback.app" not in names


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS .app bundle only")
def test_app_bundle_only_skips_desktop_command(tmp_path):
    # --app-bundle alone yields ONLY the .app -- no loose .command file.
    created = launcher_install.install(tmp_path, app_bundle=True)
    names = {p.name for p in created}
    assert "scrollback.app" in names
    assert "scrollback.command" not in names

    app = next(p for p in created if p.name == "scrollback.app")
    assert (app / "Contents" / "Info.plist").is_file()
    runner = app / "Contents" / "MacOS" / "scrollback"
    assert runner.is_file()
    # Runner must be executable.
    import os
    import stat

    assert os.stat(runner).st_mode & stat.S_IXUSR


@pytest.mark.skipif(sys.platform == "darwin", reason="non-macOS fallback")
def test_app_bundle_falls_back_to_desktop_launcher(tmp_path):
    # On Windows/Linux there is no .app, so --app-bundle still installs the
    # platform's clickable launcher instead of erroring.
    created = launcher_install.install(tmp_path, app_bundle=True)
    assert created
    assert not any(p.name == "scrollback.app" for p in created)


def test_runner_bakes_absolute_interpreter_path():
    # Regression guard: GUI/Finder launches run with a minimal PATH that
    # excludes conda/venv bins, so the runner must NOT rely on PATH lookup of
    # `scrollback` or a bare `python3`. It must bake sys.executable's path.
    script = launcher_install._runner_script()
    assert sys.executable in script
    assert "scrollback.cli web" in script


def test_command_script_bakes_absolute_interpreter_path():
    script = launcher_install._command_script()
    assert sys.executable in script
    assert "scrollback.cli web" in script


def test_installed_artifacts_finds_what_install_created(tmp_path, monkeypatch):
    # Point HOME at a temp dir, install to the (temp) Desktop, then confirm
    # installed_artifacts() reports the created launcher and that remove_path
    # actually deletes it. Never touches the real home.
    monkeypatch.setattr(launcher_install.Path, "home", classmethod(lambda cls: tmp_path))
    desktop = tmp_path / "Desktop"
    desktop.mkdir()

    created = launcher_install.install(desktop, desktop=True)
    assert created and all(p.exists() for p in created)

    found = launcher_install.installed_artifacts()
    # The platform's Desktop launcher should be discovered.
    assert any(p in found for p in created), (found, created)

    for p in found:
        launcher_install.remove_path(p)
        assert not p.exists()


def test_installed_artifacts_empty_when_nothing_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher_install.Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / "Desktop").mkdir()
    assert launcher_install.installed_artifacts() == []


# -- footprint model --------------------------------------------------------


def _isolate_home(tmp_path, monkeypatch):
    """Point every scrollback path (home, index, vault) at a temp home."""
    monkeypatch.setattr(launcher_install.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("SCROLLBACK_INDEX", str(tmp_path / ".cache" / "scrollback" / "index.db"))
    monkeypatch.setenv("SCROLLBACK_ARCHIVE", str(tmp_path / ".scrollback" / "archive"))
    (tmp_path / "Desktop").mkdir(exist_ok=True)


def test_footprint_empty_when_nothing_created(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    assert launcher_install.footprint() == []


def test_footprint_catches_browser_profile_and_index(tmp_path, monkeypatch):
    """The browser profile (the ghost the old uninstall missed) is tracked."""
    _isolate_home(tmp_path, monkeypatch)
    cache = tmp_path / ".cache" / "scrollback"
    (cache / "browser").mkdir(parents=True)
    (cache / "browser" / "prefs").write_text("{}")
    (cache / "index.db").write_text("idx")

    fp = {e.path: e.tier for e in launcher_install.footprint()}
    assert cache / "browser" in fp and fp[cache / "browser"] == "disposable"
    assert cache / "index.db" in fp and fp[cache / "index.db"] == "disposable"
    assert cache in fp  # parent cache dir tracked too


def test_footprint_tags_vault_durable(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    from scrollback.archive import ArchiveStore
    from scrollback.store import Store
    from tests.test_archive import FakeSource, _session

    ArchiveStore(tmp_path / ".scrollback" / "archive").sync(
        Store([FakeSource([_session("s1")])]))
    durable = [e for e in launcher_install.footprint() if e.tier == "durable"]
    assert len(durable) == 1
    assert "archive" in str(durable[0].path)
