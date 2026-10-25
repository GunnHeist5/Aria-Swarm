# Vela UI mockup

## What this is
A clickable UI mockup for **Vela** (working name), an AI forecasting workspace for pharma commercial and BD teams. It is a **mockup only**: one static HTML file, fictional sample data, no backend, no real AI calls. The goal is to look and feel like the real product so it can be shown to prospects and investors.

Everything lives in `mockup/index.html` (HTML, CSS and JS in one file). Open it directly in a browser. Screens are hash routes: `#home`, `#workspace`, `#trace`, `#phone`, and `#evidence` (once added). Opening the page with `?demo=1` hides the review-aid screen switcher.

## Current phase: demo-readiness
The mockup is feature-complete enough. Work only on the items in `BACKLOG.md` "Now", in order; do not add feature ideas to "Later". The `#evidence` screen shows data exported by the real model (`python -m vela export-evidence`) between the `EVIDENCE:START` / `EVIDENCE:END` markers in `mockup/index.html`; never edit inside those markers by hand once real data is there, and keep the JSON shape stable.

## Who it is for
Forecasting, commercial and business-development leads at small and mid-size biotechs. They need three things from every screen: **speed** (answers in seconds), **trust** (every number has a source), and **privacy** (client data stays on the device).

## Product principles (in priority order)
1. **Answer fast, verify visibly.** An early read appears within seconds; checks keep running and the answer updates. Never make the user stare at an empty screen.
2. **Every number has a receipt.** Any figure can be clicked to see its source file, the code that computed it, and how the method backtested.
3. **Critic flags are decisions.** When an assumption looks wrong, show it as a choice with buttons, not as buried text.
4. **Show the work while waiting.** The run card lists each agent step with a live status glyph (spinner while running, check when done, ! when flagged), an elapsed timer, and an expandable live log per step. The sail mark sways while work is running.
5. **Privacy is always visible.** The current data mode and the device are shown on screen at all times.
6. **Calm and clinical.** Dense but readable. No decoration that does not carry information.

## Design system (reuse these; do not invent new ones)
- Colors: use the CSS custom properties in `:root` only. If you truly need a new color, add a token there first, and check contrast.
- Type: IBM Plex Sans for UI, IBM Plex Mono for numbers, code and timers. Sizes in use: 11.5, 12, 13, 13.5, 14, 15, 16, 20, 22, 26, 32px.
- Radii: `--r-sm` 8, `--r` 10, `--r-md` 12, `--r-lg` 16; pills are 999px.
- Icons: add new ones as `<symbol>` in the sprite at the top of `<body>` and use `<svg class="i"><use href="#id"/></svg>`. Stroke icons only, no emoji, no icon fonts.
- Spacing: multiples of 2/4px; existing gaps are 4, 6, 8, 10, 12, 14, 18, 22, 26, 28, 36px.

## Hard rules
- **Mockup only.** No frameworks, no build step, no external JavaScript, and no network calls of any kind: the demo must work with Wi-Fi off. Fonts are self-hosted from `mockup/fonts/` (the Google Fonts link is being removed; do not add it back).
- **Fictional data only.** Company "Northbridge Bio", asset "NB-201", analogs "Launch A–F". Never use real drug or company names. Keep numbers internally consistent (peak = 620K × 11% × 64% × $32.6K ≈ $1.42B; deck case $2.33B).
- **Keep everything working.** All routes, the run animation, step expand/collapse, "Run again", composer submit, scenario buttons, tabs, critic decision buttons and links to `#trace` must keep working after your change.
- **Accessibility.** Real `<button>`/`<a>`/`<input>` with labels; touch targets at least 44px; text contrast at least 4.5:1; `aria-expanded` on toggles; `aria-live` for run updates; honor `prefers-reduced-motion`.
- **Responsive.** Works at 1440px and at 390px wide with no horizontal page scroll.
- The dark `.screens` switcher at the bottom right is a review aid, not product UI. Keep it.
- **Do not edit** `CLAUDE.md`, `prompt.md`, `package.json`, `.gitignore` or anything in `scripts/`. The loop restores them after every iteration.

## How to check your work
Run `npm run shot`. It writes PNG screenshots to `shots/` (desktop and phone widths for every screen, plus `*-workspace-running.png` mid-run and `*-workspace-done.png` after the run) and prints any JavaScript errors (also saved to `shots/errors.txt`). Read the PNGs you changed and compare against the quality bar.

## Quality bar (check before you finish)
- No JavaScript errors in `shots/errors.txt`.
- Nothing overlaps, clips or scrolls sideways at 390px.
- The change is visible in the screenshots and matches the principles above.
- Nothing that worked before is broken.
- The screen looks calmer or clearer than before, not busier. If it got busier, simplify or undo.
