#!/usr/bin/env bash
# Vela overnight UI loop.
# Each iteration runs one fresh, headless Claude Code session that makes ONE
# improvement to mockup/index.html, checks it with screenshots, and logs it.
# The script commits each iteration to a new git branch so any step can be undone.
#
# Stop early:  create a file named STOP in this folder (touch STOP).
# Settings (override like:  ITERATIONS=16 MODEL=opus ./scripts/loop.sh):
ITERATIONS="${ITERATIONS:-12}"    # most iterations to run
RUN_BUDGET="${RUN_BUDGET:-2.50}"  # USD cap per iteration (Claude Code's own estimate)
TOTAL_BUDGET="${TOTAL_BUDGET:-25}" # USD cap for the whole night (sum of estimates)
MAX_TURNS="${MAX_TURNS:-40}"      # agent turns per iteration
MODEL="${MODEL:-sonnet}"          # sonnet (cheaper) or opus
STOP_AT="${STOP_AT:-07:00}"       # do not start new iterations after this local time (HH:MM)

set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

# Only file tools, the screenshot script, and read-only git. Everything else is denied.
ALLOWED='Read,Edit,Write,Glob,Grep,Bash(npm run shot),Bash(git status *),Bash(git diff *),Bash(git log *)'
DENIED='Read(./package.json),Read(./package-lock.json),Read(./scripts/**),Read(./.gitignore)'
PROTECTED=(CLAUDE.md prompt.md package.json .gitignore scripts)
[ -f package-lock.json ] && PROTECTED+=(package-lock.json)

die() { echo "$1" >&2; exit 1; }
command -v claude >/dev/null || die "Claude Code (claude) is not installed or not on PATH."
command -v node >/dev/null || die "Node.js is not installed."
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "Not a git repo yet. Run: git init && git add -A && git commit -m 'Seed'"
[ -z "$(git status --porcelain)" ] || die "You have uncommitted changes. Commit or stash them first."
[ -d node_modules/playwright ] || die "Playwright is missing. Run: npm run setup"

ver="$(claude --version 2>/dev/null | awk '{print $1}')"
if [ -n "$ver" ] && [ "$(printf '%s\n' 2.1.302 "$ver" | sort -V 2>/dev/null | head -n1)" != "2.1.302" ]; then
  echo "Warning: Claude Code $ver detected; this script expects 2.1.302 or newer (run: claude update)." >&2
fi
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  echo "Note: ANTHROPIC_API_KEY is set, so this run bills to the API. Unset it to use your Claude subscription." >&2
fi

BASE="$(git rev-parse HEAD)"
BRANCH="overnight-$(date +%Y%m%d-%H%M)"
git switch -c "$BRANCH" >/dev/null || die "Could not create branch $BRANCH."
mkdir -p logs
rm -f STOP
LOG=logs/run.log
spent=0
fails=0

say() { echo "$(date '+%H:%M:%S')  $*" | tee -a "$LOG"; }
say "Branch $BRANCH · up to $ITERATIONS iterations · model $MODEL · \$$RUN_BUDGET per run · \$$TOTAL_BUDGET total · stop at $STOP_AT"

for i in $(seq 1 "$ITERATIONS"); do
  if [ -f STOP ]; then say "STOP file found. Stopping."; break; fi
  now="$(date +%H:%M)"
  if [[ "$now" > "$STOP_AT" && "$now" < "12:00" ]]; then say "It is past $STOP_AT. Stopping."; break; fi
  if awk "BEGIN{exit !($spent >= $TOTAL_BUDGET)}"; then say "Night budget of \$$TOTAL_BUDGET reached. Stopping."; break; fi

  say "Iteration $i starting"
  out="logs/iter-$i.json"
  claude -p "$(cat prompt.md)

This is iteration $i of up to $ITERATIONS." \
    --model "$MODEL" \
    --permission-mode dontAsk \
    --permission-prompts none \
    --allowedTools "$ALLOWED" \
    --disallowedTools "$DENIED" \
    --max-turns "$MAX_TURNS" \
    --max-budget-usd "$RUN_BUDGET" \
    --output-format json > "$out" 2>> "$LOG"
  code=$?

  cost="$(node -e 'try{const j=JSON.parse(require("fs").readFileSync(process.argv[1],"utf8"));process.stdout.write(String(Number(j.total_cost_usd)||0))}catch(e){process.stdout.write("0")}' "$out")"
  spent="$(awk "BEGIN{printf \"%.2f\", $spent + $cost}")"

  # Put back anything the loop depends on, in case it was touched.
  for p in "${PROTECTED[@]}"; do git checkout "$BASE" -- "$p" 2>/dev/null; done

  # Independent gate: the script, not the agent, decides whether the result is acceptable.
  gate=0
  if ! npm run shot > "logs/gate-$i.txt" 2>&1; then
    gate=1
    git checkout HEAD -- mockup BACKLOG.md LOG.md
    why="$(grep -E '^\[(desktop|phone)\]' "logs/gate-$i.txt" | head -n3 | cut -c1-160 | paste -sd';' -)"
    printf '\n## Iteration %s: REJECTED by the checks, changes reverted\n%s\nFix this class of problem before adding features.\n' "$i" "${why:-npm run shot failed}" >> LOG.md
    say "Iteration $i failed the checks and was reverted: ${why:-npm run shot failed}"
  fi

  git add -A mockup BACKLOG.md LOG.md
  if git diff --cached --quiet; then
    say "Iteration $i made no changes (exit $code, about \$$cost)."
  else
    summary="$(grep '^## Iteration' LOG.md | tail -n1 | sed 's/^## //' | cut -c1-90)"
    git commit -q -m "${summary:-Iteration $i: UI improvement}"
    say "Iteration $i committed: ${summary:-no summary} (exit $code, about \$$cost, night total about \$$spent)"
  fi

  if [ "$code" -ne 0 ] || [ "$gate" -ne 0 ]; then fails=$((fails + 1)); else fails=0; fi
  if [ "$fails" -ge 2 ]; then say "Two iterations in a row ended with an error. Stopping; see $LOG."; break; fi
  sleep 10
done

say "Finished. Estimated spend about \$$spent."
say "Review: open mockup/index.html · read LOG.md · git log --oneline $BASE..HEAD"
say "Undo one iteration: git revert <commit> · throw the night away: git switch - && git branch -D $BRANCH"
