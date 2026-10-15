#!/bin/bash
set -euo pipefail
# Downloads an app only. Does not run it, change Gatekeeper, use sudo, or touch accounts.
[[ "$(uname -s)" == Darwin ]] || { echo 'macOS required.' >&2; exit 1; }
[[ "$(uname -m)" == arm64 ]] || { echo 'This release is for Apple Silicon. Intel Macs: build from source (README).' >&2; exit 1; }
repo='Ayanami0730/cockpit-accounts'
version='v0.1.0'
archive='Cockpit-Accounts-0.1.0-macos-arm64.zip'
dest="$HOME/Applications/Cockpit Accounts.app"
[[ ! -e "$dest" ]] || { echo "Already exists: $dest. Quit and move the old app aside first." >&2; exit 1; }
task_tmp="$(mktemp -d)"
trap 'rm -rf "$task_tmp"' EXIT
curl --fail --location --proto '=https' --tlsv1.2 --output "$task_tmp/app.zip" "https://github.com/$repo/releases/download/$version/$archive"
ditto -x -k "$task_tmp/app.zip" "$task_tmp/unpacked"
app="$task_tmp/unpacked/Cockpit Accounts.app"
[[ -d "$app" ]] || { echo 'Release archive is missing the expected app.' >&2; exit 1; }
codesign --verify --deep --strict "$app"
mkdir -p "$HOME/Applications"
ditto "$app" "$dest"
echo "Installed: $dest"
echo 'Open from Finder. If macOS blocks the unsigned developer, review System Settings > Privacy & Security > Open Anyway.'
echo 'Claude features require Python 3 (Apple Command Line Tools) and the official Claude Code CLI.'
