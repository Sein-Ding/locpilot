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
    var body: some View {
        VStack(spacing: 10) {
            StatusButton()
            MapActionCapsule()
            ZoomCapsule()
        }
    }
}

// MARK: - 设备状态（圆形）

private struct StatusButton: View {
    @EnvironmentObject var state: AppState

    var body: some View {
        Button {
            // 点图标只展开小弹框：设备状态与操作都在里面，不打扰主界面
            state.devicePopoverShown.toggle()
        } label: {
            Image(systemName: "iphone")
                .font(.system(size: 16, weight: .medium))
                .foregroundStyle(state.statusColor)
                .shadow(color: state.statusColor.opacity(0.9), radius: 8)
                .frame(width: 42, height: 42)
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
