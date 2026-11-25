import Foundation

/// Incremental Server-Sent Events parser for `/api/chat` streams.
/// Handles arbitrary chunk boundaries, `: ping` keep-alives, and CRLF/LF line endings.
struct SSEParser {
    private var buffer = ""
    private let decoder = JSONDecoder()

    mutating func feed(_ chunk: String) -> [StreamEvent] {
        buffer.append(chunk.replacingOccurrences(of: "\r\n", with: "\n"))
        var events: [StreamEvent] = []

        while let range = buffer.range(of: "\n\n") {
            let frame = String(buffer[..<range.lowerBound])
            buffer.removeSubrange(..<range.upperBound)
            if let event = parseFrame(frame) {
                events.append(event)
            }
        }
        return events
    }

    mutating func finish() -> [StreamEvent] {
        guard !buffer.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            buffer = ""
            return []
        }
        let leftover = buffer
        buffer = ""
        if let event = parseFrame(leftover) {
            return [event]
        }
        return []
    }

    private func parseFrame(_ frame: String) -> StreamEvent? {
        var dataLines: [String] = []
        for line in frame.split(separator: "\n", omittingEmptySubsequences: false) {
            if line.hasPrefix(":") { continue } // `: ping` keep-alive
            if line.hasPrefix("data: ") {
                dataLines.append(String(line.dropFirst(6)))
            } else if line.hasPrefix("data:") {
                dataLines.append(String(line.dropFirst(5)))
            }
        }
        guard !dataLines.isEmpty else { return nil }
        let payload = dataLines.joined(separator: "\n")
        guard let data = payload.data(using: .utf8),
              let raw = try? decoder.decode(RawSSEEvent.self, from: data) else {
            return nil
        }
        return raw.toEvent()
    }
}
