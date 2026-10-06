import Combine
import Foundation

/// 后端守护：定位 Python 解释器、拉起 LocPilot HTTP 服务、健康检查与退出清理。
///
/// 解释器优先级（真实设备需要 pymobiledevice3，mock 引擎任意 python3 均可）：
///   1. 环境变量 LOCPILOT_PYTHON
///   2. ~/Library/Application Support/LocPilot/venv/bin/python3   （App 内「安装引擎」生成）
///   3. 包内向上查找 <repo>/.venv/bin/python                      （开发时 .app 位于仓库内）
///   4. /opt/homebrew/bin/python3 → /usr/local/bin/python3 → /usr/bin/python3
@MainActor
public final class BackendController: ObservableObject {
    public enum Phase: Equatable {
        case idle
        case starting
        case running
        case failed(String)

        public var text: String {
            switch self {
            case .idle: return "未启动"
            case .starting: return "正在启动引擎…"
            case .running: return "已就绪"
            case .failed(let message): return "启动失败：" + message
            }
        }
    }

    public init() {}

    @Published public private(set) var phase: Phase = .idle
    @Published public private(set) var logLines: [String] = []
    @Published public private(set) var port: Int = 8799
    @Published public private(set) var pythonPath: String = ""
    @Published public private(set) var engineReady: Bool = false

    private var process: Process?
    private var stdoutPipe: Pipe?
    private var stderrPipe: Pipe?

    /// 状态目录。默认 ~/Library/Application Support/LocPilot；
    /// 可用 LOCPILOT_APP_SUPPORT 重定向（CI、便携模式、只读家目录环境）。
    public static var appSupport: URL {
        if let override = ProcessInfo.processInfo.environment["LOCPILOT_APP_SUPPORT"], !override.isEmpty {
            let dir = URL(fileURLWithPath: override)
            try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            return dir
        }
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent("Library/Application Support")
        let dir = base.appendingPathComponent("LocPilot", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }

    public var baseURL: URL { URL(string: "http://127.0.0.1:" + String(port) + "/")! }
    public var isRunning: Bool { process?.isRunning == true }

    // MARK: - 路径解析

    /// App 内置后端目录（Contents/Resources/backend，含 locpilot/ 与 web/）。
    public var bundledBackend: URL? {
        guard let resources = Bundle.main.resourceURL else { return nil }
        let candidate = resources.appendingPathComponent("backend", isDirectory: true)
        return FileManager.default.fileExists(atPath: candidate.path) ? candidate : nil
    }

    /// 开发模式：.app 位于仓库内时，直接使用仓库源码（改代码即时生效）。
    public func repositoryRoot() -> URL? {
        if let baked = Bundle.main.object(forInfoDictionaryKey: "LPRepositoryPath") as? String, !baked.isEmpty {
            let url = URL(fileURLWithPath: baked)
            if FileManager.default.fileExists(atPath: url.appendingPathComponent("locpilot/__init__.py").path) {
                return url
            }
        }
        var current = URL(fileURLWithPath: Bundle.main.bundlePath)
        for _ in 0..<6 {
            current.deleteLastPathComponent()
            if FileManager.default.fileExists(atPath: current.appendingPathComponent("locpilot/__init__.py").path) {
                return current
            }
        }
        return nil
    }

    public func pythonCandidates() -> [String] {
        var list: [String] = []
        if let explicit = ProcessInfo.processInfo.environment["LOCPILOT_PYTHON"], !explicit.isEmpty {
            list.append(explicit)
        }
        list.append(Self.appSupport.appendingPathComponent("venv/bin/python3").path)
        if let repo = repositoryRoot() {
            list.append(repo.appendingPathComponent(".venv/bin/python").path)
        }
        list.append("/opt/homebrew/bin/python3")
        list.append("/usr/local/bin/python3")
        list.append("/usr/bin/python3")
        return list
    }

    private func firstExecutable(_ paths: [String]) -> String? {
        for path in paths where FileManager.default.isExecutableFile(atPath: path) {
            return path
        }
        return nil
    }

    public func installationPython() -> String? {
        firstExecutable(pythonCandidates())
    }

    /// 引擎能力探测：该解释器能否 import pymobiledevice3。
    public func hasEngine(_ python: String) -> Bool {
        let probe = Process()
        probe.executableURL = URL(fileURLWithPath: python)
        probe.arguments = ["-c", "import pymobiledevice3, sys; sys.stdout.write('ok')"]
        let pipe = Pipe()
        probe.standardOutput = pipe
        probe.standardError = Pipe()
        do {
            try probe.run()
            probe.waitUntilExit()
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            return String(data: data, encoding: .utf8)?.contains("ok") == true
        } catch {
            return false
        }
    }

    // MARK: - 启动 / 停止

    public func pickFreePort(startingAt first: Int = 8799, attempts: Int = 12) -> Int {
        for offset in 0..<attempts {
            let candidate = first + offset
            if !Self.portInUse(candidate) { return candidate }
        }
        return first
    }

    public static func portInUse(_ port: Int) -> Bool {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/nc")
        task.arguments = ["-z", "127.0.0.1", String(port)]
        task.standardOutput = Pipe()
        task.standardError = Pipe()
        do {
            try task.run()
            task.waitUntilExit()
            return task.terminationStatus == 0
        } catch {
            return false
        }
    }

    public func start() {
        guard process == nil else { return }
        phase = .starting
        logLines.removeAll()

        let candidates = pythonCandidates()
        guard let python = firstExecutable(candidates) else {
            phase = .failed("找不到可用的 Python 3 解释器")
            return
        }
        pythonPath = python
        engineReady = hasEngine(python)
        port = pickFreePort(startingAt: 8799)
        append("解释器: " + python + (engineReady ? "（含 pymobiledevice3 引擎）" : "（无 pymobiledevice3，仅 mock 引擎可用）"))

        var environment = ProcessInfo.processInfo.environment
        // 顺序即优先级：开发时 .app 位于仓库内 → 先用仓库源码（改完重启即生效）；
        // 分发给用户时没有仓库 → 自动回落到包内副本。
        var pathEntries: [String] = []
        if let repo = repositoryRoot() { pathEntries.append(repo.path) }
        if let backend = bundledBackend { pathEntries.append(backend.path) }
        if !pathEntries.isEmpty {
            environment["PYTHONPATH"] = pathEntries.joined(separator: ":")
        }
        // 后端只写 App 支持目录，保持用户主目录干净；也避免沙箱/CI 下的家目录不可写问题
        environment["LOCPILOT_HOME"] = Self.appSupport.path
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONPYCACHEPREFIX"] = Self.appSupport.appendingPathComponent(".pycache").path

        // GUI 进程的 PATH 极窄（launchd 只给 /usr/bin:/bin:/usr/sbin:/sbin），
        // Homebrew 工具与 venv 里的 pymobiledevice3 都找不到——必须显式补 PATH 与引擎路径。
        let inheritedPath = environment["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin"
        environment["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + inheritedPath

        let engineDir = URL(fileURLWithPath: python).deletingLastPathComponent()
        let pmd3Candidates = [
            engineDir.appendingPathComponent("pymobiledevice3").path,
            Self.appSupport.appendingPathComponent("venv/bin/pymobiledevice3").path,
        ]
        if let pmd3 = firstExecutable(pmd3Candidates) {
            environment["LOCPILOT_PMD3"] = pmd3
            environment["LOCPILOT_PMD3_PYTHON"] = python
            append("定位引擎: " + pmd3)
        } else {
            append("未找到 pymobiledevice3；真机功能需先安装引擎（菜单：引擎 → 安装 / 修复定位引擎…）")
        }

        let task = Process()
        task.executableURL = URL(fileURLWithPath: python)
        task.arguments = ["-m", "locpilot", "serve", "--port", String(port), "--tick", "1.0", "--watch-parent"]
        task.environment = environment
        // 关键：python -m 会把"当前目录"插到 sys.path 最前面，优先级高于 PYTHONPATH。
        // 因此工作目录必须与 pathEntries 的第一顺位一致，否则包内副本会盖掉仓库源码，
        // 引擎就会按包内路径去找 .venv 并失败（表现为"未找到 pymobiledevice3"）。
        task.currentDirectoryURL = repositoryRoot() ?? bundledBackend ?? Self.appSupport

        let out = Pipe(), err = Pipe()
        task.standardOutput = out
        task.standardError = err
        out.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return }
            Task { @MainActor in self?.append(text.trimmingCharacters(in: .whitespacesAndNewlines)) }
        }
        err.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return }
            Task { @MainActor in self?.append("stderr: " + text.trimmingCharacters(in: .whitespacesAndNewlines)) }
        }
        stdoutPipe = out
        stderrPipe = err
        task.terminationHandler = { [weak self] proc in
            Task { @MainActor in
                guard let self else { return }
                guard self.process === proc else { return }
                self.append("后端进程退出，状态码 " + String(proc.terminationStatus))
                if case .running = self.phase { self.phase = .failed("后端进程已退出") }
                self.process = nil
            }
        }

        do {
            try task.run()
            process = task
            writeRuntimeInfo()
        } catch {
            phase = .failed(error.localizedDescription)
        }
    }

    /// 等待 /api/health 可用。
    public func waitUntilHealthy(timeout: TimeInterval = 25) async -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        let url = URL(string: baseURL.absoluteString + "api/health")!
        while Date() < deadline {
            if Task.isCancelled { return false }
            do {
                var request = URLRequest(url: url)
                request.timeoutInterval = 2
                let (data, response) = try await URLSession.shared.data(for: request)
                if let http = response as? HTTPURLResponse, http.statusCode == 200,
                   let text = String(data: data, encoding: .utf8), text.contains("true") {
                    phase = .running
                    append("服务就绪: " + baseURL.absoluteString)
                    return true
                }
            } catch {
                // 还没起来，继续等
            }
            try? await Task.sleep(nanoseconds: 400_000_000)
        }
        phase = .failed("健康检查超时（" + String(Int(timeout)) + "s）")
        return false
    }

    public func stop() {
        guard let task = process else { return }
        append("正在停止后端…")
        task.terminationHandler = nil
        task.terminate()
        let deadline = Date().addingTimeInterval(3)
        while task.isRunning && Date() < deadline { usleep(100_000) }
        if task.isRunning { kill(task.processIdentifier, SIGKILL) }
        process = nil
        stdoutPipe?.fileHandleForReading.readabilityHandler = nil
        stderrPipe?.fileHandleForReading.readabilityHandler = nil
        phase = .idle
    }

    private func writeRuntimeInfo() {
        let info: [String: Any] = [
            "pid": process?.processIdentifier ?? 0,
            "port": port,
            "python": pythonPath,
            "engine_ready": engineReady,
            "bundle": Bundle.main.bundlePath,
            "started_at": ISO8601DateFormatter().string(from: Date()),
        ]
        let url = Self.appSupport.appendingPathComponent("runtime.json")
        if let data = try? JSONSerialization.data(withJSONObject: info, options: [.prettyPrinted, .sortedKeys]) {
            try? data.write(to: url)
        }
    }

    private func append(_ line: String) {
        guard !line.isEmpty else { return }
        logLines.append(line)
        if logLines.count > 400 { logLines.removeFirst(logLines.count - 400) }
    }

    // MARK: - 轻量 REST 客户端（供菜单命令使用）

    func request(_ path: String, method: String = "GET", body: [String: Any]? = nil) async -> (Int, Data) {
        var request = URLRequest(url: URL(string: baseURL.absoluteString + path)!)
        request.httpMethod = method
        request.timeoutInterval = 30
        if let body {
            request.httpBody = try? JSONSerialization.data(withJSONObject: body)
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            return ((response as? HTTPURLResponse)?.statusCode ?? 0, data)
        } catch {
            return (0, Data())
        }
    }
}
