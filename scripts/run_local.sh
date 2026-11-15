#!/bin/bash
# Run a market-hours bot job from this Mac (launchd), then push memory.json.
# Usage: scripts/run_local.sh execute|monitor
# bot.schedule_guard skips the run if the market is closed or the job already
# ran today (on GitHub or here), so repeated or late triggers are harmless.
set -u
JOB="$1"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO" || exit 1
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:$PATH"
echo "=== $(date) — $JOB ==="

# Get the latest memory.json (last_runs) so we don't repeat a job GitHub already did.
git pull --rebase --autostash -q origin main || { echo "git pull failed"; exit 1; }

.venv/bin/python -m "bot.$JOB"
STATUS=$?

git add memory.json
if ! git diff --staged --quiet; then
  git commit -q -m "Local $JOB $(date +%Y-%m-%d)" -m "Run from Mac launchd"
  for i in 1 2 3; do git pull --rebase --autostash -q origin main && git push -q origin main && break; sleep 5; done
fi

if [ $STATUS -ne 0 ]; then
  .venv/bin/python -m bot.notify failure "Local $JOB (Mac)" "see ~/Library/Logs/trading-bot.log"
fi
echo "=== exit $STATUS ==="
exit $STATUS
