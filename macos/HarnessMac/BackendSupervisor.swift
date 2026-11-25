import AppKit
import Foundation
import Observation
import ServiceManagement

/// Where the Harness backend engine lives on disk.
/// Checking inside `Harness.app/Contents/Resources/backend` first means the same
/// app binary works both against a developer checkout today (Option 1) and as a
/// self-contained drag-to-Applications `.dmg` tomorrow (Option 2).
enum BackendSource: Equatable {
    case bundled(backendDir: URL, python: URL, llamaServer: URL?)
    case localBinary(URL)
    case notFound

    var label: String {
        switch self {
        case .bundled: return "Bundled inside Harness.app"
        case .localBinary(let url):
            let home = FileManager.default.homeDirectoryForCurrentUser.path
            if url.path.hasPrefix(home) {
                return "~" + url.path.dropFirst(home.count)
            }
            return url.path
        case .notFound: return "Not found"
        }
    }

    var isBundled: Bool {
        if case .bundled = self { return true }
        return false
    }
}

enum BackendLocator {
    static let overrideKey = "harness.backend.binPath"

    static func locate(
        bundleResourceURL: URL? = Bundle.main.resourceURL,
        defaults: UserDefaults = .standard,
        homeDirectory: URL = FileManager.default.homeDirectoryForCurrentUser,
        fileManager: FileManager = .default
    ) -> BackendSource {
        // 1. Standalone bundled backend inside Harness.app/Contents/Resources/backend/
        if let res = bundleResourceURL {
            let backendDir = res.appendingPathComponent("backend", isDirectory: true)
            let harnessPkg = backendDir.appendingPathComponent("harness/cli.py")
            if fileManager.fileExists(atPath: harnessPkg.path) {
                let bundledPython = backendDir.appendingPathComponent("python/bin/python3")
                let python: URL
                if fileManager.isExecutableFile(atPath: bundledPython.path) {
                    python = bundledPython
                } else if fileManager.isExecutableFile(atPath: "/opt/homebrew/bin/python3") {
                    python = URL(fileURLWithPath: "/opt/homebrew/bin/python3")
                } else {
                    python = URL(fileURLWithPath: "/usr/bin/python3")
                }
                let llama = backendDir.appendingPathComponent("bin/llama-server")
                let llamaURL = fileManager.isExecutableFile(atPath: llama.path) ? llama : nil
                return .bundled(backendDir: backendDir, python: python, llamaServer: llamaURL)
            }
        }

        // 2. Explicit user override (e.g. custom checkout path)
        if let custom = defaults.string(forKey: overrideKey)?.trimmingCharacters(in: .whitespacesAndNewlines),
           !custom.isEmpty,
           fileManager.isExecutableFile(atPath: custom) {
            return .localBinary(URL(fileURLWithPath: custom))
        }

        // 3. Standard checkout / Homebrew / user bin locations
        let candidates: [URL] = [
            homeDirectory.appendingPathComponent("Developer/Harness/bin/harness"),
            homeDirectory.appendingPathComponent(".local/bin/harness"),
            URL(fileURLWithPath: "/opt/homebrew/bin/harness"),
            URL(fileURLWithPath: "/usr/local/bin/harness"),
        ]
        for candidate in candidates where fileManager.isExecutableFile(atPath: candidate.path) {
            return .localBinary(candidate)
        }
        return .notFound
    }

    /// Standard folder where Harness looks for GGUF models.
    static func modelsDirectory(homeDirectory: URL = FileManager.default.homeDirectoryForCurrentUser) -> URL {
        homeDirectory.appendingPathComponent("llm/models", isDirectory: true)
    }
}

/// Supervises the local Harness backend on macOS without disturbing an already-running
/// server, and provides native Login Item control via `SMAppService`.
@MainActor
@Observable
final class BackendSupervisor {
    static let localURL = "http://127.0.0.1:8420"

    var source: BackendSource = .notFound
    var isStartingBackend = false
    var startupError: String?
    var launchAtLogin: Bool = false

    private let defaults: UserDefaults
    private var launchedProcess: Process?

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        refreshEnvironment()
    }

    func refreshEnvironment() {
        source = BackendLocator.locate(defaults: defaults)
        if Bundle.main.bundleURL.pathExtension == "app" {
            launchAtLogin = (SMAppService.mainApp.status == .enabled)
        }
    }

    func setLaunchAtLogin(_ enabled: Bool) {
        guard Bundle.main.bundleURL.pathExtension == "app" else { return }
        do {
            if enabled {
                try SMAppService.mainApp.register()
            } else {
                try SMAppService.mainApp.unregister()
            }
            launchAtLogin = (SMAppService.mainApp.status == .enabled)
            startupError = nil
        } catch {
            startupError = "Login item update failed: \(error.localizedDescription)"
            launchAtLogin = (SMAppService.mainApp.status == .enabled)
        }
    }

    /// Connects to an already-running Harness on 127.0.0.1:8420 first; if nothing is
    /// listening yet and `autoStart` is true, launches the located backend cleanly.
    func connectOrStart(store: AppStore, autoStart: Bool = true, remote: Bool = true) async {
        startupError = nil
        refreshEnvironment()

        // Never disturb a server that is already running (e.g. started from Terminal
        // or LaunchAgent). Attach straight to loopback.
        if await probeLocalServer() {
            await store.connect(rawURL: Self.localURL)
            return
        }

        guard autoStart else { return }
        await startBackend(store: store, remote: remote)
    }

    func startBackend(store: AppStore, remote: Bool = true) async {
        guard !isStartingBackend else { return }
        refreshEnvironment()
        startupError = nil

        guard source != .notFound else {
            startupError = "Could not find the Harness backend (`bin/harness`). Set its path in Settings → General or start Harness in Terminal."
            return
        }

        isStartingBackend = true
        defer { isStartingBackend = false }

        do {
            let process = Process()
            var env = ProcessInfo.processInfo.environment
            let defaultPath = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
            env["PATH"] = [env["PATH"], defaultPath].compactMap { $0 }.joined(separator: ":")

            switch source {
            case .bundled(let backendDir, let python, let llamaServer):
                process.executableURL = python
                process.arguments = ["-m", "harness.cli"] + (remote ? ["--remote"] : [])
                process.currentDirectoryURL = backendDir
                env["PYTHONPATH"] = backendDir.path
                if let llamaServer {
                    env["HARNESS_LLAMA_SERVER"] = llamaServer.path
                }
            case .localBinary(let binURL):
                process.executableURL = binURL
                process.arguments = remote ? ["--remote"] : []
                process.currentDirectoryURL = binURL.deletingLastPathComponent().deletingLastPathComponent()
            case .notFound:
                return
            }

            process.environment = env
            process.standardInput = FileHandle.nullDevice
            process.standardOutput = FileHandle.nullDevice
            process.standardError = FileHandle.nullDevice
            try process.run()
            launchedProcess = process

            // Poll loopback briefly until Harness finishes booting.
            for _ in 0..<40 {
                try? await Task.sleep(nanoseconds: 400_000_000)
                if await probeLocalServer() {
                    await store.connect(rawURL: Self.localURL)
                    return
                }
            }
            startupError = "Harness started, but did not answer on 127.0.0.1:8420 yet. Check ~/.harness/app.log."
        } catch {
            startupError = "Could not launch Harness: \(error.localizedDescription)"
        }
    }

    func openModelsFolderInFinder() {
        let dir = BackendLocator.modelsDirectory()
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        NSWorkspace.shared.open(dir)
    }

    private func probeLocalServer() async -> Bool {
        guard let url = URL(string: "\(Self.localURL)/api/capabilities") else { return false }
        var req = URLRequest(url: url, timeoutInterval: 2)
        req.httpMethod = "GET"
        do {
            let (_, resp) = try await URLSession.shared.data(for: req)
            return (resp as? HTTPURLResponse)?.statusCode == 200
        } catch {
            return false
        }
    }
}
