import XCTest
@testable import Harness

@MainActor
final class DemoAndStoreTests: XCTestCase {
    private func makeIsolatedStore() -> AppStore {
        let suiteName = "harness.test.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        return AppStore(defaults: defaults)
    }

    func testDemoModeStreamsToolCallAndAssistantReply() async {
        let store = makeIsolatedStore()
        await store.attachClientForTesting(DemoHarnessClient(), demo: true)
        XCTAssertEqual(store.connectionState, .connected)
        XCTAssertTrue(store.isVisionAvailable)

        store.composerText = "How is my Mac memory?"
        await store.sendMessage()

        // Wait for demo streaming task to complete.
        for _ in 0..<50 where store.isStreaming {
            try? await Task.sleep(nanoseconds: 100_000_000)
        }

        XCTAssertFalse(store.isStreaming)
        XCTAssertNotNil(store.currentSessionId)
        XCTAssertTrue(store.transcript.contains(where: {
            if case .tool(_, let name, _, let out, false, false, _) = $0 {
                return name == "system_status" && out != nil
            }
            return false
        }))
        XCTAssertTrue(store.transcript.contains(where: {
            if case .assistant(_, let text, false) = $0 {
                return text.contains("Offline Demo Mode")
            }
            return false
        }))
    }

    func testDemoModeInteractiveApprovalAllowFlow() async {
        let store = makeIsolatedStore()
        await store.attachClientForTesting(DemoHarnessClient(), demo: true)

        store.composerText = "Please write a quick script"
        await store.sendMessage()

        var approvalId: String?
        for _ in 0..<40 {
            if let found = store.transcript.first(where: {
                if case .approval(_, _, .pending) = $0 { return true }
                return false
            }), case .approval(let id, _, _) = found {
                approvalId = id
                break
            }
            try? await Task.sleep(nanoseconds: 50_000_000)
        }
        let id = try! XCTUnwrap(approvalId)

        await store.respondToApproval(id: id, allow: true, always: false)

        for _ in 0..<60 where store.isStreaming {
            try? await Task.sleep(nanoseconds: 100_000_000)
        }

        XCTAssertFalse(store.isStreaming)
        XCTAssertTrue(store.transcript.contains(where: {
            if case .assistant(_, let text, false) = $0 {
                return text.contains("rename_by_date.py")
            }
            return false
        }))
    }

    func testDemoModeUploadAndRewind() async {
        let store = makeIsolatedStore()
        await store.attachClientForTesting(DemoHarnessClient(), demo: true)

        // Minimal 1x1 PNG bytes
        let pngData = Data([
            0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A,
            0x00, 0x00, 0x00, 0x0D, 0x49, 0x48, 0x44, 0x52,
            0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01,
            0x08, 0x02, 0x00, 0x00, 0x00, 0x90, 0x77, 0x53,
            0xDE, 0x00, 0x00, 0x00, 0x0C, 0x49, 0x44, 0x41,
            0x54, 0x08, 0xD7, 0x63, 0xF8, 0xCF, 0xC0, 0x00,
            0x00, 0x03, 0x01, 0x01, 0x00, 0x18, 0xDD, 0x8D,
            0xB0, 0x00, 0x00, 0x00, 0x00, 0x49, 0x45, 0x4E,
            0x44, 0xAE, 0x42, 0x60, 0x82,
        ])
        await store.attachImageData(pngData)
        XCTAssertEqual(store.pendingAttachments.count, 1)

        store.composerText = "Describe this image"
        await store.sendMessage()
        for _ in 0..<50 where store.isStreaming {
            try? await Task.sleep(nanoseconds: 100_000_000)
        }
        XCTAssertTrue(store.pendingAttachments.isEmpty)
        XCTAssertTrue(store.transcript.contains(where: {
            if case .assistant(_, let text, false) = $0 {
                return text.contains("1 image")
            }
            return false
        }))

        await store.rewindAndEdit()
        XCTAssertEqual(store.composerText, "Describe this image")
    }

    func testDemoModeInteractiveApprovalDenyFlow() async {
        let store = makeIsolatedStore()
        await store.attachClientForTesting(DemoHarnessClient(), demo: true)

        store.composerText = "Please edit a file for me"
        await store.sendMessage()

        var approvalId: String?
        for _ in 0..<40 {
            if let found = store.transcript.first(where: {
                if case .approval(_, _, .pending) = $0 { return true }
                return false
            }), case .approval(let id, _, _) = found {
                approvalId = id
                break
            }
            try? await Task.sleep(nanoseconds: 50_000_000)
        }
        let id = try! XCTUnwrap(approvalId)

        await store.respondToApproval(id: id, allow: false, always: false)

        for _ in 0..<60 where store.isStreaming {
            try? await Task.sleep(nanoseconds: 100_000_000)
        }

        XCTAssertFalse(store.isStreaming)
        XCTAssertTrue(store.transcript.contains(where: {
            if case .approval(let apId, _, .denied) = $0 { return apId == id }
            return false
        }))
        XCTAssertTrue(store.transcript.contains(where: {
            if case .assistant(_, let text, false) = $0 {
                return text.contains("nothing was written")
            }
            return false
        }))
    }
}
