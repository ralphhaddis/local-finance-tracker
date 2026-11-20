// swift-tools-version: 6.4
import PackageDescription

let package = Package(
    name: "AppleFoundationModelBridge",
    platforms: [.macOS(.v27)],
    products: [
        .executable(name: "apple-model-bridge", targets: ["AppleModelBridge"]),
    ],
    targets: [
        .executableTarget(name: "AppleModelBridge"),
    ]
)
