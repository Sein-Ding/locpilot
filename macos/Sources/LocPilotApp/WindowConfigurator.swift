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
            // 红绿灯默认紧贴左上角，比 Apple 地图更"顶"；整体下移一点更接近原生的视觉重心。
            Self.shiftTrafficLights(in: window)
            // AppKit 在窗口尺寸变化/进出全屏时会重新布局标题栏，把我们的偏移冲掉，所以跟着补一次。
            for name in [NSWindow.didResizeNotification, NSWindow.didEnterFullScreenNotification, NSWindow.didExitFullScreenNotification] {
                NotificationCenter.default.addObserver(forName: name, object: window, queue: .main) { _ in
                    Self.shiftTrafficLights(in: window)
                }
            }
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
    /// 红绿灯的位移量（pt）。
    /// 实测（探针跑出来的系统事实）：NSTitlebarView 高 32pt、未翻转，
    /// 系统把三个按钮垂直居中放在 y=9、第一个按钮 x=9。
    /// 这比 Apple 地图更贴左上角，所以往右下各收一点。
    /// 下移 10pt = 目标中心线 26pt − 标题栏居中位置 16pt。
    /// 目标中心线由设备图标决定（顶部内边距 5 + 图标半径 21 = 26），
    /// 悬停条高度 = 26 × 2 = 52pt。改图标尺寸或顶部内边距时，这个值要同步改。
    private static let trafficLightDrop: CGFloat = 10
    private static let trafficLightInset: CGFloat = 4   // 右移

    /// 只动三个按钮本身，不碰标题栏容器 —— 动容器会连带影响拖动区域与布局。
    /// 计算基于"系统居中位置"，所以重复调用幂等，不会越移越低。
    static func shiftTrafficLights(in window: NSWindow) {
        for type in [NSWindow.ButtonType.closeButton, .miniaturizeButton, .zoomButton] {
            guard let button = window.standardWindowButton(type), let host = button.superview else { continue }
            var frame = button.frame
            // 坐标系未翻转时 y 自下而上，"下移"= 减小 y；翻转时要反过来。
            let centeredY = (host.bounds.height - frame.height) / 2
            frame.origin.y = host.isFlipped ? centeredY + trafficLightDrop : centeredY - trafficLightDrop
            frame.origin.x += trafficLightInset
            if abs(button.frame.origin.y - frame.origin.y) > 0.5 || abs(button.frame.origin.x - frame.origin.x) > 0.5 {
                button.setFrameOrigin(frame.origin)
            }
        }
    }

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
