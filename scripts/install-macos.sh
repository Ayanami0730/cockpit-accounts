#!/bin/bash
set -euo pipefail
# Build the reviewed local checkout. Never runs sudo or modifies client credentials.
cd "$(dirname "$0")/.."
[[ "$(uname -s)" == Darwin ]] || { echo 'This installer requires macOS.' >&2; exit 1; }
for tool in node npm cargo rustup python3; do
  command -v "$tool" >/dev/null || { echo "Missing $tool. See README.md prerequisites." >&2; exit 1; }
done
xcode-select -p >/dev/null 2>&1 || { echo 'Install Apple Command Line Tools: xcode-select --install' >&2; exit 1; }
npm ci --ignore-scripts --no-audit --no-fund
rustup toolchain install 1.95.0 --profile minimal
python3 scripts/build-local-safe.py build
version="$(node -p 'require("./package.json").version')"
archive="target/release/bundle/Cockpit-Accounts-$version-macos-$(uname -m).zip"
mkdir -p "$HOME/Applications"
dest="$HOME/Applications/Cockpit Accounts.app"
if [[ -e "$dest" ]]; then
  echo "Already installed: $dest. Quit Cockpit Accounts, then move that copy aside before reinstalling." >&2
  exit 1
fi
ditto -x -k "$archive" "$HOME/Applications"
echo "Installed: $dest"
echo 'Open it from Finder. No accounts have been imported or changed.'
