import AppKit
import SwiftUI

/// Modern macOS sidebar-split Settings window crafted in Harness's warm dark
/// instrument-panel design language — plain-English labels, live Mac health
/// gauges, and explicit confirmations for any action that interrupts the Mac.
struct MacSettingsView: View {
    @Bindable var store: AppStore
    @Bindable var supervisor: BackendSupervisor
    @Bindable var ui: MacUIState

    @State private var pendingAction: ConfirmAction?
    @State private var pendingModelSwitch: ModelEntry?
    @State private var pendingDeleteAuto: AutomationEntry?
    @State private var instructionsDraft = ""
    @State private var safeCommandsDraft = ""
    @State private var justSavedPrompt = false
    @State private var justSavedSafeCmds = false
    @State private var copiedPairURL = false
    @State private var logKind = "app"
    @State private var logText = ""
    @State private var showNewAutomation = false

    enum ConfirmAction: String, Identifiable {
        case startModel, restartModel, restartApp, reset
        var id: String { rawValue }
    }

    var body: some View {
        HStack(spacing: 0) {
            settingsSidebar
            Rectangle().fill(HarnessTheme.line).frame(width: 1)
            settingsDetail
        }
        .background(HarnessTheme.ink)
        .onAppear {
            syncDrafts()
            Task { await store.refreshAll() }
        }
        .onChange(of: store.config) { _, _ in syncDrafts() }
        .alert(
            pendingAction.map(confirmTitle) ?? "",
            isPresented: Binding(get: { pendingAction != nil }, set: { if !$0 { pendingAction = nil } }),
            presenting: pendingAction
        ) { action in
            Button(confirmButton(action), role: action == .startModel ? nil : .destructive) {
                perform(action)
            }
            Button("Cancel", role: .cancel) {}
        } message: { action in
            Text(confirmMessage(action))
        }
        .alert(
            pendingModelSwitch.map { "Switch to \($0.name)?" } ?? "",
            isPresented: Binding(get: { pendingModelSwitch != nil }, set: { if !$0 { pendingModelSwitch = nil } }),
            presenting: pendingModelSwitch
        ) { model in
            Button("Switch Model", role: model.fit == "over" ? .destructive : nil) {
                Task { await store.selectModel(path: model.path) }
            }
            Button("Cancel", role: .cancel) {}
        } message: { model in
            Text(switchMessage(model))
        }
        .alert(
            pendingDeleteAuto.map { "Delete \u{201C}\($0.name)\u{201D}?" } ?? "",
            isPresented: Binding(get: { pendingDeleteAuto != nil }, set: { if !$0 { pendingDeleteAuto = nil } }),
            presenting: pendingDeleteAuto
        ) { auto in
            Button("Delete Automation", role: .destructive) {
                Task { await store.deleteAutomation(id: auto.id) }
            }
            Button("Cancel", role: .cancel) {}
        } message: { _ in
            Text("It stops running on schedule. Chats it already created stay in History.")
        }
        .sheet(isPresented: $showNewAutomation) {
            MacNewAutomationSheet(store: store)
        }
    }

    private func syncDrafts() {
        instructionsDraft = store.config?.systemPrompt ?? ""
        safeCommandsDraft = store.config?.extraSafeCommands.joined(separator: ", ") ?? ""
    }

    // MARK: - Left Settings Sidebar

    private var settingsSidebar: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 10) {
                HarnessBrandMark(size: 26)
                VStack(alignment: .leading, spacing: 1) {
                    Text("SETTINGS")
                        .font(.system(size: 11, weight: .semibold, design: .monospaced))
                        .tracking(1.6)
                        .foregroundStyle(HarnessTheme.text)
                    Text("v\(MacFormatting.appVersion)")
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(HarnessTheme.faint)
                }
            }
            .padding(.horizontal, 16)
            .padding(.top, 18)
            .padding(.bottom, 14)

            ScrollView {
                VStack(spacing: 3) {
                    ForEach(MacSettingsPane.allCases) { pane in
                        sidebarItem(pane)
                    }
                }
                .padding(.horizontal, 10)
            }

            Spacer()
            Rectangle().fill(HarnessTheme.line).frame(height: 1)

            Button {
                pendingAction = .reset
            } label: {
                HStack(spacing: 8) {
                    Image(systemName: "arrow.counterclockwise")
                        .font(.caption)
                    Text("Reset All Settings…")
                        .font(.caption.weight(.medium))
                    Spacer()
                }
                .foregroundStyle(HarnessTheme.rust)
                .padding(.horizontal, 12)
                .padding(.vertical, 9)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .padding(6)
        }
        .frame(width: 215)
        .background(HarnessTheme.panel)
    }

    private func sidebarItem(_ pane: MacSettingsPane) -> some View {
        let selected = ui.selectedSettingsPane == pane
        return Button {
            ui.selectedSettingsPane = pane
        } label: {
            HStack(spacing: 10) {
                MacIconTile(systemName: pane.icon, tint: selected ? pane.tint : HarnessTheme.dim, size: 24)
                Text(pane.rawValue)
                    .font(.system(size: 13, weight: selected ? .semibold : .regular))
                    .foregroundStyle(selected ? HarnessTheme.text : HarnessTheme.dim)
                Spacer()
                if let badge = sidebarBadge(for: pane) {
                    Text(badge)
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(HarnessTheme.faint)
                }
            }
            .padding(.horizontal, 8)
            .padding(.vertical, 6)
            .contentShape(Rectangle())
            .macHoverRow(cornerRadius: 7, active: selected)
        }
        .buttonStyle(.plain)
    }

    private func sidebarBadge(for pane: MacSettingsPane) -> String? {
        switch pane {
        case .model:
            if let count = store.modelsResponse?.models.count, count > 0 { return "\(count)" }
            return nil
        case .automations:
            let active = store.automations.filter(\.enabled).count
            return active > 0 ? "\(active) on" : nil
        case .voice:
            return store.capabilities?.voice == true ? "ON" : nil
        default:
            return nil
        }
    }

    // MARK: - Right Detail Container

    private var settingsDetail: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                paneHeader
                switch ui.selectedSettingsPane {
                case .overview: overviewPane
                case .model: modelPane
                case .behaviour: behaviourPane
                case .permissions: permissionsPane
                case .automations: automationsPane
                case .voice: voicePane
                case .remote: remotePane
                case .logs: logsPane
                }
            }
            .frame(maxWidth: 640, alignment: .leading)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 28)
            .padding(.vertical, 24)
        }
        .background(HarnessTheme.ink)
    }

    private var paneHeader: some View {
        HStack(spacing: 12) {
            MacIconTile(systemName: ui.selectedSettingsPane.icon, tint: ui.selectedSettingsPane.tint, size: 32)
            VStack(alignment: .leading, spacing: 2) {
                Text(ui.selectedSettingsPane.rawValue)
                    .font(.title3.weight(.semibold))
                    .foregroundStyle(HarnessTheme.text)
                Text(paneSubtitle(ui.selectedSettingsPane))
                    .font(.caption)
                    .foregroundStyle(HarnessTheme.dim)
            }
            Spacer()
            if ui.selectedSettingsPane == .overview || ui.selectedSettingsPane == .model {
                Button {
                    Task { await store.refreshAll() }
                } label: {
                    Label("Refresh", systemImage: "arrow.clockwise")
                        .font(.caption.weight(.medium))
                        .foregroundStyle(HarnessTheme.dim)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 5)
                        .background(HarnessTheme.panel2)
                        .overlay(RoundedRectangle(cornerRadius: 5).stroke(HarnessTheme.line, lineWidth: 1))
                }
                .buttonStyle(.plain)
            }
        }
    }

    private func paneSubtitle(_ pane: MacSettingsPane) -> String {
        switch pane {
        case .overview: return "Live Mac health, startup behaviour, and default project folder."
        case .model: return "Local GGUF models found in ~/llm/models on this Mac."
        case .behaviour: return "Presets, default thinking mode, instructions, and sampling controls."
        case .permissions: return "What Harness is allowed to do and when it asks first."
        case .automations: return "Scheduled prompts that run automatically while Harness is open."
        case .voice: return "Local voice cloning sidecar for reading replies aloud."
        case .remote: return "Pair your iPhone over Tailscale or local Wi-Fi."
        case .logs: return "Live diagnostic output from Harness and llama-server."
        }
    }

    // MARK: - 1. Mac & Status Pane

    @ViewBuilder
    private var overviewPane: some View {
        let activeModel = store.modelsResponse?.models.first(where: \.active)
        let running = store.systemState?.model.running ?? store.isModelLoaded
        let ledColor: Color = store.isSwitchingModel || store.isStreaming
            ? HarnessTheme.amber
            : (running ? HarnessTheme.sage : HarnessTheme.rust)

        // Hero Mac Health Card
        MacPanelCard(border: running ? HarnessTheme.lineHot : HarnessTheme.rust.opacity(0.45)) {
            HStack(spacing: 12) {
                Circle()
                    .fill(ledColor)
                    .frame(width: 10, height: 10)
                    .shadow(color: ledColor.opacity(0.6), radius: 4)
                VStack(alignment: .leading, spacing: 3) {
                    Text(store.connectionState == .connected ? store.activeModelName : "Backend offline")
                        .font(.system(size: 14, weight: .semibold, design: .monospaced))
                        .foregroundStyle(HarnessTheme.text)
                        .lineLimit(1)
                        .truncationMode(.middle)
                    if let m = activeModel, m.estGb > 0, m.budgetGb > 0 {
                        Text(String(format: "Running · ~%.1f of %.1f GB GPU budget · %dK context",
                                    m.estGb, m.budgetGb, m.ctx / 1024))
                            .font(HarnessTheme.monoSmall)
                            .foregroundStyle(HarnessTheme.dim)
                    } else {
                        Text(running ? "Running locally on Apple Silicon" : "Model server is stopped")
                            .font(HarnessTheme.monoSmall)
                            .foregroundStyle(HarnessTheme.dim)
                    }
                }
                Spacer()
                if store.isVisionAvailable {
                    MacBadge(text: "VISION", color: HarnessTheme.amber)
                }
            }

            if let sys = store.systemState {
                Rectangle().fill(HarnessTheme.line).frame(height: 1)
                HStack(spacing: 16) {
                    healthMetric("MEMORY FREE", sys.memoryFree, tint: MacFormatting.memoryTint(sys.memoryFree))
                    healthMetric("DISK FREE", MacFormatting.readableSize(sys.diskFree), tint: HarnessTheme.text)
                    healthMetric("MAC UPTIME", sys.uptime, tint: HarnessTheme.text)
                    healthMetric("ACCESS", sys.mode.uppercased(), tint: sys.tls == true ? HarnessTheme.sage : HarnessTheme.dim)
                }
            }

            Rectangle().fill(HarnessTheme.line).frame(height: 1)

            HStack(spacing: 10) {
                if running {
                    actionButton("Restart Model…", icon: "arrow.clockwise", tint: HarnessTheme.amber) {
                        pendingAction = .restartModel
                    }
                } else {
                    actionButton("Start Model…", icon: "play.fill", tint: HarnessTheme.sage) {
                        pendingAction = .startModel
                    }
                }
                actionButton("Restart Harness…", icon: "power", tint: HarnessTheme.dim) {
                    pendingAction = .restartApp
                }
                Spacer()
                Button("Switch Model →") {
                    ui.selectedSettingsPane = .model
                }
                .buttonStyle(.plain)
                .font(.caption.weight(.medium))
                .foregroundStyle(HarnessTheme.amber)
            }
        }

        // Workspace & Startup Card
        MacSectionTitle(title: "Workspace & Startup")
        MacPanelCard {
            settingsRow(title: "Project Folder",
                        subtitle: store.projectDir.isEmpty ? "No default folder — tools use full paths." : store.projectDir) {
                HStack(spacing: 8) {
                    if !store.projectDir.isEmpty {
                        Button("Clear") { Task { await store.setProjectDir("") } }
                            .buttonStyle(.plain)
                            .font(.caption)
                            .foregroundStyle(HarnessTheme.dim)
                    }
                    actionButton("Choose…", icon: "folder", tint: HarnessTheme.amber) {
                        let panel = NSOpenPanel()
                        panel.canChooseDirectories = true
                        panel.canChooseFiles = false
                        if !store.projectDir.isEmpty {
                            panel.directoryURL = URL(fileURLWithPath: store.projectDir)
                        }
                        if panel.runModal() == .OK, let url = panel.url {
                            Task { await store.setProjectDir(url.path) }
                        }
                    }
                }
            }

            Rectangle().fill(HarnessTheme.line).frame(height: 1)

            settingsRow(title: "Launch Harness App at Login",
                        subtitle: "Opens Harness automatically in your menu bar when you sign in to macOS.") {
                Toggle("", isOn: Binding(
                    get: { supervisor.launchAtLogin },
                    set: { supervisor.setLaunchAtLogin($0) }
                ))
                .labelsHidden()
                .toggleStyle(.switch)
            }

            if let sys = store.systemState {
                Rectangle().fill(HarnessTheme.line).frame(height: 1)
                settingsRow(title: "Keep Backend Running at Login",
                            subtitle: "Runs the background agent & Tailscale server even if this window is closed.") {
                    Toggle("", isOn: Binding(
                        get: { sys.autostart },
                        set: { on in Task { await store.runSystemAction(on ? "enable-autostart" : "disable-autostart") } }
                    ))
                    .labelsHidden()
                    .toggleStyle(.switch)
                }
            }

            Rectangle().fill(HarnessTheme.line).frame(height: 1)

            settingsRow(title: "Backend Runtime",
                        subtitle: supervisor.source.isBundled
                            ? "Self-contained runtime bundled inside Harness.app."
                            : "Using local Harness checkout on this Mac.") {
                Text(supervisor.source.label)
                    .font(HarnessTheme.monoSmall)
                    .foregroundStyle(HarnessTheme.dim)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .frame(maxWidth: 240, alignment: .trailing)
            }
        }
    }

    private func healthMetric(_ label: String, _ value: String, tint: Color) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(label)
                .font(.system(size: 9.5, weight: .semibold))
                .tracking(0.8)
                .foregroundStyle(HarnessTheme.faint)
            Text(value.isEmpty ? "—" : value)
                .font(.system(size: 14, weight: .medium, design: .monospaced))
                .foregroundStyle(tint)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    // MARK: - 2. Model Pane

    @ViewBuilder
    private var modelPane: some View {
        let models = store.modelsResponse?.models ?? []

        HStack {
            MacSectionTitle(title: "Installed Models",
                            subtitle: "Switching reloads weights (~30s). Nothing can run while a switch is in progress.")
            Spacer()
            actionButton("Open ~/llm/models", icon: "folder", tint: HarnessTheme.dim) {
                supervisor.openModelsFolderInFinder()
            }
        }

        if models.isEmpty {
            MacPanelCard(border: HarnessTheme.amberDim) {
                Text("No .gguf models found in ~/llm/models")
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(HarnessTheme.text)
                Text("Drop any quantised `.gguf` file into `~/llm/models` (along with its matching `mmproj*.gguf` file if it supports vision), then click Refresh.")
                    .font(.caption)
                    .foregroundStyle(HarnessTheme.dim)
            }
        } else {
            VStack(spacing: 8) {
                ForEach(models) { model in
                    modelCard(model)
                }
            }
        }
    }

    private func modelCard(_ model: ModelEntry) -> some View {
        let fitColor: Color = model.fit == "ok" ? HarnessTheme.sage : (model.fit == "tight" ? HarnessTheme.amber : HarnessTheme.rust)
        let fitLabel = model.fit == "ok" ? "FITS" : (model.fit == "tight" ? "TIGHT" : "TOO BIG")

        return MacPanelCard(
            border: model.active ? HarnessTheme.sage.opacity(0.6) : HarnessTheme.line,
            background: model.active ? HarnessTheme.panel2 : HarnessTheme.panel,
            padding: 14
        ) {
            HStack(alignment: .center, spacing: 12) {
                VStack(alignment: .leading, spacing: 5) {
                    HStack(spacing: 8) {
                        Text(model.name)
                            .font(.system(size: 13.5, weight: .semibold))
                            .foregroundStyle(HarnessTheme.text)
                        MacBadge(text: fitLabel, color: fitColor)
                        if model.vision {
                            MacBadge(text: "VISION", color: HarnessTheme.amber)
                        }
                    }
                    Text(String(format: "%.1f GB on disk  ·  ~%.1f GB GPU RAM  ·  %dK context",
                                model.gb, model.estGb, model.ctx / 1024))
                        .font(HarnessTheme.monoSmall)
                        .foregroundStyle(HarnessTheme.dim)
                }
                Spacer()
                if model.active {
                    Label("Active", systemImage: "checkmark.circle.fill")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(HarnessTheme.sage)
                } else if store.isSwitchingModel && model.selected {
                    HStack(spacing: 6) {
                        ProgressView().controlSize(.small)
                        Text("Loading…")
                            .font(HarnessTheme.monoSmall)
                            .foregroundStyle(HarnessTheme.amber)
                    }
                } else {
                    actionButton("Load Model", icon: "arrow.triangle.2.circlepath", tint: HarnessTheme.amber) {
                        pendingModelSwitch = model
                    }
                    .disabled(store.isSwitchingModel)
                }
            }
        }
    }

    // MARK: - 3. Behaviour Pane

    @ViewBuilder
    private var behaviourPane: some View {
        if let presets = store.presetsResponse?.presets, !presets.isEmpty {
            MacSectionTitle(title: "Presets", subtitle: "One-click profiles that balance speed, depth, and tool access.")
            LazyVGrid(columns: [GridItem(.flexible(), spacing: 10), GridItem(.flexible(), spacing: 10)], spacing: 10) {
                ForEach(presets) { preset in
                    let active = store.config?.preset == preset.id
                    Button {
                        Task { await store.applyPreset(id: preset.id) }
                    } label: {
                        VStack(alignment: .leading, spacing: 6) {
                            HStack {
                                Image(systemName: active ? "largecircle.fill.circle" : "circle")
                                    .foregroundStyle(active ? HarnessTheme.amber : HarnessTheme.faint)
                                Text(preset.label)
                                    .font(.subheadline.weight(.semibold))
                                    .foregroundStyle(HarnessTheme.text)
                                Spacer()
                            }
                            Text(preset.blurb)
                                .font(.caption)
                                .foregroundStyle(HarnessTheme.dim)
                                .multilineTextAlignment(.leading)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        .padding(12)
                        .frame(maxWidth: .infinity, minHeight: 84, alignment: .topLeading)
                        .background(active ? HarnessTheme.panel2 : HarnessTheme.panel)
                        .overlay(RoundedRectangle(cornerRadius: 8).stroke(
                            active ? HarnessTheme.amberDim : HarnessTheme.line, lineWidth: 1
                        ))
                        .clipShape(RoundedRectangle(cornerRadius: 8))
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                }
            }
        }

        if let cfg = store.config {
            MacSectionTitle(title: "Reasoning & Sampling")
            MacPanelCard {
                settingsRow(title: "Think First by Default",
                            subtitle: "Enables step-by-step reasoning for new chats. You can still toggle it per turn above the composer.") {
                    Toggle("", isOn: Binding(
                        get: { cfg.thinking },
                        set: { val in Task { await store.updateConfig(["thinking": val]) } }
                    ))
                    .labelsHidden()
                    .toggleStyle(.switch)
                }

                Rectangle().fill(HarnessTheme.line).frame(height: 1)

                VStack(alignment: .leading, spacing: 6) {
                    HStack {
                        Text("Temperature")
                            .font(.subheadline.weight(.medium))
                            .foregroundStyle(HarnessTheme.text)
                        Spacer()
                        Text(String(format: "%.2f", cfg.temperature))
                            .font(HarnessTheme.monoSmall)
                            .foregroundStyle(HarnessTheme.amber)
                    }
                    Slider(value: Binding(
                        get: { cfg.temperature },
                        set: { val in
                            store.config?.temperature = val
                            Task { await store.updateConfig(["temperature": (val * 100).rounded() / 100]) }
                        }
                    ), in: 0...1.5, step: 0.05)
                    Text("Lower is more deterministic and factual; higher is more varied.")
                        .font(.caption)
                        .foregroundStyle(HarnessTheme.faint)
                }

                Rectangle().fill(HarnessTheme.line).frame(height: 1)

                VStack(alignment: .leading, spacing: 6) {
                    HStack {
                        Text("Top-p")
                            .font(.subheadline.weight(.medium))
                            .foregroundStyle(HarnessTheme.text)
                        Spacer()
                        Text(String(format: "%.2f", cfg.topP))
                            .font(HarnessTheme.monoSmall)
                            .foregroundStyle(HarnessTheme.amber)
                    }
                    Slider(value: Binding(
                        get: { cfg.topP },
                        set: { val in
                            store.config?.topP = val
                            Task { await store.updateConfig(["top_p": (val * 100).rounded() / 100]) }
                        }
                    ), in: 0.05...1.0, step: 0.05)
                }

                Rectangle().fill(HarnessTheme.line).frame(height: 1)

                HStack(spacing: 16) {
                    compactStepper("Top-k", value: cfg.topK, range: 0...200, step: 5) { val in
                        Task { await store.updateConfig(["top_k": val]) }
                    }
                    compactStepper("Longest reply", value: cfg.maxTokens, range: 500...32000, step: 500, suffix: " tok") { val in
                        Task { await store.updateConfig(["max_tokens": val]) }
                    }
                    compactStepper("Max steps", value: cfg.maxSteps, range: 1...60, step: 1) { val in
                        Task { await store.updateConfig(["max_steps": val]) }
                    }
                }
            }

            MacSectionTitle(title: "System Instructions",
                            subtitle: "Sets Harness's role and tone. Shorter prompts leave more room for files and tools.")
            MacPanelCard {
                TextEditor(text: $instructionsDraft)
                    .font(HarnessTheme.monoSmall)
                    .foregroundStyle(HarnessTheme.text)
                    .scrollContentBackground(.hidden)
                    .padding(10)
                    .frame(minHeight: 140)
                    .background(HarnessTheme.ink)
                    .overlay(RoundedRectangle(cornerRadius: 6).stroke(HarnessTheme.line, lineWidth: 1))

                HStack {
                    Text("\(instructionsDraft.count) characters")
                        .font(HarnessTheme.monoSmall)
                        .foregroundStyle(HarnessTheme.faint)
                    Spacer()
                    if instructionsDraft != cfg.systemPrompt {
                        Button("Discard") { instructionsDraft = cfg.systemPrompt }
                            .buttonStyle(.plain)
                            .font(.caption)
                            .foregroundStyle(HarnessTheme.dim)
                    }
                    Button {
                        Task {
                            await store.updateConfig(["system_prompt": instructionsDraft])
                            justSavedPrompt = true
                            try? await Task.sleep(nanoseconds: 1_400_000_000)
                            justSavedPrompt = false
                        }
                    } label: {
                        Text(justSavedPrompt ? "Saved" : "Save Instructions")
                            .font(.caption.weight(.semibold))
                            .padding(.horizontal, 12)
                            .padding(.vertical, 6)
                            .background(instructionsDraft == cfg.systemPrompt ? HarnessTheme.panel2 : HarnessTheme.amber)
                            .foregroundStyle(instructionsDraft == cfg.systemPrompt ? HarnessTheme.faint : HarnessTheme.ink)
                            .clipShape(RoundedRectangle(cornerRadius: 5))
                    }
                    .buttonStyle(.plain)
                    .disabled(instructionsDraft == cfg.systemPrompt)
                }
            }
        }
    }

    private func compactStepper(_ title: String, value: Int, range: ClosedRange<Int>, step: Int,
                                suffix: String = "", onChange: @escaping (Int) -> Void) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title)
                .font(.caption)
                .foregroundStyle(HarnessTheme.dim)
            HStack(spacing: 6) {
                Text("\(value)\(suffix)")
                    .font(HarnessTheme.monoSmall)
                    .foregroundStyle(HarnessTheme.text)
                Spacer()
                Stepper("", value: Binding(get: { value }, set: onChange), in: range, step: step)
                    .labelsHidden()
            }
            .padding(.horizontal, 8)
            .padding(.vertical, 4)
            .background(HarnessTheme.ink)
            .overlay(RoundedRectangle(cornerRadius: 5).stroke(HarnessTheme.line, lineWidth: 1))
        }
        .frame(maxWidth: .infinity)
    }

    // MARK: - 4. Permissions Pane (Plain-English, Matching iOS Rework)

    private static let capabilities: [(key: String, name: String, detail: String, icon: String)] = [
        ("run_command", "Run commands", "Check disk space, list processes, run tests. Risky commands still ask first.", "terminal"),
        ("write_file", "Create files", "Write new files. Shows the contents and asks before saving.", "doc.badge.plus"),
        ("edit_file", "Edit files", "Modify existing files. Shows the exact diff and asks first.", "pencil.line"),
        ("web_search", "Search the web", "Look up current information online.", "magnifyingglass"),
        ("fetch_url", "Open web pages", "Read a page it found or you linked.", "globe"),
        ("send_email", "Send email", "Send mail from Apple Mail. Always previews recipients and attachments.", "envelope"),
    ]

    private static let readers: [(key: String, name: String)] = [
        ("list_files", "List folders"),
        ("find_files", "Find files by name"),
        ("search_in_files", "Search inside files"),
        ("read_file", "Read files"),
        ("system_status", "Check Mac health"),
        ("view_image", "Look at pictures"),
    ]

    @ViewBuilder
    private var permissionsPane: some View {
        if let cfg = store.config {
            MacSectionTitle(title: "What Harness Can Do")
            MacPanelCard(padding: 12) {
                ForEach(Array(Self.capabilities.enumerated()), id: \.element.key) { idx, cap in
                    if cfg.tools[cap.key] != nil {
                        if idx > 0 { Rectangle().fill(HarnessTheme.line).frame(height: 1) }
                        HStack(spacing: 12) {
                            MacIconTile(systemName: cap.icon, tint: HarnessTheme.amber, size: 28)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(cap.name)
                                    .font(.subheadline.weight(.medium))
                                    .foregroundStyle(HarnessTheme.text)
                                Text(cap.detail)
                                    .font(.caption)
                                    .foregroundStyle(HarnessTheme.dim)
                            }
                            Spacer()
                            Toggle("", isOn: toolBinding(cap.key, cfg))
                                .labelsHidden()
                                .toggleStyle(.switch)
                        }
                        .padding(.vertical, 2)
                    }
                }
            }

            MacSectionTitle(title: "When It Changes a File")
            approvalModeCard(
                key: "approve_files",
                current: cfg.approveFiles,
                options: [
                    ("always", "Always ask", "Every write and edit is previewed before touching disk.", false),
                    ("outside", "Only outside the project", "Trust changes inside your project folder; ask about everything else.", false),
                    ("never", "Never ask", "Write and edit freely. Protected system paths are still refused.", true),
                ]
            )

            MacSectionTitle(title: "When It Runs a Command",
                            subtitle: "Whatever you choose, /System, /usr, /etc, and ~/.ssh are always refused outright.")
            approvalModeCard(
                key: "approve_commands",
                current: cfg.approveCommands,
                options: [
                    ("always", "Always ask", "Anything that isn't plainly read-only is previewed first.", false),
                    ("destructive", "Only destructive ones", "Run everyday commands; ask for rm, sudo, installs, network fetches, and pipes.", false),
                    ("never", "Never ask", "Run whatever it likes. Only use this on a Mac you can rebuild.", true),
                ]
            )

            MacSectionTitle(title: "Commands That Never Ask",
                            subtitle: "Comma-separated read-only commands (e.g. git status, brew list).")
            MacPanelCard {
                HStack(spacing: 10) {
                    TextField("e.g. git status, brew list", text: $safeCommandsDraft)
                        .textFieldStyle(.plain)
                        .font(HarnessTheme.mono)
                        .foregroundStyle(HarnessTheme.text)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 7)
                        .background(HarnessTheme.ink)
                        .overlay(RoundedRectangle(cornerRadius: 6).stroke(HarnessTheme.line, lineWidth: 1))
                        .onSubmit { saveSafeCommands() }
                    actionButton(justSavedSafeCmds ? "Saved" : "Save", icon: justSavedSafeCmds ? "checkmark" : "square.and.arrow.down",
                                 tint: justSavedSafeCmds ? HarnessTheme.sage : HarnessTheme.amber) {
                        saveSafeCommands()
                    }
                }
            }

            MacSectionTitle(title: "Looking Around (Read-Only Tools)")
            MacPanelCard(padding: 12) {
                LazyVGrid(columns: [GridItem(.flexible(), spacing: 12), GridItem(.flexible(), spacing: 12)], spacing: 10) {
                    ForEach(Self.readers, id: \.key) { reader in
                        if cfg.tools[reader.key] != nil {
                            HStack {
                                VStack(alignment: .leading, spacing: 1) {
                                    Text(reader.name)
                                        .font(.caption.weight(.medium))
                                        .foregroundStyle(HarnessTheme.text)
                                    Text(reader.key)
                                        .font(.system(size: 10, design: .monospaced))
                                        .foregroundStyle(HarnessTheme.faint)
                                }
                                Spacer()
                                Toggle("", isOn: toolBinding(reader.key, cfg))
                                    .labelsHidden()
                                    .toggleStyle(.switch)
                                    .controlSize(.small)
                            }
                            .padding(8)
                            .background(HarnessTheme.ink)
                            .overlay(RoundedRectangle(cornerRadius: 6).stroke(HarnessTheme.line, lineWidth: 1))
                        }
                    }
                }
            }
        }
    }

    private func toolBinding(_ key: String, _ cfg: HarnessConfig) -> Binding<Bool> {
        Binding(
            get: { store.config?.tools[key] ?? false },
            set: { on in
                var tools = store.config?.tools ?? cfg.tools
                tools[key] = on
                store.config?.tools = tools
                Task { await store.updateConfig(["tools": tools]) }
            }
        )
    }

    private func approvalModeCard(key: String, current: String,
                                  options: [(id: String, name: String, detail: String, warn: Bool)]) -> some View {
        MacPanelCard(padding: 10) {
            ForEach(Array(options.enumerated()), id: \.element.id) { idx, opt in
                if idx > 0 { Rectangle().fill(HarnessTheme.line).frame(height: 1) }
                let selected = current == opt.id
                Button {
                    Task { await store.updateConfig([key: opt.id]) }
                } label: {
                    HStack(alignment: .top, spacing: 10) {
                        Image(systemName: selected ? "largecircle.fill.circle" : "circle")
                            .foregroundStyle(selected ? HarnessTheme.amber : HarnessTheme.faint)
                            .padding(.top, 2)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(opt.name)
                                .font(.subheadline.weight(.medium))
                                .foregroundStyle(HarnessTheme.text)
                            Text(opt.detail)
                                .font(.caption)
                                .foregroundStyle(opt.warn ? HarnessTheme.rust.opacity(0.9) : HarnessTheme.dim)
                        }
                        Spacer()
                    }
                    .padding(.vertical, 4)
                    .padding(.horizontal, 6)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
        }
    }

    private func saveSafeCommands() {
        let list = safeCommandsDraft.split(separator: ",")
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
        Task {
            await store.updateConfig(["extra_safe_commands": list])
            justSavedSafeCmds = true
            try? await Task.sleep(nanoseconds: 1_400_000_000)
            justSavedSafeCmds = false
        }
    }

    // MARK: - 5. Automations Pane

    @ViewBuilder
    private var automationsPane: some View {
        HStack {
            MacSectionTitle(title: "Your Automations",
                            subtitle: "Scheduled runs appear in History automatically. Write tools stay off when unattended.")
            Spacer()
            actionButton("New Automation", icon: "plus", tint: HarnessTheme.amber) {
                showNewAutomation = true
            }
        }

        if store.automations.isEmpty {
            MacPanelCard {
                Text("Nothing scheduled yet. Pick a Quick Start below or create your own.")
                    .font(.subheadline)
                    .foregroundStyle(HarnessTheme.dim)
            }
        } else {
            VStack(spacing: 10) {
                ForEach(store.automations) { auto in
                    MacPanelCard(padding: 14) {
                        HStack(alignment: .top) {
                            VStack(alignment: .leading, spacing: 3) {
                                HStack(spacing: 8) {
                                    Text(auto.name)
                                        .font(.subheadline.weight(.semibold))
                                        .foregroundStyle(auto.enabled ? HarnessTheme.text : HarnessTheme.dim)
                                    MacBadge(text: auto.schedule.summary.uppercased(),
                                             color: auto.enabled ? HarnessTheme.amber : HarnessTheme.faint)
                                }
                                Text(auto.prompt)
                                    .font(.caption)
                                    .foregroundStyle(HarnessTheme.dim)
                            }
                            Spacer()
                            Toggle("", isOn: Binding(
                                get: { auto.enabled },
                                set: { enabled in
                                    Task {
                                        await store.saveAutomation([
                                            "id": auto.id, "name": auto.name, "prompt": auto.prompt,
                                            "schedule": [
                                                "kind": auto.schedule.kind,
                                                "minutes": auto.schedule.minutes ?? 60,
                                                "time": auto.schedule.time ?? "09:00",
                                            ],
                                            "enabled": enabled,
                                        ])
                                    }
                                }
                            ))
                            .labelsHidden()
                            .toggleStyle(.switch)
                        }
                        if let res = auto.lastResult, !res.isEmpty {
                            Text("Last: \(res)")
                                .font(HarnessTheme.monoSmall)
                                .foregroundStyle(HarnessTheme.sage)
                                .lineLimit(2)
                        }
                        HStack(spacing: 12) {
                            actionButton("Run Now", icon: "play.fill", tint: HarnessTheme.amber) {
                                Task { await store.runAutomation(id: auto.id) }
                            }
                            Spacer()
                            Button {
                                pendingDeleteAuto = auto
                            } label: {
                                Label("Delete", systemImage: "trash")
                                    .font(.caption)
                                    .foregroundStyle(HarnessTheme.rust)
                            }
                            .buttonStyle(.plain)
                        }
                    }
                }
            }
        }

        let templates = (store.presetsResponse?.templates ?? []).filter { tpl in
            !store.automations.contains(where: { $0.name == tpl.name })
        }
        if !templates.isEmpty {
            MacSectionTitle(title: "Quick Start Templates")
            VStack(spacing: 8) {
                ForEach(templates) { tpl in
                    Button {
                        var sched: [String: Any] = ["kind": tpl.schedule.kind]
                        if let m = tpl.schedule.minutes { sched["minutes"] = m }
                        if let t = tpl.schedule.time { sched["time"] = t }
                        Task {
                            await store.saveAutomation([
                                "name": tpl.name, "prompt": tpl.prompt, "schedule": sched, "enabled": true,
                            ])
                        }
                    } label: {
                        MacPanelCard(padding: 12) {
                            HStack(spacing: 10) {
                                Image(systemName: "plus.circle.fill")
                                    .foregroundStyle(HarnessTheme.sage)
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(tpl.name)
                                        .font(.subheadline.weight(.medium))
                                        .foregroundStyle(HarnessTheme.text)
                                    Text(tpl.prompt)
                                        .font(.caption)
                                        .foregroundStyle(HarnessTheme.dim)
                                        .lineLimit(1)
                                }
                                Spacer()
                                MacBadge(text: tpl.schedule.summary.uppercased(), color: HarnessTheme.dim)
                            }
                        }
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }

    // MARK: - 6. Voice Pane

    @ViewBuilder
    private var voicePane: some View {
        MacPanelCard {
            if store.capabilities?.voice != true {
                HStack(spacing: 12) {
                    MacIconTile(systemName: "waveform", tint: HarnessTheme.dim, size: 34)
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Voice sidecar is not running")
                            .font(.subheadline.weight(.semibold))
                            .foregroundStyle(HarnessTheme.text)
                        Text("Start `python3 voice/server.py` on this Mac to clone a voice and enable Speak buttons under replies.")
                            .font(.caption)
                            .foregroundStyle(HarnessTheme.dim)
                    }
                }
            } else if store.voices.isEmpty {
                Text("Voice sidecar is running, but no voices are enrolled yet. Record one from the web UI to select it here.")
                    .font(.subheadline)
                    .foregroundStyle(HarnessTheme.dim)
            } else {
                ForEach(Array(store.voices.enumerated()), id: \.element.id) { idx, voice in
                    if idx > 0 { Rectangle().fill(HarnessTheme.line).frame(height: 1) }
                    Button {
                        Task { await store.selectVoice(id: voice.id) }
                    } label: {
                        HStack(spacing: 10) {
                            Image(systemName: voice.selected ? "largecircle.fill.circle" : "circle")
                                .foregroundStyle(voice.selected ? HarnessTheme.amber : HarnessTheme.faint)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(voice.name)
                                    .font(.subheadline.weight(.medium))
                                    .foregroundStyle(HarnessTheme.text)
                                Text(String(format: "%.1f s sample", voice.seconds))
                                    .font(HarnessTheme.monoSmall)
                                    .foregroundStyle(HarnessTheme.dim)
                            }
                            Spacer()
                        }
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }

    // MARK: - 7. iPhone & Remote Pane

    @ViewBuilder
    private var remotePane: some View {
        let pairURL: String = {
            guard let acc = store.accessInfo else { return "" }
            if !acc.short.isEmpty { return acc.short }
            if !acc.url.isEmpty { return acc.url }
            if !acc.tailnet.isEmpty { return "http://\(acc.tailnet):8420" }
            return ""
        }()

        MacPanelCard {
            HStack(alignment: .top, spacing: 22) {
                if !pairURL.isEmpty {
                    QRCodeCardView(urlString: pairURL, size: 150)
                        .frame(width: 175)
                }
                VStack(alignment: .leading, spacing: 10) {
                    Text(pairURL.isEmpty ? "Remote access is off" : "Scan with Harness for iOS")
                        .font(.headline)
                        .foregroundStyle(HarnessTheme.text)
                    Text(pairURL.isEmpty
                         ? "Start Harness with `--remote` (Tailscale) or `--lan` (Wi-Fi) to connect from your iPhone."
                         : "Open Harness on your iPhone, tap Scan QR, and point at this code. Over Tailscale, your traffic is encrypted end-to-end with no extra token needed.")
                        .font(.caption)
                        .foregroundStyle(HarnessTheme.dim)
                        .fixedSize(horizontal: false, vertical: true)

                    if let acc = store.accessInfo {
                        VStack(alignment: .leading, spacing: 6) {
                            HStack(spacing: 8) {
                                MacBadge(text: acc.mode.uppercased(), color: HarnessTheme.sage)
                                if acc.tls == true {
                                    MacBadge(text: "HTTPS TLS", color: HarnessTheme.sage)
                                } else if !acc.tailnet.isEmpty {
                                    MacBadge(text: "TAILSCALE WIREGUARD", color: HarnessTheme.amber)
                                }
                            }
                            if !acc.tailnet.isEmpty {
                                Text("Tailscale IP: \(acc.tailnet)")
                                    .font(HarnessTheme.monoSmall)
                                    .foregroundStyle(HarnessTheme.dim)
                            }
                        }
                    }

                    if !pairURL.isEmpty {
                        actionButton(copiedPairURL ? "Copied Address" : "Copy Address",
                                     icon: copiedPairURL ? "checkmark" : "doc.on.doc",
                                     tint: copiedPairURL ? HarnessTheme.sage : HarnessTheme.amber) {
                            PlatformClipboard.copy(pairURL)
                            copiedPairURL = true
                            Task {
                                try? await Task.sleep(nanoseconds: 1_400_000_000)
                                copiedPairURL = false
                            }
                        }
                    }
                }
            }
        }
    }

    // MARK: - 8. Logs Pane

    @ViewBuilder
    private var logsPane: some View {
        HStack(spacing: 10) {
            Picker("Log", selection: $logKind) {
                Text("Harness (app.log)").tag("app")
                Text("Model (model.log)").tag("model")
            }
            .pickerStyle(.segmented)
            .frame(maxWidth: 300)

            Spacer()

            actionButton("Copy", icon: "doc.on.doc", tint: HarnessTheme.dim) {
                PlatformClipboard.copy(logText)
            }
            .disabled(logText.isEmpty)

            actionButton("Refresh", icon: "arrow.clockwise", tint: HarnessTheme.amber) {
                Task { logText = await store.fetchLogs(kind: logKind) }
            }
        }

        ScrollView {
            Text(logText.isEmpty ? "Loading…" : logText)
                .font(HarnessTheme.monoSmall)
                .foregroundStyle(HarnessTheme.text)
                .frame(maxWidth: .infinity, alignment: .leading)
                .textSelection(.enabled)
                .padding(14)
        }
        .frame(minHeight: 380)
        .background(HarnessTheme.panel)
        .overlay(RoundedRectangle(cornerRadius: 8).stroke(HarnessTheme.line, lineWidth: 1))
        .clipShape(RoundedRectangle(cornerRadius: 8))
        .task(id: logKind) { logText = await store.fetchLogs(kind: logKind) }
    }

    // MARK: - Helpers & Confirmations

    private func settingsRow<Trailing: View>(title: String, subtitle: String,
                                             @ViewBuilder trailing: () -> Trailing) -> some View {
        HStack(alignment: .center, spacing: 14) {
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(HarnessTheme.text)
                Text(subtitle)
                    .font(.caption)
                    .foregroundStyle(HarnessTheme.dim)
                    .lineLimit(2)
            }
            Spacer(minLength: 10)
            trailing()
        }
    }

    private func actionButton(_ title: String, icon: String, tint: Color, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 6) {
                Image(systemName: icon).font(.caption)
                Text(title).font(.caption.weight(.medium))
            }
            .foregroundStyle(tint)
            .padding(.horizontal, 10)
            .padding(.vertical, 6)
            .background(tint.opacity(0.11))
            .overlay(RoundedRectangle(cornerRadius: 6).stroke(tint.opacity(0.35), lineWidth: 1))
            .clipShape(RoundedRectangle(cornerRadius: 6))
        }
        .buttonStyle(.plain)
    }

    private func confirmTitle(_ action: ConfirmAction) -> String {
        switch action {
        case .startModel: return "Start the model?"
        case .restartModel: return "Restart the model?"
        case .restartApp: return "Restart Harness?"
        case .reset: return "Reset all settings?"
        }
    }

    private func confirmButton(_ action: ConfirmAction) -> String {
        switch action {
        case .startModel: return "Start Model"
        case .restartModel: return "Restart Model"
        case .restartApp: return "Restart Harness"
        case .reset: return "Reset Settings"
        }
    }

    private func confirmMessage(_ action: ConfirmAction) -> String {
        switch action {
        case .startModel: return "Loads the selected GGUF weights into GPU memory (~30 seconds)."
        case .restartModel: return "Reloads the model weights (~30 seconds). Any running generation will stop."
        case .restartApp: return "Restarts the local Harness server for a few seconds; this window reconnects automatically."
        case .reset: return "Instructions, sampling, approvals, tools, and presets return to defaults. Your chats are kept."
        }
    }

    private func switchMessage(_ model: ModelEntry) -> String {
        var msg = "Harness reloads the weights (~30 seconds). Nothing can run until it finishes."
        if model.fit == "over" {
            msg += String(format: " Warning: needs ~%.1f GB against a ~%.1f GB GPU budget.", model.estGb, model.budgetGb)
        } else if model.fit == "tight" {
            msg += " Tight memory fit — close heavy apps if generation slows down."
        }
        return msg
    }

    private func perform(_ action: ConfirmAction) {
        switch action {
        case .startModel: Task { await store.runSystemAction("start-model") }
        case .restartModel: Task { await store.runSystemAction("restart-model") }
        case .restartApp: Task { await store.runSystemAction("restart-app") }
        case .reset:
            Task {
                await store.resetConfig()
                syncDrafts()
            }
        }
    }
}

private struct MacNewAutomationSheet: View {
    @Bindable var store: AppStore
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var prompt = ""
    @State private var kind = "daily"
    @State private var time = "09:00"
    @State private var minutes = 60

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("New Automation")
                .font(.headline)
                .foregroundStyle(HarnessTheme.text)

            TextField("Name (e.g. Morning disk check)", text: $name)
                .textFieldStyle(.plain)
                .padding(8)
                .background(HarnessTheme.ink)
                .overlay(RoundedRectangle(cornerRadius: 6).stroke(HarnessTheme.line, lineWidth: 1))

            Picker("Schedule", selection: $kind) {
                Text("Daily").tag("daily")
                Text("Every N minutes").tag("interval")
                Text("Manual").tag("manual")
            }
            .pickerStyle(.segmented)

            if kind == "daily" {
                TextField("Time (HH:MM)", text: $time)
                    .textFieldStyle(.plain)
                    .font(HarnessTheme.mono)
                    .padding(8)
                    .background(HarnessTheme.ink)
                    .overlay(RoundedRectangle(cornerRadius: 6).stroke(HarnessTheme.line, lineWidth: 1))
            } else if kind == "interval" {
                Stepper("Every \(minutes) min", value: $minutes, in: 15...1440, step: 15)
            }

            Text("PROMPT")
                .font(HarnessTheme.label)
                .tracking(1.0)
                .foregroundStyle(HarnessTheme.dim)

            TextEditor(text: $prompt)
                .font(.body)
                .scrollContentBackground(.hidden)
                .padding(8)
                .frame(minHeight: 100)
                .background(HarnessTheme.ink)
                .overlay(RoundedRectangle(cornerRadius: 6).stroke(HarnessTheme.line, lineWidth: 1))

            HStack {
                Spacer()
                Button("Cancel") { dismiss() }
                Button("Save") {
                    let payload: [String: Any] = [
                        "name": name.isEmpty ? "Untitled" : name,
                        "prompt": prompt,
                        "schedule": ["kind": kind, "time": time, "minutes": minutes],
                        "enabled": true,
                    ]
                    dismiss()
                    Task { await store.saveAutomation(payload) }
                }
                .disabled(prompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                .keyboardShortcut(.defaultAction)
            }
        }
        .padding(20)
        .frame(width: 440)
        .background(HarnessTheme.panel)
        .preferredColorScheme(.dark)
    }
}
