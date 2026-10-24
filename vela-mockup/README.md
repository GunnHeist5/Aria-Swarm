# Vela mockup: overnight build kit

A clickable UI mockup (`mockup/index.html`) plus a loop that lets Claude Code improve it while you sleep, one focused change per iteration, each committed to git.

## One-time setup (about 5 minutes)
You need Node.js 18+, git, and Claude Code 2.1.302 or newer (`claude --version`; update with `claude update`). On Windows, run these in WSL or Git Bash.

```bash
cd vela-mockup
npm run setup                        # installs Playwright and its Chromium
git init && git add -A && git commit -m "Seed mockup"
```

Open `mockup/index.html` in your browser to see the starting point. The dark bar at the bottom right jumps between screens.

## Before bed
```bash
./scripts/loop.sh                    # or: npm run overnight
```
Defaults: up to 12 iterations, Sonnet, about $2.50 cap per iteration, about $25 for the night, and no new iterations after 7:00 AM. Change any of them inline:

```bash
ITERATIONS=16 MODEL=opus TOTAL_BUDGET=40 ./scripts/loop.sh
```

To stop early from another terminal: `touch STOP` (it finishes the current iteration, then exits).

**Billing:** if `ANTHROPIC_API_KEY` is set, the run bills to your API account. To use your Claude subscription instead, run `unset ANTHROPIC_API_KEY` first; subscription runs pause when you hit your usage limit. Dollar caps are Claude Code's own estimates and can overshoot slightly.

## In the morning
1. Open `mockup/index.html` and click through every screen.
2. Read `LOG.md`: one entry per iteration, with anything to look at.
3. `git log --oneline` shows each change. Undo one with `git revert <commit>`.
4. Didn't like the night? `git switch -` then `git branch -D overnight-...` throws it all away.
5. Reorder `BACKLOG.md` to steer tomorrow night's run, then commit.

## What the loop can and can't do
Each iteration starts a fresh Claude Code session with `prompt.md`. It can read and edit files in this folder, run `npm run shot` (screenshots of every screen at desktop and phone widths, plus any JavaScript errors), and run read-only git commands. Everything else is denied; it never runs with permission checks skipped. After each iteration the script restores `CLAUDE.md`, `prompt.md`, `package.json`, `.gitignore` and `scripts/`, so the loop can't rewrite its own rules.

## Files
| File | Purpose |
|---|---|
| `mockup/index.html` | The whole mockup: HTML, CSS and JS in one file |
| `CLAUDE.md` | Product brief, design system and rules Claude follows every iteration |
| `prompt.md` | The single-iteration instructions |
| `BACKLOG.md` | Ranked improvement list; the loop takes the top item |
| `LOG.md` | What each iteration changed |
| `scripts/loop.sh` | The overnight loop with caps and git commits |
| `scripts/shot.mjs` | Playwright screenshots used to check each change |
