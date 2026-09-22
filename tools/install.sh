#!/bin/bash
# Pree HTML Station installer
# Usage: curl -fsSL https://raw.githubusercontent.com/hukairui228/pree-html-station/main/tools/install.sh | bash
set -euo pipefail

REPO="hukairui228/pree-html-station"
ZIP="PreeHTMLStation-macOS.zip"
APP="Pree HTML Station.app"
URL="https://github.com/$REPO/releases/latest/download/$ZIP"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Apple Silicon or Intel?
ARCH="$(uname -m)"
echo "→ Downloading Pree HTML Station (latest release, $ARCH)..."
curl -fsSL "$URL" -o "$TMP/$ZIP"

echo "→ Extracting..."
ditto -x -k "$TMP/$ZIP" "$TMP/app"
if [ ! -d "$TMP/app/$APP" ]; then
  echo "✗ Downloaded bundle doesn't look right. Aborting."
  exit 1
fi

# The build is ad-hoc signed; strip the quarantine flag so it opens
# on first double-click instead of triggering the Gatekeeper dance.
xattr -dr com.apple.quarantine "$TMP/app/$APP" 2>/dev/null || true

DEST="/Applications"
mkdir -p "$DEST" 2>/dev/null || true
if [ ! -w "$DEST" ]; then
  DEST="$HOME/Applications"
  mkdir -p "$DEST"
fi

if [ -d "$DEST/$APP" ]; then
  echo "→ Removing previous version..."
  rm -rf "$DEST/$APP"
fi

if mv "$TMP/app/$APP" "$DEST/" 2>/dev/null; then
  echo "✓ Installed to $DEST/$APP"
else
  DEST="$HOME/Applications"
  mkdir -p "$DEST"
  mv "$TMP/app/$APP" "$DEST/"
  echo "✓ Installed to $DEST/$APP (couldn't write to /Applications)"
fi

open -a "$DEST/$APP" 2>/dev/null || true
echo "Done — Pree HTML Station is ready. Drag any .html file in and edit away."
