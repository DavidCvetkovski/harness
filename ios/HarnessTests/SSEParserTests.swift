import XCTest
@testable import Harness

final class SSEParserTests: XCTestCase {
    func testChunkedFramesAndKeepAlives() {
        var parser = SSEParser()
        let first = parser.feed(": ping\n\ndata: {\"type\":\"session\",\"session_")
        XCTAssertTrue(first.isEmpty)

        let second = parser.feed("id\":\"abc123\"}\n\ndata: {\"type\":\"token\",\"text\":\"Hello\"}\r\n\r\n")
        XCTAssertEqual(second, [
            .session(id: "abc123"),
            .token(text: "Hello"),
        ])
    }

    func testAllEventTypesDecodeCleanly() {
        var parser = SSEParser()
        let stream = """
        data: {"type":"reasoning","text":"Checking disk"}

        data: {"type":"building","bytes":128,"budget":0.25}

        data: {"type":"tool_start","name":"search_in_files","args":{"pattern":"TODO","max_hits":10,"case_sensitive":false}}

        data: {"type":"tool_result","name":"search_in_files","result":"a.py:4: # TODO"}

        data: {"type":"approval","id":"ap1","question":"Write file?","kind":"write_file","detail":"+ print(1)"}

        data: {"type":"approval_resolved","id":"ap1","allowed":false,"timed_out":true}

        data: {"type":"stats","seconds":2.5,"tokens":42,"steps":3}

        data: {"type":"cancelled"}

        data: {"type":"error","message":"model offline"}

        data: {"type":"done","session_id":"s1"}

        """
        let events = parser.feed(stream) + parser.finish()
        XCTAssertEqual(events.count, 10)
        XCTAssertEqual(events[0], .reasoning(text: "Checking disk"))
        XCTAssertEqual(events[1], .building(bytes: 128, budget: 0.25))
        XCTAssertEqual(events[2], .toolStart(name: "search_in_files", args: [
            "pattern": "TODO",
            "max_hits": "10",
            "case_sensitive": "false",
        ]))
        XCTAssertEqual(events[3], .toolResult(name: "search_in_files", result: "a.py:4: # TODO"))
        XCTAssertEqual(events[4], .approval(ApprovalItem(id: "ap1", question: "Write file?", kind: "write_file", detail: "+ print(1)")))
        XCTAssertEqual(events[5], .approvalResolved(id: "ap1", allowed: false, timedOut: true))
        XCTAssertEqual(events[6], .stats(seconds: 2.5, tokens: 42, steps: 3))
        XCTAssertEqual(events[7], .cancelled)
        XCTAssertEqual(events[8], .error(message: "model offline"))
        XCTAssertEqual(events[9], .done(sessionId: "s1"))
    }

    func testMalformedFrameIsSkippedAndFinishFlushesTrailingFrame() {
        var parser = SSEParser()
        let first = parser.feed("data: {not-json}\n\ndata: {\"type\":\"token\",\"text\":\"ok\"}")
        XCTAssertTrue(first.isEmpty)
        let flushed = parser.finish()
        XCTAssertEqual(flushed, [.token(text: "ok")])
    }
}
