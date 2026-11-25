import XCTest
@testable import Harness

final class MacAppTests: XCTestCase {
    func testBackendLocatorPrefersBundledBackendOverLocalCheckout() throws {
        let fm = FileManager.default
        let tmp = fm.temporaryDirectory.appendingPathComponent("harness-loc-\(UUID().uuidString)")
        defer { try? fm.removeItem(at: tmp) }

        let bundleRes = tmp.appendingPathComponent("Resources")
        let cliPy = bundleRes.appendingPathComponent("backend/harness/cli.py")
        let llama = bundleRes.appendingPathComponent("backend/bin/llama-server")
        try fm.createDirectory(at: cliPy.deletingLastPathComponent(), withIntermediateDirectories: true)
        try fm.createDirectory(at: llama.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data("# cli".utf8).write(to: cliPy)
        try Data("#!/bin/sh\n".utf8).write(to: llama)
        try fm.setAttributes([.posixPermissions: 0o755], ofItemAtPath: llama.path)

        let suite = UserDefaults(suiteName: "harness.mac.test.\(UUID().uuidString)")!
        let found = BackendLocator.locate(bundleResourceURL: bundleRes, defaults: suite, homeDirectory: tmp)
        guard case .bundled(let dir, _, let llamaURL) = found else {
            return XCTFail("Expected .bundled, got \(found)")
        }
        XCTAssertEqual(dir.lastPathComponent, "backend")
        XCTAssertEqual(llamaURL?.lastPathComponent, "llama-server")
    }

    func testBackendLocatorFallsBackToLocalCheckout() throws {
        let fm = FileManager.default
        let tmp = fm.temporaryDirectory.appendingPathComponent("harness-loc-\(UUID().uuidString)")
        defer { try? fm.removeItem(at: tmp) }

        let bin = tmp.appendingPathComponent("Developer/Harness/bin/harness")
        try fm.createDirectory(at: bin.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data("#!/bin/sh\n".utf8).write(to: bin)
        try fm.setAttributes([.posixPermissions: 0o755], ofItemAtPath: bin.path)

        let suite = UserDefaults(suiteName: "harness.mac.test.\(UUID().uuidString)")!
        let found = BackendLocator.locate(bundleResourceURL: tmp.appendingPathComponent("empty"),
                                          defaults: suite, homeDirectory: tmp)
        XCTAssertEqual(found, .localBinary(bin))
    }

    func testQRCodeGeneratorProducesCrispImage() {
        let img = QRCodeGenerator.makeImage(from: "http://100.74.163.110:8420")
        XCTAssertNotNil(img)
        XCTAssertGreaterThan(img?.size.width ?? 0, 100)
        XCTAssertNil(QRCodeGenerator.makeImage(from: ""))
    }

    func testMacImageCodecResizesAndEncodesJPEG() {
        // Minimal 1x1 PNG bytes
        let png = Data([
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
        let jpeg = PlatformImageCodec.prepareJPEG(from: png, maxEdge: 1600, quality: 0.85)
        XCTAssertNotNil(jpeg)
        XCTAssertTrue(jpeg?.starts(with: [0xFF, 0xD8, 0xFF]) ?? false)
    }

    @MainActor
    func testSharedAppStoreStreamsAndApprovesOnMacOS() async {
        let suite = UserDefaults(suiteName: "harness.mac.store.\(UUID().uuidString)")!
        let store = AppStore(defaults: suite)
        await store.attachClientForTesting(DemoHarnessClient(), demo: true)
        XCTAssertEqual(store.connectionState, .connected)

        var notifiedFinished = false
        store.onTurnFinished = { _ in notifiedFinished = true }

        store.composerText = "Check my Mac memory"
        await store.sendMessage()
        for _ in 0..<50 where store.isStreaming {
            try? await Task.sleep(nanoseconds: 100_000_000)
        }
        XCTAssertFalse(store.isStreaming)
        XCTAssertTrue(notifiedFinished)
        XCTAssertTrue(store.transcript.contains(where: {
            if case .assistant(_, let text, false) = $0 { return text.contains("Offline Demo Mode") }
            return false
        }))
    }

    func testMacFormattingReadableSizeAndSettingsPanes() {
        XCTAssertEqual(MacFormatting.readableSize("75Gi"), "75 GB")
        XCTAssertEqual(MacFormatting.readableSize("1.2Ti"), "1.2 TB")
        XCTAssertEqual(MacFormatting.readableSize(""), "—")
        XCTAssertEqual(MacSettingsPane.allCases.count, 8)
    }
}
