import AppKit
import SwiftUI

/// 顶部拖拽热区：把标题栏区域的鼠标拖动交给窗口去"移动窗口"，
/// 而不是被地图的平移手势吃掉；同时上报悬停状态，让界面浮现半透明标题栏。
///
/// 背景：窗口用了 .hiddenTitleBar + fullSizeContentView，地图铺满到标题栏底下，
/// 于是拖动标题栏时窗口在动、地图也跟着轻微平移一下（用户实测反馈）。
///
/// 三个关键处理：
/// 1. mouseDown 转成窗口拖拽（performDrag）；
/// 2. 悬停用 NSTrackingArea 全宽跟踪 —— 它独立于 hitTest，所以左端也能触发提示；
/// 3. hitTest 只在 x ≥ 78 时接管，左端放行，红绿灯按钮照常可点。
struct TitlebarDragArea: NSViewRepresentable {
    /// 鼠标是否停在窗口顶部
    var onHoverChange: (Bool) -> Void

    func makeNSView(context: Context) -> NSView {
        let view = DragView()
        view.onHoverChange = onHoverChange
        return view
    }

    func updateNSView(_ nsView: NSView, context: Context) {
        (nsView as? DragView)?.onHoverChange = onHoverChange
    }

    private final class DragView: NSView {
        var onHoverChange: ((Bool) -> Void)?
        private var trackingArea: NSTrackingArea?
        private var monitor: Any?

        /// 左上角红绿灯按钮的宽度：这片区域不接管事件，按钮才点得动。
        private let trafficLightWidth: CGFloat = 78

        override var mouseDownCanMoveWindow: Bool { true }

        override func mouseDown(with event: NSEvent) {
            window?.performDrag(with: event)
        }

        override func updateTrackingAreas() {
            super.updateTrackingAreas()
            if let trackingArea { removeTrackingArea(trackingArea) }
            // 刻意不用 .inVisibleRect：SwiftUI 托管下这个视图的 visibleRect 会是**整个窗口**
            // （实测 (0,-708,1180,760)，而 bounds 只有 52 高），有效跟踪区因此被放大到全窗，
            // 悬停判定就跟条的实际位置对不上。用显式 bounds 矩形才准。
            let area = NSTrackingArea(rect: bounds,
                                      options: [.mouseEnteredAndExited, .activeInKeyWindow, .enabledDuringMouseDrag],
                                      owner: self, userInfo: nil)
            addTrackingArea(area)
            trackingArea = area
        }

        /// 鼠标移动时按真实坐标判定：trackingArea 只负责"进入/离开"，
        /// 精确判定交给这里的几何计算，避免再受 AppKit 可见区域推算的影响。
        private func updateHoverFromPointer() {
            guard let window, window.isKeyWindow else { return }
            let pointer = convert(window.mouseLocationOutsideOfEventStream, from: nil)
            onHoverChange?(bounds.contains(pointer))
        }


        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            guard window != nil, monitor == nil else { return }
            monitor = NSEvent.addLocalMonitorForEvents(matching: [.mouseMoved, .leftMouseDragged]) { [weak self] event in
                self?.updateHoverFromPointer()
                return event
            }
        }

        deinit {
            if let monitor { NSEvent.removeMonitor(monitor) }
        }

        override func mouseEntered(with event: NSEvent) {
            updateHoverFromPointer()
        }
        override func mouseExited(with event: NSEvent) { onHoverChange?(false) }

        /// 只有落在自己范围内、且不在红绿灯区域时接收事件；其余一律放行。
        override func hitTest(_ point: NSPoint) -> NSView? {
            let local = convert(point, from: superview)
            guard bounds.contains(local) else { return nil }
            return local.x < trafficLightWidth ? nil : self
        }
    }
}

/// 悬停顶部时浮现的半透明工具栏：告诉用户"这里是窗口顶部，可以拖动"。
/// 鼠标移到中间就淡出，地图恢复全面屏。
///
/// 直角、通栏；高度 = 中心线 × 2，让红绿灯与设备图标正好在它的垂直正中。
struct TitlebarVeil: View {
    /// 与拖拽热区同高。中心线 26pt → 高度 52pt。
    static let height: CGFloat = 52

    var body: some View {
        Rectangle()
            .fill(.ultraThinMaterial)
            .frame(height: Self.height)
            .overlay(alignment: .bottom) {
                // 下沿一条极细分隔线，勾出条的下边界
                Rectangle()
                    .fill(Color.primary.opacity(0.12))
                    .frame(height: 0.5)
            }
            .allowsHitTesting(false)      // 绝不能挡住下面拖拽热区
    }
}
