import AppKit
import SwiftUI

struct MacSettingsView: View {
    @Bindable var store: AppStore
    @Bindable var supervisor: BackendSupervisor

    @State private var pendingAction: ConfirmAction?
    @State private var pendingModelSwitch: ModelEntry?
    @State private var instructionsDraft = ""
    @State private var safeCommandsDraft = ""
    @State private var logKind = "app"
    @State private var logText = ""
    @State private var showNewAutomation = false

    enum ConfirmAction: String, Identifiable {
        case startModel, restartModel, restartApp, reset
        var id: String { rawValue }
    }

    var body: some View {
        TabView {
            generalTab
                .tabItem { Label("General", systemImage: "macmini") }
            modelTab
                .tabItem { Label("Model", systemImage: "cpu") }
            permissionsTab
                .tabItem { Label("Permissions", systemImage: "hand.raised") }
            automationsTab
                .tabItem { Label("Automations", systemImage: "clock.badge.checkmark") }
            remoteTab
                .tabItem { Label("iPhone & Remote", systemImage: "iphone") }
            logsTab
                .tabItem { Label("Logs", systemImage: "doc.text.magnifyingglass") }
        }
        .padding(12)
        .background(HarnessTheme.ink)
        .onAppear {
            instructionsDraft = store.config?.systemPrompt ?? ""
            safeCommandsDraft = store.config?.extraSafeCommands.joined(separator: ", ") ?? ""
            Task { await store.refreshAll() }
        }
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
            Text("Harness reloads the weights (~30 seconds). Nothing can run until it finishes.")
        }
        .sheet(isPresented: $showNewAutomation) {
            MacNewAutomationSheet(store: store)
        }
    }

    // MARK: - General Tab

    private var generalTab: some View {
        Form {
            Section("Mac Health") {
                if let sys = store.systemState {
                    LabeledContent("Active Model", value: store.activeModelName)
                    LabeledContent("Memory Free", value: sys.memoryFree)
                    LabeledContent("Disk Free", value: sys.diskFree)
                    LabeledContent("Mac Uptime", value: sys.uptime)
                } else {
                    Text("Not connected to Harness on 127.0.0.1:8420.")
                        .foregroundStyle(HarnessTheme.dim)
                }
                LabeledContent("Backend Engine", value: supervisor.source.label)
            }

            Section("Startup & Controls") {
                Toggle("Launch Harness app at Login", isOn: Binding(
                    get: { supervisor.launchAtLogin },
                    set: { supervisor.setLaunchAtLogin($0) }
                ))
                if let sys = store.systemState {
                    Toggle("Keep Harness backend running at Login", isOn: Binding(
                        get: { sys.autostart },
                        set: { on in Task { await store.runSystemAction(on ? "enable-autostart" : "disable-autostart") } }
                    ))
                }
                HStack(spacing: 10) {
                    if store.systemState?.model.running ?? store.isModelLoaded {
                        Button("Restart Model…") { pendingAction = .restartModel }
                    } else {
                        Button("Start Model…") { pendingAction = .startModel }
                    }
                    Button("Restart Harness…") { pendingAction = .restartApp }
                    Spacer()
                    Button("Reset All Settings…", role: .destructive) { pendingAction = .reset }
                }
            }

            if let cfg = store.config {
                Section("Defaults") {
                    Toggle("Think First by default", isOn: Binding(
                        get: { cfg.thinking },
                        set: { val in Task { await store.updateConfig(["thinking": val]) } }
                    ))
                    HStack {
                        LabeledContent("Project Folder", value: store.projectDir.isEmpty ? "None" : store.projectDir)
                        Button("Choose…") {
                            let panel = NSOpenPanel()
                            panel.canChooseDirectories = true
                            panel.canChooseFiles = false
                            if panel.runModal() == .OK, let url = panel.url {
                                Task { await store.setProjectDir(url.path) }
                            }
                        }
                        if !store.projectDir.isEmpty {
                            Button("Clear") { Task { await store.setProjectDir("") } }
                        }
                    }
                }
            }
        }
        .formStyle(.grouped)
    }

    // MARK: - Model Tab

    private var modelTab: some View {
        Form {
            Section("Installed Models (~/llm/models)") {
                if let models = store.modelsResponse?.models, !models.isEmpty {
                    ForEach(models) { model in
                        HStack {
                            VStack(alignment: .leading, spacing: 3) {
                                HStack(spacing: 6) {
                                    Text(model.name)
                                        .font(.subheadline.weight(.medium))
                                    if model.vision {
                                        Text("VISION")
                                            .font(.system(size: 9, weight: .semibold, design: .monospaced))
                                            .foregroundStyle(HarnessTheme.amber)
                                            .padding(.horizontal, 4)
                                            .padding(.vertical, 1)
                                            .background(HarnessTheme.amber.opacity(0.15))
                                    }
                                    Text(model.fit.uppercased())
                                        .font(.system(size: 9, weight: .semibold, design: .monospaced))
                                        .foregroundStyle(model.fit == "ok" ? HarnessTheme.sage : (model.fit == "tight" ? HarnessTheme.amber : HarnessTheme.rust))
                                }
                                Text(String(format: "%.1f GB disk · ~%.1f GB RAM · %dK ctx", model.gb, model.estGb, model.ctx / 1024))
                                    .font(HarnessTheme.monoSmall)
                                    .foregroundStyle(HarnessTheme.dim)
                            }
                            Spacer()
                            if model.active {
                                Label("Loaded", systemImage: "checkmark.circle.fill")
                                    .foregroundStyle(HarnessTheme.sage)
                                    .font(.caption)
                            } else {
                                Button("Use") { pendingModelSwitch = model }
                                    .disabled(store.isSwitchingModel)
                            }
                        }
                    }
                } else {
                    HStack {
                        Text("No .gguf models found in ~/llm/models.")
                            .foregroundStyle(HarnessTheme.dim)
                        Spacer()
                        Button("Open Folder") { supervisor.openModelsFolderInFinder() }
                    }
                }
            }

            if let presets = store.presetsResponse?.presets, !presets.isEmpty {
                Section("Behaviour Presets") {
                    ForEach(presets) { preset in
                        HStack {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(preset.label).font(.subheadline.weight(.medium))
                                Text(preset.blurb).font(.caption).foregroundStyle(HarnessTheme.dim)
                            }
                            Spacer()
                            if store.config?.preset == preset.id {
                                Image(systemName: "checkmark").foregroundStyle(HarnessTheme.amber)
                            } else {
                                Button("Apply") { Task { await store.applyPreset(id: preset.id) } }
                            }
                        }
                    }
                }
            }

            if let cfg = store.config {
                Section("Sampling & Limits") {
                    HStack {
                        Text("Temperature")
                        Slider(value: Binding(
                            get: { cfg.temperature },
                            set: { val in
                                store.config?.temperature = val
                                Task { await store.updateConfig(["temperature": (val * 100).rounded() / 100]) }
                            }
                        ), in: 0...1.5, step: 0.05)
                        Text(String(format: "%.2f", cfg.temperature))
                            .font(HarnessTheme.monoSmall)
                            .frame(width: 40, alignment: .trailing)
                    }
                    Stepper("Max reply tokens: \(cfg.maxTokens)", value: Binding(
                        get: { cfg.maxTokens },
                        set: { val in Task { await store.updateConfig(["max_tokens": val]) } }
                    ), in: 500...32000, step: 500)
                    Stepper("Max tool steps: \(cfg.maxSteps)", value: Binding(
                        get: { cfg.maxSteps },
                        set: { val in Task { await store.updateConfig(["max_steps": val]) } }
                    ), in: 1...60)
                }

                Section("System Instructions") {
                    TextEditor(text: $instructionsDraft)
                        .font(HarnessTheme.monoSmall)
                        .frame(minHeight: 110)
                    HStack {
                        Text("\(instructionsDraft.count) characters")
                            .font(.caption)
                            .foregroundStyle(HarnessTheme.dim)
                        Spacer()
                        Button("Save Instructions") {
                            Task { await store.updateConfig(["system_prompt": instructionsDraft]) }
                        }
                        .disabled(instructionsDraft == cfg.systemPrompt)
                    }
                }
            }
        }
        .formStyle(.grouped)
    }

    // MARK: - Permissions Tab

    private var permissionsTab: some View {
        Form {
            if let cfg = store.config {
                Section("Approval Policy") {
                    Picker("When changing a file", selection: Binding(
                        get: { cfg.approveFiles },
                        set: { val in Task { await store.updateConfig(["approve_files": val]) } }
                    )) {
                        Text("Always ask").tag("always")
                        Text("Only outside project folder").tag("outside")
                        Text("Never ask").tag("never")
                    }
                    Picker("When running a command", selection: Binding(
                        get: { cfg.approveCommands },
                        set: { val in Task { await store.updateConfig(["approve_commands": val]) } }
                    )) {
                        Text("Always ask").tag("always")
                        Text("Only destructive ones").tag("destructive")
                        Text("Never ask").tag("never")
                    }
                    HStack {
                        TextField("Safe read-only commands (comma-separated)", text: $safeCommandsDraft)
                            .font(HarnessTheme.mono)
                        Button("Save") {
                            let list = safeCommandsDraft.split(separator: ",")
                                .map { $0.trimmingCharacters(in: .whitespaces) }
                                .filter { !$0.isEmpty }
                            Task { await store.updateConfig(["extra_safe_commands": list]) }
                        }
                    }
                }

                Section("Enabled Tools") {
                    ForEach(cfg.tools.keys.sorted(), id: \.self) { name in
                        Toggle(name, isOn: Binding(
                            get: { cfg.tools[name] ?? false },
                            set: { on in
                                var tools = cfg.tools
                                tools[name] = on
                                Task { await store.updateConfig(["tools": tools]) }
                            }
                        ))
                        .font(HarnessTheme.mono)
                    }
                }
            }
        }
        .formStyle(.grouped)
    }

    // MARK: - Automations Tab

    private var automationsTab: some View {
        Form {
            Section {
                if store.automations.isEmpty {
                    Text("No automations configured yet.")
                        .foregroundStyle(HarnessTheme.dim)
                }
                ForEach(store.automations) { auto in
                    VStack(alignment: .leading, spacing: 6) {
                        HStack {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(auto.name).font(.subheadline.weight(.semibold))
                                Text(auto.schedule.summary)
                                    .font(HarnessTheme.monoSmall)
                                    .foregroundStyle(HarnessTheme.amber)
                            }
                            Spacer()
                            Toggle("", isOn: Binding(
                                get: { auto.enabled },
                                set: { enabled in
                                    Task {
                                        await store.saveAutomation([
                                            "id": auto.id,
                                            "name": auto.name,
                                            "prompt": auto.prompt,
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
                        }
                        Text(auto.prompt).font(.caption).foregroundStyle(HarnessTheme.dim)
                        if let res = auto.lastResult, !res.isEmpty {
                            Text("Last: \(res)")
                                .font(HarnessTheme.monoSmall)
                                .foregroundStyle(HarnessTheme.sage)
                                .lineLimit(2)
                        }
                        HStack {
                            Button("Run Now") { Task { await store.runAutomation(id: auto.id) } }
                            Spacer()
                            Button("Delete", role: .destructive) { Task { await store.deleteAutomation(id: auto.id) } }
                        }
                    }
                    .padding(.vertical, 4)
                }
                Button("New Automation…") { showNewAutomation = true }
            } header: {
                Text("Scheduled Automations")
            }
        }
        .formStyle(.grouped)
    }

    // MARK: - Remote & iPhone Pairing Tab

    private var remoteTab: some View {
        Form {
            Section("Pair Your iPhone") {
                if let access = store.accessInfo {
                    let pairURL = !access.short.isEmpty ? access.short : (!access.url.isEmpty ? access.url : (access.tailnet.isEmpty ? "" : "http://\(access.tailnet):8420"))
                    LabeledContent("Mode", value: access.mode)
                    if !access.tailnet.isEmpty {
                        LabeledContent("Tailscale IP", value: access.tailnet)
                    }
                    if !pairURL.isEmpty {
                        HStack {
                            Spacer()
                            QRCodeCardView(urlString: pairURL, size: 164)
                            Spacer()
                        }
                        .padding(.vertical, 8)
                    } else {
                        Text("Start Harness with `--remote` (or `--lan`) to pair your iPhone over Tailscale or Wi-Fi.")
                            .foregroundStyle(HarnessTheme.dim)
                    }
                }
            }
        }
        .formStyle(.grouped)
    }

    // MARK: - Logs Tab

    private var logsTab: some View {
        VStack(spacing: 10) {
            HStack {
                Picker("Log", selection: $logKind) {
                    Text("Harness (app.log)").tag("app")
                    Text("Model (model.log)").tag("model")
                }
                .pickerStyle(.segmented)
                .frame(maxWidth: 320)
                Spacer()
                Button("Refresh") {
                    Task { logText = await store.fetchLogs(kind: logKind) }
                }
            }
            ScrollView {
                Text(logText.isEmpty ? "Loading…" : logText)
                    .font(HarnessTheme.monoSmall)
                    .foregroundStyle(HarnessTheme.text)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .textSelection(.enabled)
                    .padding(10)
            }
            .background(HarnessTheme.panel)
            .overlay(RoundedRectangle(cornerRadius: 6).stroke(HarnessTheme.line, lineWidth: 1))
        }
        .padding(8)
        .task(id: logKind) { logText = await store.fetchLogs(kind: logKind) }
    }

    // MARK: - Confirmations

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
        case .startModel: return "Loads the selected GGUF weights into GPU memory (~30s)."
        case .restartModel: return "Reloads the model weights (~30s). Any running generation will stop."
        case .restartApp: return "Restarts the local Harness HTTP server for a few seconds."
        case .reset: return "Instructions, sampling, approvals, tools and presets return to defaults. Your chats are kept."
        }
    }

    private func perform(_ action: ConfirmAction) {
        switch action {
        case .startModel: Task { await store.runSystemAction("start-model") }
        case .restartModel: Task { await store.runSystemAction("restart-model") }
        case .restartApp: Task { await store.runSystemAction("restart-app") }
        case .reset:
            Task {
                await store.resetConfig()
                instructionsDraft = store.config?.systemPrompt ?? ""
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
            Text("New Automation").font(.headline)
            TextField("Name", text: $name)
            Picker("Schedule", selection: $kind) {
                Text("Daily").tag("daily")
                Text("Every N minutes").tag("interval")
                Text("Manual").tag("manual")
            }
            .pickerStyle(.segmented)
            if kind == "daily" {
                TextField("Time (HH:MM)", text: $time).font(HarnessTheme.mono)
            } else if kind == "interval" {
                Stepper("Every \(minutes) min", value: $minutes, in: 15...1440, step: 15)
            }
            Text("Prompt").font(.caption).foregroundStyle(HarnessTheme.dim)
            TextEditor(text: $prompt)
                .font(.body)
                .frame(minHeight: 90)
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
        .frame(width: 420)
    }
}
