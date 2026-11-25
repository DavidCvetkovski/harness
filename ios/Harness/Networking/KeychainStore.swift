import Foundation
import Security

/// Parses user-entered or QR-scanned Harness links into a clean base URL and optional token.
enum ConnectionParser {
    struct Parsed: Equatable {
        let baseURL: URL
        let token: String?
    }

    static func parse(_ rawInput: String) -> Parsed? {
        var trimmed = rawInput.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }
        if !trimmed.contains("://") {
            let lower = trimmed.lowercased()
            let defaultScheme = lower.contains(".ts.net") ? "https" : "http"
            trimmed = "\(defaultScheme)://\(trimmed)"
        }
        guard var components = URLComponents(string: trimmed),
              let scheme = components.scheme?.lowercased(),
              (scheme == "http" || scheme == "https"),
              let host = components.host, !host.isEmpty else {
            return nil
        }

        let token = components.queryItems?.first(where: { $0.name == "key" })?.value
        components.query = nil
        components.fragment = nil
        if components.path == "/" {
            components.path = ""
        } else if components.path.hasSuffix("/") {
            components.path.removeLast()
        }
        if components.port == nil && !host.contains("example.com") && scheme == "http" {
            components.port = 8420
        }
        guard let url = components.url else { return nil }
        return Parsed(baseURL: url, token: (token?.isEmpty == false) ? token : nil)
    }
}

/// Minimal Keychain wrapper for storing the optional Harness access token.
enum KeychainStore {
    private static let service = "com.davidcvetkovski.harness.token"
    private static let account = "default"

    static func saveToken(_ token: String?) {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        SecItemDelete(query as CFDictionary)
        guard let token, !token.isEmpty, let data = token.data(using: .utf8) else { return }
        var item = query
        item[kSecValueData as String] = data
        item[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        SecItemAdd(item as CFDictionary, nil)
    }

    static func loadToken() -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var out: AnyObject?
        guard SecItemCopyMatching(query as CFDictionary, &out) == errSecSuccess,
              let data = out as? Data else {
            return nil
        }
        return String(data: data, encoding: .utf8)
    }
}
