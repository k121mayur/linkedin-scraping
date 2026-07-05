# Fixed-Height Layout + Jobs/Grants Tabs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the web UI (`templates/index.html`) render as a fixed, non-scrollable viewport, and add a Jobs/Grants tab navbar where Jobs keeps today's functionality and Grants is a visual-only "coming soon" shell.

**Architecture:** Everything is a single-file frontend change to `templates/index.html` (no backend routes change). CSS switches the page shell from a naturally-tall scrolling document to a `100dvh` flex column where only the `.panel` element (the form/progress card) can ever scroll internally, as a safety valve — never the page itself. A small vanilla-JS ARIA-tablist controller toggles which of two sibling panels (`#panel-jobs`, `#panel-grants`) is visible, without a page reload. The existing Jobs form/JS is untouched; a structurally identical Grants panel is added with its own element IDs and a permanently-disabled submit button.

**Tech Stack:** Flask (Jinja template, unchanged route), vanilla HTML/CSS/JS (no new dependencies). Verification uses Playwright (already a project dependency per `requirements.txt`) driven headless via the project's existing venv — no new test framework is introduced.

## Global Constraints

- No backend route changes — everything stays on the existing `GET /` route in `app.py` rendering `templates/index.html`.
- No changes to the Jobs pipeline or its existing JS wiring (`#scrape-form` submit handler, `/scrape`, `/stream/<run_id>`, `/stop/<run_id>`, `/download/<run_id>/<fmt>`).
- No new Flask routes or grants backend logic — the Grants tab is a UI shell only, with its submit button permanently `disabled`.
- The page (`html`/`body`) must never show a scrollbar at any viewport size. Only `.panel` may scroll internally, and only as a rare safety valve.
- Existing responsive breakpoints (920px / 560px) are kept.
- This repo has no test suite (per `CLAUDE.md`) — verification is done by running the Flask app in `DRY_RUN=true` mode and driving it with a throwaway Playwright script via the project's existing venv Python (`./venv/Scripts/python.exe`), never by inventing a pytest suite.
- Dev server default port is `5000` (`config/__init__.py` → `FLASK_PORT`).

### Starting/stopping the dev server during verification (Windows/git-bash specifics)

On this Windows + git-bash setup, `kill $!` on a backgrounded `python.exe` does **not** reliably terminate it (confirmed by testing: the server kept responding after `kill $!`). Use this pattern for every verification step instead:

Start:
```bash
cd "e:/Silicon Mango/linkedin-scraping"
DRY_RUN=true ./venv/Scripts/python.exe app.py > /tmp/app_verify.log 2>&1 &
sleep 3
```

Stop (finds the real Windows PID bound to port 5000 and force-kills it):
```bash
WINPID=$(netstat -ano | grep ':5000' | grep LISTENING | awk '{print $NF}' | head -n1)
taskkill //PID "$WINPID" //F
```

If a verification step fails partway through, always run the stop command before retrying, or the next `app.py` start will fail with "address already in use."

---

### Task 1: Fixed-height, non-scrollable page shell (CSS only)

**Files:**
- Modify: `templates/index.html:68-78` (`body` rule)
- Modify: `templates/index.html:121-128` (`.page` rule)
- Modify: `templates/index.html:133-139` (`.masthead` rule)
- Modify: `templates/index.html:188-193` (`main.shell` rule)
- Modify: `templates/index.html:194-200` (`.hero` rule)
- Modify: `templates/index.html:203` (`.intro` rule)
- Modify: `templates/index.html:255-263` (`.panel` rule)
- Modify: `templates/index.html:552-564` (`footer` rule)

**Interfaces:**
- Consumes: nothing new — pure CSS edit of the existing single-page template.
- Produces: a page shell where `html`/`body` never scroll and `.panel` is the only element with `overflow-y: auto`, bounded to the space left after the (now shrink-proof) masthead, intro, and footer. Task 2 and Task 3 build the tab UI on top of this shell and must not reintroduce page-level scrolling.

This CSS approach was empirically validated (headless Playwright against a minimal reproduction of this exact flex chain): at a normal laptop viewport nothing scrolls anywhere; at artificially short/squeezed viewports the page itself never overflows and only `.panel` gains an internal scrollbar.

- [ ] **Step 1: Edit the `body` rule**

In `templates/index.html`, find:
```css
    body {
      min-height: 100dvh;
      font-family: var(--font-body);
      color: var(--text);
      background: var(--bg-base);
      -webkit-font-smoothing: antialiased;
      text-rendering: optimizeLegibility;
      line-height: 1.55;
      position: relative;
      overflow-x: hidden;
    }
```
Replace with:
```css
    body {
      height: 100dvh;
      font-family: var(--font-body);
      color: var(--text);
      background: var(--bg-base);
      -webkit-font-smoothing: antialiased;
      text-rendering: optimizeLegibility;
      line-height: 1.55;
      position: relative;
      overflow: hidden;
    }
```

- [ ] **Step 2: Edit the `.page` rule**

Find:
```css
    .page {
      position: relative;
      z-index: var(--z-content);
      min-height: 100dvh;
      display: flex;
      flex-direction: column;
      padding: clamp(1.25rem, 3vw, 2.25rem);
    }
```
Replace with:
```css
    .page {
      position: relative;
      z-index: var(--z-content);
      height: 100dvh;
      display: flex;
      flex-direction: column;
      padding: clamp(1.25rem, 3vw, 2.25rem);
      overflow: hidden;
    }
```

- [ ] **Step 3: Pin the masthead to its natural size**

Find:
```css
    .masthead {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 1rem;
      padding-bottom: clamp(1.5rem, 4vw, 3rem);
    }
```
Replace with:
```css
    .masthead {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 1rem;
      padding-bottom: clamp(1.5rem, 4vw, 3rem);
      flex-shrink: 0;
    }
```

- [ ] **Step 4: Let `main.shell` absorb remaining space and clip instead of overflow**

Find:
```css
    main.shell {
      flex: 1;
      display: flex;
      flex-direction: column;
      justify-content: center;
    }
```
Replace with:
```css
    main.shell {
      flex: 1;
      min-height: 0;
      display: flex;
      flex-direction: column;
      justify-content: center;
      overflow: hidden;
    }
```

- [ ] **Step 5: Let `.hero` shrink below its content size**

Find:
```css
    .hero {
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: clamp(1.5rem, 4vw, 2.4rem);
      padding: clamp(0.5rem, 2vw, 1.25rem) 0 clamp(2rem, 5vw, 3rem);
    }
```
Replace with:
```css
    .hero {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: clamp(1.5rem, 4vw, 2.4rem);
      padding: clamp(0.5rem, 2vw, 1.25rem) 0 clamp(2rem, 5vw, 3rem);
      min-height: 0;
      width: 100%;
    }
```

- [ ] **Step 6: Keep the intro copy at its natural size so only the panel absorbs squeeze pressure**

Find:
```css
    .intro { max-width: 40rem; text-align: center; }
```
Replace with:
```css
    .intro { max-width: 40rem; text-align: center; flex-shrink: 0; }
```

- [ ] **Step 7: Make `.panel` the one scrollable safety-valve element**

Find:
```css
    .panel {
      position: relative;
      width: min(700px, 100%);
      border-radius: var(--r-lg);
      padding: clamp(1.6rem, 3.4vw, 2.6rem);
      background: var(--panel);
      border: 1px solid var(--line);
      box-shadow: var(--shadow);
    }
```
Replace with:
```css
    .panel {
      position: relative;
      width: min(700px, 100%);
      border-radius: var(--r-lg);
      padding: clamp(1.6rem, 3.4vw, 2.6rem);
      background: var(--panel);
      border: 1px solid var(--line);
      box-shadow: var(--shadow);
      min-height: 0;
      max-height: 100%;
      overflow-y: auto;
    }
```

- [ ] **Step 8: Pin the footer to its natural size**

Find:
```css
    footer {
      margin-top: clamp(1.5rem, 4vw, 2.5rem);
      padding: 1.1rem 1.4rem;
      border-radius: var(--r-md);
      background: var(--accent);
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 0.8rem;
      font-size: 0.8rem;
      color: rgba(255, 255, 255, 0.92);
    }
```
Replace with:
```css
    footer {
      margin-top: clamp(1.5rem, 4vw, 2.5rem);
      padding: 1.1rem 1.4rem;
      border-radius: var(--r-md);
      background: var(--accent);
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 0.8rem;
      font-size: 0.8rem;
      color: rgba(255, 255, 255, 0.92);
      flex-shrink: 0;
    }
```

- [ ] **Step 9: Start the app and verify no page-level scroll at multiple viewport sizes**

Run (Bash tool):
```bash
cd "e:/Silicon Mango/linkedin-scraping"
DRY_RUN=true ./venv/Scripts/python.exe app.py > /tmp/app_verify.log 2>&1 &
sleep 3
./venv/Scripts/python.exe - <<'PY'
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch()
    for width, height, label in [(1440, 900, "laptop"), (1024, 600, "small laptop"), (390, 700, "mobile")]:
        page = browser.new_page(viewport={"width": width, "height": height})
        page.goto("http://localhost:5000")
        page.wait_for_selector("#scrape-form")
        overflow = page.evaluate(
            "document.documentElement.scrollHeight > document.documentElement.clientHeight "
            "|| document.documentElement.scrollWidth > document.documentElement.clientWidth"
        )
        assert not overflow, f"page overflows at {label} ({width}x{height})"
        assert page.locator("#scrape-form").is_visible(), f"form not visible at {label}"
        page.close()
    browser.close()
print("OK: no page-level scroll at any tested viewport, form still visible")
PY
```
Expected output ends with: `OK: no page-level scroll at any tested viewport, form still visible`

- [ ] **Step 10: Stop the dev server**

Run:
```bash
WINPID=$(netstat -ano | grep ':5000' | grep LISTENING | awk '{print $NF}' | head -n1)
taskkill //PID "$WINPID" //F
```
Expected: `SUCCESS: The process with PID <pid> has been terminated.`

- [ ] **Step 11: Commit**

```bash
git add templates/index.html
git commit -m "$(cat <<'EOF'
feat: make the app shell a fixed, non-scrollable viewport

html/body/.page now lock to 100dvh with overflow hidden; masthead,
intro copy, and footer are pinned to their natural size via
flex-shrink:0, and .panel is the one element that can scroll
internally, only as a safety valve when the window is unusually short.
EOF
)"
```

---

### Task 2: Jobs/Grants tab navigation shell

**Files:**
- Modify: `templates/index.html` — add `.tabbar`/`.tab` CSS (new block, placed after the `@keyframes pulse` block ending at line 185, before the `/* ---------- Hero ---------- */` comment at line 187)
- Modify: `templates/index.html:615-618` — insert the tab nav markup between `</header>` and the existing `<main class="shell" style="padding:0">` opening tag
- Modify: `templates/index.html:618` — the existing `<div class="hero">` opening tag (this div currently spans to line 723) becomes the Jobs tabpanel
- Modify: `templates/index.html:723-724` — insert a new Grants tabpanel stub between the existing hero's closing `</div>` and `</main>`
- Modify: `templates/index.html` — add the tab controller to the `<script>` block, after the existing `const` declarations (after line 760, before `let eventSource = null;`)

**Interfaces:**
- Consumes: nothing from Task 1 beyond the fixed-height shell already in place.
- Produces: `#panel-jobs` and `#panel-grants` elements (each with a `data-tab-panel` attribute of `"jobs"`/`"grants"`), and a global `activateTab(name)` JS function plus `tabButtons`/`tabPanels` bindings that Task 3 reuses when it wires up chip/preset scoping across both panels.

- [ ] **Step 1: Add tabbar CSS**

In `templates/index.html`, find the end of the pulse keyframes block:
```css
    @keyframes pulse {
      0% { box-shadow: 0 0 0 0 rgba(31, 157, 85, 0.45); }
      70% { box-shadow: 0 0 0 7px rgba(31, 157, 85, 0); }
      100% { box-shadow: 0 0 0 0 rgba(31, 157, 85, 0); }
    }

    /* ---------- Hero ---------- */
```
Replace with:
```css
    @keyframes pulse {
      0% { box-shadow: 0 0 0 0 rgba(31, 157, 85, 0.45); }
      70% { box-shadow: 0 0 0 7px rgba(31, 157, 85, 0); }
      100% { box-shadow: 0 0 0 0 rgba(31, 157, 85, 0); }
    }

    /* ---------- Tab nav ---------- */
    .tabbar {
      display: flex;
      gap: 0.3rem;
      width: fit-content;
      margin: 0 auto clamp(1rem, 2.5vw, 1.75rem);
      padding: 0.3rem;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: var(--white);
      flex-shrink: 0;
    }
    .tab {
      font-family: var(--font-display);
      font-weight: 500;
      font-size: 0.85rem;
      color: var(--text-soft);
      padding: 0.5rem 1.35rem;
      border: none;
      border-radius: 999px;
      background: transparent;
      cursor: pointer;
      transition: color 150ms ease, background 150ms ease;
    }
    .tab:hover { color: var(--text); }
    .tab.is-active { color: #fff; background: var(--accent); }
    .tab:focus-visible { outline: none; box-shadow: var(--ring); }

    /* ---------- Hero ---------- */
```

- [ ] **Step 2: Insert the tab nav markup and wrap the Jobs content as a tabpanel**

Find:
```html
        <span class="status-pill"><span class="status-dot" aria-hidden="true"></span> Engine online</span>
      </header>

      <main class="shell" style="padding:0">
        <div class="hero">
```
Replace with:
```html
        <span class="status-pill"><span class="status-dot" aria-hidden="true"></span> Engine online</span>
      </header>

      <nav class="tabbar reveal" role="tablist" aria-label="Extraction mode">
        <button type="button" class="tab is-active" id="tab-jobs" data-tab="jobs" role="tab" aria-selected="true" aria-controls="panel-jobs">Jobs</button>
        <button type="button" class="tab" id="tab-grants" data-tab="grants" role="tab" aria-selected="false" aria-controls="panel-grants" tabindex="-1">Grants</button>
      </nav>

      <main class="shell" style="padding:0">
        <div class="hero" id="panel-jobs" data-tab-panel="jobs" role="tabpanel" aria-labelledby="tab-jobs">
```

- [ ] **Step 3: Add the Grants tabpanel stub right after the Jobs hero closes**

Find:
```html
          </section>

        </div>
      </main>
```
Replace with:
```html
          </section>

        </div>

        <div class="hero" id="panel-grants" data-tab-panel="grants" role="tabpanel" aria-labelledby="tab-grants" hidden>
          <section class="intro reveal d1" aria-labelledby="grants-headline">
            <span class="eyebrow"><b>AI</b> Prompt-driven extraction</span>
            <h1 id="grants-headline">Grants content will render here.</h1>
            <p>The Grants extraction panel is built out in the next task.</p>
          </section>
        </div>
      </main>
```

- [ ] **Step 4: Add the tab controller JS**

Find:
```js
const stopBtn = $('stop-btn');
const stopLabel = $('stop-label');

let eventSource = null;
```
Replace with:
```js
const stopBtn = $('stop-btn');
const stopLabel = $('stop-label');

// Jobs/Grants tab controller
const tabButtons = Array.from(document.querySelectorAll('.tabbar [role="tab"]'));
const tabPanels = { jobs: $('panel-jobs'), grants: $('panel-grants') };
let activeTab = 'jobs';

function activateTab(name) {
  activeTab = name;
  tabButtons.forEach((btn) => {
    const isActive = btn.dataset.tab === name;
    btn.setAttribute('aria-selected', String(isActive));
    btn.tabIndex = isActive ? 0 : -1;
    btn.classList.toggle('is-active', isActive);
  });
  Object.entries(tabPanels).forEach(([key, el]) => { el.hidden = key !== name; });
}

tabButtons.forEach((btn) => {
  btn.addEventListener('click', () => activateTab(btn.dataset.tab));
});

document.querySelector('.tabbar').addEventListener('keydown', (e) => {
  if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
  e.preventDefault();
  const idx = tabButtons.findIndex((b) => b.dataset.tab === activeTab);
  const next = e.key === 'ArrowRight'
    ? (idx + 1) % tabButtons.length
    : (idx - 1 + tabButtons.length) % tabButtons.length;
  tabButtons[next].focus();
  activateTab(tabButtons[next].dataset.tab);
});

activateTab('jobs');

let eventSource = null;
```

- [ ] **Step 5: Start the app and verify tab switching + no scroll regression**

Run:
```bash
cd "e:/Silicon Mango/linkedin-scraping"
DRY_RUN=true ./venv/Scripts/python.exe app.py > /tmp/app_verify.log 2>&1 &
sleep 3
./venv/Scripts/python.exe - <<'PY'
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.goto("http://localhost:5000")

    assert page.locator("#panel-jobs").is_visible(), "jobs panel should be visible by default"
    assert not page.locator("#panel-grants").is_visible(), "grants panel should start hidden"
    assert page.get_attribute("#tab-jobs", "aria-selected") == "true"
    assert page.get_attribute("#tab-grants", "aria-selected") == "false"

    page.click("#tab-grants")
    assert page.locator("#panel-grants").is_visible(), "grants panel should show after click"
    assert not page.locator("#panel-jobs").is_visible(), "jobs panel should hide after click"
    assert page.get_attribute("#tab-grants", "aria-selected") == "true"

    page.click("#tab-jobs")
    assert page.locator("#panel-jobs").is_visible(), "jobs panel should show again"

    page.focus("#tab-jobs")
    page.keyboard.press("ArrowRight")
    assert page.get_attribute("#tab-grants", "aria-selected") == "true", "arrow-right should move to grants tab"

    for width, height in [(1440, 900), (1024, 600)]:
        page.set_viewport_size({"width": width, "height": height})
        overflow = page.evaluate(
            "document.documentElement.scrollHeight > document.documentElement.clientHeight"
        )
        assert not overflow, f"page overflows at {width}x{height} with tabs present"

    browser.close()
print("OK: tab switching, ARIA state, keyboard nav, and no-scroll all verified")
PY
```
Expected output ends with: `OK: tab switching, ARIA state, keyboard nav, and no-scroll all verified`

- [ ] **Step 6: Stop the dev server**

```bash
WINPID=$(netstat -ano | grep ':5000' | grep LISTENING | awk '{print $NF}' | head -n1)
taskkill //PID "$WINPID" //F
```

- [ ] **Step 7: Commit**

```bash
git add templates/index.html
git commit -m "$(cat <<'EOF'
feat: add Jobs/Grants tab navigation shell

Adds an ARIA-tablist navbar below the masthead that toggles between
the existing Jobs panel and a placeholder Grants panel, client-side,
with no page reload. Jobs functionality is untouched.
EOF
)"
```

---

### Task 3: Build out the Grants tab UI (coming soon)

**Files:**
- Modify: `templates/index.html` — add `.badge-soon` CSS (after the `.panel__sub` rule)
- Modify: `templates/index.html:410` — split `.submit:disabled` into a permanent-disabled style and a busy-disabled style
- Modify: `templates/index.html` — replace the Grants tabpanel stub markup added in Task 2 with the full cloned panel
- Modify: `templates/index.html` — replace the global chip/preset wiring JS with panel-scoped versions; add a defensive submit-preventer for the Grants form

**Interfaces:**
- Consumes: `#panel-jobs`/`#panel-grants` and `data-tab-panel` attribute from Task 2.
- Produces: final shipped Grants tab UI — no further tasks depend on this one.

- [ ] **Step 1: Add `.badge-soon` CSS**

Find:
```css
    .panel__sub { font-size: 0.82rem; color: var(--text-faint); margin-top: 0.1rem; }
```
Replace with:
```css
    .panel__sub { font-size: 0.82rem; color: var(--text-faint); margin-top: 0.1rem; }
    .badge-soon {
      display: inline-flex;
      align-items: center;
      padding: 0.25rem 0.7rem;
      border-radius: 999px;
      background: var(--gray-100);
      color: var(--text-faint);
      font-family: var(--font-display);
      font-size: 0.72rem;
      font-weight: 600;
      letter-spacing: 0.02em;
      white-space: nowrap;
    }
```

- [ ] **Step 2: Split the disabled-submit style so "coming soon" doesn't look like a loading spinner state**

Find:
```css
    .submit:disabled { cursor: progress; filter: saturate(0.7) brightness(0.96); transform: none; }
```
Replace with:
```css
    .submit:disabled { cursor: not-allowed; opacity: 0.6; transform: none; }
    .submit.is-busy:disabled { cursor: progress; opacity: 1; filter: saturate(0.7) brightness(0.96); }
```

- [ ] **Step 3: Replace the Grants tabpanel stub with the full cloned panel**

Find (the stub added in Task 2):
```html
        <div class="hero" id="panel-grants" data-tab-panel="grants" role="tabpanel" aria-labelledby="tab-grants" hidden>
          <section class="intro reveal d1" aria-labelledby="grants-headline">
            <span class="eyebrow"><b>AI</b> Prompt-driven extraction</span>
            <h1 id="grants-headline">Grants content will render here.</h1>
            <p>The Grants extraction panel is built out in the next task.</p>
          </section>
        </div>
      </main>
```
Replace with:
```html
        <div class="hero" id="panel-grants" data-tab-panel="grants" role="tabpanel" aria-labelledby="tab-grants" hidden>

          <section class="intro reveal d1" aria-labelledby="grants-headline">
            <span class="eyebrow"><b>AI</b> Prompt-driven extraction</span>
            <h1 id="grants-headline">Turn one sentence into a <span class="grad">ranked list of LinkedIn grant opportunities</span>.</h1>
            <p>Describe the grants you're chasing in plain English — grants extraction is coming soon.</p>
          </section>

          <section class="panel reveal d2" aria-labelledby="grants-panel-title">
            <div class="panel__head">
              <div>
                <div class="panel__title" id="grants-panel-title">New grants extraction</div>
                <div class="panel__sub">Describe it, set a target, run it.</div>
              </div>
              <span class="badge-soon">Coming soon</span>
            </div>

            <form id="grants-form" novalidate>
              <div class="field">
                <span class="field__label">
                  What grants are you looking for?
                  <span class="field__hint">be specific — sector, region, funder type</span>
                </span>
                <textarea id="grants-prompt" name="prompt"
                  placeholder="e.g. Education grants for NGOs across Maharashtra"></textarea>
                <div class="chips-group">
                  <span class="chips__title">Quick starts — tap to fill the prompt (India only)</span>
                  <div class="chips" aria-label="Example prompts">
                    <button type="button" class="chip" data-example="Education grants for NGOs in Maharashtra">Education · Maharashtra</button>
                    <button type="button" class="chip" data-example="Healthcare and public-health grants for nonprofits in India">Healthcare · India</button>
                    <button type="button" class="chip" data-example="Climate resilience grants for community organizations in India">Climate · India</button>
                  </div>
                </div>
              </div>

              <div class="controls">
                <div class="count-field">
                  <span class="field__label">Target grant count</span>
                  <div class="count-row">
                    <input type="number" id="grants-max-jobs" name="max_jobs" value="100" min="5" max="500" aria-label="Target grant count">
                    <button type="button" class="preset" data-preset="50">50</button>
                    <button type="button" class="preset is-active" data-preset="100">100</button>
                    <button type="button" class="preset" data-preset="250">250</button>
                  </div>
                </div>
                <button type="submit" id="grants-submit-btn" class="submit" disabled aria-disabled="true">
                  <span>Coming soon</span>
                </button>
              </div>
            </form>
          </section>

        </div>
      </main>
```

- [ ] **Step 4: Scope the example-chip JS to each panel's own textarea**

Find:
```js
// Example prompt chips fill the textarea
document.querySelectorAll('[data-example]').forEach((el) => {
  el.addEventListener('click', () => { promptEl.value = el.dataset.example; promptEl.focus(); });
});
```
Replace with:
```js
// Example prompt chips fill the textarea belonging to the same tab panel
document.querySelectorAll('[data-example]').forEach((el) => {
  el.addEventListener('click', () => {
    const panel = el.closest('[data-tab-panel]');
    const textarea = panel ? panel.querySelector('textarea') : promptEl;
    textarea.value = el.dataset.example;
    textarea.focus();
  });
});
```

- [ ] **Step 5: Scope the target-count preset JS to each panel's own number input**

Find:
```js
// Target-count presets
const presetEls = document.querySelectorAll('[data-preset]');
presetEls.forEach((el) => {
  el.addEventListener('click', () => {
    maxJobsEl.value = el.dataset.preset;
    presetEls.forEach((p) => p.classList.toggle('is-active', p === el));
  });
});
// Keep preset highlight in sync if the user types a custom value
maxJobsEl.addEventListener('input', () => {
  presetEls.forEach((p) => p.classList.toggle('is-active', p.dataset.preset === maxJobsEl.value));
});
```
Replace with:
```js
// Target-count presets, scoped to each panel's own number input
document.querySelectorAll('[data-tab-panel]').forEach((panel) => {
  const numberInput = panel.querySelector('input[type="number"]');
  const presetEls = panel.querySelectorAll('[data-preset]');
  if (!numberInput || !presetEls.length) return;
  presetEls.forEach((el) => {
    el.addEventListener('click', () => {
      numberInput.value = el.dataset.preset;
      presetEls.forEach((p) => p.classList.toggle('is-active', p === el));
    });
  });
  numberInput.addEventListener('input', () => {
    presetEls.forEach((p) => p.classList.toggle('is-active', p.dataset.preset === numberInput.value));
  });
});
```

- [ ] **Step 6: Add a defensive submit-preventer on the inert Grants form**

Find:
```js
// Stop button: ask the server to halt the run; collected jobs stay downloadable.
stopBtn.addEventListener('click', async () => {
```
Replace with:
```js
// Grants form has no backend wiring yet — the submit button is disabled,
// but guard against Enter-key submission too.
$('grants-form').addEventListener('submit', (e) => e.preventDefault());

// Stop button: ask the server to halt the run; collected jobs stay downloadable.
stopBtn.addEventListener('click', async () => {
```

- [ ] **Step 7: Start the app and verify the full Grants UI plus no regressions on Jobs**

Run:
```bash
cd "e:/Silicon Mango/linkedin-scraping"
DRY_RUN=true ./venv/Scripts/python.exe app.py > /tmp/app_verify.log 2>&1 &
sleep 3
./venv/Scripts/python.exe - <<'PY'
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.goto("http://localhost:5000")

    # Jobs chips/presets still target the Jobs form only
    page.click("text=NGO finance · India")
    assert "NGO sector accountant" in page.input_value("#prompt")
    page.click('#panel-jobs [data-preset="250"]')
    assert page.input_value("#max-jobs") == "250"

    # Switch to Grants
    page.click("#tab-grants")
    assert page.locator("#panel-grants").is_visible()

    # Grants submit button is disabled
    assert page.is_disabled("#grants-submit-btn")

    # Grants chips/presets target the Grants form only, not the Jobs one
    page.click("text=Education · Maharashtra")
    assert "Education grants for NGOs" in page.input_value("#grants-prompt")
    assert "NGO sector accountant" in page.input_value("#prompt"), "jobs textarea must be unaffected by grants chip click"
    page.click('#panel-grants [data-preset="50"]')
    assert page.input_value("#grants-max-jobs") == "50"
    assert page.input_value("#max-jobs") == "250", "jobs count must be unaffected by grants preset click"

    # No page-level scroll with the full Grants panel visible
    overflow = page.evaluate(
        "document.documentElement.scrollHeight > document.documentElement.clientHeight"
    )
    assert not overflow, "page overflows with full Grants panel visible"

    browser.close()
print("OK: Grants UI verified, Jobs tab unaffected, no page-level scroll")
PY
```
Expected output ends with: `OK: Grants UI verified, Jobs tab unaffected, no page-level scroll`

- [ ] **Step 8: Stop the dev server**

```bash
WINPID=$(netstat -ano | grep ':5000' | grep LISTENING | awk '{print $NF}' | head -n1)
taskkill //PID "$WINPID" //F
```

- [ ] **Step 9: Commit**

```bash
git add templates/index.html
git commit -m "$(cat <<'EOF'
feat: build out the Grants tab UI shell

Grants gets the same panel layout as Jobs (textarea, example chips,
target-count presets) with a permanently disabled "Coming soon"
submit button. Chip/preset JS is scoped per tab panel so the two
forms no longer collide now that both exist. No grants backend yet.
EOF
)"
```
