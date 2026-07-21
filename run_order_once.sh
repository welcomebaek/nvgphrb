#!/bin/sh
# One-shot cron wrapper for order_buy_005930.py --live.
#
# This script is intended to be invoked exactly once via a cron entry marked
# with the comment "# KIS_ONESHOT_BUY_005930_20260721". After it runs (success
# OR failure), it removes that crontab line so the job never fires again.
#
# Exit code: propagates order_buy_005930.py's own exit code, so cron's log of
# this job's exit status reflects the real outcome.

cd /Users/welcomebaek/Workspace/kis_MCP_trading || exit 1

/Users/welcomebaek/.local/bin/uv run order_buy_005930_real.py --live
ORDER_EXIT_CODE=$?

# Remove our own one-shot crontab entry, regardless of the order outcome.
# `crontab -l` exits non-zero when there is no crontab at all; guard that so
# it never aborts this script or clobbers anything.
CURRENT_CRONTAB=$(crontab -l 2>/dev/null)
if [ -n "$CURRENT_CRONTAB" ]; then
    printf '%s\n' "$CURRENT_CRONTAB" | grep -v "KIS_ONESHOT_BUY_005930_20260721" | crontab -
fi

# Unload and delete our own one-shot LaunchAgent, regardless of the order
# outcome. StartCalendarInterval has no Year field, so without this cleanup
# the job would otherwise recur every 7/21 at 09:00 KST forever.
launchctl bootout "gui/$(id -u)/com.welcomebaek.kis-oneshot-buy-005930" 2>/dev/null
rm -f "$HOME/Library/LaunchAgents/com.welcomebaek.kis-oneshot-buy-005930.plist"

exit "$ORDER_EXIT_CODE"
