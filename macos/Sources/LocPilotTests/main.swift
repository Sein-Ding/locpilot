import Foundation
import LocPilotKit

/// 可执行测试运行器：Command Line Tools 不含 XCTest，用退出码表达结论。
/// 断言原语、运行器与 URLProtocol 网络桩在 Harness.swift。
/// 用法：swift run LocPilotTests [--filter=关键字]

// MARK: - 装置数据（取自 locpilot serve 的真实响应，已裁剪无关字段）

let connectedStatusJSON = """
{
  "app": {"name": "LocPilot", "version": "1.0.0"},
  "runtime": {"package": "/tmp/locpilot"},
  "engine": {
    "name": "pymobiledevice3", "label": "pymobiledevice3", "opened": true, "mode": "worker:dvt",
    "device": {"name": "Sein-iPhone 18 Pro", "udid": "00008130-000123456789001E", "ios_version": "27.0.1",
               "product_type": "iPhone19,2", "connection": "usb"},
    "last_position": {"lat": 31.2304, "lon": 121.4737}
  },
  "position": {"lat": 31.2404, "lon": 121.4837},
  "error": null,
  "tile_provider": "amap",
  "tile_providers": {"amap": {"label": "高德地图"}},
  "logs": []
}
"""

let disconnectedStatusJSON = """
{"app": {"name": "LocPilot", "version": "1.0.0"},
 "engine": {"name": null, "label": null, "opened": false, "mode": null, "device": null},
 "position": null, "error": "未检测到 iOS 设备", "logs": []}
"""

print("LocPilot 原生层测试")

// MARK: - 状态快照解析

test("状态快照解析：已连接真机") {
    let snapshot = try JSONDecoder().decode(StatusSnapshot.self, from: Data(connectedStatusJSON.utf8))
    expectEqual(snapshot.app.version, "1.0.0", "版本")
    expect(snapshot.engine.opened, "opened 应为 true")
    expectEqual(snapshot.engine.name, "pymobiledevice3", "引擎名")
    expectEqual(snapshot.engine.mode, "worker:dvt", "引擎模式")
    expectEqual(snapshot.engine.device?.name, "Sein-iPhone 18 Pro", "设备名")
    expectEqual(snapshot.engine.device?.iosVersion, "27.0.1", "iOS 版本（snake_case 映射）")
    expectEqual(snapshot.engine.device?.productType, "iPhone19,2", "机型（snake_case 映射）")
    expect(abs((snapshot.position?.lat ?? 0) - 31.2404) < 1e-9, "当前纬度")
    expect(snapshot.error == nil, "无错误")
}

test("状态快照解析：未连接") {
    let snapshot = try JSONDecoder().decode(StatusSnapshot.self, from: Data(disconnectedStatusJSON.utf8))
    expect(!snapshot.engine.opened, "opened 应为 false")
    expect(snapshot.engine.device == nil, "无设备")
    expect(snapshot.position == nil, "无位置")
    expectEqual(snapshot.error, "未检测到 iOS 设备", "错误信息透传")
}

test("后端未知字段不应破坏解析") {
    let json = """
    {"app": {"name": "LocPilot", "version": "2.0.0", "extra": {"nested": [1,2,3]}},
     "engine": {"name": "mock", "opened": true, "device": null, "future_field": "x"},
     "position": {"lat": 1.5, "lon": 2.5, "altitude": 7}, "logs": [], "something_new": true}
    """
    let snapshot = try JSONDecoder().decode(StatusSnapshot.self, from: Data(json.utf8))
    expectEqual(snapshot.app.version, "2.0.0", "版本")
    expect(abs((snapshot.position?.lon ?? 0) - 2.5) < 1e-9, "经度")
}

test("相对路径拼接落在 baseURL 下") {
    let base = URL(string: "http://127.0.0.1:8799/")!
    expectEqual(URL(string: "api/teleport", relativeTo: base)?.absoluteString,
                "http://127.0.0.1:8799/api/teleport", "teleport 地址")
    expectEqual(URL(string: "api/events", relativeTo: base)?.absoluteString,
                "http://127.0.0.1:8799/api/events", "事件流地址")
}

test("错误消息面向用户") {
    expectEqual(EngineError.backend("未检测到 iOS 设备").errorDescription, "未检测到 iOS 设备", "后端错误直传")
    expectEqual(EngineError.badURL("x").errorDescription, "无效地址：x", "URL 错误包装")
}

test("端口探测避开被占用端口") {
    // pickFreePort 是 @MainActor：测试运行器本身跑在主线程，直接断言隔离
    let port = MainActor.assumeIsolated { BackendController().pickFreePort(startingAt: 8799, attempts: 3) }
    expect(port >= 8799 && port <= 8802, "端口应在探测区间内，实际 \(port)")
}

test("connect 结果契约：notes 是字符串数组") {
    let payload = """
    {"engine": "mock", "device": {"name": "LocPilot 虚拟 iPhone", "udid": "MOCK-1", "ios_version": "18.2", "product_type": "iPhone15,3", "connection": "virtual"}, "notes": ["libimobiledevice 无法处理 iOS 27 设备，已自动改用 pymobiledevice3 引擎。"]}
    """
    let result = try JSONDecoder().decode(EngineClient.ConnectResult.self, from: Data(payload.utf8))
    expectEqual(result.engine, "mock", "引擎名")
    expectEqual(result.device?.name, "LocPilot 虚拟 iPhone", "设备名")
    expectEqual(result.notes?.count, 1, "notes 条数")
    expectEqual(result.notes?.first, "libimobiledevice 无法处理 iOS 27 设备，已自动改用 pymobiledevice3 引擎。", "notes 原文")

    // 边界：空数组与字段缺失都不应解码失败
    let emptyNotes = try JSONDecoder().decode(EngineClient.ConnectResult.self,
                                              from: Data(#"{"engine": "mock", "device": null, "notes": []}"#.utf8))
    expectEqual(emptyNotes.notes?.count, 0, "notes 空数组")
    let missingNotes = try JSONDecoder().decode(EngineClient.ConnectResult.self,
                                                from: Data(#"{"engine": "mock", "device": null}"#.utf8))
    expect(missingNotes.notes == nil, "notes 字段缺失时为 nil")
}

test("经纬度结构可编解码（teleport 结果契约）") {
    let payload = """
    {"position": {"lat": 31.2404, "lon": 121.4837}, "history_id": "abc123def456"}
    """
    let result = try JSONDecoder().decode(EngineClient.TeleportResult.self, from: Data(payload.utf8))
    expect(abs(result.position.lat - 31.2404) < 1e-9, "纬度")
    expectEqual(result.historyId, "abc123def456", "history_id 映射")
}

// MARK: - EngineClient 网络行为（URLProtocol 桩：不占端口、不依赖后端）

testAsync("connect 成功：POST /api/connect 并解析设备与 notes") {
    let client = stubClient()
    StubURLProtocol.responder = { _, connection in
        connection.send(status: 200, text: """
        {"engine": "mock",
         "device": {"name": "LocPilot 虚拟 iPhone", "udid": "MOCK-0000-0000-0000-000000000001",
                    "ios_version": "18.2", "product_type": "iPhone15,3", "connection": "virtual"},
         "notes": ["引擎自动降级：mock"]}
        """)
    }
    let result = try await client.connect(engine: "mock")
    expectEqual(result.engine, "mock", "引擎名")
    expectEqual(result.device?.name, "LocPilot 虚拟 iPhone", "设备名")
    expectEqual(result.device?.iosVersion, "18.2", "iOS 版本（snake_case 映射）")
    expectEqual(result.device?.connection, "virtual", "连接方式")
    expectEqual(result.notes, ["引擎自动降级：mock"], "notes 数组透传")

    guard let request = StubURLProtocol.lastRequest else { expect(false, "未记录到请求"); return }
    expectEqual(request.method, "POST", "方法")
    expectEqual(request.path, "/api/connect", "路径")
    expectEqual(request.headers["Content-Type"], "application/json", "Content-Type")
    expectEqual(request.bodyJSON?["engine"] as? String, "mock", "请求体 engine")
    expectEqual(request.bodyJSON?.count, 1, "请求体只有 engine 一个键")
}

testAsync("connect 缺省引擎：请求体 engine=auto") {
    let client = stubClient()
    StubURLProtocol.responder = { _, connection in
        connection.send(status: 200, text: #"{"engine": "mock", "device": null, "notes": []}"#)
    }
    let result = try await client.connect()
    expectEqual(result.engine, "mock", "引擎名")
    expect(result.device == nil, "无设备")
    expectEqual(result.notes?.count, 0, "notes 空数组")
    expectEqual(StubURLProtocol.lastRequest?.bodyJSON?["engine"] as? String, "auto", "默认 engine=auto")
}

testAsync("connect 失败：{\"error\"} → EngineError.backend 原文透传") {
    let client = stubClient()
    let original = "引擎不可用: 未发现 iOS 设备：请确认①数据线能传数据②手机已解锁"
    StubURLProtocol.responder = { _, connection in
        connection.send(status: 503, text: "{\"ok\": false, \"error\": \"\(original)\"}")
    }
    guard let error = await captureEngineError("connect 503", { _ = try await client.connect(engine: "pymobiledevice3") }) else { return }
    if case .backend(let message) = error {
        expectEqual(message, original, "错误原文")
        expectEqual(error.errorDescription, original, "errorDescription 等于原文")
    } else {
        expect(false, "应为 EngineError.backend，实际 \(error)")
    }

    // 409：设备操作失败（后端 ApiError 映射）
    StubURLProtocol.responder = { _, connection in
        connection.send(status: 409, text: #"{"ok": false, "error": "设备操作失败: tunnel 建立失败"}"#)
    }
    guard let conflict = await captureEngineError("connect 409", { _ = try await client.connect() }) else { return }
    if case .backend(let message) = conflict {
        expectEqual(message, "设备操作失败: tunnel 建立失败", "409 错误原文")
    } else {
        expect(false, "409 应为 EngineError.backend，实际 \(conflict)")
    }

    // 非 JSON 错误体：回退到 HTTP 状态码，不能丢成空消息
    StubURLProtocol.responder = { _, connection in connection.send(status: 500, text: "<html>boom</html>") }
    guard let fallback = await captureEngineError("connect 500", { _ = try await client.connect() }) else { return }
    if case .backend(let message) = fallback {
        expectEqual(message, "HTTP 500", "非 JSON 错误体回退到状态码")
    } else {
        expect(false, "500 应为 EngineError.backend，实际 \(fallback)")
    }
}

testAsync("teleport：请求体 {lat,lon} 且解析 history_id") {
    let client = stubClient()
    StubURLProtocol.responder = { _, connection in
        connection.send(status: 200, text: #"{"position": {"lat": 31.2304, "lon": 121.4737}, "history_id": "abc123def456"}"#)
    }
    let result = try await client.teleport(lat: 31.2304, lon: 121.4737)
    expectClose(result.position.lat, 31.2304, "结果纬度")
    expectClose(result.position.lon, 121.4737, "结果经度")
    expectEqual(result.historyId, "abc123def456", "history_id 映射")

    guard let request = StubURLProtocol.lastRequest else { expect(false, "未记录到请求"); return }
    expectEqual(request.method, "POST", "方法")
    expectEqual(request.path, "/api/teleport", "路径")
    expectEqual(request.headers["Content-Type"], "application/json", "Content-Type")
    let body = request.bodyJSON
    expectEqual(body?.count, 2, "请求体只有 lat/lon 两个键")
    expectClose((body?["lat"] as? Double) ?? .nan, 31.2304, "请求体纬度")
    expectClose((body?["lon"] as? Double) ?? .nan, 121.4737, "请求体经度")

    // history_id 缺失（简化返回）→ nil，而不是解码失败
    StubURLProtocol.responder = { _, connection in
        connection.send(status: 200, text: #"{"position": {"lat": 1.5, "lon": 2.5}}"#)
    }
    let minimal = try await client.teleport(lat: 1.5, lon: 2.5)
    expect(minimal.historyId == nil, "history_id 缺失时为 nil")
    expectClose(minimal.position.lat, 1.5, "简化返回纬度")
}

testAsync("clear / disconnect：均走 POST，失败抛 backend") {
    let client = stubClient()
    StubURLProtocol.responder = { _, connection in connection.send(status: 200, text: #"{"ok": true}"#) }
    try await client.clear()
    try await client.disconnect()

    let requests = StubURLProtocol.recorded
    expectEqual(requests.count, 2, "两次请求")
    expectEqual(requests.first?.method, "POST", "clear 方法")
    expectEqual(requests.first?.path, "/api/clear", "clear 路径")
    expectEqual(requests.first?.headers["Content-Type"], "application/json", "clear Content-Type")
    expectEqual(requests.first?.bodyText, "{}", "clear 请求体为空对象")
    expectEqual(requests.last?.method, "POST", "disconnect 方法")
    expectEqual(requests.last?.path, "/api/disconnect", "disconnect 路径")
    expectEqual(requests.last?.headers["Content-Type"], "application/json", "disconnect Content-Type")

    StubURLProtocol.responder = { _, connection in connection.send(status: 409, text: #"{"error": "设备未连接"}"#) }
    guard let error = await captureEngineError("clear 409", { try await client.clear() }) else { return }
    if case .backend(let message) = error {
        expectEqual(message, "设备未连接", "clear 失败原文")
    } else {
        expect(false, "clear 失败应为 backend，实际 \(error)")
    }
}

testAsync("status / health：GET 且解析快照") {
    let client = stubClient()
    StubURLProtocol.responder = { request, connection in
        if request.path == "/api/health" {
            connection.send(status: 200, text: #"{"ok": true}"#)
        } else {
            connection.send(status: 200, text: connectedStatusJSON)
        }
    }
    let snapshot = try await client.status()
    expect(snapshot.engine.opened, "opened")
    expectEqual(snapshot.engine.device?.name, "Sein-iPhone 18 Pro", "设备名")
    expectClose(snapshot.position?.lat ?? .nan, 31.2404, "位置纬度")
    let healthy = try await client.health()
    expect(healthy, "health {\"ok\":true} 视为健康")

    let requests = StubURLProtocol.recorded
    expectEqual(requests.count, 2, "两次请求")
    expectEqual(requests.first?.method, "GET", "status 用 GET")
    expectEqual(requests.first?.path, "/api/status", "status 路径")
    expectEqual(requests.last?.method, "GET", "health 用 GET")
    expectEqual(requests.last?.path, "/api/health", "health 路径")

    StubURLProtocol.responder = { _, connection in connection.send(status: 200, text: #"{"status": "ok"}"#) }
    expect(try await client.health(), "status=ok 视为健康")
    StubURLProtocol.responder = { _, connection in connection.send(status: 200, text: #"{"status": "degraded"}"#) }
    let degraded = try await client.health()
    expect(!degraded, "status=degraded 视为不健康")
}

// MARK: - SSE 事件流（/api/events）

testAsync("SSE 事件流：data 行 + 空行分帧、跨块拼接") {
    let client = stubClient()
    StubURLProtocol.responder = { _, connection in
        connection.respond(status: 200, headers: ["Content-Type": "text/event-stream; charset=utf-8"])
        connection.send(": ping\n\n")  // 心跳注释行必须被忽略
        // 同一行被切成两块（"vers" | "ion"），考验跨 didLoad 的拼接
        connection.send("event: snapshot\ndata: {\"app\": {\"name\": \"LocPilot\", \"vers")
        connection.send("ion\": \"1.0.0\"}, \"engine\": {\"opened\": true, \"device\": {\"name\": \"LocPilot 虚拟 iPhone\", \"ios_version\": \"18.2\"}}}")
        connection.send("\n\n")  // 空行 → 第一帧结束
        connection.send("event: snapshot\ndata: " + #"{"app": {"name": "LocPilot", "version": "2.0.0"}, "engine": {"opened": false}}"# + "\n\n")
        // 不调用 finish()：真实 /api/events 是长连接，由测试 break 结束消费
    }
    var received: [StatusSnapshot] = []
    for await snapshot in client.events() {
        received.append(snapshot)
        if received.count >= 2 { break }
    }
    expectEqual(received.count, 2, "应解析出 2 帧快照")
    guard received.count == 2 else { return }
    expectEqual(received[0].app.version, "1.0.0", "第一帧版本（跨块拼接）")
    expect(received[0].engine.opened, "第一帧 opened")
    expectEqual(received[0].engine.device?.name, "LocPilot 虚拟 iPhone", "第一帧设备名")
    expectEqual(received[0].engine.device?.iosVersion, "18.2", "第一帧 iOS 版本")
    expectEqual(received[1].app.version, "2.0.0", "第二帧版本")
    expect(!received[1].engine.opened, "第二帧 opened 为 false")
}

testAsync("SSE 事件流：帧到达即产出，不等连接关闭") {
    let client = stubClient()
    StubURLProtocol.responder = { _, connection in
        connection.respond(status: 200, headers: ["Content-Type": "text/event-stream"])
        connection.send(#"data: {"app": {"name": "LocPilot", "version": "1.0.0"}, "engine": {"opened": true}}"# + "\n\n")
        Thread.sleep(forTimeInterval: 1.2)  // 连接保持打开，第二帧 1.2s 后才来
        connection.send(#"data: {"app": {"name": "LocPilot", "version": "2.0.0"}, "engine": {"opened": false}}"# + "\n\n")
    }
    let started = Date()
    var versions: [String] = []
    var firstFrameDelay = -1.0
    var finishedWhenFirstFrame = -1
    for await snapshot in client.events() {
        if firstFrameDelay < 0 {
            firstFrameDelay = Date().timeIntervalSince(started)
            finishedWhenFirstFrame = StubURLProtocol.finishedResponses
        }
        versions.append(snapshot.app.version)
        if versions.count >= 2 { break }
    }
    expectEqual(versions, ["1.0.0", "2.0.0"], "帧顺序")
    expect(firstFrameDelay >= 0 && firstFrameDelay < 1.0,
           "第一帧应在 1.2s 睡眠结束前产出（实测 \(String(format: "%.2f", firstFrameDelay))s）")
    expectEqual(finishedWhenFirstFrame, 0, "第一帧产出时响应尚未结束（流式而非整体缓冲）")
}

testAsync("SSE 事件流：坏 JSON 帧被跳过，后续帧照常") {
    let client = stubClient()
    StubURLProtocol.responder = { _, connection in
        connection.respond(status: 200, headers: ["Content-Type": "text/event-stream"])
        connection.send("data: {这不是 JSON}\n\n")
        Thread.sleep(forTimeInterval: 0.05)
        connection.send(#"data: {"app": {"name": "LocPilot", "version": "9.9.9"}, "engine": {"opened": true}}"# + "\n\n")
    }
    var received: [StatusSnapshot] = []
    for await snapshot in client.events() {
        received.append(snapshot)
        if received.count >= 1 { break }
    }
    expectEqual(received.count, 1, "坏帧不产出、后续帧产出")
    expectEqual(received.first?.app.version, "9.9.9", "后续帧版本")
}

test("逆地理编码结果契约：短名优先，回落完整地址") {
    let json = #"{"result": {"name": "上海外滩", "display_name": "外滩, 黄浦区, 上海市, 中国"}}"#
    struct Envelope: Decodable { let result: Place? }
    let envelope = try JSONDecoder().decode(Envelope.self, from: Data(json.utf8))
    expectEqual(envelope.result?.name, "上海外滩", "name")
    expectEqual(envelope.result?.displayName, "外滩, 黄浦区, 上海市, 中国", "display_name 映射")
    expectEqual(envelope.result?.shortLabel, "上海外滩", "短名优先")
}

test("逆地理编码结果契约：查不到时 result 为 null") {
    struct Envelope: Decodable { let result: Place? }
    let envelope = try JSONDecoder().decode(Envelope.self, from: Data(#"{"result": null}"#.utf8))
    expect(envelope.result == nil, "result 应为 nil")
}

test("逆地理编码结果契约：只有 display_name 时用它兜底") {
    struct Envelope: Decodable { let result: Place? }
    let envelope = try JSONDecoder().decode(Envelope.self, from: Data(#"{"result": {"display_name": "某地, 某省"}}"#.utf8))
    expectEqual(envelope.result?.shortLabel, "某地, 某省", "回落完整地址")
}

// MARK: - 地图大头针定位动画规格符合性（用例见 PinAnimatorTests.swift）

registerPinAnimatorTests()

finish()
