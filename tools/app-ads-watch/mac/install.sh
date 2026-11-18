#!/bin/sh
# Copy the two launchd jobs into ~/Library/LaunchAgents and load them. Run setup.sh first.
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
mkdir -p "$AGENTS" "$HOME/.config/app-ads-watch/logs"
if [ ! -x "$HOME/.config/app-ads-watch/venv/bin/python" ]; then
  echo "venv missing: run $HERE/setup.sh first" >&2
  exit 1
fi
for label in com.octofun.app-ads-listener com.octofun.app-ads-weekly; do
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  REPO="$(cd "$HERE/../../.." && pwd)"
  sed -e "s|__REPO__|$REPO|g" -e "s|__HOME__|$HOME|g" "$HERE/launchd/$label.plist" > "$AGENTS/$label.plist"
  launchctl bootstrap "gui/$(id -u)" "$AGENTS/$label.plist"
  echo "loaded $label"
done
