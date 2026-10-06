import AppKit
import LocPilotKit
import SwiftUI

@main
struct LocPilotApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @StateObject private var state = AppState()

    init() {
        // 无界面自检入口：LocPilot --selftest（供 build.sh / CI 断言用，不会创建窗口）
        if CommandLine.arguments.contains("--selftest") {
            SelfTest.run()
        }
    }

    var body: some Scene {
        WindowGroup("LocPilot") {
            RootView()
                .environmentObject(state)
                .background(WindowConfigurator())
        }
        // 全面屏：内容铺满整窗，交通灯浮在页面上方（窗口细节由 WindowConfigurator 补齐）
        .windowStyle(.hiddenTitleBar)
        .defaultSize(width: 1180, height: 760)
        .commands {
            CommandGroup(replacing: .newItem) { }

            CommandMenu("设备") {
                Button("连接设备") { Task { await state.connect() } }
                    .keyboardShortcut("k", modifiers: .command)
                Button("断开设备") { Task { await state.disconnect() } }
                    .keyboardShortcut("k", modifiers: [.command, .shift])
                Button("恢复真实定位") { Task { await state.clearLocation() } }
                    .keyboardShortcut("c", modifiers: [.command, .shift])
            }

            CommandMenu("引擎") {
                Button("安装 / 修复定位引擎…") { state.showInstaller = true }
                Button("打开状态目录") { NSWorkspace.shared.open(BackendController.appSupport) }
            }
        }
    }
}

/// 根视图：后端就绪前显示启动画面，就绪后交给地图。
struct RootView: View {
    @EnvironmentObject var state: AppState

    var body: some View {
        // ZStack 的身份是稳定的：Group/Splash→Map 的内容切换不会重启 .task
        ZStack {
            if state.backendReady {
                MapScreen()
            } else {
                SplashView()
            }
        }
        .frame(minWidth: 900, minHeight: 600)
        .sheet(isPresented: $state.showInstaller) {
            EngineInstallerView(
                installer: state.installer,
                basePython: state.backend.installationPython() ?? "/usr/bin/python3",
                onClose: { state.showInstaller = false }
            )
        }
        .task { await state.bootstrap() }
        .onAppear { AppDelegate.state = state }
    }
}

/// 启动画面：后端进程拉起 + 健康检查期间显示。
struct SplashView: View {
    @EnvironmentObject var state: AppState

    var body: some View {
        VStack(spacing: 14) {
            Image(systemName: "location.north.circle.fill")
                .font(.system(size: 52))
                .foregroundStyle(.tint)
            Text("LocPilot").font(.largeTitle).bold()
            Text(state.phase.text).foregroundStyle(.secondary)
            if case .failed(let message) = state.phase {
                Text(message).font(.caption).foregroundStyle(.red)
                Button("重试") { Task { await state.bootstrap() } }
                    .padding(.top, 4)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(.background)
    }
}

/// 应用生命周期：负责激活策略与退出时回收后端进程（避免留下孤儿端口）。
final class AppDelegate: NSObject, NSApplicationDelegate {
    static weak var state: AppState?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    /// 再次双击 App（或点 Dock 图标）时，把已有窗口带到前台而不是"没反应"。
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        NSApp.activate(ignoringOtherApps: true)
        if !flag, let window = sender.windows.first(where: { $0.canBecomeKey }) {
            window.makeKeyAndOrderFront(nil)
        }
        return true
    }

    func applicationWillTerminate(_ notification: Notification) {
        MainActor.assumeIsolated { AppDelegate.state?.shutdown() }
    }
}
