// swift-tools-version: 5.9
import PackageDescription

// LocPilot 原生 macOS 版：SwiftPM 工程，命令行 swift build 即可构建（不依赖 Xcode）。
// 说明：Command Line Tools 不含 SwiftUI 宏插件，因此源码内不使用 @State / @Bindable，
// 状态统一用 ObservableObject + @StateObject / @Published（属性包装器，CLT 可用）。
let package = Package(
    name: "LocPilot",
    // 必须是 26 起：macOS 用"链接时记录的 SDK 版本"决定是否启用 Liquid Glass 新外观，
    // 低于 26 的 SDK 会整体回落到旧版控件样式（红绿灯等系统控件都会变样）。
    // 用字符串形式而不是 .v26，兼容更早的 swift-tools-version。
    platforms: [.macOS("26.0")],
    products: [
        .library(name: "LocPilotKit", targets: ["LocPilotKit"]),
        .executable(name: "LocPilot", targets: ["LocPilotApp"]),
        // 零依赖测试运行器：Command Line Tools 没有 XCTest，用可执行 target 代替 swift test
        .executable(name: "LocPilotTests", targets: ["LocPilotTests"]),
    ],
    targets: [
        .target(name: "LocPilotKit", path: "Sources/LocPilotKit"),
        .executableTarget(name: "LocPilotApp", dependencies: ["LocPilotKit"], path: "Sources/LocPilotApp"),
        .executableTarget(name: "LocPilotTests", dependencies: ["LocPilotKit"], path: "Sources/LocPilotTests"),
    ]
)
