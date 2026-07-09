"""Synthetic, sanitized demo sessions for screenshots and local demos.

These are entirely made up -- no real project paths, prompts, or output --
so they can be screenshotted for the README without leaking anyone's data.
The content is chosen to exercise the features worth showing off: Markdown
with headings/lists, fenced code with syntax highlighting, and delimited
LaTeX math (the `rendered` mode typesets it with KaTeX).

`demo_store()` returns a `Store` backed by a read-only in-memory source, so
the web app and CLI can be pointed at it without any files on disk.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scrollback.models import Message, Part, Session
from scrollback.sources.base import Source
from scrollback.store import Store

_BASE = datetime(2026, 3, 14, 9, 30, tzinfo=timezone.utc)


def _dt(minutes: int) -> datetime:
    return _BASE + timedelta(minutes=minutes)


def _msg(mid: str, role: str, minutes: int, *parts: Part) -> Message:
    return Message(id=mid, role=role, created=_dt(minutes), parts=tuple(parts))


def _text(pid: str, body: str) -> Part:
    return Part(id=pid, type="text", text=body)


def _tool(pid: str, name: str, body: str, status: str = "completed") -> Part:
    return Part(id=pid, type="tool", text=body, tool_name=name, tool_status=status)


def _reasoning(pid: str, body: str) -> Part:
    return Part(id=pid, type="reasoning", text=body)


# -- the headline session: math + markdown + code ------------------------

_HEAT_USER = """\
Derive the 1-D heat equation update and give me a stable explicit scheme.
"""

_HEAT_ASSISTANT = """\
## The heat equation

Conservation of energy in one dimension gives the parabolic PDE

$$\\frac{\\partial u}{\\partial t} = \\alpha \\frac{\\partial^2 u}{\\partial x^2}$$

where $\\alpha > 0$ is the thermal diffusivity. A first-order-in-time,
second-order-in-space explicit (FTCS) discretization is

$$u_i^{n+1} = u_i^{n} + r\\left(u_{i+1}^{n} - 2u_i^{n} + u_{i-1}^{n}\\right),
\\qquad r = \\frac{\\alpha\\,\\Delta t}{\\Delta x^2}.$$

**Stability.** A von Neumann analysis requires $r \\le \\tfrac{1}{2}$, i.e.
the timestep is bounded by $\\Delta t \\le \\Delta x^2 / (2\\alpha)$.

Here is a compact NumPy implementation:

```python
import numpy as np

def ftcs(u0, alpha, dx, dt, steps):
    r = alpha * dt / dx**2
    assert r <= 0.5, f"unstable: r={r:.3f} > 1/2"
    u = u0.copy()
    for _ in range(steps):
        u[1:-1] += r * (u[2:] - 2 * u[1:-1] + u[:-2])
    return u
```

Pick `dt` from the stability bound and you get a well-behaved solver.
"""

_HEAT_TOOL = """\
$ python -c "import numpy, demo; print(demo.ftcs(...).max())"
0.4999987
"""


def _heat_session() -> Session:
    msgs = (
        _msg("h-u1", "user", 0, _text("h-p1", _HEAT_USER)),
        _msg(
            "h-a1", "assistant", 1,
            _reasoning("h-r1", "They want the PDE, an explicit scheme, and the "
                               "stability condition. Give FTCS with the r <= 1/2 bound."),
            _text("h-p2", _HEAT_ASSISTANT),
            _tool("h-t1", "bash", _HEAT_TOOL),
        ),
    )
    return Session(
        id="ses_demo_heat_eqn_0001", source="opencode",
        title="Derive a stable scheme for the heat equation",
        directory="~/projects/pde-solver", created=_dt(0), updated=_dt(2),
        model="claude-sonnet", agent="build",
        message_count=len(msgs), cost=0.021,
        tokens_input=1840, tokens_output=2310, messages=msgs,
    )


# -- a couple of list-filler sessions for a realistic sidebar ------------

def _refactor_session() -> Session:
    msgs = (
        _msg("r-u1", "user", 0, _text("r-p1",
            "Refactor `load_config` to validate types and raise clear errors.")),
        _msg("r-a1", "assistant", 1, _text("r-p2",
            "Done. I split parsing from validation and added a `ConfigError` "
            "with field-level messages:\n\n"
            "```python\n"
            "def load_config(path: str) -> Config:\n"
            "    data = _read(path)\n"
            "    return _validate(data)  # raises ConfigError(field, why)\n"
            "```")),
    )
    return Session(
        id="ses_demo_refactor_0002", source="claudecode",
        title="Refactor load_config with typed validation",
        directory="~/projects/api-gateway", created=_dt(-90), updated=_dt(-80),
        model="claude-opus", agent="build",
        message_count=len(msgs), cost=0.014,
        tokens_input=1200, tokens_output=900, messages=msgs,
    )


def _bugfix_session() -> Session:
    msgs = (
        _msg("b-u1", "user", 0, _text("b-p1",
            "Tests fail on timezone-naive timestamps. Fix the sort.")),
        _msg("b-a1", "assistant", 1, _text("b-p2",
            "The comparison mixed aware and naive datetimes. I normalize to "
            "UTC before sorting, so naive values no longer crash the listing.")),
    )
    return Session(
        id="ses_demo_bugfix_tz_0003", source="opencode",
        title="Fix timezone-naive timestamp crash in sort",
        directory="~/projects/scrollback", created=_dt(-1440), updated=_dt(-1438),
        model="claude-sonnet", agent="build",
        message_count=len(msgs), cost=0.006,
        tokens_input=640, tokens_output=410, messages=msgs,
    )


def demo_sessions() -> list[Session]:
    return [_heat_session(), _refactor_session(), _bugfix_session()]


class DemoSource(Source):
    """A read-only in-memory source of synthetic sessions."""

    name = "opencode"   # reuse a known source name so source-colour styling applies
    label = "demo"

    def __init__(self) -> None:
        self._sessions = {s.id: s for s in demo_sessions()}

    def is_available(self) -> bool:
        return True

    def location(self) -> Path | None:
        return Path("/demo")

    def list_sessions(self) -> Iterator[Session]:
        return iter(self._sessions.values())

    def load_session(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)


def demo_store() -> Store:
    return Store([DemoSource()])


def _extra_archived_session() -> Session:
    """A session that will be archived then 'deleted from its agent' -- to
    demonstrate the deleted (archive-only) provenance tag in the archive UI."""
    msgs = (
        _msg("d-u1", "user", 0, _text("d-p1",
            "Draft release notes for v0.4.0.")),
        _msg("d-a1", "assistant", 1, _text("d-p2",
            "Here are the highlights: durable archive, Live/Archive/All web "
            "modes, export/import for backup and cross-machine sync.")),
    )
    return Session(
        id="ses_demo_release_notes_0004", source="opencode",
        title="Draft release notes for v0.4.0",
        directory="~/projects/scrollback", created=_dt(-3000), updated=_dt(-2990),
        model="claude-sonnet", agent="build",
        message_count=len(msgs), cost=0.004,
        tokens_input=320, tokens_output=260, messages=msgs,
    )


class _ListSource(Source):
    """A minimal in-memory source over an explicit session list (for building a
    demo vault whose live set we control)."""

    name = "opencode"
    label = "demo"

    def __init__(self, sessions: list[Session]) -> None:
        self._sessions = {s.id: s for s in sessions}

    def is_available(self) -> bool:
        return True

    def location(self) -> Path | None:
        return Path("/demo")

    def list_sessions(self) -> Iterator[Session]:
        return iter(self._sessions.values())

    def load_session(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)


def build_demo_archive(vault_path: Path):
    """Build a synthetic archive vault on disk that exercises every provenance
    state for screenshots: archived-and-live, deleted-from-agent, and stale.

    Returns a `Store` over the live demo sources + the vault (the "all" store),
    so the web app shows Live/Archive/All with realistic tags + landing counts.
    """
    from dataclasses import replace

    from scrollback.archive import ArchiveStore

    live = demo_sessions()                      # 3 live sessions
    extra = _extra_archived_session()           # will become deleted-only

    vault = ArchiveStore(vault_path)
    # Archive all 3 live + the extra.
    vault.sync(Store([_ListSource([*live, extra])]))
    # Make one live session STALE: bump its updated + message_count so the live
    # copy is newer than the archived one.
    stale = replace(live[0], updated=_dt(30),
                    message_count=(live[0].message_count or 0) + 2)
    live_after = [stale, live[1], live[2]]      # `extra` dropped -> deleted-only

    return Store([_ListSource(live_after)]).with_archive(vault_path)


if __name__ == "__main__":
    # Quick manual check: print a one-line summary of the demo corpus.
    for s in demo_sessions():
        print(f"{s.source:11} {s.short_id}  {s.title}")
