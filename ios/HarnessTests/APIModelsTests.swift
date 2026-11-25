import XCTest
@testable import Harness

final class APIModelsTests: XCTestCase {
    func testDecodesCapabilities() throws {
        let json = """
        {
          "version": 1,
          "mode": "remote",
          "tls": true,
          "scheme": "https",
          "model_loaded": true,
          "vision": true,
          "n_ctx": 32768,
          "voice": false,
          "max_upload_bytes": 26214400,
          "max_images_per_message": 6,
          "tools": ["list_files", "search_in_files", "view_image"]
        }
        """
        let caps = try JSONDecoder().decode(Capabilities.self, from: Data(json.utf8))
        XCTAssertEqual(caps.version, 1)
        XCTAssertEqual(caps.scheme, "https")
        XCTAssertTrue(caps.vision)
        XCTAssertEqual(caps.nCtx, 32768)
        XCTAssertEqual(caps.maxImagesPerMessage, 6)
    }

    func testDecodesSystemState() throws {
        let json = """
        {
          "model": {"running": true, "pid": 49872, "gb": 5.89, "name": "Qwen3.5-9B"},
          "app": {"running": true, "pid": 56498, "gb": 0.04},
          "memory_free": "28%",
          "disk_free": "74Gi",
          "uptime": "3 days",
          "mode": "remote",
          "tls": false,
          "url": "http://100.74.163.110:8420",
          "tailnet": "100.74.163.110",
          "autostart": false
        }
        """
        let sys = try JSONDecoder().decode(SystemState.self, from: Data(json.utf8))
        XCTAssertTrue(sys.model.running)
        XCTAssertEqual(sys.model.pid, 49872)
        XCTAssertEqual(sys.memoryFree, "28%")
        XCTAssertEqual(sys.tls, false)
    }

    func testDecodesSessionDetailWithImageUrlsAndApprovals() throws {
        let json = """
        {
          "id": "20261125-100000-abcd",
          "running": true,
          "messages": [
            {
              "role": "user",
              "content": "What is in this photo?",
              "images": ["/Users/me/.harness/uploads/a.jpg"],
              "image_urls": ["/api/uploads/a.jpg"]
            }
          ],
          "approvals": [
            {"id": "ap123", "question": "Edit file?", "kind": "edit_file", "detail": "- old\\n+ new"}
          ]
        }
        """
        let detail = try JSONDecoder().decode(SessionDetail.self, from: Data(json.utf8))
        XCTAssertTrue(detail.running)
        XCTAssertEqual(detail.messages.first?.imageUrls, ["/api/uploads/a.jpg"])
        XCTAssertEqual(detail.approvals.first?.id, "ap123")
    }

    func testMarkdownSplitterSeparatesFencedCodeBlocks() {
        let input = """
        Here is the fix:

        ```python
        print("hello")
        ```

        All set.
        """
        let segments = MarkdownSplitter.split(input)
        XCTAssertEqual(segments, [
            .prose("Here is the fix:"),
            .code(language: "python", content: "print(\"hello\")"),
            .prose("All set."),
        ])
    }
}
