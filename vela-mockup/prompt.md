You are running ONE iteration of an unattended overnight loop that improves the Vela UI mockup. Nobody is watching, so never ask questions: decide, do one thing well, and stop. CLAUDE.md is already loaded and is the source of truth.

1. **Orient.** Read `BACKLOG.md` and the last three entries of `LOG.md`. Run `npm run shot` exactly as written, then look at the screenshots in `shots/` and at `shots/errors.txt`.
2. **Pick one item.** If the screenshots or errors show a bug (JavaScript error, overlap, clipping, sideways scroll at 390px, broken route), fix that bug: bugs beat features. Otherwise take the top unchecked item in the "Now" section of `BACKLOG.md`.
3. **Implement it** in `mockup/index.html`. Keep the change focused and consistent with the design system in CLAUDE.md.
4. **Verify.** Run `npm run shot` again. Read the screenshots for every screen your change touches, at both widths. Check the quality bar in CLAUDE.md. If the result is worse, or anything that worked before is broken, undo or fix it before finishing.
5. **Update `BACKLOG.md`.** Move the item you finished to "Done" as one line. Add at most two new ideas you noticed to "Later", one line each, saying why it helps the user. If "Now" is empty, promote the best item from "Later" first.
6. **Append to `LOG.md`** an entry that starts with a heading line in exactly this form: `## Iteration N: <one-line summary of the change>` (N is the iteration number given below). Under it, write two or three short lines: what you changed, how you checked it, and anything the human should look at in the morning.

Do only one item. Do not refactor unrelated code, do not rename things for style, and do not edit CLAUDE.md, prompt.md, package.json, .gitignore or scripts/.
