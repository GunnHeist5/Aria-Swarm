# Overnight log

Each iteration appends one entry here. The loop uses the latest `## Iteration` heading as the git commit message.

## Iteration 0: Seed mockup from the design conversation
Four screens (Home, Workspace, Trace, Phone) in mockup/index.html. The Workspace replays a sample run: an early read after about 3 seconds, the critic's check after about 6, the final answer after about 7.

## Iteration 1: Add 5-segment progress strip to the run card
Added `.progress` bar under the run header (HTML, CSS, and an update inside `setStatus`); segments turn teal when a step finishes and amber if flagged, with `role="progressbar"` and `aria-valuenow`.
NOT verified: Bash was denied mid-iteration, so I could not rerun `npm run shot`. The first `npm run shot` (before the change) showed no errors and no layout issues.
Please run `npm run shot` and check the strip at 390px and 1440px in the morning.

## Iteration 2: Stream the final answer block by block
In `setAnswer`, the final answer template now appends each block (chip, paragraphs, critic callout, suggestions) 380ms apart with a short fade; reduced motion shows it all at once. Timers are cleared on re-render so Run again is safe.
NOT verified: Bash was denied again, so I could not rerun `npm run shot`. The pre-change shots showed no errors, and the iteration 1 progress strip looks fine at both widths.
Please run `npm run shot` and check `*-workspace-done.png` has the full answer (the stream takes about 2s, so the shot script may capture it early).

## Iteration 3: Add source popovers on underlined numbers
Added one shared `.numpop` popover (CSS, JS, `data-src` on the three `.num` links in the answer); it shows on hover or focus, closes on Esc or leaving, and has an "Open trace" link.
NOT verified after the change: Bash was denied, so I could not run `npm run shot`. The pre-change shots showed no errors, and the earlier iteration 2 stream renders (mid-fade in the shot).
Please run `npm run shot`, hover the numbers at 1440px, and check the popover stays on screen at 390px. Iterations 1 to 3 are all unverified by a post-change shot.

## Iteration 4: Replace Private mode chip with a 3-way data-mode switch
Removed the header chip; added a radiogroup (Private, Private cloud, Direct API) with arrow-key support and an aria-live explanation line. The device card and both composer "On-device" labels update with the mode.
NOT verified: Bash was denied after the pre-change shots (which showed no errors), so I could not rerun `npm run shot`. Edits were made with the Edit tool only.
Please run `npm run shot` and check the switch at 390px (three buttons should fit one row) and that the composer label swap works.

## Iteration 5: Add "What changed" note and dotted-to-solid line transition
Added a `.changed` note in the model panel (share 18% → 11%, peak $2.33B → $1.42B) shown when the critic finishes, and made the P50 dash pattern transition from dotted to solid; reduced motion disables both animations.
Verified with `npm run shot`: no errors, note fits at 1440px and 390px. Iterations 1–4 (progress strip, data-mode switch) also look fine in these shots; the done shot still catches the answer mid-fade.
The dotted line is the same path as the final one, so only its style changes; see "Later".

## Iteration 6: Add analog drawer from "See the 5 analogs"
Added a right-side dialog (scrim, close button, Esc, focus return) listing Launch A–E peak share and years to peak; median share is 11%, matching the model.
NOT verified: Bash was denied after the pre-change shots (no errors, no layout bugs), so I could not rerun `npm run shot`; edits were made with Edit only.
Please run `npm run shot`, click "See the 5 analogs" at 390px and 1440px, and check the drawer fits and Esc closes it.

## Iteration 7: Collapse the model panel behind "View model" on phones
At 720px and below, the model panel shows only its header and a "View model"/"Hide model" toggle (aria-expanded); desktop is unchanged because the toggle is hidden there.
NOT verified: Bash was denied after the pre-change shots (no errors), so I could not rerun `npm run shot`; edits were made with Edit only.
Please run `npm run shot` and check the 390px workspace shots (they should now end with the collapsed panel) and that the toggle expands it.

## Iteration 8: Fix phone model panel not collapsing
The 390px shot showed the whole model panel open under a "View model" button; inline `display:flex` on `#tab-model` overrode the collapse rule, so I added `!important` to that rule.
NOT verified: Bash was denied after the pre-change shots (no JS errors), so I could not rerun `npm run shot`; I used Edit only. The composer attachments item is untouched.
Please run `npm run shot` and check the 390px workspace shots end with only the collapsed header and toggle, and that the toggle expands the panel.

## Iteration 9: Composer attachments in the workspace
"Attach files" now shows two sample file chips (xlsx, pdf) inside the composer, each with a 44px remove button; sending moves them into the user message and hides the tray.
Pre-change shots showed no errors and confirmed the iteration 8 phone collapse fix. NOT verified after my change: Bash was denied, so I used Edit only and could not rerun `npm run shot`.
Please run `npm run shot`, click the paperclip at 390px and 1440px, and check chips wrap without overflow, remove works, and sending moves them up.

## Iteration 10: Failed Data step with "Retry this step"
Sending a message with attached files now makes Data fail on KOL_interview_notes.pdf (password-protected): x glyph, log auto-expanded, alert callout with "Retry this step", which resumes the run without resetting the run card.
Pre-change shots had no errors. NOT verified after my change: Bash was denied, so I used Edit only and could not rerun `npm run shot`.
Please run `npm run shot`, attach files and send at 390px and 1440px, check the callout fits and Retry completes the run; the default run is unchanged.

## Iteration 11: Keyboard shortcuts
Added Ctrl/Cmd+K to focus engagement search, Esc to leave the trace view back to the workspace, and arrow keys to move and select between scenario buttons.
Pre-change shots had no errors. NOT verified after my change: Bash was denied, so I used Edit only and could not rerun `npm run shot`.
Please run `npm run shot`, then try Ctrl+K on Home/Workspace, Esc on #trace, and arrows on the Scenario buttons.

## Iteration 12: Ctrl K hint in the search field
Added a mono "Ctrl K" key hint inside the sidebar search (hidden while focused); shortened the placeholder to "Search" after the first shot showed the hint overlapping the longer text.
Verified with `npm run shot`: no JS errors, hint fits at 1440px and 390px. Bash was denied afterwards, so the backlog and log were updated with Edit only.
Look at: the hint also shows on phones, where there is no keyboard (queued in Later).
