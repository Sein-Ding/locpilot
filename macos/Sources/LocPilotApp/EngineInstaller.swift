import SwiftUI
import LocPilotKit
import Foundation

/// App 内「安装 / 修复定位引擎」：在 ~/Library/Application Support/LocPilot/venv 里装 pymobiledevice3。
/// 不触碰系统 Python，也不需要 sudo（隧道由 pymobiledevice3 自己以用户态建立）。
@MainActor
final class EngineInstaller: ObservableObject {
    @Published private(set) var running = false
    @Published private(set) var finished = false
    @Published private(set) var success = false
    @Published private(set) var log: [String] = []

    private var task: Process?

    var venvPython: String {
        BackendController.appSupport.appendingPathComponent("venv/bin/python3").path
    }

    func append(_ line: String) {
        guard !line.isEmpty else { return }
        log.append(line)
        if log.count > 500 { log.removeFirst(log.count - 500) }
    }

    func install(basePython: String) {
        guard !running else { return }
        running = true
        finished = false
        success = false
        log.removeAll()
        append("基础解释器: " + basePython)
        append("目标 venv: " + BackendController.appSupport.appendingPathComponent("venv").path)

        let script = """
        set -e
        export PYTHONPYCACHEPREFIX="$3"
        export PYTHONDONTWRITEBYTECODE=1
        export PIP_NO_CACHE_DIR=1
        export PIP_DISABLE_PIP_VERSION_CHECK=1
        BASE="$1"
        VENV="$2"
        if [ ! -x "$VENV/bin/python3" ]; then "$BASE" -m venv "$VENV"; fi
        "$VENV/bin/python3" -m pip install --upgrade pip setuptools wheel || true
        "$VENV/bin/python3" -m pip install pymobiledevice3
        "$VENV/bin/python3" -c "import pymobiledevice3,sys;from importlib.metadata import version;sys.stdout.write('ENGINE_OK '+version('pymobiledevice3'))"
        """

        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/bash")
        process.arguments = ["-lc", script, "install-engine", basePython, BackendController.appSupport.appendingPathComponent("venv").path,
                             BackendController.appSupport.appendingPathComponent(".pycache").path]
        var environment = ProcessInfo.processInfo.environment
        environment["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        process.environment = environment

        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return }
            Task { @MainActor in self?.append(text.trimmingCharacters(in: .whitespacesAndNewlines)) }
        }
        process.terminationHandler = { [weak self] proc in
            Task { @MainActor in
                guard let self else { return }
                self.running = false
                self.finished = true
                self.success = proc.terminationStatus == 0
                self.append(proc.terminationStatus == 0 ? "安装完成 ✅ 重启 App 后生效" : "安装失败，退出码 " + String(proc.terminationStatus))
            }
        }
        do {
            try process.run()
            task = process
        } catch {
            running = false
            finished = true
            success = false
            append("无法启动安装进程: " + error.localizedDescription)
        }
    }

    func cancel() {
        task?.terminate()
        task = nil
        running = false
    }
}

struct EngineInstallerView: View {
    @ObservedObject var installer: EngineInstaller
    let basePython: String
    var onClose: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("安装 / 修复定位引擎").font(.title3).bold()
            Text("在 ~/Library/Application Support/LocPilot/venv 中安装 pymobiledevice3（约 40 MB，无需 sudo）。安装完成后重启 LocPilot 即可连接真机。")
                .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)

            ScrollView {
                Text(installer.log.joined(separator: "\n"))
                    .font(.system(.caption, design: .monospaced))
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .textSelection(.enabled)
                    .padding(8)
            }
            .frame(minHeight: 220)
            .background(Color(nsColor: .textBackgroundColor))
            .clipShape(RoundedRectangle(cornerRadius: 8))

            HStack {
                if installer.running {
                    ProgressView().controlSize(.small)
                    Text("安装中…").foregroundStyle(.secondary)
                    Button("取消") { installer.cancel() }
                } else {
                    Button(installer.finished && !installer.success ? "重试" : "开始安装") {
                        installer.install(basePython: basePython)
                    }
                    .buttonStyle(.borderedProminent)
                }
                Spacer()
                Button("关闭") { onClose() }
            }
        }
        .padding(20)
        .frame(width: 620)
    }
}
