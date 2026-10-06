import Foundation

/// 经纬度。后端在 position / last_position / teleport 结果里都用这个形状。
public struct LatLon: Codable, Sendable, Equatable {
    public let lat: Double
    public let lon: Double
}

/// 后端状态快照。字段与 locpilot/core/session.py 的 snapshot() 一一对应，
/// 只声明原生界面真正用到的部分，其余忽略（后端新增字段不会破坏解析）。
public struct StatusSnapshot: Decodable, Sendable {
    public struct AppInfo: Decodable, Sendable {
        public let name: String
        public let version: String
    }
    public struct Device: Decodable, Sendable {
        public let name: String?
        public let udid: String?
        public let iosVersion: String?
        public let productType: String?
        public let connection: String?

        enum CodingKeys: String, CodingKey {
            case name, udid, connection
            case iosVersion = "ios_version"
            case productType = "product_type"
        }
    }
    public struct Engine: Decodable, Sendable {
        public let name: String?
        public let label: String?
        public let opened: Bool
        public let mode: String?
        public let device: Device?
    }
    public let app: AppInfo
    public let engine: Engine
    public let position: LatLon?
    public let error: String?
}

/// 与后端 REST + SSE 的唯一通道。UI 只依赖这一层，方便单测。
public actor EngineClient {
    public struct TeleportResult: Decodable, Sendable {
        public let position: LatLon
        public let historyId: String?

        enum CodingKeys: String, CodingKey {
            case position
            case historyId = "history_id"
        }
    }

    public struct ConnectResult: Decodable, Sendable {
        public let engine: String
        public let device: StatusSnapshot.Device?
        /// 后端返回的是字符串数组（例如"libimobiledevice 无法处理 iOS 27 设备，已自动改用 …"）。
        public let notes: [String]?
    }

    private let baseURL: URL
    private let session: URLSession

    public init(baseURL: URL, session: URLSession = .shared) {
        self.baseURL = baseURL
        self.session = session
    }

    // MARK: - 请求

    private func request(_ path: String, method: String = "GET", body: [String: Any]? = nil) throws -> URLRequest {
        guard let url = URL(string: path, relativeTo: baseURL) else {
            throw EngineError.badURL(path)
        }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.timeoutInterval = 30
        if let body {
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        return request
    }

    private func send<T: Decodable>(_ path: String, method: String = "GET", body: [String: Any]? = nil, as type: T.Type) async throws -> T {
        let (data, response) = try await session.data(for: request(path, method: method, body: body))
        guard let http = response as? HTTPURLResponse else { throw EngineError.transport("无 HTTP 响应") }
        guard (200..<300).contains(http.statusCode) else {
            // 后端错误体是 {"ok": false, "error": "..."}：ok 是 Bool，
            // 用 [String: String] 整体解码会失败，用户就只能看到 "HTTP 503"。
            let message = (try? JSONDecoder().decode(BackendErrorBody.self, from: data))?.error
            throw EngineError.backend(message ?? "HTTP \(http.statusCode)")
        }
        return try JSONDecoder().decode(T.self, from: data)
    }

    public func health() async throws -> Bool {
        struct Health: Decodable { let status: String? }
        let result = try await send("api/health", as: Health.self)
        return result.status == "ok" || result.status == nil
    }

    public func status() async throws -> StatusSnapshot {
        try await send("api/status", as: StatusSnapshot.self)
    }

    public func connect(engine: String = "auto") async throws -> ConnectResult {
        try await send("api/connect", method: "POST", body: ["engine": engine], as: ConnectResult.self)
    }

    public func disconnect() async throws {
        _ = try await send("api/disconnect", method: "POST", body: [:], as: EmptyResponse.self)
    }

    @discardableResult
    public func teleport(lat: Double, lon: Double) async throws -> TeleportResult {
        try await send("api/teleport", method: "POST", body: ["lat": lat, "lon": lon], as: TeleportResult.self)
    }

    /// 逆地理编码：/api/reverse 返回 {"result": {"name":…, "display_name":…}}，查不到时 result 为 null。
    public func reverse(lat: Double, lon: Double) async throws -> Place? {
        struct Envelope: Decodable { let result: Place? }
        let envelope = try await send("api/reverse?lat=\(lat)&lon=\(lon)", as: Envelope.self)
        return envelope.result
    }

    public func clear() async throws {
        _ = try await send("api/clear", method: "POST", body: [:], as: EmptyResponse.self)
    }

    // MARK: - 事件流

    /// 订阅 /api/events（text/event-stream）。后端每次状态变化推一帧 snapshot。
    /// 用 AsyncStream 暴露，调用方只关心快照；连接断开时流自然结束，由上层决定是否重连。
    public nonisolated func events() -> AsyncStream<StatusSnapshot> {
        AsyncStream { continuation in
            let task = Task {
                var attempt = 0
                while !Task.isCancelled {
                    do {
                        let request = try await self.request("api/events")
                        let (bytes, response) = try await session.bytes(for: request)
                        guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
                            throw EngineError.transport("事件流返回 \(String(describing: response))")
                        }
                        attempt = 0
                        // 关键：Foundation 的 bytes.lines **不会产出空行**，而 SSE 正是用空行分帧，
                        // 靠它做判定会让事件流永远没有输出。这里按字节自己切行，保留空行。
                        // 同时按行累积字节再整体 UTF-8 解码，避免多字节中文被逐字节拆坏。
                        var lineBytes: [UInt8] = []
                        var payload = ""
                        for try await byte in bytes {
                            if Task.isCancelled { break }
                            if byte == 0x0A {
                                let line = String(decoding: lineBytes, as: UTF8.self)
                                lineBytes.removeAll(keepingCapacity: true)
                                if line.isEmpty {
                                    // 空行 = 一帧结束
                                    if !payload.isEmpty {
                                        if let data = payload.data(using: .utf8),
                                           let snapshot = try? JSONDecoder().decode(StatusSnapshot.self, from: data) {
                                            continuation.yield(snapshot)
                                        }
                                        payload = ""
                                    }
                                } else if line.hasPrefix("data:") {
                                    payload += line.dropFirst(5).trimmingCharacters(in: .whitespaces)
                                }
                            } else if byte != 0x0D {
                                lineBytes.append(byte)
                            }
                        }
                    } catch {
                        attempt += 1
                        let delay = min(Double(attempt) * 0.5, 3.0)
                        try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
                    }
                }
                continuation.finish()
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }
}

public struct EmptyResponse: Decodable, Sendable {}

/// /api/reverse 的地点信息（字段来自 Nominatim）。
public struct Place: Decodable, Sendable {
    public let name: String?
    public let displayName: String?
    /// 后端按"原生地图落针"的优先级挑好的短名（POI → 建筑 → 门牌+道路 → 街区 → 城市）。
    public let label: String?

    enum CodingKeys: String, CodingKey {
        case name
        case label
        case displayName = "display_name"
    }

    /// 落针标签：优先后端挑好的短名，其次 name，最后才用完整地址。
    public var shortLabel: String? { label ?? name ?? displayName }
}

/// 后端错误体：{"ok": false, "error": "..."}
struct BackendErrorBody: Decodable {
    let error: String?
}

public enum EngineError: LocalizedError {
    case badURL(String)
    case transport(String)
    case backend(String)

    public var errorDescription: String? {
        switch self {
        case .badURL(let path): return "无效地址：" + path
        case .transport(let message): return message
        case .backend(let message): return message
        }
    }
}
