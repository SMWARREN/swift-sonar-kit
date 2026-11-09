import XCTest
@testable import Greeting

final class GreetingTests: XCTestCase {
    func testEmptyName() {
        XCTAssertEqual(greeting(for: ""), "Hello!")
    }

    func testNamedGreeting() {
        XCTAssertEqual(greeting(for: "Ada"), "Hello, Ada!")
    }

    func testUnicodeGreeting() {
        XCTAssertEqual(greeting(for: "世界"), "Hello, 世界!")
    }
}
