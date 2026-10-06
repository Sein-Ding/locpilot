import Combine
import Foundation
import SwiftUI

/// 地图大头针的状态机与动画时间线。
///
/// 设计约束（见用户给的开发规格）：
/// * 组件只管"怎么动"，业务只管"什么时候动"——成功/失败由业务显式调用 succeed()/fail()，
///   绝不在落下动画末尾自动触发。
/// * 落地后必须停留在 .processing 200–400ms，让用户感觉是"系统在确认"，而不是播完动画就出结果。
/// * 所有单段动画时长控制在 0.1–0.5s（Timings 各项都在这个区间）。
/// * 连续改点时旧时间线要立刻失效（runID 令牌），避免两段动画互相打架。
@MainActor
public final class PinAnimator: ObservableObject {
    public enum Phase: String, Equatable {
        case idle
        case dropping
        case landed
        case processing
        case success
        case error
    }

    public enum RippleKind: String, Equatable {
        case neutral
        case success
        case error
    }

    /// 各段时长（秒）。默认值按规格 §17 的时间线。
    public struct Timings {
        public var targetRing: Double = 0.12   // 目标圆环浮现
        public var drop: Double = 0.32         // 下落（重力加速）
        public var impact: Double = 0.30       // 落地压缩 + 回弹
        public var processing: Double = 0.28   // 系统确认停留（规格要求 200–400ms）
        public var morph: Double = 0.30        // 红 → 绿
        public var checkDraw: Double = 0.22    // ✓ 绘制
        public var shake: Double = 0.30        // 失败摇晃
        public var ripple: Double = 0.40       // 波纹扩散

        public init(targetRing: Double = 0.12,
                    drop: Double = 0.32,
                    impact: Double = 0.30,
                    processing: Double = 0.28,
                    morph: Double = 0.30,
                    checkDraw: Double = 0.22,
                    shake: Double = 0.30,
                    ripple: Double = 0.40) {
            self.targetRing = targetRing
            self.drop = drop
            self.impact = impact
            self.processing = processing
            self.morph = morph
            self.checkDraw = checkDraw
            self.shake = shake
            self.ripple = ripple
        }

        /// 规格自检用：所有时长都必须在 0.1–0.5s 之内。
        public var allWithinSpec: Bool {
            [targetRing, drop, impact, processing, morph, checkDraw, shake, ripple]
                .allSatisfy { $0 >= 0.10 && $0 <= 0.50 }
        }
    }

    @Published public private(set) var phase: Phase = .idle
    /// 0 = 悬在空中，1 = 针尖扎进地图
    @Published public private(set) var drop: Double = 1
    /// > 0 压缩，< 0 回弹，最终回到 0
    @Published public private(set) var impact: Double = 0
    /// 0 红 → 1 绿
    @Published public private(set) var morph: Double = 0
    /// 对勾绘制进度
    @Published public private(set) var checkDraw: Double = 0
    /// 感叹号淡入
    @Published public private(set) var exclamation: Double = 0
    /// 摇晃角度（度），以针尖为轴
    @Published public private(set) var shake: Double = 0
    /// 目标圆环进度
    @Published public private(set) var ring: Double = 0
    /// 波纹扩散进度
    @Published public private(set) var ripple: Double = 0
    @Published public private(set) var rippleKind: RippleKind = .neutral

    /// 供测试断言阶段顺序
    public private(set) var phaseLog: [Phase] = []

    public let timings: Timings
    private var runID = UUID()

    public init(timings: Timings = Timings()) {
        self.timings = timings
    }

    // MARK: - 业务驱动入口

    /// 落针：目标圆环 → 下落 → 落地冲击 → 进入 processing 等待业务结果。
    public func dropPin() async {
        reset()
        let token = runID

        phase = .dropping
        phaseLog.append(.dropping)

        // Stage 1：目标位置先浮现一圈克制的圆环
        withAnimation(.easeOut(duration: timings.targetRing)) { ring = 1 }

        // Stage 2：从上方 80pt 落下。用近乎重力的加速曲线（规格给的 cubic-bezier(0.55,0,0.9,0.45)）
        drop = 0
        try? await Task.sleep(nanoseconds: 16_000_000)   // 先提交"悬空"状态，否则会被合并
        guard runID == token else { return }
        withAnimation(.timingCurve(0.55, 0.0, 0.9, 0.45, duration: timings.drop)) { drop = 1 }
        try? await Task.sleep(nanoseconds: nanos(timings.drop * 0.9))
        guard runID == token else { return }

        // Stage 4：落地冲击 —— 压缩 → 回弹 → 稳定（针尖不动，只有主体形变）
        phase = .landed
        phaseLog.append(.landed)
        emitRipple(.neutral)

        let step = timings.impact / 3
        withAnimation(.easeOut(duration: step * 0.9)) { impact = 1 }        // 压缩到 0.93
        try? await Task.sleep(nanoseconds: nanos(step))
        guard runID == token else { return }
        withAnimation(.spring(response: step, dampingFraction: 0.65)) { impact = -0.55 }  // 回弹到 ~1.04
        try? await Task.sleep(nanoseconds: nanos(step))
        guard runID == token else { return }
        withAnimation(.spring(response: step, dampingFraction: 0.8)) { impact = 0 }        // 收住，只弹一次
        try? await Task.sleep(nanoseconds: nanos(step))
        guard runID == token else { return }

        // Stage 5：保持普通红针，等业务结果（规格：200–400ms 的"系统确认"）
        phase = .processing
        phaseLog.append(.processing)
        try? await Task.sleep(nanoseconds: nanos(timings.processing))
    }

    /// 定位成功：红 → 绿 + 白色对勾绘制 + 绿色波纹。
    public func succeed() async {
        guard phase == .processing else { return }
        let token = runID
        phase = .success
        phaseLog.append(.success)

        withAnimation(.easeInOut(duration: timings.morph)) { morph = 1 }
        try? await Task.sleep(nanoseconds: nanos(timings.morph * 0.55))
        guard runID == token else { return }
        withAnimation(.easeOut(duration: timings.checkDraw)) { checkDraw = 1 }
        emitRipple(.success)
    }

    /// 定位失败：白色感叹号淡入 + 以针尖为轴的轻微摇晃 + 红色波纹。
    public func fail() async {
        guard phase == .processing else { return }
        let token = runID
        phase = .error
        phaseLog.append(.error)

        withAnimation(.easeOut(duration: 0.16)) { exclamation = 1 }
        emitRipple(.error)

        // 摇晃以针尖为轴（视图侧用 anchor: .bottom），角度序列 -4 → +4 → -2 → +2 → 0
        let step = timings.shake / 5
        for angle in [-4.0, 4.0, -2.0, 2.0, 0.0] {
            withAnimation(.easeInOut(duration: step)) { shake = angle }
            try? await Task.sleep(nanoseconds: nanos(step))
            guard runID == token else { return }
        }
        shake = 0
    }

    /// 复位：清空所有动画进度与阶段记录，并让正在跑的时间线失效。
    ///
    /// 注意 `drop` 的语义：它是"针相对地面的位置"（1 = 静止落在地面），不是动画进度。
    /// 因此 reset() 后 drop = 1 —— 没有定位点时不该让针凭空悬在空中。
    /// "所有进度值归零"指的是 ring / impact / morph / checkDraw / exclamation / shake / ripple。
    public func reset() {
        runID = UUID()
        phase = .idle
        drop = 1
        impact = 0
        morph = 0
        checkDraw = 0
        exclamation = 0
        shake = 0
        ring = 0
        ripple = 0
        rippleKind = .neutral
        phaseLog.removeAll()
    }

    // MARK: - 私有

    private func emitRipple(_ kind: RippleKind) {
        rippleKind = kind
        ripple = 0
        withAnimation(.easeOut(duration: timings.ripple)) { ripple = 1 }
    }

    private func nanos(_ seconds: Double) -> UInt64 {
        UInt64(max(0, seconds) * 1_000_000_000)
    }
}
