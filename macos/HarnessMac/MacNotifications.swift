import AppKit
import Foundation
import UserNotifications

/// Sends native macOS notifications when Harness is not the active app and either
/// needs approval or finishes a reply. Silently no-ops when run outside an `.app`
/// bundle (such as `xctest`).
@MainActor
enum MacNotifications {
    private static var isBundledApp: Bool {
        Bundle.main.bundleURL.pathExtension == "app" && Bundle.main.bundleIdentifier != nil
    }

    static func requestAuthorizationIfNeeded() {
        guard isBundledApp else { return }
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in }
    }

    static func notifyApproval(_ item: ApprovalItem) {
        guard isBundledApp, !NSApp.isActive else { return }
        let content = UNMutableNotificationContent()
        content.title = "Harness needs approval"
        content.body = item.question
        content.sound = .default
        let req = UNNotificationRequest(identifier: "approval-\(item.id)", content: content, trigger: nil)
        UNUserNotificationCenter.current().add(req)
    }

    static func notifyTurnFinished(preview: String) {
        guard isBundledApp, !NSApp.isActive else { return }
        let clean = preview.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !clean.isEmpty else { return }
        let content = UNMutableNotificationContent()
        content.title = "Harness finished"
        content.body = String(clean.prefix(140))
        content.sound = .default
        let req = UNNotificationRequest(identifier: "done-\(UUID().uuidString)", content: content, trigger: nil)
        UNUserNotificationCenter.current().add(req)
    }
}
