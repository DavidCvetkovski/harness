import AppKit
import SwiftUI
import UniformTypeIdentifiers

struct MacWorkspaceView: View {
    @Bindable var store: AppStore
    @Bindable var supervisor: BackendSupervisor
    @Bindable var ui: MacUIState
    @Environment(\.openSettings) private var openSettings

    @State private var sidebarQuery = ""
    @State private var expandedGroups: Set<String> = []
    @State private var isDropTargeted = false
    @FocusState private var composerFocused: Bool

    private let starterPrompts: [(tag: String, title: String, prompt: String)] = [
        ("SYSTEM", "Check Mac health", "Check my Mac's memory, CPU load, and free disk space."),
        ("FILES", "Recent Downloads", "List the most recent files in my Downloads folder."),
        ("CODE", "Summarise project", "List the files in my project folder and summarise what this project does."),
    ]

    var body: some View {
        HStack(spacing: 0) {
            if ui.showSidebar {
                sidebar
                    .frame(width: 256)
                    .transition(.move(edge: .leading))
                Rectangle().fill(HarnessTheme.line).frame(width: 1)
            }
            detailPane
        }
        .background(HarnessTheme.ink)
        .ignoresSafeArea(.container, edges: .top)
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
            // Top bar clearing macOS traffic lights cleanly
            HStack(spacing: 8) {
                Spacer().frame(width: 68) // space for native close/min/zoom lights
                Spacer()
                Button {
                    withAnimation(.easeInOut(duration: 0.16)) { ui.showSidebar = false }
                } label: {
                    Image(systemName: "sidebar.left")
                        .font(.subheadline)
                        .foregroundStyle(HarnessTheme.dim)
                        .frame(width: 26, height: 26)
                        .macHoverRow(cornerRadius: 6)
                }
                .buttonStyle(.plain)
                .help("Hide Sidebar (⌘\\)")
            }
            .padding(.horizontal, 10)
            .padding(.top, 10)
            .padding(.bottom, 6)

            // Prominent New Chat button
            Button {
                store.newChat()
                composerFocused = true
            } label: {
                HStack(spacing: 8) {
                    Image(systemName: "plus")
                        .font(.caption.weight(.bold))
                        .foregroundStyle(HarnessTheme.amber)
                    Text("New Chat")
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(HarnessTheme.text)
                    Spacer()
                    Text("⌘N")
                        .font(.system(size: 10.5, design: .monospaced))
                        .foregroundStyle(HarnessTheme.faint)
                }
                .padding(.horizontal, 11)
                .padding(.vertical, 8)
                .background(HarnessTheme.panel2)
                .overlay(RoundedRectangle(cornerRadius: 7).stroke(HarnessTheme.lineHot, lineWidth: 1))
                .clipShape(RoundedRectangle(cornerRadius: 7))
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .padding(.horizontal, 12)
            .padding(.bottom, 10)

            // Search box
            HStack(spacing: 7) {
                Image(systemName: "magnifyingglass")
                    .font(.caption)
                    .foregroundStyle(HarnessTheme.faint)
                TextField("Search history", text: $sidebarQuery)
                    .textFieldStyle(.plain)
                    .font(.caption)
                    .foregroundStyle(HarnessTheme.text)
                if !sidebarQuery.isEmpty {
                    Button { sidebarQuery = "" } label: {
                        Image(systemName: "xmark.circle.fill")
                            .font(.caption2)
                            .foregroundStyle(HarnessTheme.faint)
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(.horizontal, 9)
            .padding(.vertical, 6)
            .background(HarnessTheme.ink)
            .overlay(RoundedRectangle(cornerRadius: 6).stroke(HarnessTheme.line, lineWidth: 1))
            .clipShape(RoundedRectangle(cornerRadius: 6))
            .padding(.horizontal, 12)
            .padding(.bottom, 10)

            Rectangle().fill(HarnessTheme.line).frame(height: 1)

            // Grouped sessions list
            ScrollView {
                if filteredSessions.isEmpty {
                    VStack(spacing: 6) {
                        Image(systemName: sidebarQuery.isEmpty ? "bubble.left.and.bubble.right" : "magnifyingglass")
                            .font(.title3)
                            .foregroundStyle(HarnessTheme.faint)
                        Text(sidebarQuery.isEmpty ? "No conversations yet" : "No matches")
                            .font(.caption)
                            .foregroundStyle(HarnessTheme.dim)
                    }
                    .frame(maxWidth: .infinity)
                    .padding(.top, 48)
                } else {
                    LazyVStack(alignment: .leading, spacing: 14) {
                        ForEach(groupedSessions, id: \.title) { group in
                            VStack(alignment: .leading, spacing: 3) {
                                Text(group.title.uppercased())
                                    .font(.system(size: 9.5, weight: .semibold))
                                    .tracking(1.1)
                                    .foregroundStyle(HarnessTheme.faint)
                                    .padding(.horizontal, 14)
                                    .padding(.bottom, 2)

                                ForEach(group.sessions) { session in
                                    sidebarRow(session)
                                }
                            }
                        }
                    }
                    .padding(.vertical, 10)
                }
            }

            Rectangle().fill(HarnessTheme.line).frame(height: 1)

            // Bottom Mac status + Settings shortcut
            Button {
                ui.selectedSettingsPane = .overview
                openSettings()
            } label: {
                HStack(spacing: 10) {
                    Circle()
                        .fill(statusLEDColor)
                        .frame(width: 8, height: 8)
                        .shadow(color: statusLEDColor.opacity(0.6), radius: 3)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(store.connectionState == .connected
                             ? (store.isDemoMode ? "Demo Mode" : shortModelName)
                             : "Offline")
                            .font(.system(size: 11.5, weight: .medium, design: .monospaced))
                            .foregroundStyle(HarnessTheme.text)
                            .lineLimit(1)
                        if let free = store.systemState?.memoryFree, !free.isEmpty {
                            Text("RAM \(free) free · Settings ⌘,")
                                .font(.system(size: 10, design: .monospaced))
                                .foregroundStyle(HarnessTheme.faint)
                        } else {
                            Text("Settings ⌘,")
                                .font(.system(size: 10, design: .monospaced))
                                .foregroundStyle(HarnessTheme.faint)
                        }
                    }
                    Spacer()
                    Image(systemName: "slider.horizontal.3")
                        .font(.caption)
                        .foregroundStyle(HarnessTheme.dim)
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 10)
                .contentShape(Rectangle())
                .macHoverRow(cornerRadius: 6)
            }
            .buttonStyle(.plain)
            .padding(6)
        }
        .background(HarnessTheme.panel)
    }

    private func sidebarRow(_ session: SessionSummary) -> some View {
        let isOpen = session.id == store.currentSessionId
        return Button {
            if !isOpen {
                expandedGroups.removeAll()
                Task { await store.openSession(id: session.id) }
            }
        } label: {
            HStack(spacing: 8) {
                RoundedRectangle(cornerRadius: 1)
                    .fill(isOpen ? HarnessTheme.amber : Color.clear)
                    .frame(width: 2.5, height: 26)
                VStack(alignment: .leading, spacing: 2) {
                    Text(session.title.isEmpty ? "Untitled chat" : session.title)
                        .font(.system(size: 12.5, weight: isOpen ? .semibold : .regular))
                        .foregroundStyle(isOpen ? HarnessTheme.text : HarnessTheme.dim)
                        .lineLimit(1)
                    HStack(spacing: 5) {
                        Text(SessionDates.label(for: session.updated))
                        Text("·")
                        Text("\(session.turns) \(session.turns == 1 ? "msg" : "msgs")")
                    }
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(HarnessTheme.faint)
                }
                Spacer(minLength: 4)
            }
            .padding(.horizontal, 8)
            .padding(.vertical, 6)
            .contentShape(Rectangle())
            .macHoverRow(cornerRadius: 6, active: isOpen)
            .padding(.horizontal, 8)
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
            Rectangle().fill(HarnessTheme.line).frame(height: 1)

            if let banner = store.bannerMessage ?? supervisor.startupError {
                HStack(spacing: 8) {
                    Image(systemName: "info.circle.fill")
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
                .padding(.horizontal, 18)
                .padding(.vertical, 8)
                .background(HarnessTheme.amber.opacity(0.09))
                Rectangle().fill(HarnessTheme.line).frame(height: 1)
            }

            if store.connectionState != .connected {
                disconnectedOnboardingView
            } else {
                transcriptArea
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
                RoundedRectangle(cornerRadius: 12)
                    .stroke(HarnessTheme.amber, style: StrokeStyle(lineWidth: 2, dash: [7]))
                    .background(HarnessTheme.amber.opacity(0.06))
                    .overlay {
                        Label("Drop image to attach", systemImage: "photo.badge.plus")
                            .font(.headline)
                            .foregroundStyle(HarnessTheme.amber)
                            .padding(.horizontal, 18)
                            .padding(.vertical, 12)
                            .background(HarnessTheme.panel)
                            .overlay(RoundedRectangle(cornerRadius: 8).stroke(HarnessTheme.amberDim, lineWidth: 1))
                            .clipShape(RoundedRectangle(cornerRadius: 8))
                    }
                    .padding(16)
                    .allowsHitTesting(false)
            }
        }
    }

    private var topBar: some View {
        HStack(spacing: 10) {
            if !ui.showSidebar {
                Spacer().frame(width: 68) // clear macOS traffic lights when sidebar is hidden
                Button {
                    withAnimation(.easeInOut(duration: 0.16)) { ui.showSidebar = true }
                } label: {
                    Image(systemName: "sidebar.left")
                        .font(.subheadline)
                        .foregroundStyle(HarnessTheme.dim)
                        .frame(width: 28, height: 28)
                        .macHoverRow(cornerRadius: 6)
                }
                .buttonStyle(.plain)
                .help("Show Sidebar (⌘\\)")
            }

            // Clickable Model Status Pill — opens Settings directly on Model pane
            Button {
                ui.selectedSettingsPane = .model
                openSettings()
            } label: {
                HStack(spacing: 8) {
                    Circle()
                        .fill(statusLEDColor)
                        .frame(width: 7, height: 7)
                        .shadow(color: statusLEDColor.opacity(0.55), radius: 3)
                    Text(store.connectionState == .connected ? store.activeModelName : "Harness Offline")
                        .font(.system(size: 12.5, weight: .semibold, design: .monospaced))
                        .foregroundStyle(HarnessTheme.text)
                        .lineLimit(1)
                        .truncationMode(.middle)
                    if store.isVisionAvailable && store.connectionState == .connected {
                        MacBadge(text: "VISION", color: HarnessTheme.amber)
                    }
                    if store.isDemoMode {
                        MacBadge(text: "DEMO", color: HarnessTheme.sage)
                    } else if store.capabilities?.tls == true {
                        MacBadge(text: "HTTPS", color: HarnessTheme.sage)
                    }
                }
                .padding(.horizontal, 10)
                .padding(.vertical, 5)
                .macHoverRow(cornerRadius: 6)
            }
            .buttonStyle(.plain)
            .help("Click to switch models")

            if let status = store.statusText {
                Text(status)
                    .font(HarnessTheme.monoSmall)
                    .foregroundStyle(HarnessTheme.amber)
                    .lineLimit(1)
            }

            Spacer()

            if store.connectionState == .connected {
                if store.hasUserTurns {
                    Button {
                        Task {
                            let md = await store.exportCurrentMarkdown()
                            saveMarkdownPanel(text: md, suggestedName: "\(store.currentSessionId ?? "harness-chat").md")
                        }
                    } label: {
                        Image(systemName: "square.and.arrow.up")
                            .font(.subheadline)
                            .foregroundStyle(HarnessTheme.dim)
                            .frame(width: 28, height: 28)
                            .macHoverRow(cornerRadius: 6)
                    }
                    .buttonStyle(.plain)
                    .help("Export transcript as Markdown")
                }

                Button {
                    ui.selectedSettingsPane = .remote
                    openSettings()
                } label: {
                    Image(systemName: "qrcode")
                        .font(.subheadline)
                        .foregroundStyle(HarnessTheme.dim)
                        .frame(width: 28, height: 28)
                        .macHoverRow(cornerRadius: 6)
                }
                .buttonStyle(.plain)
                .help("Pair iPhone (QR Code)")

                Button {
                    ui.selectedSettingsPane = .overview
                    openSettings()
                } label: {
                    Image(systemName: "slider.horizontal.3")
                        .font(.subheadline)
                        .foregroundStyle(HarnessTheme.dim)
                        .frame(width: 28, height: 28)
                        .macHoverRow(cornerRadius: 6)
                }
                .buttonStyle(.plain)
                .help("Settings (⌘,)")
            }
        }
        .padding(.horizontal, 14)
        .padding(.top, 10)
        .padding(.bottom, 9)
        .background(HarnessTheme.panel)
    }

    private var shortModelName: String {
        let name = store.activeModelName
        let parts = name.split(separator: "-")
        if parts.count > 2 { return parts.prefix(2).joined(separator: "-") }
        return name
    }

    private var statusLEDColor: Color {
        if store.isStreaming || store.isSwitchingModel || supervisor.isStartingBackend { return HarnessTheme.amber }
        return (store.connectionState == .connected && store.isModelLoaded) ? HarnessTheme.sage : HarnessTheme.rust
    }

    // MARK: - Disconnected & First-Run Onboarding

    private var disconnectedOnboardingView: some View {
        VStack(spacing: 22) {
            Spacer()
            ZStack {
                Circle()
                    .fill(
                        RadialGradient(
                            colors: [HarnessTheme.amber.opacity(0.16), HarnessTheme.amber.opacity(0.04), .clear],
                            center: .center,
                            startRadius: 10,
                            endRadius: 58
                        )
                    )
                    .frame(width: 116, height: 116)
                HarnessBrandMark(size: 56)
            }
            VStack(spacing: 6) {
                Text(supervisor.isStartingBackend ? "Starting Harness on this Mac…" : "Harness backend is stopped")
                    .font(.title3.weight(.semibold))
                    .foregroundStyle(HarnessTheme.text)
                Text("Engine: \(supervisor.source.label)")
                    .font(HarnessTheme.monoSmall)
                    .foregroundStyle(HarnessTheme.dim)
            }

            HStack(spacing: 12) {
                Button {
                    Task { await supervisor.connectOrStart(store: store, autoStart: true, remote: true) }
                } label: {
                    HStack(spacing: 7) {
                        if supervisor.isStartingBackend {
                            ProgressView().controlSize(.small)
                        } else {
                            Image(systemName: "play.fill")
                        }
                        Text(supervisor.isStartingBackend ? "Starting…" : "Start Harness")
                            .fontWeight(.semibold)
                    }
                    .padding(.horizontal, 20)
                    .padding(.vertical, 10)
                    .background(HarnessTheme.amber)
                    .foregroundStyle(HarnessTheme.ink)
                    .clipShape(RoundedRectangle(cornerRadius: 7))
                }
                .buttonStyle(.plain)
                .disabled(supervisor.isStartingBackend)

                Button {
                    Task { await store.enterDemoMode() }
                } label: {
                    Label("Try Offline Demo", systemImage: "sparkles")
                        .padding(.horizontal, 16)
                        .padding(.vertical, 10)
                        .foregroundStyle(HarnessTheme.sage)
                        .background(HarnessTheme.sage.opacity(0.09))
                        .overlay(RoundedRectangle(cornerRadius: 7).stroke(HarnessTheme.sage.opacity(0.4), lineWidth: 1))
                        .clipShape(RoundedRectangle(cornerRadius: 7))
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

                    LazyVStack(alignment: .leading, spacing: 14) {
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
                        Color.clear.frame(height: 16).id("bottom-anchor")
                    }
                    .frame(maxWidth: 760)
                    .frame(maxWidth: .infinity)
                    .padding(.horizontal, 28)
                    .padding(.top, 22)
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

    // MARK: - Atmospheric Empty State

    private var emptyStateView: some View {
        VStack(spacing: 26) {
            Spacer().frame(height: 52)

            VStack(spacing: 12) {
                ZStack {
                    Circle()
                        .fill(
                            RadialGradient(
                                colors: [HarnessTheme.amber.opacity(0.16), HarnessTheme.amber.opacity(0.04), .clear],
                                center: .center,
                                startRadius: 8,
                                endRadius: 52
                            )
                        )
                        .frame(width: 104, height: 104)
                    HarnessBrandMark(size: 52)
                }

                Text("HARNESS")
                    .font(.system(size: 18, weight: .semibold, design: .monospaced))
                    .tracking(4.5)
                    .foregroundStyle(HarnessTheme.text)

                Text(store.isDemoMode
                     ? "Offline Demo Mode — explore streaming replies, tool calls, and diff approvals."
                     : (store.isVisionAvailable
                        ? "Local AI on your Mac · drop or paste a photo, or ask about your files."
                        : "Local AI on your Mac · reads files, runs guarded commands, stays private."))
                    .font(.subheadline)
                    .foregroundStyle(HarnessTheme.dim)
                    .multilineTextAlignment(.center)
            }

            if let models = store.modelsResponse?.models, models.isEmpty, !store.isDemoMode {
                MacPanelCard(border: HarnessTheme.amberDim) {
                    Text("NO GGUF MODELS FOUND YET")
                        .font(HarnessTheme.label)
                        .tracking(1.0)
                        .foregroundStyle(HarnessTheme.amber)
                    Text("Place a `.gguf` model in `~/llm/models` (plus an optional `mmproj*.gguf` for vision), then pick it in Settings → Model.")
                        .font(.subheadline)
                        .foregroundStyle(HarnessTheme.dim)
                    Button {
                        supervisor.openModelsFolderInFinder()
                    } label: {
                        Label("Open ~/llm/models in Finder", systemImage: "folder")
                            .font(.caption.weight(.medium))
                            .padding(.horizontal, 12)
                            .padding(.vertical, 7)
                            .background(HarnessTheme.panel2)
                            .foregroundStyle(HarnessTheme.text)
                            .overlay(RoundedRectangle(cornerRadius: 6).stroke(HarnessTheme.lineHot, lineWidth: 1))
                    }
                    .buttonStyle(.plain)
                }
                .frame(maxWidth: 520)
            } else {
                VStack(spacing: 8) {
                    ForEach(starterPrompts, id: \.title) { starter in
                        StarterCardButton(tag: starter.tag, title: starter.title, prompt: starter.prompt) {
                            store.composerText = starter.prompt
                            Task { await store.sendMessage() }
                        }
                    }
                }
                .frame(maxWidth: 520)
            }
        }
        .frame(maxWidth: .infinity)
        .padding(.horizontal, 28)
        .padding(.bottom, 24)
    }

    // MARK: - Elevated Floating Composer

    private var composerArea: some View {
        VStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 10) {
                if !store.pendingAttachments.isEmpty || store.isUploadingImage {
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 8) {
                            ForEach(store.pendingAttachments) { att in
                                ZStack(alignment: .topTrailing) {
                                    if let img = PlatformImageCodec.image(from: att.previewData) {
                                        Image(platformImage: img)
                                            .resizable()
                                            .scaledToFill()
                                            .frame(width: 58, height: 58)
                                            .clipped()
                                            .clipShape(RoundedRectangle(cornerRadius: 6))
                                            .overlay(RoundedRectangle(cornerRadius: 6).stroke(HarnessTheme.lineHot, lineWidth: 1))
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
                                .frame(width: 58, height: 58)
                                .clipShape(RoundedRectangle(cornerRadius: 6))
                            }
                        }
                    }
                }

                TextField("Ask Harness anything…", text: $store.composerText, axis: .vertical)
                    .textFieldStyle(.plain)
                    .focused($composerFocused)
                    .lineLimit(1...8)
                    .font(.system(size: 14))
                    .foregroundStyle(HarnessTheme.text)
                    .onKeyPress(.return, phases: .down) { press in
                        if press.modifiers.contains(.shift) { return .ignored }
                        guard !store.isStreaming && !store.isUploadingImage else { return .handled }
                        Task { await store.sendMessage() }
                        return .handled
                    }

                // Integrated bottom action strip inside composer card
                HStack(spacing: 8) {
                    if store.isVisionAvailable {
                        composerChip(icon: "photo.badge.plus", label: "Image", active: false) {
                            pickImagesPanel()
                        }
                        .disabled(store.isStreaming || store.isUploadingImage)
                        .help("Attach image (or drag & drop)")

                        composerChip(icon: "doc.on.clipboard", label: "Paste", active: false) {
                            if let data = PlatformClipboard.pasteImageData() {
                                Task { await store.attachImageData(data) }
                            } else {
                                store.bannerMessage = "No image on the clipboard."
                            }
                        }
                        .disabled(store.isStreaming || store.isUploadingImage)
                        .help("Paste image from clipboard (⌘⇧V)")
                    }

                    composerChip(
                        icon: store.thinkFirst ? "brain.head.profile.fill" : "brain.head.profile",
                        label: "Think first",
                        active: store.thinkFirst
                    ) {
                        store.thinkFirst.toggle()
                    }

                    composerChip(
                        icon: "folder",
                        label: store.projectName.isEmpty ? "Project folder" : store.projectName,
                        active: !store.projectName.isEmpty
                    ) {
                        pickProjectFolder()
                    }
                    .help(store.projectDir.isEmpty ? "Choose working folder" : store.projectDir)

                    Spacer()

                    if let ctx = store.capabilities?.nCtx, ctx > 0 {
                        Text("\(ctx / 1024)K")
                            .font(.system(size: 10.5, design: .monospaced))
                            .foregroundStyle(HarnessTheme.faint)
                    }

                    if store.isStreaming {
                        Button {
                            Task { await store.stopTurn() }
                        } label: {
                            HStack(spacing: 5) {
                                Image(systemName: "stop.fill").font(.caption2)
                                Text("Stop").font(.caption.weight(.semibold))
                            }
                            .padding(.horizontal, 12)
                            .padding(.vertical, 6)
                            .background(HarnessTheme.rust)
                            .foregroundStyle(.white)
                            .clipShape(RoundedRectangle(cornerRadius: 7))
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
                                .font(.system(size: 12.5, weight: .bold))
                                .frame(width: 28, height: 28)
                                .background(canSend ? HarnessTheme.amber : HarnessTheme.panel2)
                                .foregroundStyle(canSend ? HarnessTheme.ink : HarnessTheme.faint)
                                .clipShape(RoundedRectangle(cornerRadius: 7))
                        }
                        .buttonStyle(.plain)
                        .disabled(!canSend || store.isUploadingImage)
                        .help("Send (Return · Shift+Return for newline)")
                    }
                }
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 12)
            .background(HarnessTheme.panel)
            .overlay(
                RoundedRectangle(cornerRadius: 12)
                    .stroke(composerFocused ? HarnessTheme.amberDim : HarnessTheme.lineHot, lineWidth: 1)
            )
            .clipShape(RoundedRectangle(cornerRadius: 12))
            .shadow(color: .black.opacity(0.35), radius: 14, y: 4)
            .frame(maxWidth: 760)
            .padding(.horizontal, 24)
            .padding(.top, 8)
            .padding(.bottom, 16)
        }
        .frame(maxWidth: .infinity)
        .background(HarnessTheme.ink)
    }

    private func composerChip(icon: String, label: String, active: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 5) {
                Image(systemName: icon)
                    .font(.system(size: 10.5))
                Text(label)
                    .font(.system(size: 11, weight: active ? .semibold : .regular, design: .monospaced))
                    .lineLimit(1)
            }
            .foregroundStyle(active ? HarnessTheme.amber : HarnessTheme.dim)
            .padding(.horizontal, 8)
            .padding(.vertical, 5)
            .background(active ? HarnessTheme.amber.opacity(0.12) : HarnessTheme.panel2)
            .overlay(RoundedRectangle(cornerRadius: 6).stroke(
                active ? HarnessTheme.amber.opacity(0.45) : HarnessTheme.line, lineWidth: 1
            ))
            .clipShape(RoundedRectangle(cornerRadius: 6))
        }
        .buttonStyle(.plain)
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

private struct StarterCardButton: View {
    let tag: String
    let title: String
    let prompt: String
    let action: () -> Void
    @State private var hovered = false

    var body: some View {
        Button(action: action) {
            HStack(spacing: 14) {
                Text(tag)
                    .font(.system(size: 9.5, weight: .semibold, design: .monospaced))
                    .tracking(1.0)
                    .foregroundStyle(hovered ? HarnessTheme.amber : HarnessTheme.faint)
                    .frame(width: 52, alignment: .leading)
                VStack(alignment: .leading, spacing: 2) {
                    Text(title)
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(HarnessTheme.text)
                    Text(prompt)
                        .font(.caption)
                        .foregroundStyle(HarnessTheme.dim)
                        .lineLimit(1)
                }
                Spacer()
                Image(systemName: "arrow.up.right")
                    .font(.caption2)
                    .foregroundStyle(hovered ? HarnessTheme.amber : HarnessTheme.faint)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 11)
            .background(hovered ? HarnessTheme.panel2 : HarnessTheme.panel)
            .overlay(RoundedRectangle(cornerRadius: 8).stroke(
                hovered ? HarnessTheme.amberDim : HarnessTheme.line, lineWidth: 1
            ))
            .clipShape(RoundedRectangle(cornerRadius: 8))
            .offset(x: hovered ? 2 : 0)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hovered = $0 }
        .animation(.easeOut(duration: 0.14), value: hovered)
    }
}
