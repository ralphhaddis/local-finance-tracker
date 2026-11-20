#!/bin/zsh
set -euo pipefail

LFT_PROJECT="$(cd "$(dirname "$0")" && pwd)"
LFT_BUILD="$(mktemp -d "${TMPDIR:-/private/tmp}/tracker-launcher.XXXXXX")"
LFT_APP="$LFT_BUILD/local-finance-tracker Launcher.app"
LFT_DEST="$HOME/Applications/local-finance-tracker.app"
LFT_CONTENTS="$LFT_APP/Contents"
LFT_ASSETS="$LFT_BUILD/Assets.xcassets"
LFT_TRASH="$HOME/.Trash"
trap 'rm -rf "$LFT_BUILD"' EXIT

mkdir -p "$LFT_CONTENTS/MacOS" "$LFT_CONTENTS/Resources" "$HOME/Applications"
cp "$LFT_PROJECT/macos/launcher/Info.plist" "$LFT_CONTENTS/Info.plist"
/usr/libexec/PlistBuddy -c "Add :TrackerProjectPath string $LFT_PROJECT" "$LFT_CONTENTS/Info.plist"

LFT_ICONSET="$LFT_ASSETS/AppIcon.appiconset"
mkdir -p "$LFT_ICONSET"
cp "$LFT_PROJECT/macos/launcher/AppIcon.appiconset/Contents.json" "$LFT_ICONSET/Contents.json"
for LFT_SIZE in 16 32 64 128 256 512 1024; do
  sips -z "$LFT_SIZE" "$LFT_SIZE" "$LFT_PROJECT/public/app-icon.png" \
    --out "$LFT_ICONSET/icon-${LFT_SIZE}.png" >/dev/null
done
cp "$LFT_ICONSET/icon-32.png" "$LFT_ICONSET/icon-32-1x.png"
cp "$LFT_ICONSET/icon-256.png" "$LFT_ICONSET/icon-256-1x.png"
cp "$LFT_ICONSET/icon-512.png" "$LFT_ICONSET/icon-512-1x.png"
xcrun actool --compile "$LFT_CONTENTS/Resources" --platform macosx \
  --minimum-deployment-target 13.0 --app-icon AppIcon \
  --output-partial-info-plist "$LFT_BUILD/asset-info.plist" "$LFT_ASSETS"

xcrun swiftc -parse-as-library -swift-version 5 \
  "$LFT_PROJECT/macos/launcher/main.swift" \
  -framework AppKit -framework Foundation -framework WebKit \
  -o "$LFT_CONTENTS/MacOS/LocalFinanceTracker"

# Build outside Documents/File Provider folders: those can attach Finder
# metadata that codesign rejects as a resource fork.
xattr -cr "$LFT_APP"
codesign --force --deep --sign - "$LFT_APP"

# Keep the existing Dock path, but replace the Safari web app with this native
# app. Preserve the Safari bundle under a distinct name in case it is needed.
if [ -d "$LFT_DEST" ]; then
  LFT_EXISTING_ID="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$LFT_DEST/Contents/Info.plist" 2>/dev/null || true)"
  if [[ "$LFT_EXISTING_ID" == com.apple.Safari.WebApp.* ]]; then
    LFT_WEB_SHORTCUT="$LFT_TRASH/local-finance-tracker Safari Shortcut.app"
    if [ -e "$LFT_WEB_SHORTCUT" ]; then
      LFT_WEB_SHORTCUT="$LFT_TRASH/local-finance-tracker Safari Shortcut $(date +%Y%m%d-%H%M%S).app"
    fi
    mv "$LFT_DEST" "$LFT_WEB_SHORTCUT"
  fi
fi

# Move the earlier build's backups out of Applications while keeping them
# recoverable in Trash.
for LFT_OLD_BACKUP in "$HOME/Applications/local-finance-tracker Safari Shortcut.app" "$HOME/Applications/local-finance-tracker Launcher Legacy.app"; do
  if [ -d "$LFT_OLD_BACKUP" ]; then
    LFT_TRASH_BACKUP="$LFT_TRASH/$(basename "$LFT_OLD_BACKUP")"
    if [ -e "$LFT_TRASH_BACKUP" ]; then
      LFT_TRASH_BACKUP="$LFT_TRASH/$(basename "${LFT_OLD_BACKUP%.app}") $(date +%Y%m%d-%H%M%S).app"
    fi
    mv "$LFT_OLD_BACKUP" "$LFT_TRASH_BACKUP"
  fi
done

ditto "$LFT_APP" "$LFT_DEST"
xattr -dr com.apple.FinderInfo "$LFT_DEST" 2>/dev/null || true
xattr -dr com.apple.ResourceFork "$LFT_DEST" 2>/dev/null || true
xattr -cr "$LFT_DEST"
codesign --force --deep --sign - "$LFT_DEST"

# Keep older Dock shortcuts working by turning the previous launcher path into
# an alias for the one native app. The old bundle is retained as a backup.
LFT_OLD_LAUNCHER="$HOME/Applications/local-finance-tracker Launcher.app"
if [ -d "$LFT_OLD_LAUNCHER" ] && [ ! -L "$LFT_OLD_LAUNCHER" ]; then
  LFT_OLD_LAUNCHER_BACKUP="$LFT_TRASH/local-finance-tracker Launcher Legacy.app"
  if [ -e "$LFT_OLD_LAUNCHER_BACKUP" ]; then
    LFT_OLD_LAUNCHER_BACKUP="$LFT_TRASH/local-finance-tracker Launcher Legacy $(date +%Y%m%d-%H%M%S).app"
  fi
  mv "$LFT_OLD_LAUNCHER" "$LFT_OLD_LAUNCHER_BACKUP"
fi
if [ -L "$LFT_OLD_LAUNCHER" ]; then
  unlink "$LFT_OLD_LAUNCHER"
fi
ln -s "$LFT_DEST" "$LFT_OLD_LAUNCHER"

echo "local-finance-tracker is installed at: $LFT_DEST"
echo "The previous launcher shortcut now opens the same native app."
