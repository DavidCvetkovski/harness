import AppKit
import SwiftUI

@main
struct HarnessMacApp: App {
    @State private var store = AppStore()
    @State private var supervisor = BackendSupervisor()
    @State private var ui = MacUIState()

    var body: some Scene {
        WindowGroup("Harness", id: "main") {
            MacWorkspaceView(store: store, supervisor: supervisor, ui: ui)
                .frame(minWidth: 860, minHeight: 580)
                .preferredColorScheme(.dark)
                .tint(HarnessTheme.amber)
                .task {
                    wireNotifications()
                    await supervisor.connectOrStart(store: store, autoStart: true)
                }
        }
        .windowStyle(.hiddenTitleBar)
        .defaultSize(width: 1160, height: 780)
        .commands {
            CommandGroup(replacing: .newItem) {
                Button("New Chat") {
                    store.newChat()
                }
                .keyboardShortcut("n", modifiers: .command)
            }
            CommandMenu("Harness") {
                Button(ui.showSidebar ? "Hide History Sidebar" : "Show History Sidebar") {
                    withAnimation(.easeInOut(duration: 0.18)) { ui.showSidebar.toggle() }
                }
                .keyboardShortcut("\\", modifiers: .command)

                Button("Refresh Status") {
                    Task { await store.refreshAll() }
                }
                .keyboardShortcut("r", modifiers: .command)

                Button("Paste Image Attachment") {
                    if store.isVisionAvailable, let data = PlatformClipboard.pasteImageData() {
                        Task { await store.attachImageData(data) }
                    }
                }
                .keyboardShortcut("v", modifiers: [.command, .shift])
                .disabled(!store.isVisionAvailable || store.isStreaming)

                Divider()

                Button("Stop Generation") {
                    Task { await store.stopTurn() }
                }
                .keyboardShortcut(.escape, modifiers: [])
                .disabled(!store.isStreaming)
            }
        }

        MenuBarExtra {
            MacMenuBarPopover(store: store, supervisor: supervisor, ui: ui)
                .preferredColorScheme(.dark)
                .tint(HarnessTheme.amber)
        } label: {
            Image(systemName: menuBarSymbol)
                .symbolRenderingMode(.palette)
                .foregroundStyle(menuBarTint, .primary)
        }
        .menuBarExtraStyle(.window)

        Settings {
            MacSettingsView(store: store, supervisor: supervisor, ui: ui)
                .frame(minWidth: 780, idealWidth: 860, minHeight: 540, idealHeight: 620)
                .preferredColorScheme(.dark)
                .tint(HarnessTheme.amber)
        }
    }

    private var menuBarSymbol: String {
        if store.isStreaming || store.isSwitchingModel || supervisor.isStartingBackend {
            return "sparkle"
        }
        return store.connectionState == .connected && store.isModelLoaded ? "cpu.fill" : "cpu"
    }

    private var menuBarTint: Color {
        if store.isStreaming || store.isSwitchingModel || supervisor.isStartingBackend {
            return HarnessTheme.amber
        }
        if store.connectionState == .connected && store.isModelLoaded {
            return HarnessTheme.sage
        }
        return HarnessTheme.rust
    }

    private func wireNotifications() {
        MacNotifications.requestAuthorizationIfNeeded()
        store.onApprovalNeeded = { item in
            MacNotifications.notifyApproval(item)
        }
        store.onTurnFinished = { text in
            MacNotifications.notifyTurnFinished(preview: text)
        }
    }
}

// MARK: - Menu Bar Popover

struct MacMenuBarPopover: View {
    @Bindable var store: AppStore
    @Bindable var supervisor: BackendSupervisor
    @Bindable var ui: MacUIState
    @Environment(\.openWindow) private var openWindow
    @Environment(\.openSettings) private var openSettings
    @State private var showQR = false
    @State private var copiedLink = false

    private var phonePairingURL: String? {
        guard let access = store.accessInfo else { return nil }
        if !access.short.isEmpty { return access.short }
        if !access.url.isEmpty { return access.url }
        if !access.tailnet.isEmpty { return "http://\(access.tailnet):8420" }
        return nil
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 10) {
                HarnessBrandMark(size: 34)
                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 6) {
                        Circle()
                            .fill(ledColor)
                            .frame(width: 7, height: 7)
                            .shadow(color: ledColor.opacity(0.6), radius: 3)
                        Text(store.connectionState == .connected ? store.activeModelName : "Harness Offline")
                            .font(.system(size: 12.5, weight: .semibold, design: .monospaced))
                            .foregroundStyle(HarnessTheme.text)
                            .lineLimit(1)
                            .truncationMode(.middle)
                    }
                    Text(statusSubtitle)
                        .font(HarnessTheme.monoSmall)
                        .foregroundStyle(HarnessTheme.dim)
                }
                Spacer()
            }

            if let sys = store.systemState, store.connectionState == .connected {
                HStack(spacing: 8) {
                    statChip("RAM FREE", sys.memoryFree, tint: MacFormatting.memoryTint(sys.memoryFree))
                    statChip("DISK", MacFormatting.readableSize(sys.diskFree), tint: HarnessTheme.text)
                    statChip("MODE", sys.mode.uppercased(), tint: sys.tls == true ? HarnessTheme.sage : HarnessTheme.dim)
                }
            }

            Rectangle().fill(HarnessTheme.line).frame(height: 1)

            VStack(spacing: 2) {
                menuButton("Open Harness Window", systemImage: "macwindow") {
                    NSApp.activate(ignoringOtherApps: true)
                    openWindow(id: "main")
                }
                menuButton("New Chat", systemImage: "square.and.pencil", shortcut: "⌘N") {
                    store.newChat()
                    NSApp.activate(ignoringOtherApps: true)
                    openWindow(id: "main")
                }

                if store.connectionState != .connected {
                    menuButton(supervisor.isStartingBackend ? "Starting Harness…" : "Start Harness Backend",
                               systemImage: "play.fill") {
                        Task { await supervisor.startBackend(store: store, remote: true) }
                    }
                    .disabled(supervisor.isStartingBackend)
                } else if !store.isModelLoaded {
                    menuButton("Start Model Server", systemImage: "play.circle") {
                        Task { await store.runSystemAction("start-model") }
                    }
                }

                if let url = phonePairingURL {
                    menuButton(showQR ? "Hide iPhone QR Code" : "Pair iPhone (QR Code)", systemImage: "qrcode") {
                        withAnimation(.easeOut(duration: 0.15)) { showQR.toggle() }
                    }
                    if showQR {
                        VStack(spacing: 8) {
                            QRCodeCardView(urlString: url, size: 136)
                            Button(copiedLink ? "Copied!" : "Copy Address") {
                                PlatformClipboard.copy(url)
                                copiedLink = true
                                Task {
                                    try? await Task.sleep(nanoseconds: 1_200_000_000)
                                    copiedLink = false
                                }
                            }
                            .buttonStyle(.bordered)
                            .controlSize(.small)
                        }
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 6)
                    }
                }

                menuButton("Settings…", systemImage: "slider.horizontal.3", shortcut: "⌘,") {
                    ui.selectedSettingsPane = .overview
                    NSApp.activate(ignoringOtherApps: true)
                    openSettings()
                }
            }

            Rectangle().fill(HarnessTheme.line).frame(height: 1)

            HStack {
                Toggle("Launch at Login", isOn: Binding(
                    get: { supervisor.launchAtLogin },
                    set: { supervisor.setLaunchAtLogin($0) }
                ))
                .toggleStyle(.checkbox)
                .font(.caption)
                .foregroundStyle(HarnessTheme.dim)
                Spacer()
                Button("Quit") {
                    NSApp.terminate(nil)
                }
                .buttonStyle(.plain)
                .font(.caption.weight(.medium))
                .foregroundStyle(HarnessTheme.rust)
            }
        }
        .padding(14)
        .frame(width: 312)
        .background(HarnessTheme.panel)
    }

    private var ledColor: Color {
        if store.isStreaming || store.isSwitchingModel || supervisor.isStartingBackend { return HarnessTheme.amber }
        return (store.connectionState == .connected && store.isModelLoaded) ? HarnessTheme.sage : HarnessTheme.rust
    }

    private var statusSubtitle: String {
        if supervisor.isStartingBackend { return "Starting local server…" }
        if store.connectionState != .connected { return "Not connected to 127.0.0.1:8420" }
        if store.isStreaming { return store.statusText ?? "Generating…" }
        if !store.isModelLoaded { return "Model stopped" }
        return store.isVisionAvailable ? "Ready · Vision enabled" : "Ready"
    }

    private func statChip(_ label: String, _ value: String, tint: Color) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label)
                .font(.system(size: 8.5, weight: .semibold))
                .tracking(0.6)
                .foregroundStyle(HarnessTheme.faint)
            Text(value.isEmpty ? "—" : value)
                .font(HarnessTheme.monoSmall.weight(.medium))
                .foregroundStyle(tint)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 8)
        .padding(.vertical, 6)
        .background(HarnessTheme.panel2)
        .overlay(RoundedRectangle(cornerRadius: 6).stroke(HarnessTheme.line, lineWidth: 1))
        .clipShape(RoundedRectangle(cornerRadius: 6))
    }

    private func menuButton(_ title: String, systemImage: String, shortcut: String? = nil,
                            action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 9) {
                Image(systemName: systemImage)
                    .font(.caption)
                    .frame(width: 16)
                    .foregroundStyle(HarnessTheme.amber)
                Text(title)
                    .font(.subheadline)
                    .foregroundStyle(HarnessTheme.text)
                Spacer()
                if let shortcut {
                    Text(shortcut)
                        .font(.system(size: 10.5, design: .monospaced))
                        .foregroundStyle(HarnessTheme.faint)
                }
            }
            .padding(.horizontal, 8)
            .padding(.vertical, 6)
            .contentShape(Rectangle())
            .macHoverRow(cornerRadius: 6)
        }
        .buttonStyle(.plain)
    }
}
