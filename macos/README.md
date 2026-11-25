# Harness for macOS

Native SwiftUI desktop + menu bar app for [Harness](../README.md) on Apple Silicon. Shares its core models, SSE streaming client, `@Observable` store, Markdown renderer, and theme directly with [`ios/`](../ios/README.md) — zero third-party packages.

## Features

- **Native macOS workspace** — `NavigationSplitView` with searchable grouped History sidebar, right-click export/delete, and instrument-panel chat view.
- **Menu Bar controller (`MenuBarExtra`)** — live green/amber/red status indicator, active model & RAM/disk readout, quick New Chat, Start/Restart Model, Launch at Login (`SMAppService`), and built-in **iPhone QR pairing** popover.
- **Desktop vision & project tools** — drag-and-drop images straight onto the window, paste images (`⌘⇧V`), choose files via `NSOpenPanel`, pick your project folder natively, and export transcripts via `NSSavePanel`.
- **Native notifications** — macOS Notification Center alerts when Harness needs approval or finishes a long turn while the window is in the background.
- **Native Settings window (`⌘,`)** — General, Model switcher (with confirmations), Permissions & safe commands, Automations, iPhone & Remote QR pairing, and live `app.log`/`model.log` viewer.

## Building & Installing Locally

```bash
cd macos
xcodegen generate
xcodebuild build -project HarnessMac.xcodeproj -scheme HarnessMac -configuration Release -derivedDataPath /tmp/harness-mac-build
cp -R /tmp/harness-mac-build/Build/Products/Release/Harness.app /Applications/Harness.app
```

Run macOS unit tests:

```bash
cd macos
xcodebuild test -project HarnessMac.xcodeproj -scheme HarnessMac -destination 'platform=macOS'
```

## Standalone Distribution Roadmap (`.dmg` for Anyone)

The app is already architected for self-contained direct distribution (non-sandboxed, hardened runtime — like Ollama, LM Studio, and Raycast):

1. **Bundled backend priority** — `BackendLocator` checks `Harness.app/Contents/Resources/backend/` first (and passes `HARNESS_LLAMA_SERVER` to `harness/model.sh`), falling back to `~/Developer/Harness/bin/harness` when running from a checkout.
2. **Release DMG & notarization script** — [`macos/scripts/package_dmg.sh`](scripts/package_dmg.sh) builds a Release `Harness.app`, optionally bundles `harness/` + `llama-server` (`--bundle-backend`), signs with `DEVELOPER_ID`, notarizes via `xcrun notarytool` (`NOTARY_PROFILE`), and builds `dist/Harness-<version>-arm64.dmg`.
3. **First-run empty-models UX** — When `~/llm/models` has no `.gguf` files yet, the app shows a clean onboarding card with a one-click Finder shortcut (ready to hook up a built-in Hugging Face GGUF downloader next).
4. **Future next step for true zero-dependency distribution** — bundle a relocatable `python-build-standalone` (or port the ~1,200-line Python loop to Swift) inside `Contents/Resources/backend/python/` alongside `llama-server`.
