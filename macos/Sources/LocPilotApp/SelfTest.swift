import Foundation
import LocPilotKit

/// 无界面自检：验证原生外壳能否拉起后端、健康检查、引擎枚举，并输出 JSON 供 CI/脚本断言。
enum SelfTest {
    static func syncGet(_ url: URL, timeout: TimeInterval = 5) -> (Int, String) {
        var status = 0
        var body = ""
        let semaphore = DispatchSemaphore(value: 0)
        var request = URLRequest(url: url)
        request.timeoutInterval = timeout
        URLSession.shared.dataTask(with: request) { data, response, _ in
            status = (response as? HTTPURLResponse)?.statusCode ?? 0
            if let data { body = String(data: data, encoding: .utf8) ?? "" }
            semaphore.signal()
        }.resume()
        _ = semaphore.wait(timeout: .now() + timeout + 2)
        return (status, body)
    }

    @MainActor
    static func run() -> Never {
        var failures: [String] = []
        let model = AppState()
        let backend = model.backend

        FileHandle.standardOutput.write("[selftest] 启动后端…\n".data(using: .utf8)!)
        backend.start()

        var healthy = false
        let deadline = Date().addingTimeInterval(30)
        while Date() < deadline {
            let (code, body) = syncGet(URL(string: backend.baseURL.absoluteString + "api/health")!)
            if code == 200, body.contains("true") { healthy = true; break }
            usleep(400_000)
        }
        if !healthy { failures.append("健康检查未通过") }

        var engineCount = 0
        var deviceNames: [String] = []
        if healthy {
            let (code, body) = syncGet(URL(string: backend.baseURL.absoluteString + "api/engines?probe=1")!, timeout: 40)
            if code != 200 {
                failures.append("api/engines 返回 " + String(code))
            } else if let data = body.data(using: .utf8),
                      let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                      let engines = object["engines"] as? [[String: Any]] {
                engineCount = engines.count
                for engine in engines {
                    if let devices = engine["devices"] as? [[String: Any]] {
                        for device in devices {
                            if let name = device["name"] as? String { deviceNames.append(name) }
                        }
                    }
                }
            }
        }

        let (statusCode, statusBody) = healthy
            ? syncGet(URL(string: backend.baseURL.absoluteString + "api/status")!)
            : (0, "")
        if healthy && statusCode != 200 { failures.append("api/status 返回 " + String(statusCode)) }

        // 失败时必须带上后端日志，否则只有一句"健康检查未通过"没法排障
        if !failures.isEmpty {
            FileHandle.standardOutput.write("--- 后端日志（尾部）---\n".data(using: .utf8)!)
            for line in backend.logLines.suffix(25) {
                FileHandle.standardOutput.write((line + "\n").data(using: .utf8)!)
            }
        }

        let summary: [String: Any] = [
            "app": "LocPilot.app",
            "app_support": BackendController.appSupport.path,
            "bundle": Bundle.main.bundlePath,
            "port": backend.port,
            "python": backend.pythonPath,
            "engine_ready": backend.engineReady,
            "healthy": healthy,
            "engines": engineCount,
            "devices": deviceNames,
            "status_bytes": statusBody.count,
            "failures": failures,
        ]
        if let data = try? JSONSerialization.data(withJSONObject: summary, options: [.prettyPrinted, .sortedKeys]),
           let text = String(data: data, encoding: .utf8) {
            FileHandle.standardOutput.write((text + "\n").data(using: .utf8)!)
        }
        backend.stop()
        exit(failures.isEmpty ? 0 : 1)
    }
}
