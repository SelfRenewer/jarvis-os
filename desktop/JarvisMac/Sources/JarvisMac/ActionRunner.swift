import AppKit
import Foundation

/// Executes the closed set of actions the core sends — after validating each one again here:
/// apps must be installed, URLs http(s), paths must exist under the home folder or /Applications, Claude Code only
/// in a project this Mac reported, volume a number 0…100, texts limited in length, Jarvis OS only its own pages.
/// Nothing else is ever run (no arbitrary shell commands; the only AppleScript is the fixed `set volume` built from
/// validated integers; the only child process is the Jarvis OS page server, see SystemSite).
final class ActionRunner {
    private let scanner: EnvironmentScanner
    private let site: SystemSite
    private let notice: @MainActor (String) -> Void

    init(scanner: EnvironmentScanner, site: SystemSite, notice: @escaping @MainActor (String) -> Void) {
        self.scanner = scanner
        self.site = site
        self.notice = notice
    }

    struct Outcome {
        let ok: Bool
        let detail: String
    }

    @MainActor
    func run(_ action: MacAction) async -> Outcome {
        switch action.kind {
        case "open_app":
            guard let name = action.value("app"), let url = scanner.appURL(named: name) else {
                return Outcome(ok: false, detail: "приложение не найдено на этом Mac")
            }
            let config = NSWorkspace.OpenConfiguration()
            config.activates = true
            do {
                _ = try await NSWorkspace.shared.openApplication(at: url, configuration: config)
                return Outcome(ok: true, detail: "Открыл \(name)")
            } catch {
                return Outcome(ok: false, detail: error.localizedDescription)
            }
        case "open_url":
            guard let raw = action.value("url"), let url = URL(string: raw), ["http", "https"].contains(url.scheme?.lowercased() ?? "") else {
                return Outcome(ok: false, detail: "недопустимая ссылка")
            }
            return await open(url, done: "Открыл \(url.host ?? raw)")
        case "open_path":
            guard let raw = action.value("path") else { return Outcome(ok: false, detail: "нет пути") }
            // L6 (review 17.10): links are followed first — «~/link → /etc» is checked (and opened) as /etc
            let path = URL(fileURLWithPath: (raw.expandingTilde as NSString).standardizingPath).resolvingSymlinksInPath().path
            let home = FileManager.default.homeDirectoryForCurrentUser.resolvingSymlinksInPath().path
            // L4 (audit 14.10): whole folder names — «/Users/me2» is not under «/Users/me», «/ApplicationsX» not apps
            let inside = path == home || path.hasPrefix(home + "/") || path.hasPrefix("/Applications/")
            guard inside, FileManager.default.fileExists(atPath: path) else {
                return Outcome(ok: false, detail: "папка не найдена")
            }
            return await open(URL(fileURLWithPath: path), done: "Открыл \((path as NSString).lastPathComponent)")
        case "claude_code":
            return await launchClaude(action)
        case "open_jarvis":
            return await openJarvis(action.value("path") ?? "/")
        case "volume":
            return setVolume(action)
        case "copy_text":
            guard let text = action.value("text"), !text.isEmpty, text.count <= 10_000 else {
                return Outcome(ok: false, detail: "нечего копировать")
            }
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(text, forType: .string)
            return Outcome(ok: true, detail: "Скопировал в буфер обмена (\(text.count) симв.)")
        case "notify":
            guard let body = action.value("body"), !body.isEmpty else { return Outcome(ok: false, detail: "пустое уведомление") }
            let shown = String(body.prefix(300))
            notice(shown)
            return Outcome(ok: true, detail: shown)
        default:
            return Outcome(ok: false, detail: "неизвестное действие \(action.kind)")
        }
    }

    /// A Jarvis OS page in Safari: the page server is started if needed, then the personal link signs the browser in
    /// (30 days) and lands on the page. The link carries the site key — it is never shown or logged.
    @MainActor
    func openJarvis(_ path: String) async -> Outcome {
        guard SystemSite.isPage(path) else { return Outcome(ok: false, detail: "это не страница Jarvis OS") }
        guard await site.ensureRunning() else {
            return Outcome(ok: false, detail: "Jarvis OS не запустился: \(site.problem)")
        }
        guard let url = site.link(to: path) else { return Outcome(ok: false, detail: "нет токена Jarvis для Mac") }
        let title = ["/sites": "Сайты", "/tlt": "TLT"][path] ?? (path.hasPrefix("/lab") ? "лабораторию" : "Jarvis OS")
        return await open(url, done: "Открыл \(title) в Safari", app: "com.apple.Safari")
    }

    /// The system volume through the fixed Standard Additions command (no other app is controlled, so macOS asks
    /// for no permission). Only integers validated here reach the script text.
    @MainActor
    private func setVolume(_ action: MacAction) -> Outcome {
        let op = action.value("op") ?? "set"
        let script: String
        let done: String
        switch op {
        case "mute":
            script = "set volume with output muted"
            done = "Звук выключен"
        case "unmute":
            script = "set volume without output muted"
            done = "Звук включён"
        case "up", "down":
            let step = min(max(Int(action.value("step") ?? "10") ?? 10, 1), 50)
            let sign = op == "up" ? "+" : "-"
            script = "set volume without output muted\nset volume output volume ((output volume of (get volume settings)) \(sign) \(step))"
            done = op == "up" ? "Громче на \(step)%" : "Тише на \(step)%"
        case "set":
            guard let level = Int(action.value("level") ?? ""), (0...100).contains(level) else {
                return Outcome(ok: false, detail: "громкость должна быть от 0 до 100")
            }
            script = "set volume without output muted\nset volume output volume \(level)"
            done = "Громкость \(level)%"
        default:
            return Outcome(ok: false, detail: "неизвестная команда громкости")
        }
        var error: NSDictionary?
        NSAppleScript(source: script)?.executeAndReturnError(&error)
        if let error {
            return Outcome(ok: false, detail: "macOS не изменил громкость: \(error[NSAppleScript.errorMessage] ?? "ошибка")")
        }
        return Outcome(ok: true, detail: done)
    }

    /// A `.command` script opened by Terminal: `cd <project> && claude [prompt]`. The prompt travels in a file the
    /// script reads and deletes — never spliced into shell code.
    /// Never the blocking `NSWorkspace.open(_:)` on the main thread, and never wait forever: macOS may hold an open
    /// while one of its own dialogs waits for the user.
    @MainActor
    private func open(_ url: URL, done: String, app bundleID: String? = nil) async -> Outcome {
        let browser = bundleID.flatMap { NSWorkspace.shared.urlForApplication(withBundleIdentifier: $0) }
        let opened: Bool? = await withCheckedContinuation { cont in
            let once = Once()
            if let browser {
                NSWorkspace.shared.open([url], withApplicationAt: browser, configuration: NSWorkspace.OpenConfiguration()) { _, error in
                    once.run { cont.resume(returning: error == nil) }
                }
            } else {
                NSWorkspace.shared.open(url, configuration: NSWorkspace.OpenConfiguration()) { _, error in
                    once.run { cont.resume(returning: error == nil) }
                }
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + 20) {
                once.run { cont.resume(returning: nil) }
            }
        }
        switch opened {
        case .some(true): return Outcome(ok: true, detail: done)
        case .some(false): return Outcome(ok: false, detail: bundleID == nil ? "macOS не открыл \(url.lastPathComponent)" : "macOS не открыл страницу")
        case .none: return Outcome(ok: false, detail: "macOS не ответил — возможно, ждёт подтверждения в системном окне")
        }
    }

    @MainActor
    private func launchClaude(_ action: MacAction) async -> Outcome {
        guard let path = action.value("path"), let project = scanner.project(atPath: path) else {
            return Outcome(ok: false, detail: "проект не из списка этого Mac")
        }
        let fm = FileManager.default
        let dir = fm.homeDirectoryForCurrentUser.appendingPathComponent("Library/Caches/Jarvis", isDirectory: true)
        do {
            try fm.createDirectory(at: dir, withIntermediateDirectories: true)
            let stamp = UUID().uuidString.prefix(8)
            var lines = [
                "#!/bin/zsh -l",
                "cd \(shellQuote(project.path)) || exit 1",
                "clear",
                "echo \(shellQuote("JARVIS → Claude Code · \(project.name)"))",
            ]
            if let prompt = action.value("prompt"), !prompt.isEmpty {
                let promptFile = dir.appendingPathComponent("claude-\(stamp).txt")
                try prompt.write(to: promptFile, atomically: true, encoding: .utf8)
                lines.append("PROMPT=\"$(cat \(shellQuote(promptFile.path)))\"; rm -f \(shellQuote(promptFile.path))")
                lines.append("claude \"$PROMPT\"")
            } else {
                lines.append("claude")
            }
            let script = dir.appendingPathComponent("claude-\(stamp).command")
            try (lines.joined(separator: "\n") + "\n").write(to: script, atomically: true, encoding: .utf8)
            try fm.setAttributes([.posixPermissions: 0o700], ofItemAtPath: script.path)
            return await open(script, done: "Claude Code запущен в «\(project.name)»")
        } catch {
            return Outcome(ok: false, detail: error.localizedDescription)
        }
    }
}

/// `it's` → `'it'\''s'` — safe as one shell word.
func shellQuote(_ value: String) -> String {
    "'" + value.replacingOccurrences(of: "'", with: "'\\''") + "'"
}

/// Runs its block at most once (a continuation resumed by whichever comes first).
final class Once: @unchecked Sendable {
    private let lock = NSLock()
    private var done = false

    func run(_ block: () -> Void) {
        lock.lock()
        defer { lock.unlock() }
        guard !done else { return }
        done = true
        block()
    }
}
