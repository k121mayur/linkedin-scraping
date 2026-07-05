# Design: Fixed-Height Layout + Jobs/Grants Tabs

**Date:** 2026-07-05
**Status:** Approved

## Problem

The current web UI (`templates/index.html`, served by the single `GET /` route in
`app.py`) is a single scrollable page containing the job-extraction form, live
progress, and download links. Two changes are needed:

1. The page (and any future page state) must never show a scrollbar — it should
   render as a fixed-size window that fits within the viewport at any window size.
2. The app needs a second mode, **Grants**, alongside the existing **Jobs** mode,
   selectable via a navbar with two tabs. The Grants tab reuses the same UI shell
   as Jobs but has no working backend yet — that's a follow-up project once this
   UI shell lands.

## Non-goals

- No grants scraping backend, LLM prompt parsing, or LinkedIn-grants Playwright
  driver is built in this pass. The Grants tab is a visual/structural shell only.
- No new Flask routes. Everything continues to be served by the existing
  `GET /` route rendering `templates/index.html`.
- No changes to the Jobs pipeline (`engine/*`) or its JS wiring
  (`#scrape-form`, `/scrape`, `/stream/<run_id>`, `/download/<run_id>/<fmt>`,
  `/stop/<run_id>`) — those stay exactly as they are today.

## Architecture

Everything stays in the single `templates/index.html` template, rendered by the
existing `GET /` route. No backend route changes.

### Tab navigation (client-side, no reload)

A new nav row is added directly below the masthead: a pill-style segmented
control (`role="tablist"`) with two tabs, "Jobs" and "Grants"
(`role="tab"`, `aria-selected`, `aria-controls`). Styled consistent with the
existing design system (same shape language as `.status-pill` / `.chip`).

Below the nav row, the current hero+panel markup is wrapped as
`<section data-tab-panel="jobs" role="tabpanel">`, and a structurally identical
clone is added as `<section data-tab-panel="grants" role="tabpanel" hidden>`.

A small vanilla-JS tab controller:
- Toggles the `hidden` attribute on whichever panel isn't active.
- Flips `aria-selected` on the tab buttons.
- Supports left/right arrow-key navigation between tabs (standard ARIA tablist
  keyboard pattern), plus click.
- Does **not** reset form state — each panel keeps whatever the user typed when
  they switch away and back, because both panels stay in the DOM (just hidden),
  not re-rendered.

The existing Jobs JS (`#scrape-form` submit handler, `EventSource` progress
stream, stop button, download links) is untouched and stays scoped to element
IDs inside the `data-tab-panel="jobs"` section.

### Fixed-size / no-scroll layout

- `html, body` change from `min-height: 100dvh` to `height: 100dvh; overflow: hidden`
  so no viewport ever shows a scrollbar, at any window size.
- Adding the new nav row costs vertical space, so existing `clamp()`-based
  spacing (masthead padding, hero gaps, panel padding) is trimmed slightly to
  keep everything fitting at typical laptop heights (~800px) down to smaller
  windows.
- The `.panel` (form + progress area) gets a `max-height` capped against
  remaining viewport space, with `overflow-y: auto` scoped to *that inner
  element only* — a safety valve for edge cases (e.g. a very short window with
  progress stats + download buttons all visible at once simultaneously). The
  outer page frame stays fixed regardless; this inner scroll is not expected to
  engage in normal use at reasonable window sizes.
- Existing responsive breakpoints (920px / 560px) are kept and re-validated
  against the new nav row so nothing clips at mobile widths either.

### Grants tab content

A structural clone of the Jobs panel:
- Same headline/intro treatment, copy reworded for grants (e.g. "Turn one
  sentence into a ranked list of LinkedIn grant opportunities").
- Same textarea + example-prompt chips, reworded for grants (e.g. "Education
  grants for NGOs in Maharashtra").
- Same target-count field with 50/100/250 presets.
- Submit button is `disabled` and relabeled "Coming soon", with a short note
  under it explaining grants extraction isn't live yet.
- No JS wiring to any backend endpoint — this tab is inert beyond the tab
  switch itself.

## Testing / verification

No test suite exists in this repo (per `CLAUDE.md`). Verification is manual:
run `python app.py` (DRY_RUN mode is fine — this is a pure frontend change),
open `http://localhost:5000`, and confirm:
- No scrollbar appears on the page at various window sizes (resize the browser
  window, including short heights).
- Jobs tab behaves exactly as before (prompt → progress → downloads).
- Switching to Grants shows the cloned shell with a disabled "Coming soon"
  submit button, and switching back to Jobs preserves whatever was typed.
- Keyboard arrow-key navigation between tabs works; screen-reader semantics
  (`role="tablist"/"tab"/"tabpanel"`, `aria-selected`) are present.
