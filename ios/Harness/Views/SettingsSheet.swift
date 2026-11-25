import SwiftUI

struct SettingsSheet: View {
    @Bindable var store: AppStore
    @Environment(\.dismiss) private var dismiss

    enum Tab: String, CaseIterable, Identifiable {
        case system = "System"
        case model = "Model"
        case safety = "Safety"
        case automations = "Auto"
        case voice = "Voice"
        var id: String { rawValue }
    }

    @State private var tab: Tab = .system
    @State private var logKind: String = "app"
    @State private var logText: String = ""
    @State private var showLogs = false
    @State private var showNewAutomation = false
    @State private var newAutoName = ""
    @State private var newAutoPrompt = ""
    @State private var newAutoKind = "daily"
    @State private var newAutoTime = "09:00"
    @State private var newAutoMinutes = 60
    @State private var promptDraft = ""

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                Picker("Section", selection: $tab) {
                    ForEach(Tab.allCases) { t in
                        Text(t.rawValue).tag(t)
                    }
                }
                .pickerStyle(.segmented)
                .padding(.horizontal, 16)
                .padding(.vertical, 10)
                .background(HarnessTheme.panel)

                Divider().background(HarnessTheme.line)

                Form {
                    switch tab {
                    case .system: systemSection
                    case .model: modelSection
                    case .safety: safetySection
                    case .automations: automationsSection
                    case .voice: voiceSection
                    }
                }
                .scrollContentBackground(.hidden)
                .background(HarnessTheme.ink)
            }
            .navigationTitle("Settings")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { dismiss() }
                }
            }
            .onAppear {
                promptDraft = store.config?.systemPrompt ?? ""
            }
            .sheet(isPresented: $showLogs) {
                NavigationStack {
                    VStack(spacing: 0) {
                        Picker("Log", selection: $logKind) {
                            Text("app.log").tag("app")
                            Text("model.log").tag("model")
                        }
                        .pickerStyle(.segmented)
                        .padding()
                        .onChange(of: logKind) { _, kind in
                            Task { logText = await store.fetchLogs(kind: kind) }
                        }
                        ScrollView {
                            Text(logText.isEmpty ? "Loading…" : logText)
                                .font(HarnessTheme.monoSmall)
                                .foregroundStyle(HarnessTheme.text)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .textSelection(.enabled)
                                .padding()
                        }
                    }
                    .background(HarnessTheme.ink)
                    .navigationTitle("Mac Logs")
                    .navigationBarTitleDisplayMode(.inline)
                    .toolbar {
                        ToolbarItem(placement: .confirmationAction) {
                            Button("Close") { showLogs = false }
                        }
                    }
                    .task { logText = await store.fetchLogs(kind: logKind) }
                }
            }
            .sheet(isPresented: $showNewAutomation) {
                newAutomationSheet
            }
        }
    }

    // MARK: - System Tab

    @ViewBuilder
    private var systemSection: some View {
        Section("Connection") {
            LabeledContent("Mode", value: store.isDemoMode ? "Offline Demo" : (store.capabilities?.mode ?? "unknown"))
            LabeledContent("Server", value: store.isDemoMode ? "On-device demo" : store.serverURLInput)
            if let tls = store.capabilities?.tls {
                LabeledContent("Transport", value: tls ? "HTTPS (Tailscale TLS)" : "HTTP (Tailnet / LAN)")
            }
            Button("Disconnect & Switch Server", role: .destructive) {
                dismiss()
                store.disconnect()
            }
        }
        .listRowBackground(HarnessTheme.panel)

        if let sys = store.systemState {
            Section("Mac Health") {
                LabeledContent("Model RAM", value: sys.model.running ? String(format: "%.2f GB", sys.model.gb) : "Stopped")
                LabeledContent("Memory Free", value: sys.memoryFree)
                LabeledContent("Disk Free", value: sys.diskFree)
                LabeledContent("Uptime", value: sys.uptime)
                if !sys.tailnet.isEmpty {
                    LabeledContent("Tailscale IP", value: sys.tailnet)
                }
            }
            .listRowBackground(HarnessTheme.panel)
        }

        Section("Mac Controls") {
            Button("Refresh Status") {
                Task { await store.refreshSystemAndModels() }
            }
            Button("Restart Model Server") {
                Task { await store.runSystemAction("restart-model") }
            }
            Button("Restart Harness App") {
                Task { await store.runSystemAction("restart-app") }
            }
            Button("View Mac Logs") {
                showLogs = true
            }
        }
        .listRowBackground(HarnessTheme.panel)
    }

    // MARK: - Model Tab

    @ViewBuilder
    private var modelSection: some View {
        Section("Installed Models on Mac") {
            if let models = store.modelsResponse?.models, !models.isEmpty {
                ForEach(models) { model in
                    Button {
                        Task { await store.selectModel(path: model.path) }
                    } label: {
                        HStack {
                            VStack(alignment: .leading, spacing: 3) {
                                HStack(spacing: 6) {
                                    Text(model.name)
                                        .font(.subheadline.weight(.medium))
                                        .foregroundStyle(HarnessTheme.text)
                                    if model.vision {
                                        Text("VISION")
                                            .font(.system(size: 9, weight: .semibold, design: .monospaced))
                                            .foregroundStyle(HarnessTheme.amber)
                                            .padding(.horizontal, 4)
                                            .padding(.vertical, 1)
                                            .background(HarnessTheme.amber.opacity(0.15))
                                    }
                                }
                                Text(String(format: "%.1f GB disk · est %.1f GB RAM · %dK ctx · %@", model.gb, model.estGb, model.ctx / 1024, model.fit))
                                    .font(HarnessTheme.monoSmall)
                                    .foregroundStyle(HarnessTheme.dim)
                            }
                            Spacer()
                            if model.active {
                                Image(systemName: "checkmark.circle.fill")
                                    .foregroundStyle(HarnessTheme.sage)
                            } else if store.isSwitchingModel && model.selected {
                                ProgressView().tint(HarnessTheme.amber)
                            }
                        }
                    }
                    .disabled(store.isSwitchingModel)
                }
            } else {
                Text("No GGUF models reported by server.")
                    .font(.caption)
                    .foregroundStyle(HarnessTheme.dim)
            }
        }
        .listRowBackground(HarnessTheme.panel)

        if let presets = store.presetsResponse?.presets, !presets.isEmpty {
            Section("Presets") {
                ForEach(presets) { preset in
                    Button {
                        Task { await store.applyPreset(id: preset.id) }
                    } label: {
                        HStack {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(preset.label)
                                    .font(.subheadline.weight(.medium))
                                    .foregroundStyle(HarnessTheme.text)
                                Text(preset.blurb)
                                    .font(.caption)
                                    .foregroundStyle(HarnessTheme.dim)
                            }
                            Spacer()
                            if store.config?.preset == preset.id {
                                Image(systemName: "checkmark")
                                    .foregroundStyle(HarnessTheme.amber)
                            }
                        }
                    }
                }
            }
            .listRowBackground(HarnessTheme.panel)
        }

        if let cfg = store.config {
            Section("Sampling & Behavior") {
                Toggle("Think before answering (default)", isOn: Binding(
                    get: { cfg.thinking },
                    set: { val in Task { await store.updateConfig(["thinking": val]) } }
                ))
                Stepper(value: Binding(
                    get: { cfg.maxSteps },
                    set: { val in Task { await store.updateConfig(["max_steps": val]) } }
                ), in: 1...30) {
                    LabeledContent("Max tool steps", value: "\(cfg.maxSteps)")
                }
                Stepper(value: Binding(
                    get: { cfg.maxTokens },
                    set: { val in Task { await store.updateConfig(["max_tokens": val]) } }
                ), in: 256...16384, step: 256) {
                    LabeledContent("Max reply tokens", value: "\(cfg.maxTokens)")
                }
                HStack {
                    Text("Temperature")
                    Spacer()
                    Text(String(format: "%.2f", cfg.temperature))
                        .font(HarnessTheme.monoSmall)
                        .foregroundStyle(HarnessTheme.dim)
                }
                Slider(value: Binding(
                    get: { cfg.temperature },
                    set: { val in
                        store.config?.temperature = val
                        Task { await store.updateConfig(["temperature": (val * 100).rounded() / 100]) }
                    }
                ), in: 0...1.5, step: 0.05)
            }
            .listRowBackground(HarnessTheme.panel)

            Section("System Prompt") {
                TextEditor(text: $promptDraft)
                    .font(HarnessTheme.monoSmall)
                    .frame(minHeight: 100)
                Button("Save System Prompt") {
                    Task { await store.updateConfig(["system_prompt": promptDraft]) }
                }
            }
            .listRowBackground(HarnessTheme.panel)
        }
    }

    // MARK: - Safety & Tools Tab

    @ViewBuilder
    private var safetySection: some View {
        if let cfg = store.config {
            Section("Approval Policy") {
                Picker("File writes & edits", selection: Binding(
                    get: { cfg.approveFiles },
                    set: { val in Task { await store.updateConfig(["approve_files": val]) } }
                )) {
                    Text("Always ask").tag("always")
                    Text("Ask outside project").tag("outside")
                    Text("Never ask").tag("never")
                }
                Picker("Shell commands", selection: Binding(
                    get: { cfg.approveCommands },
                    set: { val in Task { await store.updateConfig(["approve_commands": val]) } }
                )) {
                    Text("Always ask").tag("always")
                    Text("Ask for risky").tag("risky")
                    Text("Never ask").tag("never")
                }
            }
            .listRowBackground(HarnessTheme.panel)

            Section("Enabled Tools") {
                ForEach(cfg.tools.keys.sorted(), id: \.self) { name in
                    Toggle(name, isOn: Binding(
                        get: { cfg.tools[name] ?? false },
                        set: { enabled in
                            var updated = cfg.tools
                            updated[name] = enabled
                            Task { await store.updateConfig(["tools": updated]) }
                        }
                    ))
                    .font(HarnessTheme.mono)
                }
            }
            .listRowBackground(HarnessTheme.panel)

            Section {
                Button("Reset Settings to Defaults", role: .destructive) {
                    Task {
                        await store.resetConfig()
                        promptDraft = store.config?.systemPrompt ?? ""
                    }
                }
            }
            .listRowBackground(HarnessTheme.panel)
        }
    }

    // MARK: - Automations Tab

    @ViewBuilder
    private var automationsSection: some View {
        Section("Scheduled Tasks") {
            if store.automations.isEmpty {
                Text("No automations configured yet.")
                    .font(.caption)
                    .foregroundStyle(HarnessTheme.dim)
            } else {
                ForEach(store.automations) { auto in
                    VStack(alignment: .leading, spacing: 6) {
                        HStack {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(auto.name)
                                    .font(.subheadline.weight(.semibold))
                                    .foregroundStyle(HarnessTheme.text)
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
                        Text(auto.prompt)
                            .font(.caption)
                            .foregroundStyle(HarnessTheme.dim)
                        if let res = auto.lastResult, !res.isEmpty {
                            Text("Last: \(res)")
                                .font(HarnessTheme.monoSmall)
                                .foregroundStyle(HarnessTheme.sage)
                                .lineLimit(2)
                        }
                        HStack(spacing: 12) {
                            Button("Run Now") {
                                Task { await store.runAutomation(id: auto.id) }
                            }
                            .font(.caption.weight(.medium))
                            Spacer()
                            Button("Delete", role: .destructive) {
                                Task { await store.deleteAutomation(id: auto.id) }
                            }
                            .font(.caption)
                        }
                        .padding(.top, 2)
                    }
                    .padding(.vertical, 4)
                }
            }

            Button {
                showNewAutomation = true
            } label: {
                Label("New Automation", systemImage: "plus.circle")
            }
        }
        .listRowBackground(HarnessTheme.panel)
    }

    private var newAutomationSheet: some View {
        NavigationStack {
            Form {
                Section("Details") {
                    TextField("Name", text: $newAutoName)
                    Picker("Schedule", selection: $newAutoKind) {
                        Text("Daily").tag("daily")
                        Text("Every N minutes").tag("interval")
                        Text("On demand").tag("manual")
                    }
                    if newAutoKind == "daily" {
                        TextField("Time (HH:MM)", text: $newAutoTime)
                            .font(HarnessTheme.mono)
                    } else if newAutoKind == "interval" {
                        Stepper("Every \(newAutoMinutes) min", value: $newAutoMinutes, in: 15...1440, step: 15)
                    }
                }
                Section("Prompt") {
                    TextEditor(text: $newAutoPrompt)
                        .frame(minHeight: 90)
                }
            }
            .navigationTitle("New Automation")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { showNewAutomation = false }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") {
                        let sched: [String: Any] = [
                            "kind": newAutoKind,
                            "time": newAutoTime,
                            "minutes": newAutoMinutes,
                        ]
                        let payload: [String: Any] = [
                            "name": newAutoName.isEmpty ? "Untitled" : newAutoName,
                            "prompt": newAutoPrompt,
                            "schedule": sched,
                            "enabled": true,
                        ]
                        showNewAutomation = false
                        newAutoName = ""
                        newAutoPrompt = ""
                        Task { await store.saveAutomation(payload) }
                    }
                    .disabled(newAutoPrompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            }
        }
    }

    // MARK: - Voice Tab

    @ViewBuilder
    private var voiceSection: some View {
        Section("Local Voice Sidecar") {
            if store.capabilities?.voice == true {
                if store.voices.isEmpty {
                    Text("Voice sidecar is running, but no voices are enrolled yet. Record a voice from Harness on your Mac.")
                        .font(.caption)
                        .foregroundStyle(HarnessTheme.dim)
                } else {
                    ForEach(store.voices) { voice in
                        Button {
                            Task { await store.selectVoice(id: voice.id) }
                        } label: {
                            HStack {
                                VStack(alignment: .leading) {
                                    Text(voice.name)
                                        .foregroundStyle(HarnessTheme.text)
                                    Text(String(format: "%.1fs sample", voice.seconds))
                                        .font(HarnessTheme.monoSmall)
                                        .foregroundStyle(HarnessTheme.dim)
                                }
                                Spacer()
                                if voice.selected {
                                    Image(systemName: "checkmark.circle.fill")
                                        .foregroundStyle(HarnessTheme.sage)
                                }
                            }
                        }
                    }
                }
            } else {
                Text("Voice sidecar is not running on your Mac. Start it with `python3 voice/server.py` to enable Speak buttons on replies.")
                    .font(.caption)
                    .foregroundStyle(HarnessTheme.dim)
            }
        }
        .listRowBackground(HarnessTheme.panel)
    }
}
