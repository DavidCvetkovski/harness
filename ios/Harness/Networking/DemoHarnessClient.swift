import Foundation

/// Self-contained Offline Demo backend for App Store Review (Guideline 2.1) and offline preview.
/// Replays realistic Harness turns — thinking, tool calls, interactive diff approvals, and vision.
final class DemoHarnessClient: HarnessAPI, @unchecked Sendable {
    let baseURL = URL(string: "https://demo.harness.local")!
    let token: String? = nil

    private let lock = NSLock()
    private var cfg = HarnessConfig(
        systemPrompt: "You are a helpful assistant on a macOS machine.\n\nAnswer directly from what you know. Only use a tool when needed.",
        thinking: false,
        temperature: 0.6,
        topP: 0.95,
        topK: 20,
        maxTokens: 4000,
        maxSteps: 12,
        tools: [
            "list_files": true, "find_files": true, "search_in_files": true,
            "read_file": true, "write_file": true, "edit_file": true,
            "web_search": true, "fetch_url": true, "run_command": true,
            "system_status": true, "send_email": true, "view_image": true,
        ],
        extraSafeCommands: [],
        approveFiles: "always",
        approveCommands: "always",
        preset: "balanced",
        projectDir: "/Users/demo/Developer/Harness"
    )

    private var storedSessions: [String: (title: String, updated: String, messages: [SavedMessage])] = [
        "demo-20261124": (
            title: "Check Mac memory and disk space",
            updated: "2026-11-24T19:12:00",
            messages: [
                SavedMessage(role: "user", content: "Is my Mac short on memory right now?", reasoningContent: nil, toolCalls: nil, toolCallId: nil, images: nil, imageUrls: nil),
                SavedMessage(role: "assistant", content: "", reasoningContent: "I should check live memory metrics using system_status.", toolCalls: [
                    SavedToolCall(id: "c1", type: "function", function: ToolFunctionPayload(name: "system_status", arguments: "{}"))
                ], toolCallId: nil, images: nil, imageUrls: nil),
                SavedMessage(role: "tool", content: "Memory: fine.\n  16 GB installed; 29% free; 7.8 GB wired; 1.1 GB compressed; 0.4 GB in swap\nUsing the most memory: llama-server 5.9 GB, Xcode 1.4 GB, Safari 620 MB.\nDisk: 78 GB free of 500 GB (84% used).", reasoningContent: nil, toolCalls: nil, toolCallId: "c1", images: nil, imageUrls: nil),
                SavedMessage(role: "assistant", content: "Your Mac is doing fine — **29% free** out of 16 GB (0.4 GB in swap), with **78 GB free** on disk. The top RAM users are `llama-server` (5.9 GB), `Xcode` (1.4 GB), and `Safari` (620 MB).", reasoningContent: nil, toolCalls: nil, toolCallId: nil, images: nil, imageUrls: nil),
            ]
        )
    ]

    private var storedAutomations: [AutomationEntry] = [
        AutomationEntry(
            id: "demo-disk",
            name: "Disk space check",
            prompt: "Check how much free disk space this Mac has and say whether it is running low.",
            schedule: AutomationSchedule(kind: "daily", minutes: nil, time: "09:00"),
            enabled: true,
            lastRun: "2026-11-25T09:00:02",
            lastResult: "78 GB free out of 500 GB (16% free) — plenty of headroom."
        )
    ]

    private var uploads: [String: Data] = [:]
    private var pendingApprovals: [String: CheckedContinuation<(Bool, Bool), Never>] = [:]
    private var cancelledTurns: Set<String> = []

    func capabilities() async throws -> Capabilities {
        Capabilities(
            version: 1,
            mode: "remote",
            tls: true,
            scheme: "https",
            modelLoaded: true,
            vision: true,
            nCtx: 32768,
            voice: false,
            maxUploadBytes: 26_214_400,
            maxImagesPerMessage: 6,
            tools: Array(cfg.tools.keys).sorted()
        )
    }

    func systemState() async throws -> SystemState {
        SystemState(
            model: ProcessMetric(running: true, pid: 49872, gb: 5.89, name: "Qwen3.5-9B-Vision-Q4_K_M"),
            app: ProcessMetric(running: true, pid: 56498, gb: 0.04, name: nil),
            memoryFree: "29%",
            diskFree: "78Gi",
            uptime: "4 days",
            mode: "remote",
            tls: true,
            url: "https://demo-mac.ts.net:8420",
            tailnet: "100.64.0.42",
            autostart: true
        )
    }

    func accessInfo() async throws -> AccessInfo {
        AccessInfo(
            mode: "remote",
            tls: true,
            scheme: "https",
            fqdn: "demo-mac.ts.net",
            url: "https://demo-mac.ts.net:8420",
            short: "https://demo-mac.ts.net:8420",
            tailnet: "100.64.0.42"
        )
    }

    func models() async throws -> ModelsResponse {
        ModelsResponse(
            models: [
                ModelEntry(name: "Qwen3.5-9B-Vision-Q4_K_M", path: "/models/qwen3.5-9b.gguf", gb: 6.1, estGb: 7.0, ctx: 32768, fit: "ok", vision: true, budgetGb: 10.7, active: true, selected: true),
                ModelEntry(name: "Nanbeige4.2-3B-Q4_K_M", path: "/models/nanbeige-3b.gguf", gb: 2.4, estGb: 4.1, ctx: 16384, fit: "ok", vision: false, budgetGb: 10.7, active: false, selected: false),
            ],
            loaded: true,
            vision: true,
            live: "Qwen3.5-9B-Vision-Q4_K_M"
        )
    }

    func selectModel(path: String) async throws {}
    func config() async throws -> HarnessConfig { lock.withLock { cfg } }

    func updateConfig(_ patch: [String: Any]) async throws -> HarnessConfig {
        lock.withLock {
            if let v = patch["thinking"] as? Bool { cfg.thinking = v }
            if let v = patch["temperature"] as? Double { cfg.temperature = v }
            if let v = patch["max_tokens"] as? Int { cfg.maxTokens = v }
            if let v = patch["max_steps"] as? Int { cfg.maxSteps = v }
            if let v = patch["approve_files"] as? String { cfg.approveFiles = v }
            if let v = patch["approve_commands"] as? String { cfg.approveCommands = v }
            if let v = patch["system_prompt"] as? String { cfg.systemPrompt = v }
            if let v = patch["tools"] as? [String: Bool] { cfg.tools = v }
            if let v = patch["project_dir"] as? String { cfg.projectDir = v }
            return cfg
        }
    }

    func resetConfig() async throws -> HarnessConfig { lock.withLock { cfg } }

    func presets() async throws -> PresetsResponse {
        PresetsResponse(
            presets: [
                PresetInfo(id: "balanced", label: "Balanced", blurb: "Sensible defaults. Can read, search, and write files (asking first)."),
                PresetInfo(id: "safe", label: "Read only", blurb: "Can look at things and search the web, but cannot change anything."),
                PresetInfo(id: "quick", label: "Quick answers", blurb: "Short replies, no web, no file changes. Fastest option."),
                PresetInfo(id: "thorough", label: "Thorough", blurb: "Extended reasoning and more tool steps. Slow but tries harder."),
            ],
            templates: [
                AutomationTemplate(name: "Disk space check", schedule: AutomationSchedule(kind: "daily", minutes: nil, time: "09:00"), prompt: "Check free disk space."),
                AutomationTemplate(name: "Uptime check", schedule: AutomationSchedule(kind: "interval", minutes: 360, time: nil), prompt: "Report uptime and CPU load."),
            ]
        )
    }

    func applyPreset(id: String) async throws -> HarnessConfig {
        lock.withLock {
            cfg.preset = id
            cfg.thinking = (id == "thorough")
            return cfg
        }
    }

    func sessions() async throws -> [SessionSummary] {
        lock.withLock {
            storedSessions.map { key, val in
                let turns = val.messages.filter { $0.role == "user" }.count
                return SessionSummary(id: key, title: val.title, updated: val.updated, turns: turns)
            }.sorted { $0.updated > $1.updated }
        }
    }

    func sessionDetail(id: String) async throws -> SessionDetail {
        let msgs = lock.withLock { storedSessions[id]?.messages ?? [] }
        return SessionDetail(id: id, messages: msgs, running: false, approvals: [])
    }

    func deleteSession(id: String) async throws {
        lock.withLock { _ = storedSessions.removeValue(forKey: id) }
    }

    func rewindSession(id: String) async throws -> String {
        lock.withLock {
            guard var entry = storedSessions[id],
                  let lastUser = entry.messages.lastIndex(where: { $0.role == "user" }) else {
                return ""
            }
            let text = entry.messages[lastUser].content ?? ""
            entry.messages = Array(entry.messages[..<lastUser])
            if entry.messages.isEmpty {
                storedSessions.removeValue(forKey: id)
            } else {
                storedSessions[id] = entry
            }
            return text
        }
    }

    func exportMarkdown(id: String) async throws -> String {
        let detail = try await sessionDetail(id: id)
        return detail.messages.compactMap { msg -> String? in
            guard let text = msg.content, !text.isEmpty else { return nil }
            return msg.role == "user" ? "## You\n\n\(text)" : "## Harness\n\n\(text)"
        }.joined(separator: "\n\n")
    }

    func automations() async throws -> [AutomationEntry] { lock.withLock { storedAutomations } }

    func upsertAutomation(_ entry: [String: Any]) async throws -> AutomationEntry {
        lock.withLock {
            let id = (entry["id"] as? String) ?? UUID().uuidString.prefix(8).lowercased()
            let schedDict = entry["schedule"] as? [String: Any]
            let schedule = AutomationSchedule(
                kind: (schedDict?["kind"] as? String) ?? "manual",
                minutes: schedDict?["minutes"] as? Int,
                time: schedDict?["time"] as? String
            )
            let item = AutomationEntry(
                id: id,
                name: (entry["name"] as? String) ?? "Untitled",
                prompt: (entry["prompt"] as? String) ?? "",
                schedule: schedule,
                enabled: (entry["enabled"] as? Bool) ?? true,
                lastRun: nil,
                lastResult: ""
            )
            if let idx = storedAutomations.firstIndex(where: { $0.id == id }) {
                storedAutomations[idx] = item
            } else {
                storedAutomations.append(item)
            }
            return item
        }
    }

    func deleteAutomation(id: String) async throws {
        lock.withLock { storedAutomations.removeAll { $0.id == id } }
    }

    func runAutomation(id: String) async throws {
        lock.withLock {
            if let idx = storedAutomations.firstIndex(where: { $0.id == id }) {
                storedAutomations[idx].lastRun = ISO8601DateFormatter().string(from: Date())
                storedAutomations[idx].lastResult = "Demo run completed cleanly."
            }
        }
    }

    func systemAction(_ action: String, extra: [String: Any]) async throws {}

    func folders(path: String) async throws -> FoldersResponse {
        let tree: [String: [String]] = [
            "/Users/demo": ["Desktop", "Developer", "Documents", "Downloads", "Pictures"],
            "/Users/demo/Developer": ["Harness", "LyricStats", "Mimic"],
            "/Users/demo/Documents": ["Lightroom", "Thesis"],
        ]
        let base = (path == "~" || path.isEmpty) ? "/Users/demo" : path
        let parent = (base as NSString).deletingLastPathComponent
        return FoldersResponse(path: base, parent: parent.isEmpty ? "/" : parent,
                               folders: tree[base] ?? [])
    }

    func logs(kind: String) async throws -> LogsResponse {
        LogsResponse(name: "\(kind).log", text: "[demo] Harness running smoothly on Apple Silicon.\n[demo] 18.4 tok/s · KV cache reuse 99%.")
    }

    func uploadImage(data: Data, mimeType: String) async throws -> UploadResponse {
        let id = "demo-\(UUID().uuidString.prefix(8).lowercased()).jpg"
        lock.withLock { uploads[id] = data }
        return UploadResponse(id: id, path: "/demo/uploads/\(id)", url: "/api/uploads/\(id)", mime: mimeType, bytes: data.count)
    }

    func fetchData(path: String) async throws -> Data {
        let name = path.split(separator: "/").last.map(String.init) ?? ""
        if let data = lock.withLock({ uploads[name] }) {
            return data
        }
        throw HarnessError.http(404, "Not found")
    }

    func approve(id: String, allow: Bool, always: Bool) async throws {
        let cont = lock.withLock { pendingApprovals.removeValue(forKey: id) }
        cont?.resume(returning: (allow, always))
    }

    func cancel(turnId: String) async throws {
        let all = lock.withLock { () -> [CheckedContinuation<(Bool, Bool), Never>] in
            cancelledTurns.insert(turnId)
            let values = Array(pendingApprovals.values)
            pendingApprovals.removeAll()
            return values
        }
        all.forEach { $0.resume(returning: (false, false)) }
    }

    func voices() async throws -> VoicesResponse {
        VoicesResponse(voices: [], script: nil)
    }

    func selectVoice(id: String) async throws {}
    func speak(text: String) async throws -> Data { throw HarnessError.http(503, "Voice sidecar unavailable in demo mode.") }

    func streamChat(
        sessionId: String?,
        message: String,
        images: [String],
        turnId: String,
        thinking: Bool
    ) -> AsyncThrowingStream<StreamEvent, Error> {
        AsyncThrowingStream { continuation in
            Task {
                let sid = sessionId ?? "demo-\(Int(Date().timeIntervalSince1970))"
                continuation.yield(.session(id: sid))
                let lower = message.lowercased()

                if thinking {
                    for word in ["Let me inspect ", "the local state ", "before answering."] {
                        try? await Task.sleep(nanoseconds: 120_000_000)
                        continuation.yield(.reasoning(text: word))
                    }
                }

                var finalAnswer = ""
                if !images.isEmpty {
                    finalAnswer = "I received **\(images.count) image\(images.count == 1 ? "" : "s")** via `/api/uploads`. In live mode your Mac's local Qwen3.5-9B vision projector inspects the image directly on Apple Silicon without sending pixels to the cloud."
                } else if lower.contains("write") || lower.contains("edit") || lower.contains("script") || lower.contains("fix") {
                    let approvalId = "demo-ask-\(UUID().uuidString.prefix(6))"
                    continuation.yield(.toolStart(name: "write_file", args: ["path": "rename_by_date.py"]))
                    continuation.yield(.approval(ApprovalItem(
                        id: approvalId,
                        question: "Write this file?",
                        kind: "write_file",
                        detail: "  CREATE  /Users/demo/Developer/Harness/rename_by_date.py\n  writing 312 bytes:\n    + import os, time\n    + for name in sorted(os.listdir('.')):\n    +     stamp = time.strftime('%Y-%m-%d', time.localtime(os.path.getmtime(name)))\n    +     print(f'{stamp}-{name}')"
                    )))
                    let (allowed, _) = await withCheckedContinuation { cont in
                        lock.withLock { pendingApprovals[approvalId] = cont }
                    }
                    if lock.withLock({ cancelledTurns.contains(turnId) }) {
                        continuation.yield(.cancelled)
                        continuation.yield(.done(sessionId: sid))
                        continuation.finish()
                        return
                    }
                    if allowed {
                        continuation.yield(.toolResult(name: "write_file", result: "Wrote 312 bytes to /Users/demo/Developer/Harness/rename_by_date.py"))
                        finalAnswer = "Done — saved `rename_by_date.py` after your approval.\n\n```python\nimport os, time\nfor name in sorted(os.listdir('.')):\n    stamp = time.strftime('%Y-%m-%d', time.localtime(os.path.getmtime(name)))\n    print(f'{stamp}-{name}')\n```"
                    } else {
                        continuation.yield(.toolResult(name: "write_file", result: "Error: the user declined to write this file."))
                        finalAnswer = "Understood — nothing was written to disk. Let me know if you want to adjust the script first."
                    }
                } else {
                    continuation.yield(.toolStart(name: "system_status", args: [:]))
                    try? await Task.sleep(nanoseconds: 250_000_000)
                    continuation.yield(.toolResult(name: "system_status", result: "Memory: fine (29% free, 16 GB installed).\nDisk: 78 GB free of 500 GB.\nCPU: idle — load 1.24 on 10 cores."))
                    finalAnswer = "Everything looks healthy on your Mac (**29% RAM free**, **78 GB disk free**, CPU idle). This is **Offline Demo Mode** — connect to your Mac over Tailscale in Settings to run real local tools."
                }

                for chunk in finalAnswer.split(separator: " ") {
                    if lock.withLock({ cancelledTurns.contains(turnId) }) {
                        continuation.yield(.cancelled)
                        break
                    }
                    try? await Task.sleep(nanoseconds: 28_000_000)
                    continuation.yield(.token(text: String(chunk) + " "))
                }

                continuation.yield(.stats(seconds: 1.4, tokens: 64, steps: 2))
                lock.withLock {
                    var msgs = storedSessions[sid]?.messages ?? []
                    let urls = images.map { "/api/uploads/\($0)" }
                    msgs.append(SavedMessage(role: "user", content: message, reasoningContent: nil, toolCalls: nil, toolCallId: nil, images: images.isEmpty ? nil : images, imageUrls: urls.isEmpty ? nil : urls))
                    msgs.append(SavedMessage(role: "assistant", content: finalAnswer, reasoningContent: nil, toolCalls: nil, toolCallId: nil, images: nil, imageUrls: nil))
                    storedSessions[sid] = (title: String(message.prefix(60)), updated: ISO8601DateFormatter().string(from: Date()), messages: msgs)
                }
                continuation.yield(.done(sessionId: sid))
                continuation.finish()
            }
        }
    }
}
