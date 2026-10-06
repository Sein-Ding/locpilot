import Foundation
import LocPilotKit

/// 零依赖测试运行器（本机只有 Command Line Tools，没有 XCTest，所以不能 swift test）。
/// 分工：本文件提供断言原语、运行器与 URLProtocol 网络桩；main.swift 只声明被测行为与断言。
/// 失败计数非零 → 退出码 1（CI/脚本可判定）。
/// 用法：swift run LocPilotTests [--filter=关键字]

var passed = 0
var failed = 0
var current = ""

private let filterKeyword: String? = {
    for argument in CommandLine.arguments.dropFirst() where argument.hasPrefix("--filter=") {
        return String(argument.dropFirst("--filter=".count))
    }
    return nil
}()

func expect(_ condition: Bool, _ message: String, file: String = #fileID, line: Int = #line) {
    if condition {
        passed += 1
    } else {
        failed += 1
        print("  ✗ [\(current)] \(message)  (\(file):\(line))")
    }
}

func expectEqual<T: Equatable>(_ actual: T, _ expected: T, _ label: String, file: String = #fileID, line: Int = #line) {
    expect(actual == expected, "\(label): 期望 \(expected)，实际 \(actual)", file: file, line: line)
}

func expectClose(_ actual: Double, _ expected: Double, tolerance: Double = 1e-9, _ label: String, file: String = #fileID, line: Int = #line) {
    expect(abs(actual - expected) <= tolerance, "\(label): 期望 \(expected) ±\(tolerance)，实际 \(actual)", file: file, line: line)
}

func shouldRun(_ name: String) -> Bool {
    guard let filterKeyword else { return true }
    return name.contains(filterKeyword)
}

func test(_ name: String, _ body: () throws -> Void) {
    guard shouldRun(name) else { return }
    current = name
    let before = failed
    do {
        try body()
        if failed == before { print("  ✓ \(name)") }
    } catch {
        failed += 1
        print("  ✗ \(name) 抛出异常: \(error)")
    }
}

/// 异步测试：跑在 detached Task 上，主线程用信号量等待。
/// 超时按失败处理（挂起/死等不会让整个运行器卡死）。
func testAsync(_ name: String, timeout: TimeInterval = 20, _ body: @escaping () async throws -> Void) {
    guard shouldRun(name) else { return }
    current = name
    let before = failed
    let semaphore = DispatchSemaphore(value: 0)
    let failure = Box<Error>()
    Task.detached {
        do { try await body() } catch { failure.set(error) }
        semaphore.signal()
    }
    if semaphore.wait(timeout: .now() + timeout) == .timedOut {
        failed += 1
        print("  ✗ [\(name)] 超时 >\(Int(timeout))s（疑似挂起或死等）")
        return
    }
    if let error = failure.get() {
        failed += 1
        print("  ✗ \(name) 抛出异常: \(error)")
        return
    }
    if failed == before { print("  ✓ \(name)") }
}

/// 打印总结并以退出码表达结论。
func finish() -> Never {
    print("")
    print(failed == 0 ? "全部通过：\(passed) 项" : "失败 \(failed) 项 / 通过 \(passed) 项")
    exit(failed == 0 ? 0 : 1)
}

/// 跨线程传递结果用的最小盒子（NSLock 保护）。
final class Box<T>: @unchecked Sendable {
    private let lock = NSLock()
    private var value: T?

    init() {}

    func set(_ newValue: T) {
        lock.lock()
        value = newValue
        lock.unlock()
    }

    func get() -> T? {
        lock.lock()
        defer { lock.unlock() }
        return value
    }
}

// MARK: - 捕获 EngineError

/// 执行操作并捕获 EngineError；没有抛错或抛了别的错误都会记一次断言失败。
func captureEngineError(_ label: String, _ operation: () async throws -> Void) async -> EngineError? {
    do {
        try await operation()
        expect(false, "\(label)：应当抛错，但调用成功")
        return nil
    } catch let error as EngineError {
        return error
    } catch {
        expect(false, "\(label)：期望 EngineError，实际 \(error)")
        return nil
    }
}

// MARK: - URLProtocol 桩
//
// 用 URLSessionConfiguration.protocolClasses 注入，不占端口、不依赖网络，
// 既能断言 EngineClient 发出的请求（方法/路径/头/体），也能把 SSE 分块喂给它。

final class StubURLProtocol: URLProtocol {
    struct Recorded {
        let method: String
        let url: URL
        let headers: [String: String]
        let body: Data?

        var path: String { url.path }

        var bodyJSON: [String: Any]? {
            guard let body, let object = try? JSONSerialization.jsonObject(with: body) else { return nil }
            return object as? [String: Any]
        }

        var bodyText: String? {
            guard let body else { return nil }
            return String(data: body, encoding: .utf8)
        }
    }

    private static let lock = NSLock()
    private static var _recorded: [Recorded] = []
    private static var _finishedResponses = 0
    private static var _responder: ((Recorded, StubConnection) -> Void)?

    /// 测试设置：收到请求后如何应答。
    static var responder: ((Recorded, StubConnection) -> Void)? {
        get { lock.lock(); defer { lock.unlock() }; return _responder }
        set { lock.lock(); _responder = newValue; lock.unlock() }
    }

    static var recorded: [Recorded] {
        lock.lock(); defer { lock.unlock() }
        return _recorded
    }

    static var lastRequest: Recorded? { recorded.last }

    /// 已完整结束（didFinishLoading）的响应数：用于断言"帧到达时连接还没关"。
    static var finishedResponses: Int {
        lock.lock(); defer { lock.unlock() }
        return _finishedResponses
    }

    static func reset() {
        lock.lock()
        _recorded = []
        _finishedResponses = 0
        _responder = nil
        lock.unlock()
    }

    static func markFinished() {
        lock.lock()
        _finishedResponses += 1
        lock.unlock()
    }

    private static func record(_ request: URLRequest) -> Recorded {
        let item = Recorded(
            method: request.httpMethod ?? "GET",
            url: request.url ?? URL(string: "http://stub.invalid/")!,
            headers: request.allHTTPHeaderFields ?? [:],
            body: readBody(request)
        )
        lock.lock()
        _recorded.append(item)
        lock.unlock()
        return item
    }

    /// URLSession 把 POST 体放进 httpBodyStream（httpBody 通常为 nil），两者都要读。
    private static func readBody(_ request: URLRequest) -> Data? {
        if let body = request.httpBody { return body }
        guard let stream = request.httpBodyStream else { return nil }
        stream.open()
        defer { stream.close() }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 4096)
        while stream.hasBytesAvailable {
            let count = stream.read(&buffer, maxLength: buffer.count)
            if count <= 0 { break }
            data.append(buffer, count: count)
        }
        return data.isEmpty ? nil : data
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let item = StubURLProtocol.record(request)
        let connection = StubConnection(self)
        if let responder = StubURLProtocol.responder {
            responder(item, connection)
        } else {
            connection.send(status: 500, text: "{\"error\": \"测试未设置桩响应: \(item.path)\"}")
        }
    }

    override func stopLoading() {}
}

/// 桩的连接句柄：测试用它回状态码、分批喂数据、结束响应。
final class StubConnection {
    private let proto: URLProtocol
    private var started = false

    init(_ proto: URLProtocol) {
        self.proto = proto
    }

    func respond(status: Int = 200, headers: [String: String] = [:]) {
        var all = headers
        if all["Content-Type"] == nil { all["Content-Type"] = "application/json; charset=utf-8" }
        let url = proto.request.url ?? URL(string: "http://stub.invalid/")!
        let response = HTTPURLResponse(url: url, statusCode: status, httpVersion: "HTTP/1.1", headerFields: all)!
        proto.client?.urlProtocol(proto, didReceive: response, cacheStoragePolicy: .notAllowed)
        started = true
    }

    func send(_ text: String) { send(Data(text.utf8)) }

    func send(_ data: Data) {
        if !started { respond() }
        proto.client?.urlProtocol(proto, didLoad: data)
    }

    func finish() {
        if !started { respond() }
        StubURLProtocol.markFinished()
        proto.client?.urlProtocolDidFinishLoading(proto)
    }

    /// 一步到位：状态码 + 文本体 + 结束响应。
    func send(status: Int, text: String, headers: [String: String] = [:]) {
        respond(status: status, headers: headers)
        proto.client?.urlProtocol(proto, didLoad: Data(text.utf8))
        StubURLProtocol.markFinished()
        proto.client?.urlProtocolDidFinishLoading(proto)
    }
}

/// 让桩响应只生效一次（SSE 客户端在响应结束后会立刻重连，避免重复喂帧）。
final class Once: @unchecked Sendable {
    private let lock = NSLock()
    private var used = false

    func claim() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        if used { return false }
        used = true
        return true
    }
}

// MARK: - 桩会话与客户端

/// 每个测试前调用：清空记录与响应，返回只走桩的 EngineClient。
func stubClient(baseURL: String = "http://locpilot.stub/") -> EngineClient {
    StubURLProtocol.reset()
    let configuration = URLSessionConfiguration.ephemeral
    configuration.protocolClasses = [StubURLProtocol.self]
    configuration.timeoutIntervalForRequest = 10
    configuration.timeoutIntervalForResource = 30
    let session = URLSession(configuration: configuration)
    return EngineClient(baseURL: URL(string: baseURL)!, session: session)
}
