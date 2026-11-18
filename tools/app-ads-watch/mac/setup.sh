#!/bin/sh
# Create the venv for the Slack listener and install the pinned slack_sdk.
set -eu
STATE_DIR="$HOME/.config/app-ads-watch"
VENV="$STATE_DIR/venv"
PYTHON="${PYTHON:-/opt/homebrew/bin/python3}"
SLACK_SDK_VERSION="3.46.0"

mkdir -p "$STATE_DIR/logs"
if [ ! -x "$VENV/bin/python" ]; then
  "$PYTHON" -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet "slack_sdk==$SLACK_SDK_VERSION"
"$VENV/bin/python" -c "import slack_sdk, slack_sdk.socket_mode.builtin; print('slack_sdk', slack_sdk.version.__version__)"
