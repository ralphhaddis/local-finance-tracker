#!/bin/zsh
LFT_AGENT="$HOME/Library/LaunchAgents/local.finance-tracker.app.plist"
LFT_UID="$(id -u)"
launchctl bootout "gui/$LFT_UID" "$LFT_AGENT" 2>/dev/null || true
rm -f "$LFT_AGENT"
echo "local-finance-tracker login service removed. Project data and statement folders were left in place."
