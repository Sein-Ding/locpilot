import LocPilotKit
import SwiftUI

/// 右上角控件簇 —— 按 Apple 地图的分组规范排列：
///
///     ●  设备状态（圆形，连上后绿色泛光）
///     ▮  定位箭头 + 恢复真实定位（一条胶囊）
///     ▮  ＋ / －（另一条胶囊）
///
/// 每个控件都是独立的浮层（不是一个大盒子塞五个按钮），胶囊只用于成组动作。
struct ControlsCluster: View {
    /// 顶部内边距：让设备图标的**中心**与左上角红绿灯的中心在同一条水平线上。
    ///
    /// 推导（都是实测值，改任一处都要同步）：
    ///   标题栏 32pt，系统把红绿灯垂直居中 → 中心距顶 16pt；
    ///   我们为对齐 44pt 悬停条又下移 6pt → 红绿灯中心距顶 22pt；
    ///   图标直径 42pt，顶部内边距固定 5pt → 图标中心距顶 5 + 42/2 = 26pt；
    ///   红绿灯下移量与该中心线同步；悬停条高度 = 26 × 2 = 52pt，上下等长。
    static let topInset: CGFloat = 5

    /// 这条中心线到窗口顶部的距离：设备图标、红绿灯、悬停条三者共用。
    static var centerLine: CGFloat { topInset + StatusButton.diameter / 2 }

    /// 右侧内边距 = 顶部内边距，让图标"到上边"与"到右边"的间隙相等。
    ///
    /// 这是 iOS 图标贴屏幕圆角的对齐规则：间隙均匀时，圆形轮廓看起来才与窗口圆角"同心"。
    /// （窗口圆角半径读不到 —— AppKit 把它交给窗口服务器渲染，NSThemeFrame.layer.cornerRadius 恒为 0，
    ///  所以不按半径反推，直接用等间隙这条更稳的规则。）
    static let trailingInset: CGFloat = topInset

    var body: some View {
        // 必须显式 .trailing：VStack 默认水平居中，42pt 的圆图标会"居中"在 84pt 的胶囊上方，
        // 右边缘对不齐 —— 看起来就是图标没跟下面的胶囊在同一列上。
        VStack(alignment: .trailing, spacing: 10) {
            StatusButton()
            MapActionCapsule()
            ZoomCapsule()
        }
    }
}

// MARK: - 设备状态（圆形）

private struct StatusButton: View {
    /// 图标直径。恢复原始尺寸；顶部内边距仍是 5pt，所以中心线降到 5 + 42/2 = 26pt，
    /// 红绿灯与悬停条都按这条新中心线对齐。
    static let diameter: CGFloat = 42

    @EnvironmentObject var state: AppState

    var body: some View {
        Button {
            // 点图标只展开小弹框：设备状态与操作都在里面，不打扰主界面
            state.devicePopoverShown.toggle()
        } label: {
            Image(systemName: "iphone")
                .font(.system(size: 16, weight: .medium))
                .foregroundStyle(state.statusColor)
                .shadow(color: state.statusColor.opacity(0.9), radius: 7)
                .frame(width: StatusButton.diameter, height: StatusButton.diameter)
        }
        .buttonStyle(.plain)
        .modifier(GlassChrome(shape: Circle()))
        .popover(isPresented: $state.devicePopoverShown, arrowEdge: .trailing) {
            DevicePopover()
        }
        .help(state.connection.isOnline ? (state.deviceName ?? "已连接") : "设备未连接")
    }
}

/// 手机图标的小弹框：一行状态 + 一个按钮，不放任何解释文案。
private struct DevicePopover: View {
    @EnvironmentObject var state: AppState

    var body: some View {
        HStack(spacing: 10) {
            Circle()
                .fill(state.statusColor)
                .frame(width: 8, height: 8)
                .shadow(color: state.statusColor.opacity(0.8), radius: 4)
            Text(title)
                .font(.system(size: 12, weight: .semibold))
                .lineLimit(1)
            Spacer(minLength: 6)
            action
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .frame(width: 208)
    }

    @ViewBuilder private var action: some View {
        switch state.connection {
        case .online:
            Button("断开连接") {
                state.devicePopoverShown = false
                Task { await state.disconnect() }
            }
            .controlSize(.small)
        case .connecting:
            ProgressView().controlSize(.small)
        default:
            Button("重新连接") {
                state.devicePopoverShown = false
                Task { await state.connect() }
            }
            .controlSize(.small)
        }
    }

    private var title: String {
        switch state.connection {
        case .online: return state.deviceName ?? "已连接"
        case .connecting: return "正在连接…"
        default: return "设备未连接"
        }
    }
}

// MARK: - 定位类动作（一条胶囊）

private struct MapActionCapsule: View {
    @EnvironmentObject var state: AppState

    var body: some View {
        VStack(spacing: 0) {
            CapsuleButton(
                systemName: "location.fill",
                help: state.hasPosition
                    ? (state.followsPosition ? "正在跟随定位（点击停止）" : "跟随定位：镜头随定位自动移动")
                    : "还没有设定位置",
                tint: state.followsPosition ? Color.accentColor : Color.primary,
                dimmed: !state.hasPosition,
                action: state.toggleFollow)
            CapsuleButton(systemName: "arrow.uturn.backward", help: "恢复真实定位", tint: Color.red.opacity(0.9)) {
                Task { await state.clearLocation() }
            }
        }
        .modifier(GlassChrome(shape: Capsule()))
    }
}

// MARK: - 缩放（另一条胶囊）

private struct ZoomCapsule: View {
    @EnvironmentObject var state: AppState

    var body: some View {
        VStack(spacing: 0) {
            CapsuleButton(systemName: "plus", help: "放大") { state.zoom(factor: 0.5) }
            CapsuleButton(systemName: "minus", help: "缩小") { state.zoom(factor: 2) }
        }
        .modifier(GlassChrome(shape: .capsule))
    }
}

/// 胶囊内的单个按钮：整条胶囊是一块玻璃，按钮只负责命中区域与悬停反馈。
private struct CapsuleButton: View {
    let systemName: String
    let help: String
    var tint: Color = .primary
    var dimmed: Bool = false
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: systemName)
                .font(.system(size: 15, weight: .medium))
                .foregroundStyle(tint)
                .frame(width: 42, height: 38)
                .contentShape(Rectangle())
                .opacity(dimmed ? 0.4 : 1)
        }
        .buttonStyle(.plain)
        .help(help)
    }
}

// MARK: - 玻璃材质

/// 系统玻璃容器：macOS 26 起用真正的 Liquid Glass，否则回落到材质。
/// 形状由调用方决定：独立控件用圆形，成组动作用胶囊。
struct GlassChrome<S: InsettableShape>: ViewModifier {
    let shape: S

    func body(content: Content) -> some View {
        if #available(macOS 26.0, *) {
            content.glassEffect(.regular.interactive(), in: shape)
        } else {
            content
                .background(.regularMaterial, in: shape)
                .overlay(shape.strokeBorder(Color.primary.opacity(0.08), lineWidth: 0.5))
                .shadow(color: .black.opacity(0.18), radius: 12, y: 4)
        }
    }
}
