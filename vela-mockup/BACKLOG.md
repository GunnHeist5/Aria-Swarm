# Backlog

The loop takes the top unchecked item in "Now" each iteration. Reorder freely before you run it.

## Now
- [ ] After "Continue without this file", show a "KOL notes excluded" chip on the answer so the user sees what the forecast left out.

## Later
- [ ] Show the preliminary chart (deck-value dotted line) already in the running shot; it is blank while the step is pending, so the early read has no visual until later.
- [ ] After "Continue without this file", show a "KOL notes excluded" chip on the answer so the user sees what the forecast left out.
- [ ] Add a shot step that opens the analog drawer, so the loop can verify it (scripts/ is off limits, so a human must add it).
- [ ] Take the done shot after the answer stream finishes (the shot script captures it mid-fade), so reviewers see the full answer.
- [ ] Give the Drivers cards the same hover preview as inline numbers, so every figure has a quick receipt.
- [ ] Memo draft tab with a real-looking draft whose numbers link to traces.
- [ ] Version compare: v2 vs v3 of the model side by side.
- [ ] Dark theme from the same tokens, contrast-checked.
- [ ] Phone: tapping "Switch to 11%" shows a confirmation and updates the pulse card.

## Done
- [x] Preliminary P50 line starts at the deck-case values (early read, tile and log now say ~$2.3B) and animates down to the critic-adjusted line, endpoint dot following; instant under reduced motion (verified by screenshot at desktop, no errors).
- [x] "Continue without this file" button beside Retry on the failed step; resumes the run (verified by screenshot at 390px, no errors).
- [x] Phone menu closes automatically when an engagement or "New analysis" is picked (shot run passes with no errors; the open-menu state is not captured by shots).
- [x] "Ctrl K" hint hidden on touch devices via `@media (hover:none)` (shot run shows no errors; the shot browser likely does not emulate touch, so the hint still shows there).
- [x] Phone top bar: sidebar collapses at ≤720px to logo, Menu button (aria-expanded), engagement name and live data mode; Ctrl+K opens it (verified by screenshot at 390px).
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
