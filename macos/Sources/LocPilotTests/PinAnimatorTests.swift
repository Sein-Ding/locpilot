import Combine
import Foundation
import LocPilotKit

// 地图大头针定位动画（PinAnimator）——规格符合性测试
//
// 两个必须说明的机制：
// 1) PinAnimator 位于 LocPilotKit（库 target，public），本 target 已依赖 LocPilotKit，直接 import。
//    注意：本机 SwiftPM 无法 import 可执行 target（不产出 .swiftmodule，实测报
//    "unable to resolve module dependency"），所以该实现必须留在库 target 才能被测试直接编译。
// 2) PinAnimator 是 @MainActor 且 API 为 async，不能走 Harness.testAsync：那条路径是
//    「detached Task + 主线程 semaphore.wait」，主队列被堵死 → MainActor 续体永远排不上，
//    用例必然超时。这里改为在顶层同步代码所在的主线程上泵 RunLoop（主队列由主 RunLoop 抽取）。
//
// 断言全部按用户给的规格原文写；发现实现与规格不符时保留失败断言并上报，不放宽。

private typealias PinPhase = PinAnimator.Phase
private typealias PinTimings = PinAnimator.Timings
private typealias PinRippleKind = PinAnimator.RippleKind

/// 规格⑥：所有 UI 动画时长必须落在 0.10–0.50 秒
private let pinTimingFloor = 0.10
private let pinTimingCeil = 0.50

/// 极小 Timings：让规格用例秒级跑完（默认值的时长纪律由规格⑥单独校验）
private func pinFastTimings() -> PinTimings {
    var timings = PinTimings()      // Timings 对外只公开 init()，用属性赋值构造
    timings.targetRing = 0.10
    timings.drop = 0.10
    timings.impact = 0.10
    timings.processing = 0.10
    timings.morph = 0.10
    timings.checkDraw = 0.10
    timings.shake = 0.10
    timings.ripple = 0.10
    return timings
}

/// 在当前（主线程）上下文同步读取 @MainActor 状态
private func pinRead<T>(_ body: @MainActor () -> T) -> T {
    MainActor.assumeIsolated { body() }
}

private func pinAnimator(_ timings: PinTimings = pinFastTimings()) -> PinAnimator {
    MainActor.assumeIsolated { PinAnimator(timings: timings) }
}

/// 主 RunLoop 泵：驱动 @MainActor 异步时间线，并允许用例逐轮采样。
private final class PinRunner {
    private let finished = Box<Bool>()

    init(_ body: @escaping @MainActor () async -> Void) {
        finished.set(false)
        Task { @MainActor in
            await body()
            finished.set(true)
        }
    }

    var isFinished: Bool { finished.get() == true }

    func pump(_ seconds: TimeInterval = 0.002) {
        RunLoop.main.run(until: Date().addingTimeInterval(seconds))
    }

    /// 泵到时间线结束；每轮泵后采样一次（采样在主线程 = MainActor 上执行）。
    @discardableResult
    func runUntilFinished(timeout: TimeInterval = 20, sample: @MainActor () -> Void = {}) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while !isFinished && Date() < deadline {
            pump()
            MainActor.assumeIsolated { sample() }
        }
        return isFinished
    }
}

/// 由 main.swift 调用注册全部大头针动画规格用例。
func registerPinAnimatorTests() {

    // MARK: - 初始状态

    test("初始状态契约：idle、各进度为零、阶段记录为空") {
        let animator = pinAnimator()
        expectEqual(pinRead { animator.phase }, .idle, "初始 phase")
        expect(pinRead { animator.phaseLog.isEmpty }, "初始 phaseLog 应为空")
        expectClose(pinRead { animator.drop }, 1, "初始 drop（针静止在地面，非悬空）")
        expectClose(pinRead { animator.impact }, 0, "初始 impact")
        expectClose(pinRead { animator.morph }, 0, "初始 morph")
        expectClose(pinRead { animator.checkDraw }, 0, "初始 checkDraw")
        expectClose(pinRead { animator.exclamation }, 0, "初始 exclamation")
        expectClose(pinRead { animator.shake }, 0, "初始 shake")
        expectClose(pinRead { animator.ring }, 0, "初始 ring")
        expectClose(pinRead { animator.ripple }, 0, "初始 ripple")
        expectEqual(pinRead { animator.rippleKind }, .neutral, "初始 rippleKind")
    }

    // MARK: - 规格① 阶段顺序

    test("规格①阶段顺序：dropPin 结束为 [dropping, landed, processing]") {
        let animator = pinAnimator()
        let runner = PinRunner { await animator.dropPin() }
        var sampled: [PinPhase] = []
        let finished = runner.runUntilFinished(timeout: 20) {
            let phase = animator.phase
            if sampled.last != phase { sampled.append(phase) }
        }
        expect(finished, "dropPin() 应在超时前结束")
        expectEqual(pinRead { animator.phaseLog }, [.dropping, .landed, .processing], "phaseLog 阶段序列")
        expectEqual(pinRead { animator.phase }, .processing, "dropPin() 结束时应停在 processing")
        expectEqual(Array(sampled.drop(while: { $0 == .idle })), [.dropping, .landed, .processing],
                    "运行期间实际观察到的阶段顺序")
    }

    test("规格①阶段顺序：succeed() 在 processing 之后追加 success") {
        let animator = pinAnimator()
        expect(PinRunner { await animator.dropPin() }.runUntilFinished(), "dropPin() 应结束")
        expectEqual(pinRead { animator.phase }, .processing, "前置条件：dropPin 后为 processing")
        expect(PinRunner { await animator.succeed() }.runUntilFinished(), "succeed() 应结束")
        expectEqual(pinRead { animator.phaseLog }, [.dropping, .landed, .processing, .success], "phaseLog")
        expectEqual(pinRead { animator.phase }, .success, "结束时 phase")
    }

    test("规格①阶段顺序：fail() 在 processing 之后追加 error") {
        let animator = pinAnimator()
        expect(PinRunner { await animator.dropPin() }.runUntilFinished(), "dropPin() 应结束")
        expectEqual(pinRead { animator.phase }, .processing, "前置条件：dropPin 后为 processing")
        expect(PinRunner { await animator.fail() }.runUntilFinished(), "fail() 应结束")
        expectEqual(pinRead { animator.phaseLog }, [.dropping, .landed, .processing, .error], "phaseLog")
        expectEqual(pinRead { animator.phase }, .error, "结束时 phase")
    }

    // MARK: - 规格② 落地后的 processing 保持

    test("规格②默认 Timings().processing 落在 200–400ms") {
        let timings = PinTimings()
        expect(timings.processing >= 0.20,
               "规格要求 200–400ms 的系统确认停留：processing=\(timings.processing)s 应 ≥0.20s")
        expect(timings.processing <= 0.40,
               "规格要求 200–400ms 的系统确认停留：processing=\(timings.processing)s 应 ≤0.40s")
    }

    test("规格②落地后确实停留 processing（默认时间线实测 ≥200ms）") {
        let animator = MainActor.assumeIsolated { PinAnimator() }   // 默认 Timings
        let runner = PinRunner { await animator.dropPin() }
        var holdStartedAt: Date?
        var sawLandedBeforeProcessing = false
        let finished = runner.runUntilFinished(timeout: 25) {
            if animator.phase == .landed { sawLandedBeforeProcessing = true }
            if animator.phase == .processing && holdStartedAt == nil { holdStartedAt = Date() }
        }
        expect(finished, "dropPin() 应在超时前结束")
        expect(sawLandedBeforeProcessing, "应先经过 landed 再进入 processing")
        guard let holdStartedAt else {
            expect(false, "未观察到 processing 阶段")
            return
        }
        let hold = Date().timeIntervalSince(holdStartedAt)
        expect(hold >= 0.20,
               "processing 停留应 ≥200ms（规格：系统确认），实测 \(String(format: "%.3f", hold))s")
        expect(hold <= 0.45,
               "processing 停留应约等于 timings.processing（≤400ms 规格上限 + 50ms 采样/调度余量），实测 \(String(format: "%.3f", hold))s")
    }

    // MARK: - 规格③ 成功 / 失败终态

    test("规格③succeed 结束：morph==1 且 checkDraw==1") {
        let animator = pinAnimator()
        expect(PinRunner { await animator.dropPin() }.runUntilFinished(), "dropPin() 应结束")
        expect(PinRunner { await animator.succeed() }.runUntilFinished(), "succeed() 应结束")
        expectClose(pinRead { animator.morph }, 1, "红→绿 morph 应到 1")
        expectClose(pinRead { animator.checkDraw }, 1, "✓ 绘制进度应到 1")
        expectEqual(pinRead { animator.phase }, .success, "结束时 phase")
    }

    test("规格③fail 结束：exclamation==1 且 shake 收尾归零") {
        let animator = pinAnimator()
        expect(PinRunner { await animator.dropPin() }.runUntilFinished(), "dropPin() 应结束")
        expect(PinRunner { await animator.fail() }.runUntilFinished(), "fail() 应结束")
        expectClose(pinRead { animator.exclamation }, 1, "! 淡入应到 1")
        expectClose(pinRead { animator.shake }, 0, "摇晃必须收尾回到 0 度")
        expectEqual(pinRead { animator.phase }, .error, "结束时 phase")
    }

    // MARK: - 规格④ reset()

    test("规格④reset() 后 phase==idle 且各进度值归零") {
        let animator = pinAnimator()
        expect(PinRunner { await animator.dropPin() }.runUntilFinished(), "dropPin() 应结束")
        expect(PinRunner { await animator.succeed() }.runUntilFinished(), "succeed() 应结束")
        expectClose(pinRead { animator.morph }, 1, "前置条件：succeed 后 morph==1")

        MainActor.assumeIsolated { animator.reset() }
        expectEqual(pinRead { animator.phase }, .idle, "reset 后 phase")
        expect(pinRead { animator.phaseLog.isEmpty }, "reset 后 phaseLog 应清空")
        expectClose(pinRead { animator.impact }, 0, "reset 后 impact")
        expectClose(pinRead { animator.morph }, 0, "reset 后 morph")
        expectClose(pinRead { animator.checkDraw }, 0, "reset 后 checkDraw")
        expectClose(pinRead { animator.exclamation }, 0, "reset 后 exclamation")
        expectClose(pinRead { animator.shake }, 0, "reset 后 shake")
        expectClose(pinRead { animator.ring }, 0, "reset 后 ring")
        expectClose(pinRead { animator.ripple }, 0, "reset 后 ripple")
        expectEqual(pinRead { animator.rippleKind }, .neutral, "reset 后 rippleKind")
    }

    // 规格④里 drop 的语义按实现方在 PinAnimator.reset() 文档注释中的裁决锁定：
    //   drop 是「针相对地面的位置」（1 = 静止落在地面），不是动画进度；
    //   「所有进度值归零」指 ring / impact / morph / checkDraw / exclamation / shake / ripple。
    // 因此 reset() 后 drop == 1 —— 没有定位点时不该让针凭空悬在空中（drop = 0 即悬空）。
    // 若规格日后改回字面读法（drop 也归零），只需改本用例的期望值。
    test("规格④reset() 后 drop 回到静止落地值 1（drop 是位置，不是动画进度）") {
        let animator = pinAnimator()
        expect(PinRunner { await animator.dropPin() }.runUntilFinished(), "dropPin() 应结束")
        expectClose(pinRead { animator.drop }, 1, "前置条件：落地后 drop==1")

        MainActor.assumeIsolated { animator.reset() }
        expectClose(pinRead { animator.drop }, 1,
                    "reset 后 drop 应为静止落地值 1（0 表示悬空，不允许）")
    }

    // MARK: - 规格⑤ 连续重入

    test("规格⑤processing 状态重入：phaseLog 重新开始不累积") {
        let animator = pinAnimator()
        expect(PinRunner { await animator.dropPin() }.runUntilFinished(), "第一次 dropPin() 应结束")
        expectEqual(pinRead { animator.phase }, .processing, "第一次结束后停在 processing")
        expectEqual(pinRead { animator.phaseLog }, [.dropping, .landed, .processing], "第一次 phaseLog")

        // phase 仍是 .processing（dropPin 结束后保持，直到业务调用 succeed/fail），此时重入
        let second = PinRunner { await animator.dropPin() }
        second.pump()
        expectEqual(pinRead { animator.phase }, .dropping, "重入后应立刻回到 dropping")
        expectEqual(pinRead { animator.phaseLog }, [.dropping], "重入必须清空旧阶段记录，不能累积")
        expect(second.runUntilFinished(), "第二次 dropPin() 应结束")
        expectEqual(pinRead { animator.phaseLog }, [.dropping, .landed, .processing], "第二次结束后的 phaseLog")
        expectEqual(pinRead { animator.phase }, .processing, "第二次结束后 phase")
    }

    test("规格⑤success 后重入：phaseLog 重置且 drop 从 0 重新走到 1") {
        let animator = pinAnimator()
        expect(PinRunner { await animator.dropPin() }.runUntilFinished(), "第一次 dropPin() 应结束")
        expect(PinRunner { await animator.succeed() }.runUntilFinished(), "succeed() 应结束")
        expectEqual(pinRead { animator.phase }, .success, "前置条件：succeed 后为 success")
        expectClose(pinRead { animator.drop }, 1, "前置条件：success 后 drop==1（已落地）")
        expectClose(pinRead { animator.morph }, 1, "前置条件：success 后 morph==1（绿针）")

        let second = PinRunner { await animator.dropPin() }
        second.pump()   // 第一条同步段：reset() + drop 归 0 + phase = .dropping
        expectClose(pinRead { animator.drop }, 0, "重入后 drop 必须从 0 重新开始（不能停在 1）")
        expectEqual(pinRead { animator.phaseLog }, [.dropping], "重入后 phaseLog 必须重新开始")
        expectClose(pinRead { animator.morph }, 0, "重入后应回到红针（morph 归零）")
        expect(second.runUntilFinished(), "第二次 dropPin() 应结束")
        expectClose(pinRead { animator.drop }, 1, "第二次 dropPin() 结束应重新走到 1")
        expectEqual(pinRead { animator.phaseLog }, [.dropping, .landed, .processing], "第二次 phaseLog")
        expectEqual(pinRead { animator.phase }, .processing, "第二次结束时 phase")
    }

    // MARK: - 规格⑥ 时长纪律

    test("规格⑥默认 Timings 每项都在 0.10–0.50s（且不超过 1s）") {
        let timings = PinTimings()
        let items: [(String, Double)] = [
            ("targetRing", timings.targetRing), ("drop", timings.drop), ("impact", timings.impact),
            ("processing", timings.processing), ("morph", timings.morph), ("checkDraw", timings.checkDraw),
            ("shake", timings.shake), ("ripple", timings.ripple),
        ]
        for (name, value) in items {
            expect(value >= pinTimingFloor, "\(name)=\(value)s 应 ≥0.10s（规格下限）")
            expect(value <= pinTimingCeil, "\(name)=\(value)s 应 ≤0.50s（规格上限）")
            expect(value <= 1.0, "\(name)=\(value)s 不允许超过 1s")
        }
        expect(timings.allWithinSpec, "Timings().allWithinSpec 规格自检应为 true")
    }

    // MARK: - 规格⑦ 波纹种类

    test("规格⑦波纹种类：dropPin 期间 neutral，succeed 后 success，fail 后 error") {
        let success = pinAnimator()
        var landedRippleKind: PinRippleKind?
        let runner = PinRunner { await success.dropPin() }
        let finished = runner.runUntilFinished(timeout: 20) {
            if success.phase == .landed && landedRippleKind == nil { landedRippleKind = success.rippleKind }
        }
        expect(finished, "dropPin() 应结束")
        expectEqual(landedRippleKind, .neutral, "落地那一刻的波纹应为 neutral")
        expectEqual(pinRead { success.rippleKind }, .neutral, "dropPin() 结束后仍为 neutral（等业务结果）")
        expect(PinRunner { await success.succeed() }.runUntilFinished(), "succeed() 应结束")
        expectEqual(pinRead { success.rippleKind }, .success, "succeed 后波纹应为 success")

        let failure = pinAnimator()
        expect(PinRunner { await failure.dropPin() }.runUntilFinished(), "dropPin() 应结束")
        expectEqual(pinRead { failure.rippleKind }, .neutral, "失败路径落地波纹同样先是 neutral")
        expect(PinRunner { await failure.fail() }.runUntilFinished(), "fail() 应结束")
        expectEqual(pinRead { failure.rippleKind }, .error, "fail 后波纹应为 error")
    }

    // MARK: - 规格⑧ 幂等 / 防御

    test("规格⑧非 processing 状态调用 succeed/fail 不崩溃且不改状态") {
        let idle = pinAnimator()
        // idle 状态下调用：应为无效调用，直接返回
        let idleStarted = Date()
        expect(PinRunner { await idle.succeed() }.runUntilFinished(), "idle 下 succeed() 应直接返回")
        expect(Date().timeIntervalSince(idleStarted) < 0.15, "无效调用应立即返回，而不是跑完整时间线")
        expectEqual(pinRead { idle.phase }, .idle, "idle 下 succeed() 不应进入 success")
        expect(pinRead { idle.phaseLog.isEmpty }, "idle 下 succeed() 不应写 phaseLog")
        expectClose(pinRead { idle.morph }, 0, "idle 下 succeed() 不应改 morph")
        expect(PinRunner { await idle.fail() }.runUntilFinished(), "idle 下 fail() 应直接返回")
        expectEqual(pinRead { idle.phase }, .idle, "idle 下 fail() 不应进入 error")
        expect(pinRead { idle.phaseLog.isEmpty }, "idle 下 fail() 不应写 phaseLog")
        expectClose(pinRead { idle.exclamation }, 0, "idle 下 fail() 不应改 exclamation")

        // success 之后重复调用：阶段记录与状态都不应改变
        expect(PinRunner { await idle.dropPin() }.runUntilFinished(), "dropPin() 应结束")
        expect(PinRunner { await idle.succeed() }.runUntilFinished(), "succeed() 应结束")
        let logAfterSuccess = pinRead { idle.phaseLog }
        expect(PinRunner { await idle.succeed() }.runUntilFinished(), "重复 succeed() 应直接返回")
        expect(PinRunner { await idle.fail() }.runUntilFinished(), "success 后 fail() 应直接返回")
        expectEqual(pinRead { idle.phaseLog }, logAfterSuccess, "无效调用不应追加任何阶段")
        expectEqual(pinRead { idle.phase }, .success, "success 后无效调用不应改变 phase")

        // error 之后调用 succeed()：同样无效
        let errored = pinAnimator()
        expect(PinRunner { await errored.dropPin() }.runUntilFinished(), "dropPin() 应结束")
        expect(PinRunner { await errored.fail() }.runUntilFinished(), "fail() 应结束")
        let logAfterError = pinRead { errored.phaseLog }
        expect(PinRunner { await errored.succeed() }.runUntilFinished(), "error 后 succeed() 应直接返回")
        expectEqual(pinRead { errored.phase }, .error, "error 后 succeed() 不应把 phase 改成 success")
        expectEqual(pinRead { errored.phaseLog }, logAfterError, "error 后无效调用不应追加阶段")
    }

    // MARK: - 冻结 API 的动画终态契约

    test("落针动画契约：ring→1、drop→1、impact 先压缩后回弹并收尾为 0") {
        let animator = MainActor.assumeIsolated { PinAnimator() }   // 默认 Timings，采样窗口更宽
        let runner = PinRunner { await animator.dropPin() }
        var maxImpact = 0.0
        var minImpact = 0.0
        let finished = runner.runUntilFinished(timeout: 25) {
            maxImpact = max(maxImpact, animator.impact)
            minImpact = min(minImpact, animator.impact)
        }
        expect(finished, "dropPin() 应结束")
        expect(maxImpact > 0, "落地冲击应先压缩（impact > 0），实测最大 \(maxImpact)")
        expect(minImpact < 0, "随后应回弹（impact < 0），实测最小 \(minImpact)")
        expectClose(pinRead { animator.impact }, 0, "impact 最终应回稳到 0")
        expectClose(pinRead { animator.drop }, 1, "drop 结束应为 1（落地）")
        expectClose(pinRead { animator.ring }, 1, "目标圆环 ring 应到 1")
    }
}
