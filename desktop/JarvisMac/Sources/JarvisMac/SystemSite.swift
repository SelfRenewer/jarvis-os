import AppKit
import CryptoKit
import Foundation

/// Jarvis OS — the page server `desktop/system-site/server.mjs` (the map, the sites, TLT, the lab). This app keeps it
/// running (16.10, the user: «всё должно включаться и открываться из графического интерфейса»): it is a fixed child
/// process — the configured node with the repo's own server.mjs, nothing from outside in its arguments — restarted
/// when it falls. Pages open in Safari signed in with the site key this app derives from its own token
/// (HMAC-SHA256(token, "jarvis-system-site")[:32], the same as auth.mjs); the key never leaves this Mac.
@MainActor
final class SystemSite {
    static let port = 39110
    private static let pagePattern = try! NSRegularExpression(pattern: "^/(?:lab|sites|tlt)?(?:#[a-z0-9=_-]{1,40})?$")

    private let config: JarvisConfig?
    private var process: Process?
    private var exits: [Date] = []
    private var stopping = false

    init(config: JarvisConfig?) {
        self.config = config
    }

    /// The only pages that may be opened: the map, the lab (with its #command), the sites, TLT.
    static func isPage(_ path: String) -> Bool {
        pagePattern.firstMatch(in: path, range: NSRange(path.startIndex..., in: path)) != nil
    }

    private var siteDir: URL? {
        config?.repo.map { URL(fileURLWithPath: $0).appendingPathComponent("desktop/system-site", isDirectory: true) }
    }

    private var nodeURL: URL? {
        let fm = FileManager.default
        let home = fm.homeDirectoryForCurrentUser.path
        let candidates = [
            config?.node, "\(home)/.local/share/fnm/aliases/default/bin/node", "/opt/homebrew/bin/node", "/usr/local/bin/node",
        ].compactMap { $0 }
        return candidates.first { fm.isExecutableFile(atPath: $0) }.map { URL(fileURLWithPath: $0) }
    }

    func healthy() async -> Bool {
        var request = URLRequest(url: URL(string: "http://127.0.0.1:\(Self.port)/style.css")!)
        request.httpMethod = "HEAD"
        request.timeoutInterval = 1.5
        guard let (_, response) = try? await URLSession.shared.data(for: request) else { return false }
        return (response as? HTTPURLResponse)?.statusCode == 200
    }

    /// Up already, or started now and answering within ~8 s.
    func ensureRunning() async -> Bool {
        if await healthy() { return true }
        guard start() else { return false }
        for _ in 0..<32 {
            try? await Task.sleep(nanoseconds: 250_000_000)
            if await healthy() { return true }
        }
        return false
    }

    var problem: String {
        if config?.repo == nil { return "в настройках нет папки Jarvis — выполните ./desktop/install.sh" }
        if nodeURL == nil { return "не нашёл node на этом Mac" }
        return "журнал: ~/Library/Logs/Jarvis/system-site.log"
    }

    @discardableResult
    private func start() -> Bool {
        if let process, process.isRunning { return true }
        guard let dir = siteDir, let node = nodeURL,
              FileManager.default.fileExists(atPath: dir.appendingPathComponent("server.mjs").path) else { return false }
        let child = Process()
        child.executableURL = node
        child.arguments = ["server.mjs"]
        child.currentDirectoryURL = dir
        var env = ProcessInfo.processInfo.environment
        env["PORT"] = String(Self.port)
        env["PATH"] = node.deletingLastPathComponent().path + ":/usr/bin:/bin:/usr/sbin:/sbin"
        child.environment = env
        if let log = Self.logHandle() {
            child.standardOutput = log
            child.standardError = log
        }
        child.terminationHandler = { [weak self] ended in
            Task { @MainActor in self?.exited(ended) }
        }
        do {
            try child.run()
        } catch {
            return false
        }
        stopping = false
        process = child
        return true
    }

    /// Fell by itself: back in 2, 4, 8… seconds — at most 5 times in 5 minutes (then at the next «open»).
    private func exited(_ ended: Process) {
        guard ended === process else { return }
        process = nil
        guard !stopping else { return }
        let now = Date()
        exits = exits.filter { now.timeIntervalSince($0) < 300 } + [now]
        guard exits.count <= 5 else { return }
        let delay = pow(2.0, Double(exits.count))
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
            Task { @MainActor in _ = await self?.ensureRunning() }
        }
    }

    /// The app quits: so does the page server (TLT keeps running — Jarvis OS takes it back on its next start).
    func stop() {
        stopping = true
        process?.terminate()
    }

    private static func logHandle() -> FileHandle? {
        let fm = FileManager.default
        let dir = fm.homeDirectoryForCurrentUser.appendingPathComponent("Library/Logs/Jarvis", isDirectory: true)
        try? fm.createDirectory(at: dir, withIntermediateDirectories: true)
        let file = dir.appendingPathComponent("system-site.log")
        if let size = (try? fm.attributesOfItem(atPath: file.path)[.size] as? NSNumber)?.intValue, size > 5_000_000 {
            try? fm.removeItem(at: file)
        }
        if !fm.fileExists(atPath: file.path) {
            fm.createFile(atPath: file.path, contents: nil, attributes: [.posixPermissions: 0o600])
        }
        guard let handle = try? FileHandle(forWritingTo: file) else { return nil }
        handle.seekToEndOfFile()
        return handle
    }

    /// The personal link for a page: /?key=<site key>&to=<page>. The page server checks `to` against its own list.
    func link(to path: String) -> URL? {
        guard let token = config?.token, !token.isEmpty, Self.isPage(path) else { return nil }
        let mac = HMAC<SHA256>.authenticationCode(for: Data("jarvis-system-site".utf8), using: SymmetricKey(data: Data(token.utf8)))
        let key = mac.map { String(format: "%02x", $0) }.joined().prefix(32)
        var parts = URLComponents()
        parts.scheme = "http"
        parts.host = "127.0.0.1"
        parts.port = Self.port
        parts.path = "/"
        parts.queryItems = [URLQueryItem(name: "key", value: String(key)), URLQueryItem(name: "to", value: path)]
        parts.percentEncodedQuery = parts.percentEncodedQuery?.replacingOccurrences(of: "#", with: "%23")
        return parts.url
    }
}
