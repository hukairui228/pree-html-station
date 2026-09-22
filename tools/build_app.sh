#!/bin/zsh
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="$ROOT/Pree HTML Station.app"
RES="$APP/Contents/Resources"
MAC="$APP/Contents/MacOS"

rm -rf "$APP"; mkdir -p "$MAC" "$RES"

echo "→ Compiling host with swiftc"
xcrun swiftc -O -framework Cocoa -framework WebKit "$ROOT/tools/host/main.swift" -o "$MAC/PreeHTMLStation"

echo "→ Writing Info.plist"
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key><string>PreeHTMLStation</string>
    <key>CFBundleIdentifier</key><string>local.pree.htmlstation</string>
    <key>CFBundleName</key><string>Pree HTML Station</string>
    <key>CFBundleDisplayName</key><string>Pree HTML Station</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>1.2</string>
    <key>CFBundleVersion</key><string>3</string>
    <key>CFBundleIconFile</key><string>AppIcon</string>
    <key>NSHighResolutionCapable</key><true/>
    <key>NSRequiresAquaSystemAppearance</key><false/>
    <key>LSMinimumSystemVersion</key><string>13.0</string>
    <key>LSApplicationCategoryType</key><string>public.app-category.developer-tools</string>
    <key>CFBundleDocumentTypes</key>
    <array>
        <dict>
            <key>CFBundleTypeName</key><string>HTML document</string>
            <key>CFBundleTypeRole</key><string>Editor</string>
            <key>LSHandlerRank</key><string>Default</string>
            <key>LSItemContentTypes</key>
            <array>
                <string>public.html</string>
                <string>public.xhtml</string>
            </array>
        </dict>
    </array>
</dict>
</plist>
PLIST
plutil -lint "$APP/Contents/Info.plist"

echo "→ Icon"
if [ -f "$ROOT/tools/AppIcon.icns" ]; then
  cp "$ROOT/tools/AppIcon.icns" "$RES/AppIcon.icns"
else
  echo "  (no icns found, using default icon)"
fi

codesign --force --deep --sign - "$APP" 2>/dev/null || echo "  (skipping ad-hoc codesign)"
echo "✓ Build complete: $APP"
