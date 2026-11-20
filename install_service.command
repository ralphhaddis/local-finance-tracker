#!/bin/zsh
set -e
LFT_PROJECT="$(cd "$(dirname "$0")" && pwd)"
LFT_AGENT_DIR="$HOME/Library/LaunchAgents"
LFT_AGENT="$LFT_AGENT_DIR/local.finance-tracker.app.plist"
LFT_UID="$(id -u)"

mkdir -p "$LFT_AGENT_DIR" "$LFT_PROJECT/data"
sed "s|__LFT_PROJECT__|$LFT_PROJECT|g" "$LFT_PROJECT/launchd/local.finance-tracker.app.plist" > "$LFT_AGENT"
launchctl bootout "gui/$LFT_UID" "$LFT_AGENT" 2>/dev/null || true
launchctl bootstrap "gui/$LFT_UID" "$LFT_AGENT"
launchctl kickstart -k "gui/$LFT_UID/local.finance-tracker.app"
for LFT_ATTEMPT in {1..12}; do
  if curl --max-time 1 -fsS -o /dev/null "http://127.0.0.1:8765/"; then
    echo "local-finance-tracker is installed as a login service. Open http://127.0.0.1:8765 in your browser."
    echo "To stop it: launchctl bootout gui/$LFT_UID $LFT_AGENT"
    echo "To remove it: run uninstall_service.command"
    exit 0
  fi
  sleep 1
done
launchctl bootout "gui/$LFT_UID" "$LFT_AGENT" 2>/dev/null || true
echo "local-finance-tracker could not start. macOS may be blocking LaunchAgent access to this project in Documents."
echo "Use run_tracker.command in Terminal, or move the project to a folder the agent can read."
exit 1
