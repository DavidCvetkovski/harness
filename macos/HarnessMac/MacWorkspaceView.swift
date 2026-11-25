import AppKit
import SwiftUI
import UniformTypeIdentifiers

struct MacWorkspaceView: View {
    @Bindable var store: AppStore
    @Bindable var supervisor: BackendSupervisor
    @Environment(\.openSettings) private var openSettings

    @State private var sidebarQuery = ""
    @State private var expandedGroups: Set<String> = []
    @State private var isDropTargeted = false

    private let starterPrompts: [(title: String, prompt: String)] = [
        ("Mac health", "Check my Mac's memory, CPU load, and free disk space."),
        ("Recent files", "List the most recent files in my Downloads folder."),
        ("Code review", "List the files in my project folder and summarise what this project does."),
    ]

    var body: some View {
        NavigationSplitView {
            sidebar
                .navigationSplitViewColumnWidth(min: 220, ideal: 260, max: 340)
        } detail: {
            detailPane
        }
        .background(HarnessTheme.ink)
    }

    // MARK: - History Sidebar

    private var filteredSessions: [SessionSummary] {
        let q = sidebarQuery.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !q.isEmpty else { return store.sessions }
        return store.sessions.filter {
            $0.title.lowercased().contains(q) || $0.id.lowercased().contains(q)
        }
    }

    private var groupedSessions: [(title: String, sessions: [SessionSummary])] {
        let calendar = Calendar.current
        let now = Date()
        var buckets: [(String, [SessionSummary])] = [
            ("Today", []), ("Yesterday", []), ("Previous 7 Days", []), ("Older", []),
        ]
        for session in filteredSessions {
            let idx: Int
            if let date = SessionDates.parse(session.updated) {
                if calendar.isDateInToday(date) { idx = 0 }
                else if calendar.isDateInYesterday(date) { idx = 1 }
                else if let days = calendar.dateComponents([.day], from: date, to: now).day, days < 7 { idx = 2 }
                else { idx = 3 }
            } else {
                idx = 3
            }
            buckets[idx].1.append(session)
        }
        return buckets.filter { !$0.1.isEmpty }.map { (title: $0.0, sessions: $0.1) }
    }

    private var sidebar: some View {
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                HarnessBrandMark(size: 24)
                Text("HARNESS")
                    .font(.system(size: 12, weight: .semibold, design: .monospaced))
                    .tracking(1.8)
                    .foregroundStyle(HarnessTheme.text)
                Spacer()
                Button {
                    store.newChat()
                } label: {
                    Image(systemName: "square.and.pencil")
                        .font(.subheadline)
                        .foregroundStyle(HarnessTheme.amber)
                }
                .buttonStyle(.plain)
                .help("New Chat (⌘N)")
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 10)
            .background(HarnessTheme.panel)

            Divider().background(HarnessTheme.line)

            HStack(spacing: 6) {
                Image(systemName: "magnifyingglass")
                    .font(.caption)
                    .foregroundStyle(HarnessTheme.faint)
                TextField("Search history", text: $sidebarQuery)
                    .textFieldStyle(.plain)
                    .font(.subheadline)
                    .foregroundStyle(HarnessTheme.text)
                if !sidebarQuery.isEmpty {
                    Button { sidebarQuery = "" } label: {
                        Image(systemName: "xmark.circle.fill")
                            .font(.caption)
                            .foregroundStyle(HarnessTheme.faint)
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 7)
            .background(HarnessTheme.panel2)

            Divider().background(HarnessTheme.line)

            ScrollView {
                if filteredSessions.isEmpty {
                    VStack(spacing: 8) {
                        Text(sidebarQuery.isEmpty ? "No saved chats yet" : "No matches")
                            .font(.caption)
                            .foregroundStyle(HarnessTheme.dim)
                    }
                    .frame(maxWidth: .infinity)
                    .padding(.top, 40)
                } else {
                    LazyVStack(alignment: .leading, spacing: 12) {
                        ForEach(groupedSessions, id: \.title) { group in
                            VStack(alignment: .leading, spacing: 4) {
                                Text(group.title.uppercased())
                                    .font(HarnessTheme.label)
                                    .tracking(1.0)
                                    .foregroundStyle(HarnessTheme.faint)
                                    .padding(.horizontal, 12)
                                    .padding(.top, 4)

                                ForEach(group.sessions) { session in
                                    sidebarRow(session)
                                }
                            }
                        }
                    }
                    .padding(.vertical, 8)
                }
            }

            Divider().background(HarnessTheme.line)

            HStack(spacing: 8) {
                Circle()
                    .fill(statusLEDColor)
                    .frame(width: 7, height: 7)
                Text(store.connectionState == .connected ? (store.isDemoMode ? "Demo Mode" : "127.0.0.1:8420") : "Offline")
                    .font(HarnessTheme.monoSmall)
                    .foregroundStyle(HarnessTheme.dim)
                    .lineLimit(1)
                Spacer()
                Button {
                    openSettings()
                } label: {
                    Image(systemName: "slider.horizontal.3")
                        .font(.caption)
                        .foregroundStyle(HarnessTheme.dim)
                }
                .buttonStyle(.plain)
                .help("Settings (⌘,)")
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            .background(HarnessTheme.panel)
        }
        .background(HarnessTheme.ink)
    }

    private func sidebarRow(_ session: SessionSummary) -> some View {
        let isOpen = session.id == store.currentSessionId
        return Button {
            if !isOpen {
                expandedGroups.removeAll()
                Task { await store.openSession(id: session.id) }
            }
        } label: {
            VStack(alignment: .leading, spacing: 3) {
                Text(session.title.isEmpty ? "Untitled chat" : session.title)
                    .font(.subheadline.weight(isOpen ? .semibold : .regular))
                    .foregroundStyle(isOpen ? HarnessTheme.text : HarnessTheme.dim)
                    .lineLimit(2)
                    .multilineTextAlignment(.leading)
                HStack(spacing: 5) {
                    Text(SessionDates.label(for: session.updated))
                    Text("·")
                    Text("\(session.turns) \(session.turns == 1 ? "msg" : "msgs")")
                }
                .font(.system(size: 10, design: .monospaced))
                .foregroundStyle(HarnessTheme.faint)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 10)
            .padding(.vertical, 7)
            .background(isOpen ? HarnessTheme.panel2 : Color.clear)
            .overlay(
                RoundedRectangle(cornerRadius: 5)
                    .stroke(isOpen ? HarnessTheme.amberDim : Color.clear, lineWidth: 1)
            )
            .clipShape(RoundedRectangle(cornerRadius: 5))
            .padding(.horizontal, 8)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .contextMenu {
            Button("Export Markdown…") {
                Task {
                    if let md = try? await store.client?.exportMarkdown(id: session.id) {
                        saveMarkdownPanel(text: md, suggestedName: "\(session.id).md")
                    }
                }
            }
            Divider()
            Button("Delete", role: .destructive) {
                Task { await store.deleteSession(id: session.id) }
            }
        }
    }

    // MARK: - Detail Pane

    private var detailPane: some View {
        VStack(spacing: 0) {
            topBar
            Divider().background(HarnessTheme.line)

            if let banner = store.bannerMessage ?? supervisor.startupError {
                HStack(spacing: 8) {
                    Image(systemName: "info.circle")
                        .font(.caption)
                        .foregroundStyle(HarnessTheme.amber)
                    Text(banner)
                        .font(.caption)
                        .foregroundStyle(HarnessTheme.amber)
                    Spacer()
                    Button {
                        store.bannerMessage = nil
                        supervisor.startupError = nil
                    } label: {
                        Image(systemName: "xmark")
                            .font(.caption2)
                            .foregroundStyle(HarnessTheme.dim)
                    }
                    .buttonStyle(.plain)
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 8)
                .background(HarnessTheme.amber.opacity(0.1))
                Divider().background(HarnessTheme.line)
            }

            if store.connectionState != .connected {
                disconnectedOnboardingView
            } else {
                transcriptArea
                Divider().background(HarnessTheme.line)
                composerArea
            }
        }
        .background(HarnessTheme.ink)
        .onDrop(of: [.image, .fileURL], isTargeted: $isDropTargeted) { providers in
            guard store.isVisionAvailable, store.connectionState == .connected else { return false }
            handleImageDrop(providers)
            return true
        }
        .overlay {
            if isDropTargeted {
                RoundedRectangle(cornerRadius: 8)
                    .stroke(HarnessTheme.amber, style: StrokeStyle(lineWidth: 2, dash: [6]))
                    .background(HarnessTheme.amber.opacity(0.07))
                    .overlay {
                        Label("Drop image to attach", systemImage: "photo.badge.plus")
                            .font(.headline)
                            .foregroundStyle(HarnessTheme.amber)
                            .padding(14)
                            .background(HarnessTheme.panel)
                            .clipShape(RoundedRectangle(cornerRadius: 8))
                    }
                    .padding(12)
                    .allowsHitTesting(false)
            }
        }
    }

    private var topBar: some View {
        HStack(spacing: 12) {
            HStack(spacing: 8) {
                Circle()
                    .fill(statusLEDColor)
                    .frame(width: 8, height: 8)
                Text(store.connectionState == .connected ? store.activeModelName : "Harness")
                    .font(.system(size: 13, weight: .semibold, design: .monospaced))
                    .foregroundStyle(HarnessTheme.text)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }

            if store.connectionState == .connected {
                if store.isDemoMode {
                    pill("DEMO", color: HarnessTheme.sage)
                } else if store.capabilities?.tls == true {
                    pill("HTTPS", color: HarnessTheme.sage)
                } else {
                    pill((store.capabilities?.mode ?? "local").uppercased(), color: HarnessTheme.dim)
                }
                if store.isVisionAvailable {
                    pill("VISION", color: HarnessTheme.amber)
                }
                if let status = store.statusText {
                    Text(status)
                        .font(HarnessTheme.monoSmall)
                        .foregroundStyle(HarnessTheme.amber)
                        .lineLimit(1)
                }
            }

            Spacer()

            if store.connectionState == .connected {
                Button {
                    pickProjectFolder()
                } label: {
                    HStack(spacing: 5) {
                        Image(systemName: "folder")
                            .font(.caption)
                            .foregroundStyle(store.projectName.isEmpty ? HarnessTheme.dim : HarnessTheme.amber)
                        Text(store.projectName.isEmpty ? "Set Project Folder" : store.projectName)
                            .font(HarnessTheme.monoSmall)
                            .foregroundStyle(store.projectName.isEmpty ? HarnessTheme.dim : HarnessTheme.text)
                            .lineLimit(1)
                    }
                    .padding(.horizontal, 9)
                    .padding(.vertical, 5)
                    .harnessCard(
                        border: store.projectName.isEmpty ? HarnessTheme.line : HarnessTheme.amberDim,
                        background: HarnessTheme.panel2
                    )
                }
                .buttonStyle(.plain)
                .help(store.projectDir.isEmpty ? "Choose working folder for tools" : store.projectDir)

                if store.hasUserTurns {
                    Button {
                        Task {
                            let md = await store.exportCurrentMarkdown()
                            saveMarkdownPanel(text: md, suggestedName: "\(store.currentSessionId ?? "harness-chat").md")
                        }
                    } label: {
                        Image(systemName: "square.and.arrow.up")
                            .foregroundStyle(HarnessTheme.dim)
                            .frame(width: 28, height: 28)
                    }
                    .buttonStyle(.plain)
                    .help("Export transcript as Markdown")
                }

                Button {
                    Task { await store.refreshAll() }
                } label: {
                    Image(systemName: "arrow.clockwise")
                        .foregroundStyle(HarnessTheme.dim)
                        .frame(width: 28, height: 28)
                }
                .buttonStyle(.plain)
                .help("Refresh status (⌘R)")
            }

            Button {
                openSettings()
            } label: {
                Image(systemName: "slider.horizontal.3")
                    .foregroundStyle(HarnessTheme.dim)
                    .frame(width: 28, height: 28)
            }
            .buttonStyle(.plain)
            .help("Settings (⌘,)")
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
        .background(HarnessTheme.panel)
    }

    private var statusLEDColor: Color {
        if store.isStreaming || store.isSwitchingModel || supervisor.isStartingBackend { return HarnessTheme.amber }
        return (store.connectionState == .connected && store.isModelLoaded) ? HarnessTheme.sage : HarnessTheme.rust
    }

    private func pill(_ text: String, color: Color) -> some View {
        Text(text)
            .font(.system(size: 9.5, weight: .semibold, design: .monospaced))
            .foregroundStyle(color)
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(color.opacity(0.12))
            .overlay(RoundedRectangle(cornerRadius: 3).stroke(color.opacity(0.35), lineWidth: 0.75))
    }

    // MARK: - Disconnected & First-Run Onboarding (Option 2 Foundation)

    private var disconnectedOnboardingView: some View {
        VStack(spacing: 20) {
            Spacer()
            HarnessBrandMark(size: 56)
            VStack(spacing: 6) {
                Text(supervisor.isStartingBackend ? "Starting Harness on this Mac…" : "Harness backend is not running")
                    .font(.title3.weight(.semibold))
                    .foregroundStyle(HarnessTheme.text)
                Text("Runtime: \(supervisor.source.label)")
                    .font(HarnessTheme.monoSmall)
                    .foregroundStyle(HarnessTheme.dim)
            }

            HStack(spacing: 12) {
                Button {
                    Task { await supervisor.connectOrStart(store: store, autoStart: true, remote: true) }
                } label: {
                    HStack(spacing: 6) {
                        if supervisor.isStartingBackend {
                            ProgressView().controlSize(.small)
                        } else {
                            Image(systemName: "play.fill")
                        }
                        Text(supervisor.isStartingBackend ? "Starting…" : "Start Harness")
                            .fontWeight(.semibold)
                    }
                    .padding(.horizontal, 18)
                    .padding(.vertical, 9)
                    .background(HarnessTheme.amber)
                    .foregroundStyle(HarnessTheme.ink)
                    .clipShape(RoundedRectangle(cornerRadius: 5))
                }
                .buttonStyle(.plain)
                .disabled(supervisor.isStartingBackend)

                Button {
                    Task { await store.enterDemoMode() }
                } label: {
                    Label("Try Offline Demo", systemImage: "sparkles")
                        .padding(.horizontal, 14)
                        .padding(.vertical, 9)
                        .foregroundStyle(HarnessTheme.sage)
                        .harnessCard(border: HarnessTheme.sage.opacity(0.45), background: HarnessTheme.sage.opacity(0.08))
                }
                .buttonStyle(.plain)
            }

            Spacer()
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(32)
    }

    // MARK: - Transcript

    private var transcriptArea: some View {
        ScrollViewReader { proxy in
            ScrollView {
                if store.transcript.isEmpty {
                    emptyStateView
                } else {
                    let rows = TranscriptRow.build(from: store.transcript)
                    let lastAssistantId = store.transcript.last(where: {
                        if case .assistant = $0 { return true }
                        return false
                    })?.id
                    let lastStepsId = rows.last(where: {
                        if case .steps = $0 { return true }
                        return false
                    })?.id

                    LazyVStack(alignment: .leading, spacing: 12) {
                        ForEach(rows) { row in
                            switch row {
                            case .single(let item):
                                itemRow(item, lastAssistantId: lastAssistantId)
                                    .id(item.id)
                            case .steps(let groupId, let items):
                                StepsGroupView(
                                    items: items,
                                    isLive: store.isStreaming && groupId == lastStepsId,
                                    isExpanded: expandedGroups.contains(groupId),
                                    onToggle: {
                                        if expandedGroups.contains(groupId) {
                                            expandedGroups.remove(groupId)
                                        } else {
                                            expandedGroups.insert(groupId)
                                        }
                                    },
                                    content: { itemRow($0, lastAssistantId: lastAssistantId) }
                                )
                                .id(groupId)
                            }
                        }
                        Color.clear.frame(height: 12).id("bottom-anchor")
                    }
                    .frame(maxWidth: 780)
                    .frame(maxWidth: .infinity)
                    .padding(.horizontal, 24)
                    .padding(.vertical, 18)
                }
            }
            .onChange(of: store.transcript.count) { _, _ in
                withAnimation(.easeOut(duration: 0.15)) {
                    proxy.scrollTo("bottom-anchor", anchor: .bottom)
                }
            }
            .onChange(of: store.transcript.last) { _, _ in
                proxy.scrollTo("bottom-anchor", anchor: .bottom)
            }
        }
    }

    @ViewBuilder
    private func itemRow(_ item: TranscriptItem, lastAssistantId: String?) -> some View {
        switch item {
        case .user(_, let text, let urls, let local):
            UserTurnView(store: store, text: text, imageUrls: urls, localImages: local)
        case .thinking(_, let text, let started, let finished, let expanded):
            ThinkingCardView(text: text, startedAt: started, finishedAt: finished, isExpanded: expanded) {
                store.toggleExpanded(itemId: item.id)
            }
        case .tool(_, let name, let args, let out, let isErr, let isRun, let expanded):
            ToolCardView(name: name, args: args, output: out, isError: isErr, isRunning: isRun, isExpanded: expanded) {
                store.toggleExpanded(itemId: item.id)
            }
        case .approval(let apId, let apItem, let status):
            ApprovalCardView(item: apItem, status: status) { allow, always in
                Task { await store.respondToApproval(id: apId, allow: allow, always: always) }
            }
        case .assistant(_, let text, let streaming):
            AssistantTurnView(
                itemId: item.id,
                text: text,
                isStreaming: streaming,
                isLastAssistant: item.id == lastAssistantId,
                store: store
            )
        case .error(_, let text):
            ErrorCardView(text: text)
        case .cost(_, let seconds, let tokens, let steps):
            CostFooterView(seconds: seconds, tokens: tokens, steps: steps)
        }
    }

    private var emptyStateView: some View {
        VStack(alignment: .leading, spacing: 20) {
            Spacer().frame(height: 44)
            HStack(spacing: 14) {
                HarnessBrandMark(size: 44)
                VStack(alignment: .leading, spacing: 3) {
                    Text(store.isDemoMode ? "Offline Demo Mode" : "Ready on your Mac")
                        .font(.title3.weight(.semibold))
                        .foregroundStyle(HarnessTheme.text)
                    Text(store.isVisionAvailable
                         ? "Ask anything, run local tools, or drop/paste an image into the window."
                         : "Ask anything or run local tools — everything stays on this Mac.")
                        .font(.subheadline)
                        .foregroundStyle(HarnessTheme.dim)
                }
            }

            if let models = store.modelsResponse?.models, models.isEmpty, !store.isDemoMode {
                VStack(alignment: .leading, spacing: 10) {
                    Text("NO GGUF MODELS FOUND YET")
                        .font(HarnessTheme.label)
                        .tracking(1.0)
                        .foregroundStyle(HarnessTheme.amber)
                    Text("Place any `.gguf` model in `~/llm/models` (and an optional `mmproj*.gguf` for vision), then pick it in Settings → Model.")
                        .font(.subheadline)
                        .foregroundStyle(HarnessTheme.dim)
                    Button {
                        supervisor.openModelsFolderInFinder()
                    } label: {
                        Label("Open ~/llm/models in Finder", systemImage: "folder")
                            .padding(.horizontal, 12)
                            .padding(.vertical, 7)
                            .background(HarnessTheme.panel2)
                            .foregroundStyle(HarnessTheme.text)
                            .harnessCard(border: HarnessTheme.lineHot, background: HarnessTheme.panel2)
                    }
                    .buttonStyle(.plain)
                }
                .padding(14)
                .harnessCard(border: HarnessTheme.amberDim, background: HarnessTheme.panel)
            } else {
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 220), spacing: 10)], spacing: 10) {
                    ForEach(starterPrompts, id: \.title) { starter in
                        Button {
                            store.composerText = starter.prompt
                            Task { await store.sendMessage() }
                        } label: {
                            VStack(alignment: .leading, spacing: 6) {
                                HStack {
                                    Text(starter.title.uppercased())
                                        .font(HarnessTheme.label)
                                        .tracking(1.0)
                                        .foregroundStyle(HarnessTheme.amber)
                                    Spacer()
                                    Image(systemName: "arrow.up.right")
                                        .font(.caption2)
                                        .foregroundStyle(HarnessTheme.faint)
                                }
                                Text(starter.prompt)
                                    .font(.subheadline)
                                    .foregroundStyle(HarnessTheme.text)
                                    .multilineTextAlignment(.leading)
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(12)
                            .harnessCard(border: HarnessTheme.line, background: HarnessTheme.panel)
                        }
                        .buttonStyle(.plain)
                    }
                }
            }
        }
        .frame(maxWidth: 740)
        .frame(maxWidth: .infinity)
        .padding(24)
    }

    // MARK: - Composer

    private var composerArea: some View {
        VStack(spacing: 8) {
            if !store.pendingAttachments.isEmpty || store.isUploadingImage {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 8) {
                        ForEach(store.pendingAttachments) { att in
                            ZStack(alignment: .topTrailing) {
                                if let img = PlatformImageCodec.image(from: att.previewData) {
                                    Image(platformImage: img)
                                        .resizable()
                                        .scaledToFill()
                                        .frame(width: 60, height: 60)
                                        .clipped()
                                        .clipShape(RoundedRectangle(cornerRadius: 4))
                                        .overlay(RoundedRectangle(cornerRadius: 4).stroke(HarnessTheme.lineHot, lineWidth: 1))
                                }
                                Button {
                                    store.removeAttachment(id: att.id)
                                } label: {
                                    Image(systemName: "xmark.circle.fill")
                                        .font(.caption)
                                        .foregroundStyle(HarnessTheme.text)
                                        .background(Circle().fill(HarnessTheme.ink))
                                }
                                .buttonStyle(.plain)
                                .offset(x: 5, y: -5)
                            }
                        }
                        if store.isUploadingImage {
                            ZStack {
                                HarnessTheme.panel2
                                ProgressView().controlSize(.small)
                            }
                            .frame(width: 60, height: 60)
                            .clipShape(RoundedRectangle(cornerRadius: 4))
                        }
                    }
                }
            }

            HStack(alignment: .bottom, spacing: 10) {
                if store.isVisionAvailable {
                    Button {
                        pickImagesPanel()
                    } label: {
                        Image(systemName: "photo.badge.plus")
                            .font(.body)
                            .foregroundStyle(HarnessTheme.dim)
                            .frame(width: 34, height: 34)
                            .harnessCard(border: HarnessTheme.lineHot, background: HarnessTheme.panel2)
                    }
                    .buttonStyle(.plain)
                    .disabled(store.isStreaming || store.isUploadingImage)
                    .help("Attach image (or drag & drop, or ⌘⇧V to paste)")
                }

                TextField("Ask Harness… (Return to send, Shift+Return for newline)", text: $store.composerText, axis: .vertical)
                    .textFieldStyle(.plain)
                    .lineLimit(1...8)
                    .font(.body)
                    .foregroundStyle(HarnessTheme.text)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 8)
                    .background(HarnessTheme.panel2)
                    .overlay(RoundedRectangle(cornerRadius: 6).stroke(HarnessTheme.lineHot, lineWidth: 1))
                    .onKeyPress(.return, phases: .down) { press in
                        if press.modifiers.contains(.shift) {
                            return .ignored
                        }
                        guard !store.isStreaming && !store.isUploadingImage else { return .handled }
                        Task { await store.sendMessage() }
                        return .handled
                    }

                if store.isStreaming {
                    Button {
                        Task { await store.stopTurn() }
                    } label: {
                        Image(systemName: "stop.fill")
                            .font(.subheadline.weight(.semibold))
                            .frame(width: 34, height: 34)
                            .background(HarnessTheme.rust)
                            .foregroundStyle(.white)
                            .clipShape(RoundedRectangle(cornerRadius: 6))
                    }
                    .buttonStyle(.plain)
                    .help("Stop generation (Esc)")
                } else {
                    let canSend = !store.composerText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                        || !store.pendingAttachments.isEmpty
                    Button {
                        Task { await store.sendMessage() }
                    } label: {
                        Image(systemName: "arrow.up")
                            .font(.subheadline.weight(.bold))
                            .frame(width: 34, height: 34)
                            .background(canSend ? HarnessTheme.amber : HarnessTheme.panel2)
                            .foregroundStyle(canSend ? HarnessTheme.ink : HarnessTheme.faint)
                            .clipShape(RoundedRectangle(cornerRadius: 6))
                    }
                    .buttonStyle(.plain)
                    .disabled(!canSend || store.isUploadingImage)
                }
            }

            HStack(spacing: 10) {
                Button {
                    store.thinkFirst.toggle()
                } label: {
                    HStack(spacing: 5) {
                        Image(systemName: store.thinkFirst ? "brain.head.profile.fill" : "brain.head.profile")
                            .font(.caption2)
                        Text(store.thinkFirst ? "Think first: ON" : "Think first: OFF")
                            .font(HarnessTheme.monoSmall)
                    }
                    .foregroundStyle(store.thinkFirst ? HarnessTheme.amber : HarnessTheme.dim)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 4)
                    .background(store.thinkFirst ? HarnessTheme.amber.opacity(0.12) : HarnessTheme.panel2)
                    .overlay(RoundedRectangle(cornerRadius: 4).stroke(
                        store.thinkFirst ? HarnessTheme.amber.opacity(0.5) : HarnessTheme.line, lineWidth: 1
                    ))
                }
                .buttonStyle(.plain)

                if store.isVisionAvailable {
                    Button {
                        if let data = PlatformClipboard.pasteImageData() {
                            Task { await store.attachImageData(data) }
                        } else {
                            store.bannerMessage = "No image on the clipboard."
                        }
                    } label: {
                        HStack(spacing: 4) {
                            Image(systemName: "doc.on.clipboard")
                                .font(.caption2)
                            Text("Paste image")
                                .font(HarnessTheme.monoSmall)
                        }
                        .foregroundStyle(HarnessTheme.dim)
                        .padding(.horizontal, 8)
                        .padding(.vertical, 4)
                        .harnessCard(border: HarnessTheme.line, background: HarnessTheme.panel2)
                    }
                    .buttonStyle(.plain)
                    .disabled(store.isStreaming || store.isUploadingImage)
                }

                Spacer()

                if let ctx = store.capabilities?.nCtx, ctx > 0 {
                    Text("\(ctx / 1024)K ctx")
                        .font(HarnessTheme.monoSmall)
                        .foregroundStyle(HarnessTheme.faint)
                }
            }
        }
        .frame(maxWidth: 780)
        .frame(maxWidth: .infinity)
        .padding(.horizontal, 24)
        .padding(.vertical, 12)
        .background(HarnessTheme.panel)
    }

    // MARK: - Native macOS Panels & Image Drop

    private func pickProjectFolder() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.prompt = "Use Project Folder"
        if !store.projectDir.isEmpty {
            panel.directoryURL = URL(fileURLWithPath: store.projectDir)
        }
        if panel.runModal() == .OK, let url = panel.url {
            Task { await store.setProjectDir(url.path) }
        }
    }

    private func pickImagesPanel() {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [.image]
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = true
        panel.prompt = "Attach"
        if panel.runModal() == .OK {
            let urls = panel.urls
            Task {
                for url in urls {
                    if let data = try? Data(contentsOf: url) {
                        await store.attachImageData(data)
                    }
                }
            }
        }
    }

    private func handleImageDrop(_ providers: [NSItemProvider]) {
        for provider in providers {
            if provider.hasItemConformingToTypeIdentifier(UTType.fileURL.identifier) {
                provider.loadItem(forTypeIdentifier: UTType.fileURL.identifier, options: nil) { item, _ in
                    guard let data = item as? Data,
                          let url = URL(dataRepresentation: data, relativeTo: nil),
                          let bytes = try? Data(contentsOf: url) else { return }
                    Task { @MainActor in await store.attachImageData(bytes) }
                }
            } else if provider.hasItemConformingToTypeIdentifier(UTType.image.identifier) {
                provider.loadDataRepresentation(forTypeIdentifier: UTType.image.identifier) { data, _ in
                    guard let data else { return }
                    Task { @MainActor in await store.attachImageData(data) }
                }
            }
        }
    }

    private func saveMarkdownPanel(text: String, suggestedName: String) {
        let panel = NSSavePanel()
        panel.allowedContentTypes = [.plainText]
        panel.nameFieldStringValue = suggestedName
        if panel.runModal() == .OK, let url = panel.url {
            try? text.write(to: url, atomically: true, encoding: .utf8)
        }
    }
}
