import SwiftUI

// The pages Settings drills into. Each one explains what its settings do in
// plain language — mostly the same words the web app uses — because "top_k"
// and "approve_commands: destructive" mean nothing on a phone.

private extension View {
    func settingsPageStyle(_ title: String) -> some View {
        self
            .scrollContentBackground(.hidden)
            .background(HarnessTheme.ink)
            .navigationTitle(title)
            .navigationBarTitleDisplayMode(.inline)
    }
}

// MARK: - Model

struct ModelsPage: View {
    @Bindable var store: AppStore
    @State private var pending: ModelEntry?

    private var models: [ModelEntry] { store.modelsResponse?.models ?? [] }

    var body: some View {
        List {
            Section {
                if models.isEmpty {
                    Text("Your Mac didn't report any models. Put .gguf files in ~/llm/models.")
                        .font(.subheadline)
                        .foregroundStyle(HarnessTheme.dim)
                }
                ForEach(models) { model in
                    Button {
                        if !model.active { pending = model }
                    } label: {
                        ModelRow(model: model,
                                 isSwitching: store.isSwitchingModel && model.selected && !model.active)
                    }
                    .disabled(store.isSwitchingModel)
                }
            } header: {
                Text("ON YOUR MAC").font(HarnessTheme.label).tracking(1.0)
            } footer: {
                Text("Switching reloads the weights — about 30 seconds, and nothing can run until it finishes. "
                     + "\u{201C}Tight\u{201D} models fit with little room to spare; close other apps if replies slow down.")
            }
            .listRowBackground(HarnessTheme.panel)
        }
        .settingsPageStyle("Model")
        .refreshable { await store.refreshSystemAndModels() }
        .confirmationDialog(
            pending.map { "Switch to \($0.name)?" } ?? "",
            isPresented: Binding(get: { pending != nil }, set: { if !$0 { pending = nil } }),
            titleVisibility: .visible,
            presenting: pending
        ) { model in
            Button("Switch Model", role: model.fit == "over" ? .destructive : nil) {
                Task { await store.selectModel(path: model.path) }
            }
            Button("Cancel", role: .cancel) {}
        } message: { model in
            Text(switchMessage(model))
        }
    }

    private func switchMessage(_ model: ModelEntry) -> String {
        var parts = ["Harness reloads the weights, about 30 seconds. Nothing can run on your Mac until it's done."]
        if store.isStreaming { parts.append("The reply that's running now will stop.") }
        switch model.fit {
        case "over":
            parts.append(String(format: "This one needs about %.1f GB but your Mac can give the GPU only %.1f GB — "
                                + "it may fail to load or run very slowly.", model.estGb, model.budgetGb))
        case "tight":
            parts.append("It's a tight fit in memory; close other apps if it stalls.")
        default:
            break
        }
        return parts.joined(separator: " ")
    }
}

private struct ModelRow: View {
    let model: ModelEntry
    let isSwitching: Bool

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            VStack(alignment: .leading, spacing: 5) {
                Text(model.name)
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(HarnessTheme.text)
                    .multilineTextAlignment(.leading)
                HStack(spacing: 6) {
                    badge(fitLabel, color: fitColor)
                    if model.vision { badge("VISION", color: HarnessTheme.amber) }
                }
                Text(String(format: "%.1f GB file · ~%.1f GB in use · %dK context",
                            model.gb, model.estGb, model.ctx / 1024))
                    .font(HarnessTheme.monoSmall)
                    .foregroundStyle(HarnessTheme.dim)
            }
            Spacer(minLength: 6)
            if model.active {
                Label("Loaded", systemImage: "checkmark.circle.fill")
                    .labelStyle(.iconOnly)
                    .font(.title3)
                    .foregroundStyle(HarnessTheme.sage)
                    .accessibilityLabel("Loaded")
            } else if isSwitching {
                ProgressView().tint(HarnessTheme.amber)
            }
        }
        .padding(.vertical, 4)
        .contentShape(Rectangle())
    }

    private var fitLabel: String {
        switch model.fit {
        case "ok": return "FITS"
        case "tight": return "TIGHT"
        case "over": return "TOO BIG"
        default: return model.fit.uppercased()
        }
    }

    private var fitColor: Color {
        switch model.fit {
        case "ok": return HarnessTheme.sage
        case "tight": return HarnessTheme.amber
        default: return HarnessTheme.rust
        }
    }

    private func badge(_ text: String, color: Color) -> some View {
        Text(text)
            .font(.system(size: 9, weight: .semibold, design: .monospaced))
            .foregroundStyle(color)
            .padding(.horizontal, 5)
            .padding(.vertical, 1.5)
            .background(color.opacity(0.14))
            .overlay(RoundedRectangle(cornerRadius: 3).stroke(color.opacity(0.35), lineWidth: 0.75))
    }
}

// MARK: - Behaviour presets

struct PresetsPage: View {
    @Bindable var store: AppStore

    var body: some View {
        List {
            Section {
                ForEach(store.presetsResponse?.presets ?? []) { preset in
                    Button {
                        Task { await store.applyPreset(id: preset.id) }
                    } label: {
                        HStack(alignment: .top, spacing: 12) {
                            Image(systemName: store.config?.preset == preset.id ? "largecircle.fill.circle" : "circle")
                                .foregroundStyle(store.config?.preset == preset.id ? HarnessTheme.amber : HarnessTheme.faint)
                                .padding(.top, 2)
                            VStack(alignment: .leading, spacing: 3) {
                                Text(preset.label)
                                    .font(.subheadline.weight(.medium))
                                    .foregroundStyle(HarnessTheme.text)
                                Text(preset.blurb)
                                    .font(.caption)
                                    .foregroundStyle(HarnessTheme.dim)
                                    .multilineTextAlignment(.leading)
                            }
                            Spacer(minLength: 0)
                        }
                        .padding(.vertical, 3)
                        .contentShape(Rectangle())
                    }
                }
            } footer: {
                Text("A preset changes the instructions, sampling and limits together. You can still adjust each one afterwards.")
            }
            .listRowBackground(HarnessTheme.panel)
        }
        .settingsPageStyle("Behaviour")
    }
}

// MARK: - Instructions (system prompt)

struct InstructionsPage: View {
    @Bindable var store: AppStore
    @State private var draft = ""
    @State private var justSaved = false

    private var saved: String { store.config?.systemPrompt ?? "" }

    var body: some View {
        Form {
            Section {
                TextEditor(text: $draft)
                    .font(HarnessTheme.monoSmall)
                    .foregroundStyle(HarnessTheme.text)
                    .frame(minHeight: 260)
                    .scrollContentBackground(.hidden)
            } footer: {
                Text("Sets its role and tone. Every word here is context it can't spend on your actual question — \(draft.count) characters now.")
            }
            .listRowBackground(HarnessTheme.panel)

            if draft != saved {
                Section {
                    Button("Discard Changes", role: .destructive) { draft = saved }
                }
                .listRowBackground(HarnessTheme.panel)
            }
        }
        .settingsPageStyle("Instructions")
        .toolbar {
            ToolbarItem(placement: .confirmationAction) {
                Button(justSaved ? "Saved" : "Save") {
                    Task {
                        await store.updateConfig(["system_prompt": draft])
                        justSaved = true
                        try? await Task.sleep(nanoseconds: 1_500_000_000)
                        justSaved = false
                    }
                }
                .disabled(draft == saved)
            }
        }
        .onAppear { draft = saved }
    }
}

// MARK: - Sampling & limits

struct TuningPage: View {
    @Bindable var store: AppStore
    @State private var temperature = 0.6
    @State private var topP = 0.95
    @State private var topK = 20
    @State private var maxTokens = 4000
    @State private var maxSteps = 8
    @State private var loaded = false

    var body: some View {
        Form {
            Section {
                sliderRow("Temperature", value: $temperature, range: 0...1.5, step: 0.05, key: "temperature")
                sliderRow("Top-p", value: $topP, range: 0.05...1, step: 0.05, key: "top_p")
                Stepper(value: $topK, in: 0...200, step: 5) {
                    valueRow("Top-k", "\(topK)")
                }
                .onChange(of: topK) { _, value in commit(["top_k": value]) }
            } header: {
                Text("SAMPLING").font(HarnessTheme.label).tracking(1.0)
            } footer: {
                Text("Higher temperature is more varied, lower more predictable. Top-p and top-k limit which words it considers; the model's card values are usually best. When thinking is on, Harness may raise these to what the model recommends for reasoning.")
            }
            .listRowBackground(HarnessTheme.panel)

            Section {
                Stepper(value: $maxTokens, in: 500...32000, step: 500) {
                    valueRow("Longest reply", "\(maxTokens.formatted()) tokens")
                }
                .onChange(of: maxTokens) { _, value in commit(["max_tokens": value]) }
                Stepper(value: $maxSteps, in: 1...60) {
                    valueRow("Tool steps per message", "\(maxSteps)")
                }
                .onChange(of: maxSteps) { _, value in commit(["max_steps": value]) }
            } header: {
                Text("LIMITS").font(HarnessTheme.label).tracking(1.0)
            } footer: {
                Text("Thinking counts toward the reply length, so keep it generous when Think First is on. Tool steps cap how many searches, reads and commands one message may take.")
            }
            .listRowBackground(HarnessTheme.panel)
        }
        .settingsPageStyle("Sampling & Limits")
        .onAppear(perform: load)
        .onChange(of: store.config) { _, _ in load() }
    }

    private func load() {
        guard let cfg = store.config else { return }
        loaded = false
        temperature = cfg.temperature
        topP = cfg.topP
        topK = cfg.topK
        maxTokens = cfg.maxTokens
        maxSteps = cfg.maxSteps
        // Let the onChange handlers that these assignments trigger pass
        // before edits count as the user's.
        DispatchQueue.main.async { loaded = true }
    }

    private func commit(_ patch: [String: Any]) {
        guard loaded else { return }
        Task { await store.updateConfig(patch) }
    }

    private func valueRow(_ title: String, _ value: String) -> some View {
        HStack {
            Text(title).foregroundStyle(HarnessTheme.text)
            Spacer()
            Text(value)
                .font(HarnessTheme.monoSmall)
                .foregroundStyle(HarnessTheme.dim)
        }
    }

    private func sliderRow(_ title: String, value: Binding<Double>, range: ClosedRange<Double>,
                           step: Double, key: String) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            valueRow(title, String(format: "%.2f", value.wrappedValue))
            // Sent once, when the finger lifts — not on every tick of the drag.
            Slider(value: value, in: range, step: step) { editing in
                if !editing { commit([key: (value.wrappedValue * 100).rounded() / 100]) }
            }
            .tint(HarnessTheme.amber)
        }
        .padding(.vertical, 2)
    }
}

// MARK: - Permissions & approvals

struct PermissionsPage: View {
    @Bindable var store: AppStore
    @State private var safeCommands = ""

    /// The tools that can change something, in the web app's words.
    static let capabilities: [(key: String, name: String, detail: String, icon: String)] = [
        ("run_command", "Run commands",
         "Check disk space, list processes, and so on. Anything risky still asks first.", "terminal"),
        ("write_file", "Create files",
         "Write new files. Always shows the contents and asks before saving.", "doc.badge.plus"),
        ("edit_file", "Edit files",
         "Change existing files. Shows the exact change and asks first.", "pencil.line"),
        ("web_search", "Search the web",
         "Look things up online when it needs current information.", "magnifyingglass"),
        ("fetch_url", "Open web pages",
         "Read a page it found or you linked.", "globe"),
        ("send_email", "Send email",
         "Send mail, with attachments, from the Mail app. Shows exactly what goes to whom and asks every time.", "envelope"),
    ]

    /// Read-only tools — they look, they don't touch.
    static let readers: [String: String] = [
        "list_files": "List folders",
        "find_files": "Find files by name",
        "search_in_files": "Search inside files",
        "read_file": "Read files",
        "system_status": "Check the Mac's health",
        "view_image": "Look at pictures",
    ]

    static let fileModes: [(id: String, name: String, detail: String)] = [
        ("always", "Always ask", "Every write and edit is previewed first."),
        ("outside", "Only outside the project", "Trust changes inside the project folder; ask about anything else."),
        ("never", "Never ask", "Write and edit freely. Protected system paths are still refused."),
    ]

    static let commandModes: [(id: String, name: String, detail: String)] = [
        ("always", "Always ask", "Anything that isn't plainly read-only is previewed first."),
        ("destructive", "Only destructive ones",
         "Run everyday commands; ask for rm, sudo, installs, network fetches, and anything piped."),
        ("never", "Never ask", "Run whatever it likes. Only use this on a Mac you can rebuild."),
    ]

    var body: some View {
        List {
            if let cfg = store.config {
                Section {
                    ForEach(Self.capabilities, id: \.key) { cap in
                        if cfg.tools[cap.key] != nil {
                            Toggle(isOn: toolBinding(cap.key, cfg)) {
                                HStack(alignment: .top, spacing: 12) {
                                    Image(systemName: cap.icon)
                                        .foregroundStyle(HarnessTheme.amber)
                                        .frame(width: 22)
                                        .padding(.top, 2)
                                    VStack(alignment: .leading, spacing: 3) {
                                        Text(cap.name).foregroundStyle(HarnessTheme.text)
                                        Text(cap.detail)
                                            .font(.caption)
                                            .foregroundStyle(HarnessTheme.dim)
                                    }
                                }
                                .padding(.vertical, 2)
                            }
                        }
                    }
                } header: {
                    header("What Harness can do")
                }
                .listRowBackground(HarnessTheme.panel)

                modeSection("When it changes a file", key: "approve_files",
                            current: cfg.approveFiles, modes: Self.fileModes)
                modeSection("When it runs a command", key: "approve_commands",
                            current: cfg.approveCommands, modes: Self.commandModes,
                            footer: "Whatever you pick, /System, /usr, /etc, ~/.ssh and the like are refused outright.")

                Section {
                    TextField("e.g. git status, brew list", text: $safeCommands)
                        .font(HarnessTheme.mono)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .submitLabel(.done)
                        .onSubmit(saveSafeCommands)
                } header: {
                    header("Commands that never ask")
                } footer: {
                    Text("Comma-separated, saved when you press Done. Only add read-only inspection commands.")
                }
                .listRowBackground(HarnessTheme.panel)

                let readerKeys = cfg.tools.keys.filter { key in
                    !Self.capabilities.contains(where: { $0.key == key })
                }.sorted()
                if !readerKeys.isEmpty {
                    Section {
                        ForEach(readerKeys, id: \.self) { key in
                            Toggle(isOn: toolBinding(key, cfg)) {
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(Self.readers[key] ?? key).foregroundStyle(HarnessTheme.text)
                                    Text(key)
                                        .font(HarnessTheme.monoSmall)
                                        .foregroundStyle(HarnessTheme.faint)
                                }
                            }
                        }
                    } header: {
                        header("Looking around")
                    } footer: {
                        Text("These only read. Turning one off just means Harness can't use it.")
                    }
                    .listRowBackground(HarnessTheme.panel)
                }
            } else {
                ProgressView().tint(HarnessTheme.amber)
                    .frame(maxWidth: .infinity)
                    .listRowBackground(Color.clear)
            }
        }
        .settingsPageStyle("Permissions")
        .onAppear {
            safeCommands = store.config?.extraSafeCommands.joined(separator: ", ") ?? ""
        }
        .onDisappear(perform: saveSafeCommands)
    }

    private func header(_ text: String) -> some View {
        Text(text.uppercased()).font(HarnessTheme.label).tracking(1.0)
    }

    private func toolBinding(_ key: String, _ cfg: HarnessConfig) -> Binding<Bool> {
        Binding(
            get: { store.config?.tools[key] ?? false },
            set: { enabled in
                var tools = store.config?.tools ?? cfg.tools
                tools[key] = enabled
                store.config?.tools = tools
                Task { await store.updateConfig(["tools": tools]) }
            }
        )
    }

    @ViewBuilder
    private func modeSection(_ title: String, key: String, current: String,
                             modes: [(id: String, name: String, detail: String)],
                             footer: String? = nil) -> some View {
        Section {
            ForEach(modes, id: \.id) { mode in
                Button {
                    Task { await store.updateConfig([key: mode.id]) }
                } label: {
                    HStack(alignment: .top, spacing: 12) {
                        Image(systemName: current == mode.id ? "largecircle.fill.circle" : "circle")
                            .foregroundStyle(current == mode.id ? HarnessTheme.amber : HarnessTheme.faint)
                            .padding(.top, 2)
                        VStack(alignment: .leading, spacing: 3) {
                            Text(mode.name).foregroundStyle(HarnessTheme.text)
                            Text(mode.detail)
                                .font(.caption)
                                .foregroundStyle(mode.id == "never" ? HarnessTheme.rust.opacity(0.85) : HarnessTheme.dim)
                                .multilineTextAlignment(.leading)
                        }
                        Spacer(minLength: 0)
                    }
                    .padding(.vertical, 2)
                    .contentShape(Rectangle())
                }
            }
        } header: {
            header(title)
        } footer: {
            if let footer { Text(footer) }
        }
        .listRowBackground(HarnessTheme.panel)
    }

    private func saveSafeCommands() {
        let list = safeCommands.split(separator: ",")
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
        guard list != (store.config?.extraSafeCommands ?? []) else { return }
        Task { await store.updateConfig(["extra_safe_commands": list]) }
    }
}

// MARK: - Automations

struct AutomationsPage: View {
    @Bindable var store: AppStore
    @State private var showNew = false
    @State private var pendingDelete: AutomationEntry?

    var body: some View {
        List {
            Section {
                if store.automations.isEmpty {
                    Text("Nothing scheduled yet. Start from one below, or tap + to write your own.")
                        .font(.subheadline)
                        .foregroundStyle(HarnessTheme.dim)
                }
                ForEach(store.automations) { auto in
                    AutomationRow(auto: auto,
                                  onToggle: { enabled in
                                      Task { await store.saveAutomation(Self.payload(auto, enabled: enabled)) }
                                  },
                                  onRun: { Task { await store.runAutomation(id: auto.id) } },
                                  onDelete: { pendingDelete = auto })
                }
            } header: {
                Text("YOUR AUTOMATIONS").font(HarnessTheme.label).tracking(1.0)
            } footer: {
                Text("Automations run on your Mac while Harness is running, and appear in History when they do.")
            }
            .listRowBackground(HarnessTheme.panel)

            let templates = (store.presetsResponse?.templates ?? []).filter { template in
                !store.automations.contains(where: { $0.name == template.name })
            }
            if !templates.isEmpty {
                Section {
                    ForEach(templates) { template in
                        Button {
                            Task {
                                await store.saveAutomation([
                                    "name": template.name,
                                    "prompt": template.prompt,
                                    "schedule": Self.schedulePayload(template.schedule),
                                    "enabled": true,
                                ])
                            }
                        } label: {
                            HStack(spacing: 12) {
                                Image(systemName: "plus.circle.fill")
                                    .foregroundStyle(HarnessTheme.sage)
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(template.name).foregroundStyle(HarnessTheme.text)
                                    Text(template.schedule.summary)
                                        .font(HarnessTheme.monoSmall)
                                        .foregroundStyle(HarnessTheme.dim)
                                }
                                Spacer(minLength: 0)
                            }
                            .contentShape(Rectangle())
                        }
                    }
                } header: {
                    Text("QUICK START").font(HarnessTheme.label).tracking(1.0)
                }
                .listRowBackground(HarnessTheme.panel)
            }
        }
        .settingsPageStyle("Automations")
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button {
                    showNew = true
                } label: {
                    Image(systemName: "plus")
                }
                .accessibilityLabel("New Automation")
            }
        }
        .sheet(isPresented: $showNew) {
            NewAutomationSheet(store: store)
        }
        .confirmationDialog(
            pendingDelete.map { "Delete \u{201C}\($0.name)\u{201D}?" } ?? "",
            isPresented: Binding(get: { pendingDelete != nil }, set: { if !$0 { pendingDelete = nil } }),
            titleVisibility: .visible,
            presenting: pendingDelete
        ) { auto in
            Button("Delete Automation", role: .destructive) {
                Task { await store.deleteAutomation(id: auto.id) }
            }
            Button("Cancel", role: .cancel) {}
        } message: { _ in
            Text("It stops running. Chats it already created stay in History.")
        }
    }

    static func schedulePayload(_ schedule: AutomationSchedule) -> [String: Any] {
        var out: [String: Any] = ["kind": schedule.kind]
        if let minutes = schedule.minutes { out["minutes"] = minutes }
        if let time = schedule.time { out["time"] = time }
        return out
    }

    static func payload(_ auto: AutomationEntry, enabled: Bool) -> [String: Any] {
        ["id": auto.id, "name": auto.name, "prompt": auto.prompt,
         "schedule": schedulePayload(auto.schedule), "enabled": enabled]
    }
}

private struct AutomationRow: View {
    let auto: AutomationEntry
    let onToggle: (Bool) -> Void
    let onRun: () -> Void
    let onDelete: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(auto.name)
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(auto.enabled ? HarnessTheme.text : HarnessTheme.dim)
                    Text(auto.schedule.summary)
                        .font(HarnessTheme.monoSmall)
                        .foregroundStyle(auto.enabled ? HarnessTheme.amber : HarnessTheme.faint)
                }
                Spacer()
                Toggle("Enabled", isOn: Binding(get: { auto.enabled }, set: onToggle))
                    .labelsHidden()
            }
            Text(auto.prompt)
                .font(.caption)
                .foregroundStyle(HarnessTheme.dim)
                .lineLimit(3)
            if let result = auto.lastResult, !result.isEmpty {
                HStack(alignment: .top, spacing: 5) {
                    Image(systemName: "clock.arrow.circlepath")
                    Text(result).lineLimit(2)
                }
                .font(HarnessTheme.monoSmall)
                .foregroundStyle(HarnessTheme.sage)
            }
            HStack(spacing: 16) {
                Button(action: onRun) {
                    Label("Run Now", systemImage: "play.fill")
                }
                .buttonStyle(.borderless)
                .foregroundStyle(HarnessTheme.amber)
                Spacer()
                Button(role: .destructive, action: onDelete) {
                    Label("Delete", systemImage: "trash")
                        .labelStyle(.iconOnly)
                }
                .buttonStyle(.borderless)
                .foregroundStyle(HarnessTheme.rust)
                .accessibilityLabel("Delete \(auto.name)")
            }
            .font(.caption.weight(.medium))
        }
        .padding(.vertical, 4)
    }
}

private struct NewAutomationSheet: View {
    @Bindable var store: AppStore
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var prompt = ""
    @State private var kind = "daily"
    @State private var time = Calendar.current.date(bySettingHour: 9, minute: 0, second: 0, of: Date()) ?? Date()
    @State private var minutes = 60

    private let intervals = [15, 30, 60, 120, 240, 480, 720, 1440]

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("Name", text: $name)
                    Picker("Repeat", selection: $kind) {
                        Text("Daily").tag("daily")
                        Text("Every…").tag("interval")
                        Text("Manual").tag("manual")
                    }
                    .pickerStyle(.segmented)
                    if kind == "daily" {
                        DatePicker("At", selection: $time, displayedComponents: .hourAndMinute)
                    } else if kind == "interval" {
                        Picker("Every", selection: $minutes) {
                            ForEach(intervals, id: \.self) { value in
                                Text(value % 60 == 0 ? "\(value / 60) h" : "\(value) min").tag(value)
                            }
                        }
                    }
                }
                .listRowBackground(HarnessTheme.panel)

                Section {
                    TextEditor(text: $prompt)
                        .frame(minHeight: 120)
                        .scrollContentBackground(.hidden)
                } header: {
                    Text("WHAT TO DO").font(HarnessTheme.label).tracking(1.0)
                } footer: {
                    Text("Written like a message to Harness, e.g. \u{201C}Check free disk space and warn me if it's under 20 GB.\u{201D}")
                }
                .listRowBackground(HarnessTheme.panel)
            }
            .scrollContentBackground(.hidden)
            .background(HarnessTheme.ink)
            .navigationTitle("New Automation")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") {
                        let formatter = DateFormatter()
                        formatter.locale = Locale(identifier: "en_US_POSIX")
                        formatter.dateFormat = "HH:mm"
                        let payload: [String: Any] = [
                            "name": name.isEmpty ? "Untitled" : name,
                            "prompt": prompt,
                            "schedule": ["kind": kind, "time": formatter.string(from: time), "minutes": minutes],
                            "enabled": true,
                        ]
                        dismiss()
                        Task { await store.saveAutomation(payload) }
                    }
                    .disabled(prompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            }
        }
    }
}

// MARK: - Voice

struct VoicePage: View {
    @Bindable var store: AppStore

    var body: some View {
        List {
            Section {
                if store.capabilities?.voice != true {
                    Text("The voice sidecar isn't running on your Mac. Start it there to get Speak buttons under replies.")
                        .font(.subheadline)
                        .foregroundStyle(HarnessTheme.dim)
                } else if store.voices.isEmpty {
                    Text("The sidecar is running, but no voices are saved yet. Record one from Harness on your Mac.")
                        .font(.subheadline)
                        .foregroundStyle(HarnessTheme.dim)
                } else {
                    ForEach(store.voices) { voice in
                        Button {
                            Task { await store.selectVoice(id: voice.id) }
                        } label: {
                            HStack(spacing: 12) {
                                Image(systemName: voice.selected ? "largecircle.fill.circle" : "circle")
                                    .foregroundStyle(voice.selected ? HarnessTheme.amber : HarnessTheme.faint)
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(voice.name).foregroundStyle(HarnessTheme.text)
                                    Text(String(format: "%.1f s sample", voice.seconds))
                                        .font(HarnessTheme.monoSmall)
                                        .foregroundStyle(HarnessTheme.dim)
                                }
                                Spacer(minLength: 0)
                            }
                            .contentShape(Rectangle())
                        }
                    }
                }
            } header: {
                Text("READ REPLIES ALOUD").font(HarnessTheme.label).tracking(1.0)
            } footer: {
                Text("Speech is generated on your Mac in the voice you pick and played on this phone.")
            }
            .listRowBackground(HarnessTheme.panel)
        }
        .settingsPageStyle("Voice")
    }
}
