# Contributing to scrollback

Thanks for your interest. scrollback is a small, local-first, read-only
tool, and contributions that keep it that way are very welcome.

## Development setup

```bash
git clone https://github.com/a-attia/scrollback
cd scrollback
python -m pip install -e ".[dev]"   # editable install + dev tooling
```

Everything a user needs at runtime (the CLI, the web app, the native app
window, and coloured terminal output) is installed by default with the
package itself; the `[dev]` extra adds test/lint tooling on top. Requires
Python 3.10+.

## Running the checks

```bash
pytest -q          # the test suite
ruff check src tests   # lint
```

Both must pass before a change is merged. The test suite is fast (~4s) and
runs against synthetic fixtures plus, where present, your real local data
(those tests skip gracefully when no data is available).

The lint rule set is stated explicitly in `pyproject.toml` under
`[tool.ruff.lint]`, and the `dev` extra caps the ruff version. Both are
deliberate: ruff's *default* rule set grows between releases, so an
unpinned linter changes what "clean" means without anyone touching the
code. Widening the rule set is welcome — as its own change, with the
resulting fixes in their own commit.

## Project conventions

- **Read-only, always.** Nothing in scrollback may write to, lock for
  writing, or upload a user's agent data. The opencode SQLite DB is opened
  with `mode=ro`; JSONL files are read-only. Tests assert this invariant.
- **Lightest tool that does the job.** Prefer the stdlib. New runtime
  dependencies for the core CLI are a hard sell; put optional features
  behind extras (see `[project.optional-dependencies]`).
- **Platform-agnostic.** Keep OS-specific code guarded by `sys.platform`
  and best-effort (it must degrade, not crash, elsewhere). Window/icon
  handling lives in Python, not baked into per-OS launcher scripts.
- **Tests for fixes.** Bug fixes should come with a regression test;
  numeric/parsing assertions should be backed by a known-correct value.

## Test discipline

Beyond "add a test", two rules have earned their place the hard way.

**Verify the test fails without the fix.** A regression test that passes
against the unfixed code is not testing what you think it is. The cheap
check:

```bash
git stash push -- src/ && pytest -q; git stash pop
```

**Fakes must be able to express real-world messiness.** Several bugs here
were invisible for months because every test fake was too well-behaved to
exhibit them. The clearest example: a `Source` whose `list_sessions()`
reports a different `message_count` than `load_session()` — which real
adapters do — breaks archive change-detection, but no fake could produce
that state, so no test could catch it. `AsymmetricSource` in
`tests/test_archive.py` exists for exactly this; use it when touching sync.

If you are working on the archive or on a source adapter, read the
"Signature discipline" section of [`AGENTS.md`](AGENTS.md) first. It
documents two classes of bug that have already shipped once.

## Adding a new agent source

Implement the `Source` interface in `src/scrollback/sources/base.py` and
register it in `src/scrollback/sources/registry.py`. Everything else (CLI,
search, export, web, index) works against the common model automatically.
See `opencode.py` (SQLite) and `claudecode.py` (JSONL) as references.

## Regenerating the README screenshots

The images in the README are generated from synthetic, sanitized demo data
(`scripts/demo_data.py`) — never from real sessions — so they are safe to
publish. To regenerate them after a UI change:

```bash
pip install -e ".[screenshots]"
playwright install chromium       # one-time headless-browser download
python scripts/screenshots.py     # writes assets/screenshots/{cli.svg,cli.png,web.png}
```

The CLI image is rendered with `rich` (SVG for GitHub, plus a PNG for PyPI,
which does not display SVGs); the web image is captured with headless
Chromium via Playwright. The README embeds the PNGs via absolute,
release-pinned `raw.githubusercontent.com` URLs so they render on both
GitHub and PyPI (relative paths only work on GitHub). When cutting a new
release, bump the version in those URLs. Neither the `screenshots` extra nor
the browser is needed to run scrollback.

## Submitting changes

1. Fork and branch.
2. Make the change with a focused scope and a test.
3. Run `pytest -q` and `ruff check src tests`.
4. Open a pull request describing the change and how you verified it.
