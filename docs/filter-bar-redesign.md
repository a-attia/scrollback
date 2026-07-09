# Plan: Filter bar redesign — legible two-axis filtering

Status: **planned** (not started), as of 2026-07-08. A focused visual redesign
of the web UI's filter bar so the two filtering axes — **corpus** (live /
archive / all) and **source** (opencode / Claude Code / …) — read as distinct
controls instead of one undifferentiated row of pills.

See also: [`docs/web-redesign.md`](web-redesign.md) (the Live/Archive/All
feature this refines).

---

## 1. Problem

The filter bar packs three unrelated control types into one flat, evenly
spaced row (`static/index.html` `.filters`):

- **mode switch** `live / archive / all` — single-select (radio) — picks the
  *corpus*;
- **source filter** chips — multi-select (checkboxes) — picks the *agents*;
- **date filters** `since / until`.

Both the mode switch and the source chips render as pill shapes, so the eye
reads six sibling pills rather than "one switch + a filter set". The lowercase
`SHOWING` label floats ambiguously between them. Users cannot tell that
clicking `archive` *replaces* the view while clicking `Codex` *adds* to it,
because the two selection models look identical.

## 2. Design direction

Make the bar read as a sentence, with two visually distinct control families:

```text
Showing  [ Live | Archive | All ]   from   (✓ opencode)(✓ Claude Code)  · Codex · Aider     since ▸ until ▸
 eyebrow      segmented control     conn.        multi-select chips            (dimmed)        date range
```

- **Two shapes for two selection models.**
  - *Mode* = a true **segmented control**: one connected unit, a single filled
    active segment. Unmistakably "pick one".
  - *Sources* = **multi-select chips**: lighter, flatter, checkmark = on.
    Visually subordinate to the segmented control so they don't compete.
- **Labels as connective tissue.** Replace the floating `SHOWING` with an
  eyebrow `Showing` before the mode switch and a quiet connector `from`
  before the sources: *"Showing **Live** from **opencode, Claude Code**."*
- **Spatial zones.** A hairline divider (or larger gap) separates the mode
  switch from the source group.
- **Unavailable sources** read as plainly inert (dotted/dimmed, not
  almost-clickable).
- **Responsive.** Date filters wrap to their own line under the sources on
  narrow widths.

Boldness is spent in one place — the two-family structure. Everything else
stays quiet (no new colour, no decoration). Palette + type unchanged (mono
utility face, existing `--focus` / per-source accents).

## 3. Changes

- `static/index.html` `.filters`: add the `Showing` eyebrow + `from`
  connector; keep `#modeswitch` and `#srcfilter`; add a divider element.
- `static/style.css`:
  - `.modeswitch` → refine into a segmented control (connected segments,
    filled active segment, clearer than the current subtle tint).
  - `.src-toggle` → lighter multi-select chip treatment; stronger inert state
    for `.src-unavailable`.
  - `.filters` → grouping (gaps, divider, eyebrow/connector label styles);
    responsive wrap for dates.
- `static/app.js`: no behavioural change (same `state.mode` / `state.enabled`
  and handlers). Only markup the loader emits for chips may gain a class.

## 4. Non-goals

- No change to what the controls *do* (mode + source semantics unchanged).
- No header restructure (mode switch stays in the filter bar).
- No new endpoints or backend changes.

## 5. Acceptance

- The mode switch and source chips are visually distinguishable as
  single-select vs multi-select at a glance.
- The row reads as "Showing <mode> from <sources>".
- Tooltips remain on every control; keyboard focus visible; reduced-motion
  respected; works down to a narrow (split-screen) width.
