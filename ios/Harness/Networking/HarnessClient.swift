import Foundation

enum HarnessError: LocalizedError, Equatable {
    case invalidURL
    case unauthorized
    case http(Int, String)
    case serverMessage(String)
    case network(String)

    var errorDescription: String? {
        switch self {
        case .invalidURL:
            return "Invalid Harness server URL."
        case .unauthorized:
            return "Unauthorised — check your access token or connect over Tailscale."
        case .http(let code, let body):
            return body.isEmpty ? "Server returned HTTP \(code)." : body
        case .serverMessage(let msg):
            return msg
        case .network(let msg):
            return msg
        }
    }
}

protocol HarnessAPI: Sendable {
    var baseURL: URL { get }
    var token: String? { get }

    func capabilities() async throws -> Capabilities
    func systemState() async throws -> SystemState
    func accessInfo() async throws -> AccessInfo
    func models() async throws -> ModelsResponse
    func selectModel(path: String) async throws
    func config() async throws -> HarnessConfig
    func updateConfig(_ patch: [String: Any]) async throws -> HarnessConfig
    func resetConfig() async throws -> HarnessConfig
    func presets() async throws -> PresetsResponse
    func applyPreset(id: String) async throws -> HarnessConfig
    func sessions() async throws -> [SessionSummary]
    func sessionDetail(id: String) async throws -> SessionDetail
    func deleteSession(id: String) async throws
    func rewindSession(id: String) async throws -> String
    func exportMarkdown(id: String) async throws -> String
    func automations() async throws -> [AutomationEntry]
    func upsertAutomation(_ entry: [String: Any]) async throws -> AutomationEntry
    func deleteAutomation(id: String) async throws
    func runAutomation(id: String) async throws
    func systemAction(_ action: String, extra: [String: Any]) async throws
    func logs(kind: String) async throws -> LogsResponse
    func folders(path: String) async throws -> FoldersResponse
    func uploadImage(data: Data, mimeType: String) async throws -> UploadResponse
    func fetchData(path: String) async throws -> Data
    func approve(id: String, allow: Bool, always: Bool) async throws
    func cancel(turnId: String) async throws
    func voices() async throws -> VoicesResponse
    func selectVoice(id: String) async throws
    func speak(text: String) async throws -> Data
    func streamChat(
        sessionId: String?,
        message: String,
        images: [String],
        turnId: String,
        thinking: Bool
    ) -> AsyncThrowingStream<StreamEvent, Error>
}

final class LiveHarnessClient: HarnessAPI, @unchecked Sendable {
    let baseURL: URL
    let token: String?
    private let session: URLSession
    private let decoder = JSONDecoder()

    init(baseURL: URL, token: String? = nil) {
        self.baseURL = baseURL
        self.token = token
        let cfg = URLSessionConfiguration.default
        cfg.timeoutIntervalForRequest = 900
        cfg.timeoutIntervalForResource = 3600
        cfg.waitsForConnectivity = false
        self.session = URLSession(configuration: cfg)
    }

    private func makeRequest(
        path: String,
        method: String = "GET",
        contentType: String? = "application/json",
        body: Data? = nil,
        timeout: TimeInterval = 30
    ) throws -> URLRequest {
        let cleanPath = path.hasPrefix("/") ? String(path.dropFirst()) : path
        guard let url = URL(string: cleanPath, relativeTo: baseURL) else {
            throw HarnessError.invalidURL
        }
        var request = URLRequest(url: url, timeoutInterval: timeout)
        request.httpMethod = method
        if let contentType {
            request.setValue(contentType, forHTTPHeaderField: "Content-Type")
        }
        if let token, !token.isEmpty {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        request.httpBody = body
        return request
    }

    private func execute(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            throw HarnessError.network(error.localizedDescription)
        }
        guard let http = response as? HTTPURLResponse else {
            throw HarnessError.network("Unexpected server response.")
        }
        if http.statusCode == 401 {
            throw HarnessError.unauthorized
        }
        guard (200..<300).contains(http.statusCode) else {
            let msg = parseErrorMessage(from: data)
            throw HarnessError.http(http.statusCode, msg)
        }
        return (data, http)
    }

    private func parseErrorMessage(from data: Data) -> String {
        if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let err = obj["error"] as? String {
            return err
        }
        return String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    }

    private func get<T: Decodable>(_ path: String, timeout: TimeInterval = 20) async throws -> T {
        let req = try makeRequest(path: path, method: "GET", contentType: nil, timeout: timeout)
        let (data, _) = try await execute(req)
        return try decoder.decode(T.self, from: data)
    }

    private func postJSON(_ path: String, payload: [String: Any], timeout: TimeInterval = 30) async throws -> Data {
        let body = try JSONSerialization.data(withJSONObject: payload)
        let req = try makeRequest(path: path, method: "POST", body: body, timeout: timeout)
        let (data, _) = try await execute(req)
        return data
    }

    func capabilities() async throws -> Capabilities { try await get("/api/capabilities", timeout: 10) }
    func systemState() async throws -> SystemState { try await get("/api/system") }
    func accessInfo() async throws -> AccessInfo { try await get("/api/access") }
    func models() async throws -> ModelsResponse { try await get("/api/models") }
    func selectModel(path: String) async throws { _ = try await postJSON("/api/model", payload: ["path": path]) }
    func config() async throws -> HarnessConfig { try await get("/api/config") }

    func updateConfig(_ patch: [String: Any]) async throws -> HarnessConfig {
        let data = try await postJSON("/api/config", payload: patch)
        return try decoder.decode(HarnessConfig.self, from: data)
    }

    func resetConfig() async throws -> HarnessConfig {
        let data = try await postJSON("/api/config/reset", payload: [:])
        return try decoder.decode(HarnessConfig.self, from: data)
    }

    func presets() async throws -> PresetsResponse { try await get("/api/presets") }

    func applyPreset(id: String) async throws -> HarnessConfig {
        let data = try await postJSON("/api/preset", payload: ["id": id])
        return try decoder.decode(HarnessConfig.self, from: data)
    }

    func sessions() async throws -> [SessionSummary] { try await get("/api/sessions") }
    func sessionDetail(id: String) async throws -> SessionDetail { try await get("/api/session/\(id)") }
    func deleteSession(id: String) async throws { _ = try await postJSON("/api/sessions/delete", payload: ["id": id]) }

    func rewindSession(id: String) async throws -> String {
        let data = try await postJSON("/api/sessions/rewind", payload: ["id": id])
        return try decoder.decode(RewindResponse.self, from: data).message
    }

    func exportMarkdown(id: String) async throws -> String {
        let req = try makeRequest(path: "/api/export/\(id)", method: "GET", contentType: nil)
        let (data, _) = try await execute(req)
        return String(data: data, encoding: .utf8) ?? ""
    }

    func automations() async throws -> [AutomationEntry] { try await get("/api/automations") }

    func upsertAutomation(_ entry: [String: Any]) async throws -> AutomationEntry {
        let data = try await postJSON("/api/automations", payload: entry)
        return try decoder.decode(AutomationEntry.self, from: data)
    }

    func deleteAutomation(id: String) async throws {
        _ = try await postJSON("/api/automations/delete", payload: ["id": id])
    }

    func runAutomation(id: String) async throws {
        _ = try await postJSON("/api/automations/run", payload: ["id": id])
    }

    func systemAction(_ action: String, extra: [String: Any] = [:]) async throws {
        var payload = extra
        payload["action"] = action
        _ = try await postJSON("/api/system/action", payload: payload)
    }

    func logs(kind: String) async throws -> LogsResponse {
        try await get("/api/logs?\(kind)")
    }

    func folders(path: String) async throws -> FoldersResponse {
        var allowed = CharacterSet.urlQueryAllowed
        allowed.remove(charactersIn: "&+=?#")
        let encoded = path.addingPercentEncoding(withAllowedCharacters: allowed) ?? path
        return try await get("/api/folders?path=\(encoded)")
    }

    func uploadImage(data: Data, mimeType: String) async throws -> UploadResponse {
        let req = try makeRequest(
            path: "/api/uploads",
            method: "POST",
            contentType: mimeType,
            body: data,
            timeout: 60
        )
        let (respData, _) = try await execute(req)
        return try decoder.decode(UploadResponse.self, from: respData)
    }

    func fetchData(path: String) async throws -> Data {
        let req = try makeRequest(path: path, method: "GET", contentType: nil, timeout: 30)
        let (data, _) = try await execute(req)
        return data
    }

    func approve(id: String, allow: Bool, always: Bool) async throws {
        _ = try await postJSON("/api/approve", payload: ["id": id, "allow": allow, "always": always])
    }

    func cancel(turnId: String) async throws {
        _ = try await postJSON("/api/cancel", payload: ["turn_id": turnId])
    }

    func voices() async throws -> VoicesResponse { try await get("/api/voice/voices", timeout: 10) }

    func selectVoice(id: String) async throws {
        _ = try await postJSON("/api/voice/voices/\(id)/select", payload: [:])
    }

    func speak(text: String) async throws -> Data {
        try await postJSON("/api/voice/speak", payload: ["text": text], timeout: 300)
    }

    func streamChat(
        sessionId: String?,
        message: String,
        images: [String],
        turnId: String,
        thinking: Bool
    ) -> AsyncThrowingStream<StreamEvent, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    var payload: [String: Any] = [
                        "message": message,
                        "turn_id": turnId,
                        "thinking": thinking,
                    ]
                    if let sessionId, !sessionId.isEmpty {
                        payload["session_id"] = sessionId
                    }
                    if !images.isEmpty {
                        payload["images"] = images
                    }
                    let body = try JSONSerialization.data(withJSONObject: payload)
                    let req = try makeRequest(path: "/api/chat", method: "POST", body: body, timeout: 900)
                    let (bytes, response) = try await session.bytes(for: req)
                    guard let http = response as? HTTPURLResponse else {
                        throw HarnessError.network("Unexpected stream response.")
                    }
                    if http.statusCode == 401 {
                        throw HarnessError.unauthorized
                    }
                    guard (200..<300).contains(http.statusCode) else {
                        var errBytes: [UInt8] = []
                        for try await b in bytes { errBytes.append(b) }
                        let msg = parseErrorMessage(from: Data(errBytes))
                        throw HarnessError.http(http.statusCode, msg)
                    }

                    var parser = SSEParser()
                    var lineBytes: [UInt8] = []
                    for try await byte in bytes {
                        lineBytes.append(byte)
                        if byte == 0x0A { // '\n'
                            let piece = String(decoding: lineBytes, as: UTF8.self)
                            lineBytes.removeAll(keepingCapacity: true)
                            for ev in parser.feed(piece) {
                                continuation.yield(ev)
                            }
                        }
                    }
                    if !lineBytes.isEmpty {
                        let piece = String(decoding: lineBytes, as: UTF8.self)
                        for ev in parser.feed(piece) { continuation.yield(ev) }
                    }
                    for ev in parser.finish() {
                        continuation.yield(ev)
                    }
                    continuation.finish()
                } catch is CancellationError {
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in
                task.cancel()
            }
        }
    }
}
