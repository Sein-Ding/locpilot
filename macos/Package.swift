// swift-tools-version: 5.9
import PackageDescription

// LocPilot 原生 macOS 版：SwiftPM 工程，命令行 swift build 即可构建（不依赖 Xcode）。
// 说明：Command Line Tools 不含 SwiftUI 宏插件，因此源码内不使用 @State / @Bindable，
// 状态统一用 ObservableObject + @StateObject / @Published（属性包装器，CLT 可用）。
let package = Package(
    name: "LocPilot",
    platforms: [.macOS(.v14)],
    products: [
        .library(name: "LocPilotKit", targets: ["LocPilotKit"]),
        .executable(name: "LocPilot", targets: ["LocPilotApp"]),
    ],
    targets: [
        .target(name: "LocPilotKit", path: "Sources/LocPilotKit"),
        .executableTarget(name: "LocPilotApp", dependencies: ["LocPilotKit"], path: "Sources/LocPilotApp"),
    ]
)
