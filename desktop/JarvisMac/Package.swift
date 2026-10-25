// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "JarvisMac",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(
            name: "JarvisMac",
            path: "Sources/JarvisMac",
            swiftSettings: [.unsafeFlags(["-Osize"])]
        )
    ],
    swiftLanguageVersions: [.v5]
)
