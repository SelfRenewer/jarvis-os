import Foundation

/// `~/Library/Application Support/Jarvis/config.json`, written by `uv run jarvis desktop-setup` (mode 600).
struct JarvisConfig: Codable {
    var base_url: String
    var token: String
    var project_roots: [String]?
    var repo: String?
    var node: String?  // node for the Jarvis OS page server (written by `jarvis desktop-setup`)
}

enum ConfigStore {
    static var url: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/Jarvis/config.json")
    }

    static func load() -> JarvisConfig? {
        guard let data = try? Data(contentsOf: url) else { return nil }
        return try? JSONDecoder().decode(JarvisConfig.self, from: data)
    }
}

/// Small per-user switches (not secrets): kept in UserDefaults.
enum Prefs {
    private static let defaults = UserDefaults.standard

    static var wakeWord: Bool {
        get { defaults.object(forKey: "wakeWord") as? Bool ?? true }
        set { defaults.set(newValue, forKey: "wakeWord") }
    }

    static var speakReplies: Bool {
        get { defaults.object(forKey: "speakReplies") as? Bool ?? true }
        set { defaults.set(newValue, forKey: "speakReplies") }
    }
}

extension String {
    /// `~/Downloads` → `/Users/<me>/Downloads`.
    var expandingTilde: String { (self as NSString).expandingTildeInPath }
}
