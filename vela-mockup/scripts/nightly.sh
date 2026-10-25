#!/usr/bin/env bash
# Unattended entry point for a scheduled run (cloud session or cron).
# Installs what is missing, runs the loop, then prints the branch and the LOG entries
# so the caller can push the branch and open a pull request.
# Usage: ./scripts/nightly.sh        (same ITERATIONS / MODEL / budget settings as loop.sh)
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

[ -d node_modules/playwright ] || npm install --no-audit --no-fund >&2 || { echo "npm install failed" >&2; exit 1; }

# If Playwright's browser download is blocked (sandboxed hosts), reuse a preinstalled Chromium
# by linking it under the revision this Playwright version expects.
if ! node -e 'process.exit(require("fs").existsSync(require("playwright").chromium.executablePath())?0:1)' 2>/dev/null; then
  if ls -d /opt/pw-browsers/chromium-* >/dev/null 2>&1; then
    shim="${TMPDIR:-/tmp}/pw-shim"
    mkdir -p "$shim"
    rev="$(node -e 'const b=require("./node_modules/playwright-core/browsers.json").browsers;const r=n=>b.find(x=>x.name===n).revision;console.log(r("chromium")+" "+r("chromium-headless-shell"))')"
    set -- $rev
    src="$(ls -d /opt/pw-browsers/chromium-* | head -n1)"
    shell="$(ls -d /opt/pw-browsers/chromium_headless_shell-* 2>/dev/null | head -n1)"
    ln -sfn "$src" "$shim/chromium-$1"
    [ -n "$shell" ] && ln -sfn "$shell" "$shim/chromium_headless_shell-$2"
    export PLAYWRIGHT_BROWSERS_PATH="$shim"
  else
    npx playwright install chromium >&2 || { echo "Could not install Chromium" >&2; exit 1; }
  fi
fi

npm run shot >/dev/null 2>&1 || echo "Warning: the checks already fail on the starting point; every iteration will be reverted until that is fixed." >&2

# Demo-readiness phase: four iterations a night.
export ITERATIONS="${ITERATIONS:-4}"
# Scheduled runs are bounded by ITERATIONS and the budget caps, not by wall-clock time (the host clock is UTC).
STOP_AT="${STOP_AT:-23:59}" ./scripts/loop.sh
status=$?

branch="$(git branch --show-current)"
echo
echo "BRANCH=$branch"
echo "EXIT=$status"
echo "--- most recent LOG.md entries ---"
grep -A4 '^## Iteration' LOG.md | tail -n 80
