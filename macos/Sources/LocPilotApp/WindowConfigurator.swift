import AppKit
import SwiftUI

/// 全面屏窗口配置。
///
/// 两个必须遵守的点（都是踩过的坑）：
/// 1. 绝不在 viewDidMoveToWindow 里直接改窗口 —— 那一刻 AppKit 正在跑布局，
///    改 styleMask 会重入约束求解，异常会被 NSApplication 变成 SIGTRAP（启动即闪退）。
///    所以统一 DispatchQueue.main.async 推到下一个 runloop。
/// 2. 不使用任何 KVC 私有键（历史事故：setValue(forKey: "pageZoomEnabled") 抛
///    NSUnknownKeyException → 同样表现为启动闪退）。
final class WindowConfiguratorView: NSView {
    private var didConfigure = false

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        guard let window, !didConfigure else { return }
        didConfigure = true
        DispatchQueue.main.async {
            window.styleMask.insert(.fullSizeContentView)
            window.titlebarAppearsTransparent = true
            window.titleVisibility = .hidden
            window.isMovableByWindowBackground = false
            window.minSize = NSSize(width: 900, height: 620)
            // 首启时 applicationDidFinishLaunching 里的 activate 早于 SwiftUI 建窗口，
            // 窗口会落在其它应用后面，看起来像"点了没反应"，于是用户再点一次才被带到前台。
            // 这里在窗口真正就绪后再抢一次前台，保证一次启动就可见。
            Self.bringToFront(window)
            for delay in [0.15, 0.45, 1.0] {
                DispatchQueue.main.asyncAfter(deadline: .now() + delay) { Self.bringToFront(window) }
            }
        }
    }
}

extension WindowConfiguratorView {
    /// 把窗口带到前台：makeKeyAndOrderFront + 激活应用，两步都要，缺一会被系统忽略。
    static func bringToFront(_ window: NSWindow) {
        NSApp.setActivationPolicy(.regular)
        // macOS 14+ 会限制应用抢焦点：activate 可能被忽略，窗口就落在别的应用后面，
        // 用户看着像"点了没反应、要再点一次"。orderFrontRegardless 是唯一无条件的置顶手段，
        // 三个动作叠加 + 多次重试才能稳定"一次启动就到前台"。
        window.makeKeyAndOrderFront(nil)
        window.orderFrontRegardless()
        NSApp.activate(ignoringOtherApps: true)
        NSRunningApplication.current.activate(options: [.activateAllWindows, .activateIgnoringOtherApps])
    }
}

struct WindowConfigurator: NSViewRepresentable {
    func makeNSView(context: Context) -> NSView { WindowConfiguratorView() }
    func updateNSView(_ nsView: NSView, context: Context) {}
}
