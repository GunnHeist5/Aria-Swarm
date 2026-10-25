# Backlog

The loop takes the top unchecked item in "Now" each iteration. Reorder freely before you run it.

## Now
Demo-readiness only. Do not add feature ideas to Later; finish these, in order.
- [ ] a) Device card: change to "RTX Spark · 64 GB", models "qwen3.6-35b-a3b · chronos-2 · timesfm-2.5", memory bar about 38 / 55 GB (the label and the bar fill must agree).
- [ ] b) Fix the "What changed" note: it stays nearly transparent after the run finishes. It must be fully opaque and readable in the done state at 1440px and 390px.
- [ ] c) Phone: confirm the top-bar sidebar fix works (workspace starts at the top at 390px, run card header not clipped); hide the "Ctrl K" hint below 720px.
- [ ] d) Self-host the IBM Plex fonts so the demo works with Wi-Fi off. The woff2 files are already in `mockup/fonts/` (Sans 400/500/600, Mono 400/500, latin subset: `ibm-plex-sans-latin-<weight>-normal.woff2`, `ibm-plex-mono-latin-<weight>-normal.woff2`). Add matching `@font-face` rules (font-display: swap) and remove the Google Fonts `<link>` tags. CLAUDE.md allows the Google Fonts link only; this replaces it and is intended.
- [ ] e) Add an `#evidence` screen that renders a JSON block embedded in `mockup/index.html` between the markers `EVIDENCE:START` and `EVIDENCE:END` (inside a `<script type="application/json" id="evidence-data">`). It shows: the model leaderboard, range coverage, launches tested, error by date, and a label that reads "Real data" or "Sample data" from a field in the JSON. Ship it with plausible sample data and the label "Sample data". Link it from the Backtest error tile.
- [ ] f) Demo mode: opening the page with `?demo=1` hides the `.screens` switcher (all routes still reachable by their hash and in-page links).

## Later
- [ ] "Continue without this file" button next to Retry on the failed step, so a locked file never blocks the forecast.
- [ ] Add a shot step that opens the analog drawer, so the loop can verify it (scripts/ is off limits, so a human must add it).
- [ ] Start the dotted preliminary line from the deck-case values so the move to solid shows the actual correction, not just a style change.
- [ ] Take the done shot after the answer stream finishes (the shot script captures it mid-fade), so reviewers see the full answer.
- [ ] Give the Drivers cards the same hover preview as inline numbers, so every figure has a quick receipt.
- [ ] Memo draft tab with a real-looking draft whose numbers link to traces.
- [ ] Version compare: v2 vs v3 of the model side by side.
- [ ] Dark theme from the same tokens, contrast-checked.
- [ ] Phone: tapping "Switch to 11%" shows a confirmation and updates the pulse card.

## Done
- [x] "Ctrl K" hint in the sidebar search field (hidden on focus); placeholder shortened to "Search" so they do not collide (verified by screenshot at both widths).
- [x] Keyboard: Ctrl/Cmd+K focuses engagement search, Esc leaves the trace view, arrow keys move and select between scenario buttons (UNVERIFIED by screenshot: Bash denied in iteration 11 after the pre-change shots showed no errors).
- [x] Failed-step state: sending with attached files makes Data fail on KOL_interview_notes.pdf (x glyph, log expanded, alert callout); "Retry this step" resumes the run (UNVERIFIED by screenshot: Bash denied in iteration 10 after the pre-change shots showed no errors).
- [x] Composer attachments: workspace "Attach files" adds two sample chips with 44px remove buttons; sending moves them into the user message (UNVERIFIED by screenshot: Bash denied in iteration 9, after the pre-change shots confirmed the iteration 8 phone collapse fix).
- [x] Bug fix: phone model panel stayed fully open because inline `display:flex` on `#tab-model` beat the collapse rule; added `!important` (UNVERIFIED by screenshot: Bash denied after the pre-change shots in iteration 8).
- [x] Phone-width workspace: at ≤720px the model panel collapses behind a "View model" toggle (aria-expanded) below the chat (UNVERIFIED by screenshot: Bash denied in iteration 7).
- [x] Analog drawer: "See the 5 analogs" opens a right-hand dialog with Launch A–E peak share and years to peak; close button, scrim click and Esc close it, focus returns (UNVERIFIED by screenshot: Bash denied in iteration 6).
- [x] What changed: note in the model panel (share 18% → 11%, peak $2.33B → $1.42B) appears when the critic finishes; the P50 line transitions from dotted to solid (verified by screenshot at both widths).
- [x] Data-mode switch: 3-way radiogroup (Private, Private cloud, Direct API) under the workspace header with a one-line explanation; device card and composer label follow (UNVERIFIED by screenshot: Bash denied in iteration 4).
- [x] Number previews: hover/focus on `.num` links shows a popover with source and "Open trace" (UNVERIFIED by screenshot: Bash denied in iteration 3).
- [x] Stream the final answer block by block, instant under reduced motion (UNVERIFIED by screenshot: Bash denied in iteration 2).
- [x] Progress strip: 5-segment bar under the run header fills as steps finish (UNVERIFIED by screenshot: Bash was denied in iteration 1; re-run `npm run shot` and check).
- [x] Seed mockup: Home, Workspace with live run (spinner, timer, expandable step logs, early read before verification), Trace, Phone.
