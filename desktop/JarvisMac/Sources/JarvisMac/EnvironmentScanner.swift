import Foundation

/// What this Mac has: installed apps (name → bundle URL) and the user's projects (git/package folders under the
/// configured roots). Reported to the core so «открой …» resolves only to things that exist here.
final class EnvironmentScanner {
    private(set) var apps: [String: URL] = [:]
    private(set) var projects: [ProjectInfo] = []
    private let roots: [String]

    private static let appDirs = [
        "/Applications", "/Applications/Utilities", "/System/Applications", "/System/Applications/Utilities",
        "~/Applications", "/System/Library/CoreServices/Applications",
    ]
    private static let projectMarkers = [
        ".git", "package.json", "pyproject.toml", "Package.swift", "Cargo.toml", "go.mod", "Gemfile", "pom.xml",
        "build.gradle", "composer.json", "requirements.txt",
    ]

    init(roots: [String]) {
        self.roots = roots
    }

    func scan() {
        var found: [String: URL] = [:]
        let fm = FileManager.default
        for dir in Self.appDirs.map(\.expandingTilde) {
            guard let items = try? fm.contentsOfDirectory(atPath: dir) else { continue }
            for item in items {
                let path = (dir as NSString).appendingPathComponent(item)
                if item.hasSuffix(".app") {
                    let name = String(item.dropLast(4))
                    if found[name] == nil { found[name] = URL(fileURLWithPath: path) }  // /Applications wins
                } else if dir == "/Applications", isDirectory(path), let nested = try? fm.contentsOfDirectory(atPath: path) {
                    for sub in nested where sub.hasSuffix(".app") {  // e.g. /Applications/Setapp/*.app
                        found[String(sub.dropLast(4))] = URL(fileURLWithPath: (path as NSString).appendingPathComponent(sub))
                    }
                }
            }
        }
        found["Finder"] = URL(fileURLWithPath: "/System/Library/CoreServices/Finder.app")
        apps = found

        var seen = Set<String>()
        var list: [ProjectInfo] = []
        for root in roots.map(\.expandingTilde) {
            guard isDirectory(root), let items = try? fm.contentsOfDirectory(atPath: root) else { continue }
            for item in items.sorted() where !item.hasPrefix(".") {
                let path = (root as NSString).appendingPathComponent(item)
                guard isDirectory(path), !seen.contains(path), isProject(path) else { continue }
                seen.insert(path)
                list.append(ProjectInfo(name: item, path: path))
            }
        }
        projects = Array(list.prefix(200))
    }

    func appURL(named name: String) -> URL? {
        apps[name] ?? apps.first { $0.key.caseInsensitiveCompare(name) == .orderedSame }?.value
    }

    func project(atPath path: String) -> ProjectInfo? {
        projects.first { $0.path == path }
    }

    private func isProject(_ path: String) -> Bool {
        Self.projectMarkers.contains { FileManager.default.fileExists(atPath: (path as NSString).appendingPathComponent($0)) }
    }

    private func isDirectory(_ path: String) -> Bool {
        var dir: ObjCBool = false
        return FileManager.default.fileExists(atPath: path, isDirectory: &dir) && dir.boolValue
    }
}
