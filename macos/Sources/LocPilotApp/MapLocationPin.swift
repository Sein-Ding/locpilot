import LocPilotKit
import SwiftUI

/// 地图定位大头针。
///
/// 结构分成互不干扰的两层（规格 §2）：
/// * 地面阴影：独立于针体做缩放 / 透明度 / 模糊，跟随"针离地高度"变化；
/// * 针体：单一结构，靠 bodyColor + symbol 动态变化完成红→绿、无→✓/! 的形态转换，
///   不使用三张图片替换。
///
/// 锚点：整个组件的底部中心就是针尖，调用方用 Annotation(anchor: .bottom) 把它钉在坐标上；
/// 所有缩放与旋转都以 .bottom 为轴，保证针尖永远不离开地图坐标（规格 §3 / §13）。
struct MapLocationPin: View {
    @ObservedObject var animator: PinAnimator
    let label: String

    private let dropHeight: Double = 80

    var body: some View {
        ZStack(alignment: .bottom) {
            groundShadow
            targetRing
            ripple

            pinBody
                .scaleEffect(x: 1 + 0.04 * animator.impact,
                             y: 1 - 0.07 * animator.impact,
                             anchor: .bottom)
                .rotationEffect(.degrees(animator.shake), anchor: .bottom)
                .offset(y: -dropHeight * (1 - animator.drop))
        }
        .frame(width: 44, height: 52, alignment: .bottom)
        .overlay(alignment: .bottom) { labelView }
    }

    // MARK: - 针体（同一结构，动态颜色与符号）

    private var pinBody: some View {
        ZStack {
            PinTail()
                .fill(bodyColor)
                .frame(width: 13, height: 15)
                .offset(y: 18.5)          // 尾尖正好落在 52pt 高的底边
            Circle()
                .fill(bodyColor)
                .frame(width: 38, height: 38)
            symbolLayer
        }
        .frame(width: 44, height: 52)
        .shadow(color: .black.opacity(0.35 * animator.drop), radius: 3, y: 2)
    }

    private var symbolLayer: some View {
        ZStack {
            Checkmark()
                .trim(from: 0, to: animator.checkDraw)
                .stroke(Color.white, style: StrokeStyle(lineWidth: 3, lineCap: .round, lineJoin: .round))
                .frame(width: 17, height: 17)
                .opacity(animator.checkDraw > 0.001 ? 1 : 0)

            ExclamationMark()
                .opacity(animator.exclamation)
        }
    }

    /// 红（#FF3B30）→ 绿（#34C759）连续插值，保证 Morph 自然。
    private var bodyColor: Color {
        let t = min(max(animator.morph, 0), 1)
        return Color(red: 1.0 - 0.796 * t,
                     green: 0.231 + 0.549 * t,
                     blue: 0.188 + 0.161 * t)
    }

    // MARK: - 地面阴影（随高度变化，规格 §6）

    private var groundShadow: some View {
        Ellipse()
            .fill(Color.black.opacity(0.26 - 0.18 * (1 - animator.drop)))
            .frame(width: 30 * shadowScale, height: 8 * shadowScale)
            .blur(radius: 6 + 8 * (1 - animator.drop))
            .offset(y: 2)
    }

    private var shadowScale: Double { 1.0 + 0.6 * (1 - animator.drop) }

    // MARK: - 目标圆环与落地波纹

    private var targetRing: some View {
        Circle()
            .stroke(Color.white.opacity(0.9), lineWidth: 1.5)
            .frame(width: 26, height: 26)
            .scaleEffect(0.8 + 0.2 * animator.ring)
            .opacity(0.4 * animator.ring)
            .offset(y: 13)                 // 圆环中心落在针尖上
    }

    private var ripple: some View {
        Circle()
            .stroke(rippleColor.opacity(0.35 * (1 - animator.ripple)), lineWidth: 1.8)
            .frame(width: 30, height: 30)
            .scaleEffect(0.5 + animator.ripple)
            .offset(y: 15)
    }

    private var rippleColor: Color {
        switch animator.rippleKind {
        case .neutral: return Color(white: 0.95)
        case .success: return Color(red: 0.204, green: 0.780, blue: 0.349)
        case .error: return Color(red: 1.0, green: 0.231, blue: 0.188)
        }
    }

    // MARK: - 名称（针尖下方，不进布局）

    private var labelView: some View {
        Group {
            if !label.isEmpty {
                Text(label)
                    .font(.system(size: 14, weight: .bold))
                    .foregroundStyle(.white)
                    .shadow(color: .black.opacity(0.9), radius: 1)
                    .shadow(color: .black.opacity(0.5), radius: 3)
                    .lineLimit(1)
                    .fixedSize()
                    .offset(y: 24)
                    .transition(.opacity)
            }
        }
    }
}

// MARK: - 形状

/// 针尾：一个向下的三角，底边与组件底边重合（决定针尖位置）。
private struct PinTail: Shape {
    func path(in rect: CGRect) -> Path {
        var path = Path()
        path.move(to: CGPoint(x: rect.minX, y: rect.minY))
        path.addLine(to: CGPoint(x: rect.maxX, y: rect.minY))
        path.addLine(to: CGPoint(x: rect.midX, y: rect.maxY))
        path.closeSubpath()
        return path
    }
}

/// 对勾：两段折线，靠 trim 做"从左下向右上画出"的绘制动画。
private struct Checkmark: Shape {
    func path(in rect: CGRect) -> Path {
        var path = Path()
        path.move(to: CGPoint(x: rect.minX + rect.width * 0.10, y: rect.midY + rect.height * 0.08))
        path.addLine(to: CGPoint(x: rect.minX + rect.width * 0.40, y: rect.maxY - rect.height * 0.14))
        path.addLine(to: CGPoint(x: rect.maxX - rect.width * 0.08, y: rect.minY + rect.height * 0.16))
        return path
    }
}

/// 感叹号：短棒 + 圆点，比字体渲染更可控。
private struct ExclamationMark: View {
    var body: some View {
        VStack(spacing: 2.5) {
            Capsule().frame(width: 4.5, height: 9.5)
            Circle().frame(width: 4.5, height: 4.5)
        }
        .foregroundStyle(Color.white)
        .offset(y: 0.5)
    }
}
