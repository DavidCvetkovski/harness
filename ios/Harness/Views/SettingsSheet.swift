import SwiftUI

/// Settings as one list that drills into pages, the way iOS Settings works,
/// rather than five cramped segments. Everything that interrupts the Mac —
/// switching or restarting the model, restarting Harness, resetting —
/// confirms first and says what it will interrupt.
struct SettingsSheet: View {
    @Bindable var store: AppStore
    @Environment(\.dismiss) private var dismiss

    @State private var showFolderPicker = false
    @State private var showLogs = false
    @State private var pendingAction: MacAction?

    enum MacAction: String, Identifiable {
        case startModel, restartModel, restartApp, disconnect, reset
        var id: String { rawValue }
    }

    var body: some View {
        NavigationStack {
            List {
                Section {
                    MacStatusCard(store: store)
                }
                .listRowBackground(HarnessTheme.panel)

                assistantSection
                safetySection
                macSection
                connectionSection

                Section {
                    Button(role: .destructive) {
                        pendingAction = .reset
                    } label: {
                        SettingsRowLabel(icon: "arrow.counterclockwise", tint: HarnessTheme.rust,
                                         title: "Reset All Settings…", titleColor: HarnessTheme.rust)
                    }
                } footer: {
                    Text("Harness for iOS \(Self.appVersion)")
                        .frame(maxWidth: .infinity)
                        .padding(.top, 12)
                }
                .listRowBackground(HarnessTheme.panel)
            }
            .scrollContentBackground(.hidden)
            .background(HarnessTheme.ink)
            .refreshable { await store.refreshAll() }
            .navigationTitle("Settings")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { dismiss() }
                }
            }
            .confirmationDialog(
                pendingAction.map(confirmTitle) ?? "",
                isPresented: Binding(get: { pendingAction != nil },
                                     set: { if !$0 { pendingAction = nil } }),
                titleVisibility: .visible,
                presenting: pendingAction
            ) { action in
                Button(confirmButton(action), role: action == .startModel ? nil : .destructive) {
                    perform(action)
                }
                Button("Cancel", role: .cancel) {}
            } message: { action in
                Text(confirmMessage(action))
            }
            .sheet(isPresented: $showFolderPicker) { FolderPickerSheet(store: store) }
            .sheet(isPresented: $showLogs) { LogsSheet(store: store) }
        }
    }

    // MARK: - Sections

    @ViewBuilder
    private var assistantSection: some View {
        Section {
            NavigationLink {
                ModelsPage(store: store)
            } label: {
                SettingsRowLabel(icon: "cpu", tint: HarnessTheme.amber, title: "Model",
                                 value: store.isSwitchingModel ? "Switching…" : shortModelName)
            }

            NavigationLink {
                PresetsPage(store: store)
            } label: {
                SettingsRowLabel(icon: "theatermasks", tint: HarnessTheme.sage, title: "Behaviour",
                                 value: presetLabel)
            }

            Toggle(isOn: Binding(
                get: { store.config?.thinking ?? false },
                set: { value in Task { await store.updateConfig(["thinking": value]) } }
            )) {
                SettingsRowLabel(icon: "brain.head.profile", tint: HarnessTheme.amber,
                                 title: "Think First by Default")
            }
            .disabled(store.config == nil)

            Button {
                showFolderPicker = true
            } label: {
                SettingsRowLabel(icon: "folder", tint: HarnessTheme.amberDim, title: "Project Folder",
                                 value: store.projectName.isEmpty ? "None" : store.projectName,
                                 showsChevron: true)
            }
            .disabled(store.client == nil)

            NavigationLink {
                InstructionsPage(store: store)
            } label: {
                SettingsRowLabel(icon: "text.quote", tint: HarnessTheme.dim, title: "Instructions",
                                 value: instructionsSummary)
            }

            NavigationLink {
                TuningPage(store: store)
            } label: {
                SettingsRowLabel(icon: "dial.medium", tint: HarnessTheme.dim, title: "Sampling & Limits",
                                 value: store.config.map { String(format: "temp %.2f", $0.temperature) } ?? "")
            }
        } header: {
            sectionHeader("Assistant")
        } footer: {
            Text("Think First sets the default for new chats; the chip above the keyboard changes it for the chat you're in.")
        }
        .listRowBackground(HarnessTheme.panel)
    }

    @ViewBuilder
    private var safetySection: some View {
        Section {
            NavigationLink {
                PermissionsPage(store: store)
            } label: {
                SettingsRowLabel(icon: "hand.raised", tint: HarnessTheme.rust, title: "Permissions",
                                 value: approvalSummary)
            }
            NavigationLink {
                AutomationsPage(store: store)
            } label: {
                SettingsRowLabel(icon: "clock.badge.checkmark", tint: HarnessTheme.sage, title: "Automations",
                                 value: automationSummary)
            }
            NavigationLink {
                VoicePage(store: store)
            } label: {
                SettingsRowLabel(icon: "waveform", tint: HarnessTheme.amber, title: "Voice",
                                 value: voiceSummary)
            }
        } header: {
            sectionHeader("Safety & Automation")
        }
        .listRowBackground(HarnessTheme.panel)
    }

    @ViewBuilder
    private var macSection: some View {
        Section {
            if store.systemState?.model.running ?? store.isModelLoaded {
                Button {
                    pendingAction = .restartModel
                } label: {
                    SettingsRowLabel(icon: "arrow.clockwise", tint: HarnessTheme.amber, title: "Restart Model")
                }
            } else {
                Button {
                    pendingAction = .startModel
                } label: {
                    SettingsRowLabel(icon: "play.fill", tint: HarnessTheme.sage, title: "Start Model")
                }
            }

            Button {
                pendingAction = .restartApp
            } label: {
                SettingsRowLabel(icon: "power", tint: HarnessTheme.amber, title: "Restart Harness")
            }

            if let sys = store.systemState {
                Toggle(isOn: Binding(
                    get: { sys.autostart },
                    set: { on in
                        Task { await store.runSystemAction(on ? "enable-autostart" : "disable-autostart") }
                    }
                )) {
                    SettingsRowLabel(icon: "sunrise", tint: HarnessTheme.sage, title: "Start at Login")
                }
            }

            Button {
                showLogs = true
            } label: {
                SettingsRowLabel(icon: "doc.text.magnifyingglass", tint: HarnessTheme.dim,
                                 title: "View Logs", showsChevron: true)
            }
        } header: {
            sectionHeader("Your Mac")
        } footer: {
            Text("Start at Login also brings Harness back if it ever crashes.")
        }
        .listRowBackground(HarnessTheme.panel)
    }

    @ViewBuilder
    private var connectionSection: some View {
        Section {
            LabeledContent("Server") {
                Text(store.isDemoMode ? "On-device demo" : store.serverURLInput)
                    .font(HarnessTheme.monoSmall)
                    .foregroundStyle(HarnessTheme.dim)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .textSelection(.enabled)
            }
            if let tls = store.capabilities?.tls, !store.isDemoMode {
                LabeledContent("Security") {
                    Text(tls ? "HTTPS (Tailscale certificate)" : "Encrypted by Tailscale")
                        .foregroundStyle(HarnessTheme.dim)
                }
            }
            if let ip = store.systemState?.tailnet, !ip.isEmpty {
                LabeledContent("Tailscale IP") {
                    Text(ip)
                        .font(HarnessTheme.monoSmall)
                        .foregroundStyle(HarnessTheme.dim)
                        .textSelection(.enabled)
                }
            }
            Button(role: .destructive) {
                pendingAction = .disconnect
            } label: {
                Text("Disconnect…")
                    .foregroundStyle(HarnessTheme.rust)
            }
        } header: {
            sectionHeader("Connection")
        }
        .listRowBackground(HarnessTheme.panel)
    }

    private func sectionHeader(_ text: String) -> some View {
        Text(text.uppercased())
            .font(HarnessTheme.label)
            .tracking(1.0)
            .foregroundStyle(HarnessTheme.dim)
    }

    // MARK: - Summaries

    private var activeModel: ModelEntry? {
        store.modelsResponse?.models.first(where: \.active)
    }

    private var shortModelName: String {
        let name = activeModel?.name ?? store.activeModelName
        // "Qwen3.5-9B-Uncensored-HauhauCS-Aggressive-Q4_K_M" → "Qwen3.5-9B…"
        let parts = name.split(separator: "-")
        if parts.count > 2 { return parts.prefix(2).joined(separator: "-") + "…" }
        return name
    }

    private var presetLabel: String {
        guard let id = store.config?.preset, !id.isEmpty else { return "Custom" }
        return store.presetsResponse?.presets.first(where: { $0.id == id })?.label ?? id.capitalized
    }

    private var instructionsSummary: String {
        let count = store.config?.systemPrompt.count ?? 0
        return count == 0 ? "None" : "\(count) chars"
    }

    private var approvalSummary: String {
        guard let cfg = store.config else { return "" }
        if cfg.approveFiles == "always" && cfg.approveCommands == "always" { return "Always ask" }
        if cfg.approveFiles == "never" && cfg.approveCommands == "never" { return "Never ask" }
        return "Custom"
    }

    private var automationSummary: String {
        let on = store.automations.filter(\.enabled).count
        return store.automations.isEmpty ? "None" : "\(on) on"
    }

    private var voiceSummary: String {
        guard store.capabilities?.voice == true else { return "Off" }
        return store.voices.first(where: \.selected)?.name ?? "\(store.voices.count) voices"
    }

    static var appVersion: String {
        let info = Bundle.main.infoDictionary
        let version = info?["CFBundleShortVersionString"] as? String ?? "1.0"
        let build = info?["CFBundleVersion"] as? String ?? "1"
        return "\(version) (\(build))"
    }

    // MARK: - Confirmations

    private func confirmTitle(_ action: MacAction) -> String {
        switch action {
        case .startModel: return "Start the model?"
        case .restartModel: return "Restart the model?"
        case .restartApp: return "Restart Harness on your Mac?"
        case .disconnect: return "Disconnect from this Mac?"
        case .reset: return "Reset all settings?"
        }
    }

    private func confirmButton(_ action: MacAction) -> String {
        switch action {
        case .startModel: return "Start Model"
        case .restartModel: return "Restart Model"
        case .restartApp: return "Restart Harness"
        case .disconnect: return "Disconnect"
        case .reset: return "Reset Settings"
        }
    }

    private func confirmMessage(_ action: MacAction) -> String {
        let running = store.isStreaming ? " The reply that's running now will stop." : ""
        switch action {
        case .startModel:
            return "Loads \(shortModelName) into memory, about 30 seconds."
        case .restartModel:
            return "Reloads the weights, about 30 seconds. Nothing can run on your Mac until it's done — "
                + "including anything else using the model, like a test or an automation.\(running)"
        case .restartApp:
            return "Harness stops for a few seconds and this app reconnects by itself.\(running)"
        case .disconnect:
            return "You'll need the address or the QR code from your Mac to connect again."
        case .reset:
            return "Instructions, sampling, approvals, tools and presets go back to their defaults. Your chats are kept."
        }
    }

    private func perform(_ action: MacAction) {
        switch action {
        case .startModel:
            Task { await store.runSystemAction("start-model") }
        case .restartModel:
            Task { await store.runSystemAction("restart-model") }
        case .restartApp:
            Task { await store.runSystemAction("restart-app") }
        case .disconnect:
            dismiss()
            store.disconnect()
        case .reset:
            Task { await store.resetConfig() }
        }
    }

    // MARK: - Formatting

    /// `df -h` sizes ("75Gi", "1.2Ti") into the units people read ("75 GB").
    static func readableSize(_ raw: String) -> String {
        let units = ["Ki": "KB", "Mi": "MB", "Gi": "GB", "Ti": "TB", "K": "KB", "M": "MB", "G": "GB", "T": "TB"]
        for (suffix, unit) in units.sorted(by: { $0.key.count > $1.key.count }) where raw.hasSuffix(suffix) {
            return "\(raw.dropLast(suffix.count)) \(unit)"
        }
        return raw
    }
}

// MARK: - Mac Status Card

/// The Mac at a glance: what's loaded, how much it costs, and how much room
/// is left. Memory goes amber then red as it runs out, because on a 16 GB
/// Mac that is what decides whether replies come at full speed.
struct MacStatusCard: View {
    let store: AppStore

    private var activeModel: ModelEntry? {
        store.modelsResponse?.models.first(where: \.active)
    }

    private var isRunning: Bool {
        store.systemState?.model.running ?? store.isModelLoaded
    }

    private var ledColor: Color {
        if store.isSwitchingModel || store.isStreaming { return HarnessTheme.amber }
        return isRunning ? HarnessTheme.sage : HarnessTheme.rust
    }

    private var modelStatus: String {
        if store.isDemoMode { return "Offline demo — nothing runs on a Mac" }
        if store.isSwitchingModel { return "Switching model…" }
        guard isRunning else { return "Stopped — start it below" }
        if let model = activeModel, model.estGb > 0 {
            if model.budgetGb > 0 {
                return String(format: "Running · ~%.1f of %.1f GB GPU memory", model.estGb, model.budgetGb)
            }
            return String(format: "Running · ~%.1f GB", model.estGb)
        }
        return "Running"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 10) {
                Circle()
                    .fill(ledColor)
                    .frame(width: 9, height: 9)
                    .shadow(color: ledColor.opacity(0.6), radius: 3)
                VStack(alignment: .leading, spacing: 3) {
                    Text(store.activeModelName)
                        .font(.system(size: 14, weight: .semibold, design: .monospaced))
                        .foregroundStyle(HarnessTheme.text)
                        .lineLimit(1)
                        .truncationMode(.middle)
                    Text(modelStatus)
                        .font(HarnessTheme.monoSmall)
                        .foregroundStyle(HarnessTheme.dim)
                }
            }

            if let sys = store.systemState {
                HStack(alignment: .top, spacing: 0) {
                    metric("MEMORY FREE", sys.memoryFree, tint: memoryTint(sys.memoryFree))
                    metric("DISK FREE", SettingsSheet.readableSize(sys.diskFree), tint: HarnessTheme.text)
                    metric("MAC UP", sys.uptime, tint: HarnessTheme.text)
                }
            }
        }
        .padding(.vertical, 6)
        .accessibilityElement(children: .combine)
    }

    private func metric(_ label: String, _ value: String, tint: Color) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(label)
                .font(.system(size: 9.5, weight: .semibold))
                .tracking(0.8)
                .foregroundStyle(HarnessTheme.faint)
            Text(value.isEmpty ? "—" : value)
                .font(.system(size: 15, weight: .medium, design: .monospaced))
                .foregroundStyle(tint)
                .lineLimit(1)
                .minimumScaleFactor(0.7)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    static func freePercent(_ raw: String) -> Int? {
        Int(raw.filter(\.isNumber))
    }

    private func memoryTint(_ raw: String) -> Color {
        guard let percent = Self.freePercent(raw) else { return HarnessTheme.text }
        if percent < 15 { return HarnessTheme.rust }
        if percent < 30 { return HarnessTheme.amber }
        return HarnessTheme.sage
    }
}

// MARK: - Row Label

/// An iOS-Settings-style row: tinted icon tile, title, and the current value.
struct SettingsRowLabel: View {
    let icon: String
    let tint: Color
    let title: String
    var value: String = ""
    var titleColor: Color = HarnessTheme.text
    var showsChevron = false

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: icon)
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(tint)
                .frame(width: 28, height: 28)
                .background(RoundedRectangle(cornerRadius: 7).fill(tint.opacity(0.16)))
            Text(title)
                .foregroundStyle(titleColor)
                .lineLimit(1)
            Spacer(minLength: 8)
            if !value.isEmpty {
                Text(value)
                    .font(.subheadline)
                    .foregroundStyle(HarnessTheme.dim)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
            if showsChevron {
                Image(systemName: "chevron.right")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(HarnessTheme.faint)
            }
        }
        .contentShape(Rectangle())
    }
}

// MARK: - Logs

struct LogsSheet: View {
    let store: AppStore
    @Environment(\.dismiss) private var dismiss
    @State private var kind = "app"
    @State private var text = ""

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                Picker("Log", selection: $kind) {
                    Text("Harness").tag("app")
                    Text("Model").tag("model")
                }
                .pickerStyle(.segmented)
                .padding()
                ScrollViewReader { proxy in
                    ScrollView {
                        Text(text.isEmpty ? "Loading…" : text)
                            .font(HarnessTheme.monoSmall)
                            .foregroundStyle(HarnessTheme.text)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .textSelection(.enabled)
                            .padding()
                        Color.clear.frame(height: 1).id("end")
                    }
                    .onChange(of: text) { _, _ in proxy.scrollTo("end", anchor: .bottom) }
                }
            }
            .background(HarnessTheme.ink)
            .navigationTitle("Mac Logs")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Close") { dismiss() }
                }
                ToolbarItem(placement: .primaryAction) {
                    HStack {
                        ShareLink(item: text) { Image(systemName: "square.and.arrow.up") }
                            .disabled(text.isEmpty)
                        Button {
                            Task { text = await store.fetchLogs(kind: kind) }
                        } label: {
                            Image(systemName: "arrow.clockwise")
                        }
                    }
                }
            }
            .task(id: kind) { text = await store.fetchLogs(kind: kind) }
        }
    }
}
