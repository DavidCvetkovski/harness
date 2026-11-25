# Harness for iOS

Native SwiftUI companion app for [Harness](../README.md) on macOS. Zero third-party dependencies — built with SwiftUI, Observation, URLSession SSE streaming, PhotosUI, AVFoundation, and Security (Keychain).

## Features

- **Real-time streaming chat** over `/api/chat` SSE with collapsible thinking traces, tool-call inspectors, live stats, and background/foreground session recovery when your iPhone screen locks.
- **Interactive approvals** for file writes, edits, and shell commands with inline color-coded diffs (`Allow`, `Always`, `Deny`).
- **Vision & photo uploads** via Photo Library or Camera (automatically shown when the loaded Mac model supports vision, resized on-device and uploaded to `/api/uploads`).
- **QR & Tailscale connect** — scan the QR code printed by `harness --remote` or `harness --lan`, paste a link (extracting `?key=` into iOS Keychain), or type your Tailscale MagicDNS host.
- **Full Mac control** — switch GGUF models, apply presets, tune sampling & system prompt, toggle individual tools and approval policies, manage/run automations, inspect `app.log` & `model.log`, and play replies via the local voice sidecar.
- **Offline Demo Mode** — built-in self-contained demo backend (streaming replies, tool calls, interactive diff approvals, image attachments, and sessions) for trying the app without a Mac nearby and for App Store Review (Guideline 2.1).

## Opening & Running on Your iPhone

1. Open [`ios/Harness.xcodeproj`](Harness.xcodeproj) in Xcode 16+ (or regenerate it from [`project.yml`](project.yml) via `cd ios && xcodegen generate`).
2. Select the **Harness** target → **Signing & Capabilities** → pick your Apple ID / Team.
3. Connect your iPhone (or choose an iOS Simulator) and press **Run (`⌘R`)**.
4. On your Mac, start Harness in remote mode:
   ```bash
   harness --remote
   ```
   Scan the QR code in Terminal (or enter your Mac's Tailscale address) in the iOS app.

## Enabling HTTPS over Tailscale (Recommended for TestFlight & App Store)

Right now `Info.plist` includes an App Transport Security exception for `*.ts.net` so you can connect immediately over Tailscale HTTP (`http://100.x.x.x:8420` or `http://my-mac.ts.net:8420`). Before submitting to the App Store, switch to real Tailscale HTTPS:

1. Enable **HTTPS Certificates** in your [Tailscale Admin Console → DNS](https://login.tailscale.com/admin/dns).
2. On your Mac, run:
   ```bash
   harness --remote --tls
   ```
   Harness automatically provisions and renews certificates under `~/.harness/certs/` via `tailscale cert` and serves `https://<your-mac>.<tailnet>.ts.net:8420`.
3. The app allows plain HTTP (`NSAllowsArbitraryLoads` in [`ios/project.yml`](project.yml)), because a Tailscale `100.x` address is not "local" to iOS and was refused outright. Tailscale already encrypts that traffic. If you switch to HTTPS only, change that key and explain the choice in App Review Notes: a self-hosted server at a user-supplied address is the accepted reason.

## TestFlight & App Store Review Notes

- **Archive**: Product → Archive in Xcode with your Apple Developer Team selected, then upload to App Store Connect / TestFlight.
- **App Store Review (Guideline 2.1)**: In App Review Notes, point the reviewer to **"Try Offline Demo Mode"** on the connect screen so they can test streaming chat, tool calls, interactive file-edit approvals (`"Write a small Python script..."`), photo attachments, history, and settings without needing a paired Mac.
- **Privacy**: Harness connects only to the user's own Mac server (or runs completely offline in Demo Mode). No analytics, tracking SDKs, or cloud relays.

## Running Unit Tests

```bash
cd ios
xcodebuild test \
  -project Harness.xcodeproj \
  -scheme Harness \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' \
  CODE_SIGNING_ALLOWED=NO
```
