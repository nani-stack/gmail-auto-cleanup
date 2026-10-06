#!/usr/bin/env bash
# Weekly triage: judge new mail, then trash cleanup mail past its grace period.
#
# Cron calls this every hour; the guard below keeps it weekly. It prefers a set
# day and hour, and if the machine was off then, it catches up at the next
# opportunity instead of skipping a whole week.
#
#   crontab -e   ->   0 * * * * /path/to/gmail-auto-cleanup/run_weekly.sh
#
# Run it by hand any time with: ./run_weekly.sh --force
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-$DIR/.venv/bin/python}"
cd "$DIR" || exit 1

PREFERRED_DOW=1      # 1 = Monday ... 7 = Sunday
PREFERRED_HOUR=18    # 24-hour clock
DUE_HOURS=144        # 6 days: earliest the preferred slot may fire
CATCHUP_HOURS=204    # 8.5 days: the slot was missed, so run at the next check

STAMP="$DIR/.last_run"

if [ "${1:-}" != "--force" ]; then
    last=$(cat "$STAMP" 2>/dev/null || echo 0)
    age_hours=$(( ($(date +%s) - last) / 3600 ))
    hour=$(date +%H)
    if [ "$(date +%u)" = "$PREFERRED_DOW" ] && [ "${hour#0}" = "$PREFERRED_HOUR" ] \
       && [ "$age_hours" -ge "$DUE_HOURS" ]; then
        : # preferred slot, and enough time has passed
    elif [ "$age_hours" -ge "$CATCHUP_HOURS" ]; then
        : # the slot was missed; catch up now
    else
        exit 0
    fi
fi

LOG="$DIR/weekly_$(date +%Y-%m-%d).log"
{
    echo "===== weekly run $(date) ====="
    echo "--- bulk tabs ---"
    "$PYTHON" -u -m gmail_cleanup --limit 2000 --apply
    echo "--- primary ---"
    "$PYTHON" -u -m gmail_cleanup --primary --limit 2000 --apply
    echo "--- trash cleanup past the grace period ---"
    "$PYTHON" -u -m gmail_cleanup --trash-cleanup --apply
    echo "===== done $(date) ====="
} >> "$LOG" 2>&1

date +%s > "$STAMP"

# Keep the last 8 weekly logs.
ls -t "$DIR"/weekly_*.log 2>/dev/null | tail -n +9 | xargs -r rm
