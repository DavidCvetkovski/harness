import XCTest
@testable import Harness

final class URLNormalizerTests: XCTestCase {
    func testBareTailnetIPDefaultsToHttp8420() {
        let parsed = ConnectionParser.parse("100.74.163.110")
        XCTAssertEqual(parsed?.baseURL.absoluteString, "http://100.74.163.110:8420")
        XCTAssertNil(parsed?.token)
    }

    func testTailnetFQDNDefaultsToHttps() {
        let parsed = ConnectionParser.parse("my-mac.tail08b5a9.ts.net:8420/")
        XCTAssertEqual(parsed?.baseURL.absoluteString, "https://my-mac.tail08b5a9.ts.net:8420")
        XCTAssertNil(parsed?.token)
    }

    func testExtractsKeyQueryParameterFromQRLink() {
        let parsed = ConnectionParser.parse("http://192.168.1.42:8420/?key=secret123")
        XCTAssertEqual(parsed?.baseURL.absoluteString, "http://192.168.1.42:8420")
        XCTAssertEqual(parsed?.token, "secret123")
    }

    func testRejectsEmptyOrUnsupportedScheme() {
        XCTAssertNil(ConnectionParser.parse("   "))
        XCTAssertNil(ConnectionParser.parse("ftp://100.74.163.110:8420"))
    }
}
