import Foundation

// MARK: - Server Handshake & System

struct Capabilities: Codable, Equatable {
    let version: Int
    let mode: String
    let tls: Bool
    let scheme: String
    let modelLoaded: Bool
    let vision: Bool
    let nCtx: Int
    let voice: Bool
    let maxUploadBytes: Int
    let maxImagesPerMessage: Int
    let tools: [String]

    enum CodingKeys: String, CodingKey {
        case version, mode, tls, scheme, vision, voice, tools
        case modelLoaded = "model_loaded"
        case nCtx = "n_ctx"
        case maxUploadBytes = "max_upload_bytes"
        case maxImagesPerMessage = "max_images_per_message"
    }
}

struct ProcessMetric: Codable, Equatable {
    let running: Bool
    let pid: Int?
    let gb: Double
    let name: String?
}

struct SystemState: Codable, Equatable {
    let model: ProcessMetric
    let app: ProcessMetric
    let memoryFree: String
    let diskFree: String
    let uptime: String
    let mode: String
    let tls: Bool?
    let url: String
    let tailnet: String
    let autostart: Bool

    enum CodingKeys: String, CodingKey {
        case model, app, uptime, mode, tls, url, tailnet, autostart
        case memoryFree = "memory_free"
        case diskFree = "disk_free"
    }
}

struct AccessInfo: Codable, Equatable {
    let mode: String
    let tls: Bool?
    let scheme: String?
    let fqdn: String?
    let url: String
    let short: String
    let tailnet: String
}

// MARK: - Models & Config

struct ModelEntry: Codable, Identifiable, Equatable {
    let name: String
    let path: String
    let gb: Double
    let estGb: Double
    let ctx: Int
    let fit: String
    let vision: Bool
    let budgetGb: Double
    let active: Bool
    let selected: Bool

    var id: String { path }

    enum CodingKeys: String, CodingKey {
        case name, path, gb, ctx, fit, vision, active, selected
        case estGb = "est_gb"
        case budgetGb = "budget_gb"
    }
}

struct ModelsResponse: Codable, Equatable {
    let models: [ModelEntry]
    let loaded: Bool
    let vision: Bool?
    let live: String
}

struct HarnessConfig: Codable, Equatable {
    var systemPrompt: String
    var thinking: Bool
    var temperature: Double
    var topP: Double
    var topK: Int
    var maxTokens: Int
    var maxSteps: Int
    var tools: [String: Bool]
    var extraSafeCommands: [String]
    var approveFiles: String
    var approveCommands: String
    var preset: String
    var projectDir: String

    enum CodingKeys: String, CodingKey {
        case thinking, temperature, tools, preset
        case systemPrompt = "system_prompt"
        case topP = "top_p"
        case topK = "top_k"
        case maxTokens = "max_tokens"
        case maxSteps = "max_steps"
        case extraSafeCommands = "extra_safe_commands"
        case approveFiles = "approve_files"
        case approveCommands = "approve_commands"
        case projectDir = "project_dir"
    }
}

struct PresetInfo: Codable, Identifiable, Equatable {
    let id: String
    let label: String
    let blurb: String
}

struct AutomationSchedule: Codable, Equatable {
    var kind: String
    var minutes: Int?
    var time: String?

    var summary: String {
        switch kind {
        case "interval":
            let mins = minutes ?? 60
            return mins % 60 == 0 ? "Every \(mins / 60)h" : "Every \(mins) min"
        case "daily":
            return "Daily \(time ?? "09:00")"
        default:
            return "On demand"
        }
    }
}

struct AutomationTemplate: Codable, Identifiable, Equatable {
    let name: String
    let schedule: AutomationSchedule
    let prompt: String
    var id: String { name }
}

struct PresetsResponse: Codable, Equatable {
    let presets: [PresetInfo]
    let templates: [AutomationTemplate]
}

struct AutomationEntry: Codable, Identifiable, Equatable {
    let id: String
    var name: String
    var prompt: String
    var schedule: AutomationSchedule
    var enabled: Bool
    var lastRun: String?
    var lastResult: String?

    enum CodingKeys: String, CodingKey {
        case id, name, prompt, schedule, enabled
        case lastRun = "last_run"
        case lastResult = "last_result"
    }
}

// MARK: - Sessions, Messages, Approvals, Uploads

struct SessionSummary: Codable, Identifiable, Equatable {
    let id: String
    let title: String
    let updated: String
    let turns: Int
}

struct ToolFunctionPayload: Codable, Equatable {
    let name: String
    let arguments: String
}

struct SavedToolCall: Codable, Equatable {
    let id: String?
    let type: String?
    let function: ToolFunctionPayload
}

struct SavedMessage: Codable, Equatable {
    let role: String
    let content: String?
    let reasoningContent: String?
    let toolCalls: [SavedToolCall]?
    let toolCallId: String?
    let images: [String]?
    let imageUrls: [String]?

    enum CodingKeys: String, CodingKey {
        case role, content, images
        case reasoningContent = "reasoning_content"
        case toolCalls = "tool_calls"
        case toolCallId = "tool_call_id"
        case imageUrls = "image_urls"
    }
}

struct ApprovalItem: Codable, Identifiable, Equatable {
    let id: String
    let question: String
    let kind: String
    let detail: String
}

struct SessionDetail: Codable, Equatable {
    let id: String
    let messages: [SavedMessage]
    let running: Bool
    let approvals: [ApprovalItem]
}

struct UploadResponse: Codable, Equatable {
    let id: String
    let path: String
    let url: String
    let mime: String
    let bytes: Int
}

struct RewindResponse: Codable, Equatable {
    let message: String
}

struct LogsResponse: Codable, Equatable {
    let name: String
    let text: String
}

/// One level of the Mac's folder tree, from `/api/folders` — what the project
/// picker walks through to choose the folder Harness treats as "here".
struct FoldersResponse: Codable, Equatable {
    let path: String
    let parent: String
    let folders: [String]
}

struct VoiceEntry: Codable, Identifiable, Equatable {
    let id: String
    let name: String
    let seconds: Double
    let created: String
    let selected: Bool
}

struct VoicesResponse: Codable, Equatable {
    let voices: [VoiceEntry]
    let script: String?
}

// MARK: - Streaming SSE Events

enum StreamEvent: Equatable {
    case session(id: String)
    case reasoning(text: String)
    case token(text: String)
    case building(bytes: Int, budget: Double)
    case toolStart(name: String, args: [String: String])
    case toolResult(name: String, result: String)
    case approval(ApprovalItem)
    case approvalResolved(id: String, allowed: Bool, timedOut: Bool)
    case error(message: String)
    case cancelled
    case stats(seconds: Double, tokens: Int, steps: Int)
    case done(sessionId: String)
}

struct RawSSEEvent: Decodable {
    let type: String
    let sessionId: String?
    let text: String?
    let bytes: Int?
    let budget: Double?
    let name: String?
    let args: [String: AnyCodableValue]?
    let result: String?
    let id: String?
    let question: String?
    let kind: String?
    let detail: String?
    let allowed: Bool?
    let timedOut: Bool?
    let message: String?
    let seconds: Double?
    let tokens: Int?
    let steps: Int?

    enum CodingKeys: String, CodingKey {
        case type, text, bytes, budget, name, args, result, id, question, kind, detail
        case allowed, message, seconds, tokens, steps
        case sessionId = "session_id"
        case timedOut = "timed_out"
    }

    func toEvent() -> StreamEvent? {
        switch type {
        case "session":
            return .session(id: sessionId ?? "")
        case "reasoning":
            return .reasoning(text: text ?? "")
        case "token":
            return .token(text: text ?? "")
        case "building":
            return .building(bytes: bytes ?? 0, budget: budget ?? 0)
        case "tool_start":
            let stringArgs = (args ?? [:]).mapValues { $0.displayString }
            return .toolStart(name: name ?? "tool", args: stringArgs)
        case "tool_result":
            return .toolResult(name: name ?? "tool", result: result ?? "")
        case "approval":
            return .approval(ApprovalItem(
                id: id ?? UUID().uuidString,
                question: question ?? "Approve action?",
                kind: kind ?? "",
                detail: detail ?? ""
            ))
        case "approval_resolved":
            return .approvalResolved(
                id: id ?? "",
                allowed: allowed ?? false,
                timedOut: timedOut ?? false
            )
        case "error":
            return .error(message: message ?? "Unknown error")
        case "cancelled":
            return .cancelled
        case "stats":
            return .stats(seconds: seconds ?? 0, tokens: tokens ?? 0, steps: steps ?? 1)
        case "done":
            return .done(sessionId: sessionId ?? "")
        default:
            return nil
        }
    }
}

/// Lossless JSON value wrapper so tool arguments of any JSON type decode cleanly.
enum AnyCodableValue: Decodable, Equatable {
    case string(String)
    case int(Int)
    case double(Double)
    case bool(Bool)
    case array([AnyCodableValue])
    case object([String: AnyCodableValue])
    case null

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
        } else if let v = try? container.decode(Bool.self) {
            self = .bool(v)
        } else if let v = try? container.decode(Int.self) {
            self = .int(v)
        } else if let v = try? container.decode(Double.self) {
            self = .double(v)
        } else if let v = try? container.decode(String.self) {
            self = .string(v)
        } else if let v = try? container.decode([AnyCodableValue].self) {
            self = .array(v)
        } else if let v = try? container.decode([String: AnyCodableValue].self) {
            self = .object(v)
        } else {
            self = .null
        }
    }

    var displayString: String {
        switch self {
        case .string(let s): return s
        case .int(let i): return String(i)
        case .double(let d): return String(d)
        case .bool(let b): return b ? "true" : "false"
        case .array(let a): return "[" + a.map(\.displayString).joined(separator: ", ") + "]"
        case .object(let o):
            return "{" + o.map { "\($0.key): \($0.value.displayString)" }.sorted().joined(separator: ", ") + "}"
        case .null: return ""
        }
    }
}
