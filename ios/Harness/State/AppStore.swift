import AVFoundation
import Foundation
import Observation

enum ApprovalStatus: Equatable {
    case pending
    case allowed
    case denied
    case timedOut
}

enum TranscriptItem: Identifiable, Equatable {
    case user(id: UUID, text: String, imageUrls: [String], localImages: [Data])
    case thinking(id: UUID, text: String, startedAt: Date, finishedAt: Date?, isExpanded: Bool)
    case tool(id: UUID, name: String, args: [String: String], output: String?, isError: Bool, isRunning: Bool, isExpanded: Bool)
    case approval(id: String, item: ApprovalItem, status: ApprovalStatus)
    case assistant(id: UUID, text: String, isStreaming: Bool)
    case error(id: UUID, text: String)
    case cost(id: UUID, seconds: Double, tokens: Int, steps: Int)

    var id: String {
        switch self {
        case .user(let id, _, _, _): return "u-\(id.uuidString)"
        case .thinking(let id, _, _, _, _): return "th-\(id.uuidString)"
        case .tool(let id, _, _, _, _, _, _): return "tl-\(id.uuidString)"
        case .approval(let id, _, _): return "ap-\(id)"
        case .assistant(let id, _, _): return "as-\(id.uuidString)"
        case .error(let id, _): return "er-\(id.uuidString)"
        case .cost(let id, _, _, _): return "co-\(id.uuidString)"
        }
    }
}

struct PendingAttachment: Identifiable, Equatable {
    let id: String
    let url: String
    let previewData: Data
}

enum ConnectionState: Equatable {
    case disconnected
    case connecting
    case connected
}

@MainActor
@Observable
final class AppStore {
    // Connection & persistence
    var connectionState: ConnectionState = .disconnected
    var isDemoMode: Bool = false
    var serverURLInput: String = ""
    var tokenInput: String = ""
    var connectionError: String?
    var bannerMessage: String?

    // Server metadata
    var capabilities: Capabilities?
    var systemState: SystemState?
    var accessInfo: AccessInfo?
    var modelsResponse: ModelsResponse?
    var config: HarnessConfig?
    var presetsResponse: PresetsResponse?
    var sessions: [SessionSummary] = []
    var automations: [AutomationEntry] = []
    var voices: [VoiceEntry] = []

    // Active chat state
    var currentSessionId: String?
    var transcript: [TranscriptItem] = []
    var composerText: String = ""
    var thinkFirst: Bool = false
    var pendingAttachments: [PendingAttachment] = []
    var isUploadingImage: Bool = false
    var isStreaming: Bool = false
    var isSwitchingModel: Bool = false
    var statusText: String?
    var speakingItemId: String?

    // Navigation sheets
    var showSessionsSheet: Bool = false
    var showSettingsSheet: Bool = false
    var showQRScanner: Bool = false

    // Optional hooks for native platform notifications (e.g. macOS UNUserNotificationCenter).
    @ObservationIgnored var onApprovalNeeded: ((ApprovalItem) -> Void)?
    @ObservationIgnored var onTurnFinished: ((String) -> Void)?

    private(set) var client: HarnessAPI?
    private var streamTask: Task<Void, Never>?
    private var pollTask: Task<Void, Never>?
    private var activeTurnId: String?
    private var audioPlayer: AVAudioPlayer?
    private let imageCache = NSCache<NSString, PlatformImage>()

    private let defaults: UserDefaults
    private let urlKey = "harness.server.url"
    private let demoKey = "harness.demo.mode"

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        self.serverURLInput = defaults.string(forKey: urlKey) ?? ""
        self.isDemoMode = defaults.bool(forKey: demoKey)
        self.tokenInput = KeychainStore.loadToken() ?? ""
    }

    var isVisionAvailable: Bool {
        capabilities?.vision ?? modelsResponse?.vision ?? false
    }

    var isVoiceAvailable: Bool {
        (capabilities?.voice ?? false) && !voices.isEmpty
    }

    var isModelLoaded: Bool {
        modelsResponse?.loaded ?? capabilities?.modelLoaded ?? false
    }

    var activeModelName: String {
        if let live = modelsResponse?.live, !live.isEmpty { return live }
        if let sysName = systemState?.model.name, !sysName.isEmpty { return sysName }
        return isModelLoaded ? "Model ready" : "No model loaded"
    }

    var maxImagesPerMessage: Int {
        capabilities?.maxImagesPerMessage ?? 6
    }

    var hasUserTurns: Bool {
        transcript.contains {
            if case .user = $0 { return true }
            return false
        }
    }

    /// The folder Harness treats as "here" — relative paths and "this folder"
    /// in a prompt resolve against it. Empty when none is set.
    var projectDir: String {
        config?.projectDir ?? ""
    }

    /// Just the last path component, for the composer chip.
    var projectName: String {
        let trimmed = projectDir.hasSuffix("/") ? String(projectDir.dropLast()) : projectDir
        return trimmed.split(separator: "/").last.map(String.init) ?? ""
    }

    // MARK: - Project Folder

    func browseFolders(path: String) async throws -> FoldersResponse {
        guard let client else { throw HarnessError.network("Not connected.") }
        return try await client.folders(path: path)
    }

    func setProjectDir(_ path: String) async {
        await updateConfig(["project_dir": path])
    }

    // MARK: - Connection Lifecycle

    func restoreConnectionIfSaved() async {
        if isDemoMode {
            await enterDemoMode()
            return
        }
        guard !serverURLInput.isEmpty else { return }
        await connect(rawURL: serverURLInput, explicitToken: tokenInput)
    }

    func connect(rawURL: String, explicitToken: String? = nil) async {
        connectionError = nil
        guard let parsed = ConnectionParser.parse(rawURL) else {
            connectionError = "Enter a valid Harness address (for example, my-mac.ts.net:8420 or 100.x.x.x:8420)."
            return
        }
        let chosenToken = parsed.token ?? (explicitToken?.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty == false ? explicitToken?.trimmingCharacters(in: .whitespacesAndNewlines) : nil)

        connectionState = .connecting
        let liveClient = LiveHarnessClient(baseURL: parsed.baseURL, token: chosenToken)
        do {
            let caps = try await liveClient.capabilities()
            self.client = liveClient
            self.capabilities = caps
            self.isDemoMode = false
            self.serverURLInput = parsed.baseURL.absoluteString
            self.tokenInput = chosenToken ?? ""
            defaults.set(parsed.baseURL.absoluteString, forKey: urlKey)
            defaults.set(false, forKey: demoKey)
            KeychainStore.saveToken(chosenToken)
            connectionState = .connected
            await refreshAll()
        } catch {
            connectionState = .disconnected
            connectionError = error.localizedDescription
        }
    }

    func enterDemoMode() async {
        connectionError = nil
        connectionState = .connecting
        let demo = DemoHarnessClient()
        self.client = demo
        self.isDemoMode = true
        defaults.set(true, forKey: demoKey)
        do {
            self.capabilities = try await demo.capabilities()
            connectionState = .connected
            await refreshAll()
        } catch {
            connectionState = .disconnected
            connectionError = error.localizedDescription
        }
    }

    /// Used by unit tests to inject a custom HarnessAPI directly.
    func attachClientForTesting(_ customClient: HarnessAPI, demo: Bool = true) async {
        self.client = customClient
        self.isDemoMode = demo
        self.capabilities = try? await customClient.capabilities()
        self.connectionState = .connected
        await refreshAll()
    }

    func disconnect() {
        streamTask?.cancel()
        pollTask?.cancel()
        stopAudio()
        client = nil
        connectionState = .disconnected
        isDemoMode = false
        defaults.set(false, forKey: demoKey)
        currentSessionId = nil
        transcript = []
        pendingAttachments = []
        isStreaming = false
        statusText = nil
        bannerMessage = nil
    }

    func refreshAll() async {
        guard let client else { return }
        async let capsTask = try? client.capabilities()
        async let modelsTask = try? client.models()
        async let cfgTask = try? client.config()
        async let sessionsTask = try? client.sessions()
        async let systemTask = try? client.systemState()
        async let accessTask = try? client.accessInfo()
        async let presetsTask = try? client.presets()
        async let autosTask = try? client.automations()

        if let c = await capsTask { capabilities = c }
        if let m = await modelsTask { modelsResponse = m }
        if let cf = await cfgTask {
            config = cf
            thinkFirst = cf.thinking
        }
        if let s = await sessionsTask { sessions = s }
        if let sys = await systemTask { systemState = sys }
        if let acc = await accessTask { accessInfo = acc }
        if let pre = await presetsTask { presetsResponse = pre }
        if let aut = await autosTask { automations = aut }

        if capabilities?.voice == true, let v = try? await client.voices() {
            voices = v.voices
        } else {
            voices = []
        }
    }

    func refreshSystemAndModels() async {
        guard let client else { return }
        if let c = try? await client.capabilities() { capabilities = c }
        if let m = try? await client.models() { modelsResponse = m }
        if let s = try? await client.systemState() { systemState = s }
        if let a = try? await client.accessInfo() { accessInfo = a }
    }

    // MARK: - Background / Foreground Recovery

    func handleScenePhaseActive() async {
        guard connectionState == .connected, client != nil else { return }
        await refreshSystemAndModels()

        // If the OS suspended an SSE stream while the screen was locked, re-sync the
        // session from disk/live memory so finished replies and pending approvals appear.
        guard let sid = currentSessionId else { return }
        if !isStreaming || streamTask == nil {
            await syncSessionFromServer(id: sid)
        }
    }

    private func syncSessionFromServer(id: String) async {
        guard let client else { return }
        guard let detail = try? await client.sessionDetail(id: id) else { return }
        applySessionDetail(detail)
        if detail.running {
            isStreaming = true
            statusText = "Finishing in background…"
            startPollingRunningSession(id: id)
        } else {
            isStreaming = false
            statusText = nil
        }
    }

    private func startPollingRunningSession(id: String) {
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 1_500_000_000)
                guard let self, let client = self.client else { break }
                guard self.currentSessionId == id else { break }
                guard let detail = try? await client.sessionDetail(id: id) else { continue }
                self.applySessionDetail(detail)
                if !detail.running {
                    self.isStreaming = false
                    self.statusText = nil
                    await self.refreshSessionsList()
                    break
                }
            }
        }
    }

    // MARK: - Sessions

    func newChat() {
        streamTask?.cancel()
        pollTask?.cancel()
        stopAudio()
        currentSessionId = nil
        transcript = []
        pendingAttachments = []
        isStreaming = false
        statusText = nil
        bannerMessage = nil
    }

    func refreshSessionsList() async {
        guard let client else { return }
        if let list = try? await client.sessions() {
            sessions = list
        }
    }

    func openSession(id: String) async {
        guard let client else { return }
        streamTask?.cancel()
        pollTask?.cancel()
        stopAudio()
        isStreaming = false
        statusText = nil
        do {
            let detail = try await client.sessionDetail(id: id)
            currentSessionId = detail.id
            applySessionDetail(detail)
            if detail.running {
                isStreaming = true
                statusText = "Running on Mac…"
                startPollingRunningSession(id: id)
            }
        } catch {
            bannerMessage = error.localizedDescription
        }
    }

    func deleteSession(id: String) async {
        guard let client else { return }
        do {
            try await client.deleteSession(id: id)
            sessions.removeAll { $0.id == id }
            if currentSessionId == id {
                newChat()
            }
        } catch {
            bannerMessage = error.localizedDescription
        }
    }

    func rewindAndRetry() async {
        guard let client, let sid = currentSessionId, !isStreaming else { return }
        do {
            let prompt = try await client.rewindSession(id: sid)
            guard !prompt.isEmpty else { return }
            if let detail = try? await client.sessionDetail(id: sid) {
                applySessionDetail(detail)
            }
            composerText = prompt
            await sendMessage()
        } catch {
            bannerMessage = error.localizedDescription
        }
    }

    func rewindAndEdit() async {
        guard let client, let sid = currentSessionId, !isStreaming else { return }
        do {
            let prompt = try await client.rewindSession(id: sid)
            guard !prompt.isEmpty else { return }
            if let detail = try? await client.sessionDetail(id: sid) {
                applySessionDetail(detail)
            }
            composerText = prompt
        } catch {
            bannerMessage = error.localizedDescription
        }
    }

    func exportCurrentMarkdown() async -> String {
        guard let client, let sid = currentSessionId else {
            return buildLocalMarkdown()
        }
        return (try? await client.exportMarkdown(id: sid)) ?? buildLocalMarkdown()
    }

    private func buildLocalMarkdown() -> String {
        transcript.compactMap { item -> String? in
            switch item {
            case .user(_, let text, _, _): return "## You\n\n\(text)"
            case .assistant(_, let text, _): return "## Harness\n\n\(text)"
            default: return nil
            }
        }.joined(separator: "\n\n")
    }

    private func applySessionDetail(_ detail: SessionDetail) {
        var items: [TranscriptItem] = []
        var toolOutputsByCallId: [String: String] = [:]
        for msg in detail.messages where msg.role == "tool" {
            if let cid = msg.toolCallId {
                toolOutputsByCallId[cid] = msg.content ?? ""
            }
        }

        for msg in detail.messages {
            switch msg.role {
            case "user":
                items.append(.user(
                    id: UUID(),
                    text: msg.content ?? "",
                    imageUrls: msg.imageUrls ?? [],
                    localImages: []
                ))
            case "assistant":
                if let reasoning = msg.reasoningContent, !reasoning.isEmpty {
                    let now = Date()
                    items.append(.thinking(id: UUID(), text: reasoning, startedAt: now, finishedAt: now, isExpanded: false))
                }
                if let calls = msg.toolCalls {
                    for call in calls {
                        let argsDict = parseToolArgs(call.function.arguments)
                        let out = call.id.flatMap { toolOutputsByCallId[$0] }
                        let isErr = (out ?? "").hasPrefix("Error")
                        items.append(.tool(
                            id: UUID(),
                            name: call.function.name,
                            args: argsDict,
                            output: out,
                            isError: isErr,
                            isRunning: out == nil && detail.running,
                            isExpanded: false
                        ))
                    }
                }
                if let content = msg.content, !content.isEmpty {
                    items.append(.assistant(id: UUID(), text: content, isStreaming: false))
                }
            default:
                break
            }
        }

        for approval in detail.approvals {
            items.append(.approval(id: approval.id, item: approval, status: .pending))
        }
        transcript = items
    }

    private func parseToolArgs(_ raw: String) -> [String: String] {
        guard let data = raw.data(using: .utf8),
              let obj = try? JSONDecoder().decode([String: AnyCodableValue].self, from: data) else {
            return [:]
        }
        return obj.mapValues(\.displayString)
    }

    // MARK: - Image Attachments & Authenticated Image Loading

    func attachImageData(_ rawData: Data) async {
        guard let client else { return }
        guard pendingAttachments.count < maxImagesPerMessage else {
            bannerMessage = "Up to \(maxImagesPerMessage) images per message."
            return
        }
        isUploadingImage = true
        defer { isUploadingImage = false }

        // Resize & JPEG-compress on-device so cellular/Tailscale uploads stay snappy.
        let prepared = PlatformImageCodec.prepareJPEG(from: rawData, maxEdge: 1600, quality: 0.85) ?? rawData
        let mime = Self.detectMime(prepared)
        do {
            let uploaded = try await client.uploadImage(data: prepared, mimeType: mime)
            pendingAttachments.append(PendingAttachment(id: uploaded.id, url: uploaded.url, previewData: prepared))
            if let img = PlatformImageCodec.image(from: prepared) {
                imageCache.setObject(img, forKey: uploaded.url as NSString)
            }
        } catch {
            bannerMessage = "Image upload failed: \(error.localizedDescription)"
        }
    }

    func removeAttachment(id: String) {
        pendingAttachments.removeAll { $0.id == id }
    }

    func loadImage(path: String) async -> PlatformImage? {
        if let cached = imageCache.object(forKey: path as NSString) {
            return cached
        }
        guard let client, let data = try? await client.fetchData(path: path),
              let image = PlatformImageCodec.image(from: data) else {
            return nil
        }
        imageCache.setObject(image, forKey: path as NSString)
        return image
    }

    private static func detectMime(_ data: Data) -> String {
        if data.starts(with: [0x89, 0x50, 0x4E, 0x47]) { return "image/png" }
        if data.starts(with: [0xFF, 0xD8, 0xFF]) { return "image/jpeg" }
        return "image/jpeg"
    }

    // MARK: - Chat Streaming

    func sendMessage() async {
        guard let client, !isStreaming else { return }
        let text = composerText.trimmingCharacters(in: .whitespacesAndNewlines)
        let attachments = pendingAttachments
        guard !text.isEmpty || !attachments.isEmpty else { return }

        let promptText = text.isEmpty ? "What is in this image?" : text
        composerText = ""
        pendingAttachments = []
        bannerMessage = nil

        transcript.append(.user(
            id: UUID(),
            text: promptText,
            imageUrls: attachments.map(\.url),
            localImages: attachments.map(\.previewData)
        ))

        let turnId = UUID().uuidString.lowercased()
        activeTurnId = turnId
        isStreaming = true
        statusText = thinkFirst ? "Thinking…" : "Working…"
        pollTask?.cancel()

        let imageIds = attachments.map(\.id)
        let sessionId = currentSessionId
        let shouldThink = thinkFirst

        let stream = client.streamChat(
            sessionId: sessionId,
            message: promptText,
            images: imageIds,
            turnId: turnId,
            thinking: shouldThink
        )

        streamTask?.cancel()
        streamTask = Task { [weak self] in
            guard let self else { return }
            do {
                for try await event in stream {
                    self.handleStreamEvent(event)
                }
            } catch {
                if !Task.isCancelled {
                    // If the socket dropped (for example screen lock), try re-syncing
                    // the session before displaying a hard connection error.
                    if let sid = self.currentSessionId,
                       let detail = try? await client.sessionDetail(id: sid) {
                        self.applySessionDetail(detail)
                        if detail.running {
                            self.statusText = "Reconnected — finishing on Mac…"
                            self.startPollingRunningSession(id: sid)
                            return
                        }
                    } else {
                        self.transcript.append(.error(id: UUID(), text: error.localizedDescription))
                    }
                }
            }
            self.finishStreamingCleanup()
            await self.refreshSessionsList()
        }
    }

    func stopTurn() async {
        let turnId = activeTurnId
        streamTask?.cancel()
        pollTask?.cancel()
        if let turnId, let client {
            try? await client.cancel(turnId: turnId)
        }
        finishStreamingCleanup()
    }

    func respondToApproval(id: String, allow: Bool, always: Bool) async {
        guard let client else { return }
        updateApprovalStatus(id: id, status: allow ? .allowed : .denied)
        do {
            try await client.approve(id: id, allow: allow, always: always)
        } catch {
            bannerMessage = error.localizedDescription
        }
    }

    func toggleExpanded(itemId: String) {
        guard let idx = transcript.firstIndex(where: { $0.id == itemId }) else { return }
        switch transcript[idx] {
        case .thinking(let id, let text, let started, let finished, let expanded):
            transcript[idx] = .thinking(id: id, text: text, startedAt: started, finishedAt: finished, isExpanded: !expanded)
        case .tool(let id, let name, let args, let out, let isErr, let isRun, let expanded):
            transcript[idx] = .tool(id: id, name: name, args: args, output: out, isError: isErr, isRunning: isRun, isExpanded: !expanded)
        default:
            break
        }
    }

    private func handleStreamEvent(_ event: StreamEvent) {
        switch event {
        case .session(let sid):
            if !sid.isEmpty { currentSessionId = sid }

        case .reasoning(let chunk):
            statusText = "Thinking…"
            if case .thinking(let id, let existing, let started, _, let expanded) = transcript.last {
                transcript[transcript.count - 1] = .thinking(
                    id: id,
                    text: existing + chunk,
                    startedAt: started,
                    finishedAt: nil,
                    isExpanded: expanded
                )
            } else {
                transcript.append(.thinking(id: UUID(), text: chunk, startedAt: Date(), finishedAt: nil, isExpanded: false))
            }

        case .token(let chunk):
            statusText = "Responding…"
            closeActiveThinkingBlock()
            if case .assistant(let id, let existing, _) = transcript.last {
                transcript[transcript.count - 1] = .assistant(id: id, text: existing + chunk, isStreaming: true)
            } else {
                transcript.append(.assistant(id: UUID(), text: chunk, isStreaming: true))
            }

        case .building(let bytes, _):
            statusText = "Preparing tool call (\(bytes) B)…"

        case .toolStart(let name, let args):
            statusText = "Running \(name)…"
            closeActiveThinkingBlock()
            finalizeActiveAssistantBlock()
            transcript.append(.tool(
                id: UUID(),
                name: name,
                args: args,
                output: nil,
                isError: false,
                isRunning: true,
                isExpanded: false
            ))

        case .toolResult(let name, let result):
            statusText = "Working…"
            let isErr = result.hasPrefix("Error")
            if let idx = transcript.lastIndex(where: {
                if case .tool(_, let n, _, _, _, let running, _) = $0 {
                    return n == name && running
                }
                return false
            }) {
                if case .tool(let id, let n, let args, _, _, _, let expanded) = transcript[idx] {
                    transcript[idx] = .tool(
                        id: id,
                        name: n,
                        args: args,
                        output: result,
                        isError: isErr,
                        isRunning: false,
                        isExpanded: expanded
                    )
                }
            } else {
                transcript.append(.tool(id: UUID(), name: name, args: [:], output: result, isError: isErr, isRunning: false, isExpanded: false))
            }

        case .approval(let item):
            statusText = "Waiting for approval…"
            transcript.append(.approval(id: item.id, item: item, status: .pending))
            onApprovalNeeded?(item)

        case .approvalResolved(let id, let allowed, let timedOut):
            let newStatus: ApprovalStatus = timedOut ? .timedOut : (allowed ? .allowed : .denied)
            updateApprovalStatus(id: id, status: newStatus)
            statusText = "Working…"

        case .error(let message):
            closeActiveThinkingBlock()
            finalizeActiveAssistantBlock()
            transcript.append(.error(id: UUID(), text: message))

        case .cancelled:
            closeActiveThinkingBlock()
            finalizeActiveAssistantBlock()
            transcript.append(.error(id: UUID(), text: "Stopped."))

        case .stats(let seconds, let tokens, let steps):
            closeActiveThinkingBlock()
            finalizeActiveAssistantBlock()
            transcript.append(.cost(id: UUID(), seconds: seconds, tokens: tokens, steps: steps))

        case .done(let sid):
            if !sid.isEmpty { currentSessionId = sid }
            finishStreamingCleanup()
            if case .assistant(_, let text, _) = transcript.last(where: {
                if case .assistant = $0 { return true }
                return false
            }) {
                onTurnFinished?(text)
            }
        }
    }

    private func updateApprovalStatus(id: String, status: ApprovalStatus) {
        guard let idx = transcript.firstIndex(where: {
            if case .approval(let apId, _, _) = $0 { return apId == id }
            return false
        }) else { return }
        if case .approval(let apId, let item, _) = transcript[idx] {
            transcript[idx] = .approval(id: apId, item: item, status: status)
        }
    }

    private func closeActiveThinkingBlock() {
        for idx in transcript.indices {
            if case .thinking(let id, let text, let started, nil, let expanded) = transcript[idx] {
                transcript[idx] = .thinking(id: id, text: text, startedAt: started, finishedAt: Date(), isExpanded: expanded)
            }
        }
    }

    private func finalizeActiveAssistantBlock() {
        for idx in transcript.indices {
            if case .assistant(let id, let text, true) = transcript[idx] {
                transcript[idx] = .assistant(id: id, text: text, isStreaming: false)
            }
        }
    }

    private func finishStreamingCleanup() {
        closeActiveThinkingBlock()
        finalizeActiveAssistantBlock()
        isStreaming = false
        statusText = nil
        activeTurnId = nil
    }

    // MARK: - Voice Playback

    func toggleSpeak(itemId: String, text: String) async {
        if speakingItemId == itemId {
            stopAudio()
            return
        }
        guard let client else { return }
        stopAudio()
        speakingItemId = itemId
        do {
            let wavData = try await client.speak(text: text)
            let player = try AVAudioPlayer(data: wavData)
            audioPlayer = player
            player.prepareToPlay()
            player.play()
            Task { [weak self] in
                while let p = self?.audioPlayer, p.isPlaying {
                    try? await Task.sleep(nanoseconds: 250_000_000)
                }
                if self?.speakingItemId == itemId {
                    self?.speakingItemId = nil
                }
            }
        } catch {
            speakingItemId = nil
            bannerMessage = error.localizedDescription
        }
    }

    func stopAudio() {
        audioPlayer?.stop()
        audioPlayer = nil
        speakingItemId = nil
    }

    // MARK: - Settings, Models, Automations

    func selectModel(path: String) async {
        guard let client else { return }
        isSwitchingModel = true
        defer { isSwitchingModel = false }
        do {
            try await client.selectModel(path: path)
            // Poll briefly until llama-server finishes reloading weights.
            for _ in 0..<40 {
                try? await Task.sleep(nanoseconds: 1_500_000_000)
                await refreshSystemAndModels()
                if modelsResponse?.loaded == true,
                   modelsResponse?.models.first(where: { $0.path == path })?.active == true {
                    break
                }
            }
        } catch {
            bannerMessage = error.localizedDescription
        }
    }

    func updateConfig(_ patch: [String: Any]) async {
        guard let client else { return }
        do {
            let updated = try await client.updateConfig(patch)
            config = updated
            if patch["thinking"] != nil {
                thinkFirst = updated.thinking
            }
        } catch {
            bannerMessage = error.localizedDescription
        }
    }

    func applyPreset(id: String) async {
        guard let client else { return }
        do {
            let updated = try await client.applyPreset(id: id)
            config = updated
            thinkFirst = updated.thinking
        } catch {
            bannerMessage = error.localizedDescription
        }
    }

    func resetConfig() async {
        guard let client else { return }
        do {
            let updated = try await client.resetConfig()
            config = updated
            thinkFirst = updated.thinking
        } catch {
            bannerMessage = error.localizedDescription
        }
    }

    func saveAutomation(_ payload: [String: Any]) async {
        guard let client else { return }
        do {
            _ = try await client.upsertAutomation(payload)
            automations = try await client.automations()
        } catch {
            bannerMessage = error.localizedDescription
        }
    }

    func deleteAutomation(id: String) async {
        guard let client else { return }
        do {
            try await client.deleteAutomation(id: id)
            automations.removeAll { $0.id == id }
        } catch {
            bannerMessage = error.localizedDescription
        }
    }

    func runAutomation(id: String) async {
        guard let client else { return }
        do {
            try await client.runAutomation(id: id)
            bannerMessage = "Automation started on Mac."
            try? await Task.sleep(nanoseconds: 1_000_000_000)
            automations = (try? await client.automations()) ?? automations
        } catch {
            bannerMessage = error.localizedDescription
        }
    }

    func runSystemAction(_ action: String, extra: [String: Any] = [:]) async {
        guard let client else { return }
        do {
            try await client.systemAction(action, extra: extra)
            bannerMessage = "Sent \(action) to Mac."
            try? await Task.sleep(nanoseconds: 1_500_000_000)
            await refreshSystemAndModels()
        } catch {
            bannerMessage = error.localizedDescription
        }
    }

    func fetchLogs(kind: String) async -> String {
        guard let client else { return "(not connected)" }
        return (try? await client.logs(kind: kind).text) ?? "(could not load logs)"
    }

    func selectVoice(id: String) async {
        guard let client else { return }
        do {
            try await client.selectVoice(id: id)
            voices = (try? await client.voices().voices) ?? voices
        } catch {
            bannerMessage = error.localizedDescription
        }
    }
}
