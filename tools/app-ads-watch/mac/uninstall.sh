#!/bin/sh
# Unload the two launchd jobs and remove them from ~/Library/LaunchAgents. Secrets, state and logs stay.
set -eu
AGENTS="$HOME/Library/LaunchAgents"
for label in com.octofun.app-ads-listener com.octofun.app-ads-weekly; do
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  rm -f "$AGENTS/$label.plist"
  echo "removed $label"
done
