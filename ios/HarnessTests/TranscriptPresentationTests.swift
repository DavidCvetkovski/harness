import XCTest
@testable import Harness

@MainActor
final class TranscriptPresentationTests: XCTestCase {
    private func tool(_ name: String, running: Bool = false, error: Bool = false) -> TranscriptItem {
        .tool(id: UUID(), name: name, args: ["command": "ls"], output: running ? nil : "ok",
              isError: error, isRunning: running, isExpanded: false)
    }

    private func thought() -> TranscriptItem {
        .thinking(id: UUID(), text: "hmm", startedAt: Date(), finishedAt: Date(), isExpanded: false)
    }

    private func kinds(_ rows: [TranscriptRow]) -> [String] {
        rows.map {
            switch $0 {
            case .single(let item):
                switch item {
                case .user: return "user"
                case .assistant: return "assistant"
                case .tool: return "tool"
                case .thinking: return "thinking"
                case .approval: return "approval"
                case .error: return "error"
                case .cost: return "cost"
                }
            case .steps(_, let items): return "steps(\(items.count))"
            }
        }
    }

    func testRunOfToolsAndThoughtsFoldsIntoOneGroup() {
        let transcript: [TranscriptItem] = [
            .user(id: UUID(), text: "find my pictures", imageUrls: [], localImages: []),
            thought(), tool("run_command"), thought(), tool("run_command"), tool("read_file"),
            .assistant(id: UUID(), text: "Found them.", isStreaming: false),
        ]
        XCTAssertEqual(kinds(TranscriptRow.build(from: transcript)),
                       ["user", "steps(5)", "assistant"])
    }

    func testLoneToolOrLoneThoughtStaysOnItsOwn() {
        let transcript: [TranscriptItem] = [
            .user(id: UUID(), text: "hi", imageUrls: [], localImages: []),
            tool("system_status"),
            .assistant(id: UUID(), text: "Fine.", isStreaming: false),
            .user(id: UUID(), text: "why", imageUrls: [], localImages: []),
            thought(),
            .assistant(id: UUID(), text: "Because.", isStreaming: false),
        ]
        XCTAssertEqual(kinds(TranscriptRow.build(from: transcript)),
                       ["user", "tool", "assistant", "user", "thinking", "assistant"])
    }

    func testApprovalBreaksARunSoItStaysVisible() {
        let approval = ApprovalItem(id: "a1", question: "Run this command?", kind: "run_command",
                                    detail: "RUN rm x")
        let transcript: [TranscriptItem] = [
            tool("list_files"), tool("run_command"),
            .approval(id: "a1", item: approval, status: .pending),
            tool("run_command"),
        ]
        XCTAssertEqual(kinds(TranscriptRow.build(from: transcript)),
                       ["steps(2)", "approval", "tool"])
    }

    func testGroupIdIsStableWhileTheRunGrows() {
        let first = thought()
        let early = TranscriptRow.build(from: [first, tool("run_command")])
        let later = TranscriptRow.build(from: [first, tool("run_command"), thought(), tool("read_file")])
        XCTAssertEqual(early.map(\.id), later.map(\.id))
    }

    func testSessionDateLabels() {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = .current
        let now = SessionDates.parse("2026-11-25T18:00:00")!

        XCTAssertEqual(SessionDates.label(for: "2026-11-24T09:30:00", now: now, calendar: calendar),
                       "Yesterday")
        XCTAssertFalse(SessionDates.label(for: "2026-11-21T09:30:00", now: now, calendar: calendar)
                        .contains("2026"))
        XCTAssertTrue(SessionDates.label(for: "2025-03-02T09:30:00", now: now, calendar: calendar)
                        .contains("2025"))
        XCTAssertEqual(SessionDates.label(for: "not a date", now: now, calendar: calendar), "not a date")
    }

    func testProseBlocksParseHeadingsListsTablesAndRules() {
        let text = """
        ## Found 3 pictures
        These are the **latest** versions:
        - beach.jpg
          - edited twice
        2. sunset.png
        > Older copies were skipped.

        | File | Size |
        |------|-----:|
        | beach.jpg | 2.1 MB |
        ---
        #hashtag is not a heading
        """
        XCTAssertEqual(ProseBlock.parse(text), [
            .heading(level: 2, text: "Found 3 pictures"),
            .paragraph("These are the **latest** versions:"),
            .listItem(marker: "•", text: "beach.jpg", depth: 0),
            .listItem(marker: "•", text: "edited twice", depth: 1),
            .listItem(marker: "2.", text: "sunset.png", depth: 0),
            .quote("Older copies were skipped."),
            .table([["File", "Size"], ["beach.jpg", "2.1 MB"]]),
            .rule,
            .paragraph("#hashtag is not a heading"),
        ])
    }

    func testAlignedTablePadsColumns() {
        let table = ProseBlocksView.alignedTable([["File", "Size"], ["beach.jpg", "2 MB"]])
        XCTAssertEqual(table, "File       Size\n─────────  ────\nbeach.jpg  2 MB")
    }

    func testReadableDiskSizes() {
        XCTAssertEqual(SettingsSheet.readableSize("75Gi"), "75 GB")
        XCTAssertEqual(SettingsSheet.readableSize("1.2Ti"), "1.2 TB")
        XCTAssertEqual(SettingsSheet.readableSize("512M"), "512 MB")
        XCTAssertEqual(SettingsSheet.readableSize("?"), "?")
    }

    func testDemoFolderBrowsingAndProjectChoice() async {
        let store = AppStore(defaults: UserDefaults(suiteName: "harness.test.\(UUID().uuidString)")!)
        await store.attachClientForTesting(DemoHarnessClient(), demo: true)
        await store.refreshAll()

        let home = try? await store.browseFolders(path: "~")
        XCTAssertEqual(home?.path, "/Users/demo")
        XCTAssertTrue(home?.folders.contains("Developer") ?? false)

        await store.setProjectDir("/Users/demo/Developer/Mimic")
        XCTAssertEqual(store.projectDir, "/Users/demo/Developer/Mimic")
        XCTAssertEqual(store.projectName, "Mimic")

        await store.setProjectDir("")
        XCTAssertEqual(store.projectDir, "")
        XCTAssertEqual(store.projectName, "")
    }
}
