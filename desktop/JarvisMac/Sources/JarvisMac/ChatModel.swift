import AppKit
import Combine
import Foundation

/// The small window for voice: «Слушаю…» → what was heard → a short answer; the big chat opens only on click.
enum HUDPhase: Equatable {
    case hidden
    case listening
    case thinking(String)
    case reply(String, more: Bool)
}

struct ChatMessage: Identifiable, Equatable {
    enum Role { case user, jarvis, system }

    let id = UUID()
    var serverId: Int?
    let role: Role
    var segments: [TextSegment]
    var buttons: [[ReplyButton]]
    var spoken: Bool = false
}

enum CoreStatus: Equatable {
    case unknown
    case online
    case offline  // Docker / the Jarvis core is not running
    case notConfigured
}

/// Everything the window shows, and the loops that keep the Mac connected to the core.
@MainActor
final class ChatModel: ObservableObject {
    @Published var messages: [ChatMessage] = []
    @Published var input = ""
    @Published private(set) var status: CoreStatus = .unknown
    @Published private(set) var busy = false
    /// Bumped every time the panel is shown: the input field takes the keyboard focus again.
    @Published private(set) var focusTick = 0
    @Published var wakeWord = Prefs.wakeWord {
        didSet {
            Prefs.wakeWord = wakeWord
            speech.setWakeWord(wakeWord)
        }
    }

    /// Every reply spoken on this Mac. Mirrors the server's setting (also changed by «отвечай текстом» said here).
    @Published var speakReplies = Prefs.speakReplies {
        didSet {
            Prefs.speakReplies = speakReplies
            guard !syncingVoice, oldValue != speakReplies else { return }
            let wanted = speakReplies
            if !wanted { player.stop() }
            Task { if let saved = try? await client.setVoice(wanted) { syncVoice(saved) } }
        }
    }
    private var syncingVoice = false

    /// The server said what the setting is: show it without sending it back.
    func syncVoice(_ value: Bool?) {
        guard let value, value != speakReplies else { return }
        syncingVoice = true
        speakReplies = value
        syncingVoice = false
    }

    let speech = SpeechListener()
    let config = ConfigStore.load()
    private lazy var client = JarvisClient(config: config)
    private lazy var scanner = EnvironmentScanner(roots: config?.project_roots ?? ["~/Projects", "~/Developer"])
    lazy var site = SystemSite(config: config)
    private lazy var runner = ActionRunner(scanner: scanner, site: site) { [weak self] text in self?.showNotice(text) }
    private let player = VoicePlayer()
    private var lastHello: Date?
    private var loopsStarted = false
    /// Show the window (the wake word was heard, or a reply arrived while it was hidden).
    /// Is the big chat window on screen? (Then voice goes there and no small window is shown.)
    var isPanelVisible: () -> Bool = { false }
    @Published private(set) var hud: HUDPhase = .hidden
    private var hudTimer: Timer?
    private var cancellables = Set<AnyCancellable>()

    static let suggestions = [
        "Что у меня сегодня?", "Иду в магазин", "Открой телеграм", "Чем бы мне заняться?", "📊 Статистика",
        "Запусти Claude в JarvisOS",
    ]

    init() {
        speech.onCommand = { [weak self] text in self?.submit(text, spoken: true) }
        speech.onWake = { [weak self] in self?.wakeHeard() }
        player.onPlaying = { [weak self] playing in
            guard let self else { return }
            self.speech.pause(playing)
            if !playing, case .reply = self.hud { self.hideHUD(after: 2.5) }  // the voice answer ended
        }
        // Listening ended without a command («Джарвис» and silence): the small window goes away.
        speech.$state.removeDuplicates().sink { [weak self] state in
            guard let self, state != .listening, self.hud == .listening else { return }
            self.hideHUD(after: 1.2, onlyIf: .listening)
        }.store(in: &cancellables)
        if config == nil { status = .notConfigured }
    }

    // MARK: lifecycle

    func start() {
        guard !loopsStarted else { return }
        loopsStarted = true
        Task.detached(priority: .utility) { [scanner] in scanner.scan() }
        Task { await self.healthLoop() }
        Task { await self.actionsLoop() }
        // Jarvis OS (and through it TLT) comes up with the app and is checked every 30 s: Jarvis in Docker wakes TLT
        // through it at any hour, and a page server left by an earlier copy of this app has no one else to restart it.
        Task {
            while true {
                _ = await self.site.ensureRunning()
                try? await Task.sleep(nanoseconds: 30_000_000_000)
            }
        }
        if wakeWord { speech.setWakeWord(true) }
    }

    private func healthLoop() async {
        while true {
            guard client.isConfigured else {
                status = .notConfigured
                return
            }
            let up = await client.health()
            let wasOnline = status == .online
            status = up ? .online : .offline
            if up, !wasOnline || (lastHello.map { Date().timeIntervalSince($0) > 600 } ?? true) {
                await sendHello()
            }
            try? await Task.sleep(nanoseconds: (up ? 15 : 5) * 1_000_000_000)
        }
    }

    func sendHello(rescan: Bool = false) async {
        if rescan || scanner.apps.isEmpty {
            let scanner = self.scanner
            await Task.detached(priority: .utility) { scanner.scan() }.value
        }
        do {
            updateSpeechHints()
            let hello = try await client.hello(apps: Array(scanner.apps.keys).sorted(), projects: scanner.projects)
            syncVoice(hello.voiceMac)
            lastHello = Date()
        } catch {
            lastHello = nil
        }
    }

    private func actionsLoop() async {
        while true {
            guard client.isConfigured else { return }
            guard status == .online else {
                try? await Task.sleep(nanoseconds: 3_000_000_000)
                continue
            }
            do {
                let actions = try await client.pollActions(wait: 20)
                for action in actions {
                    let outcome = await runner.run(action)
                    await client.report(action.id, ok: outcome.ok, detail: outcome.detail)
                    if !outcome.ok || action.source == "telegram" {
                        let icon = outcome.ok ? "🖥" : "⚠️"
                        append(.system, "\(icon) \(outcome.detail)")
                    }
                }
            } catch {
                try? await Task.sleep(nanoseconds: 3_000_000_000)
            }
        }
    }

    // MARK: conversation

    func submit(_ raw: String? = nil, spoken: Bool = false) {
        let text = (raw ?? input).trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        if raw == nil { input = "" }
        speech.cancelCommand()
        messages.append(ChatMessage(role: .user, segments: [.text(AttributedString(text))], buttons: [], spoken: spoken))
        // Said aloud with the big window hidden: the answer always goes to the small window under the notch.
        let compact = hud != .hidden || (spoken && !isPanelVisible())
        if compact { showHUD(.thinking(text)) }
        guard status != .notConfigured else {
            append(.system, ClientError.notConfigured.localizedDescription)
            if compact { showHUD(.reply(ClientError.notConfigured.localizedDescription, more: true), hideAfter: 8) }
            return
        }
        busy = true
        Task {
            defer { busy = false }
            do {
                let reply = try await client.send(text, spoken: spoken)
                apply(reply)
                if compact { showReplyInHUD(reply) }
            } catch {
                let hint = offlineHint(error)
                append(.system, hint)
                if compact { showHUD(.reply(hint, more: true), hideAfter: 8) }
            }
        }
    }

    // MARK: small window

    /// «Джарвис, …» heard: the small window, unless the big one is open (then it shows the listening itself).
    private func wakeHeard() {
        if isPanelVisible() { return }
        player.stop()
        showHUD(.listening)
    }

    /// ⌥Space while the big window is closed: listen with the small window.
    func listenCompact() {
        if hud == .listening {
            speech.cancelCommand()
            hideHUD()
            return
        }
        player.stop()
        showHUD(.listening)
        speech.listenForCommand()
    }

    func hideHUD() {
        hudTimer?.invalidate()
        hud = .hidden
    }

    /// A reminder from Jarvis: the small window at the top of the screen with a sound (no system permission needed).
    func showNotice(_ text: String) {
        NSSound(named: NSSound.Name("Glass"))?.play()
        showHUD(.reply(text, more: false), hideAfter: 20)
    }

    private func showHUD(_ phase: HUDPhase, hideAfter: TimeInterval? = nil) {
        hudTimer?.invalidate()
        hud = phase
        if let hideAfter { hideHUD(after: hideAfter) }
    }

    private func hideHUD(after seconds: TimeInterval, onlyIf phase: HUDPhase? = nil) {
        hudTimer?.invalidate()
        hudTimer = Timer.scheduledTimer(withTimeInterval: seconds, repeats: false) { [weak self] _ in
            Task { @MainActor in
                guard let self else { return }
                if let phase, self.hud != phase { return }
                self.hud = .hidden
            }
        }
    }

    /// The first reply in a few lines; buttons or a long answer → «Открыть ›» and more time to click.
    private func showReplyInHUD(_ reply: ServerReply) {
        guard let first = reply.messages.first else {
            hideHUD(after: 1.5)
            return
        }
        let text = Self.plain(first.text)
        let more = reply.messages.count > 1 || reply.messages.contains { !$0.buttons.isEmpty } || text.count > 220
        let speaking = reply.messages.contains { $0.audio != nil }
        showHUD(.reply(text, more: more), hideAfter: speaking ? nil : (more ? 15 : 6))
        if speaking { hideHUD(after: 60) }  // safety net; the end of the voice reply hides it sooner
    }

    static func plain(_ html: String) -> String {
        RichText.plain(html).trimmingCharacters(in: .whitespacesAndNewlines)
    }

    func tap(_ button: ReplyButton, in message: ChatMessage) {
        busy = true
        Task {
            defer { busy = false }
            do {
                let reply = try await client.tap(button.data, messageId: message.serverId)
                if let index = messages.firstIndex(where: { $0.id == message.id }), reply.edits.isEmpty,
                   !button.data.hasPrefix("us:") {
                    messages[index].buttons = []  // a used choice disappears, as in Telegram
                }
                apply(reply)
            } catch {
                append(.system, offlineHint(error))
            }
        }
    }

    private func apply(_ reply: ServerReply) {
        syncVoice(reply.voiceMac)
        for edit in reply.edits {
            if let index = messages.firstIndex(where: { $0.serverId == edit.id }) {
                messages[index].segments = RichText.segments(edit.text, format: edit.format)
                messages[index].buttons = edit.buttons
            }
        }
        for message in reply.messages {
            messages.append(
                ChatMessage(
                    serverId: message.id, role: .jarvis,
                    segments: RichText.segments(message.text, format: message.format),
                    buttons: message.buttons, spoken: message.audio != nil
                )
            )
            if let audio = message.audio { player.play(base64: audio) }
        }
        if reply.messages.isEmpty, reply.edits.isEmpty, let toast = reply.toast { append(.system, toast) }
    }

    /// From the menu: a Jarvis OS page (the map, the sites, the lab, TLT) in Safari.
    func openJarvis(_ path: String) {
        Task {
            let outcome = await runner.openJarvis(path)
            if !outcome.ok { append(.system, "⚠️ \(outcome.detail)") }
        }
    }

    private func append(_ role: ChatMessage.Role, _ text: String) {
        messages.append(ChatMessage(role: role, segments: [.text(AttributedString(text))], buttons: []))
    }

    private func offlineHint(_ error: Error) -> String {
        if status == .offline || (error as? URLError) != nil {
            return "Ядро Jarvis не отвечает — Docker выключен? Нажмите «Запустить» вверху."
        }
        return "⚠️ \(error.localizedDescription)"
    }

    func clear() {
        messages.removeAll()
    }

    func requestInputFocus() {
        focusTick += 1
    }

    /// Words the recognizer should expect (it heard «Claude» as «cloud»): names of this Mac's apps and projects.
    private func updateSpeechHints() {
        let fixed = ["Claude", "Claude Code", "Джарвис", "Jarvis", "Todoist", "Telegram"]
        let projects = scanner.projects.map(\.name)
        let apps = scanner.apps.keys.sorted().filter { $0.count <= 24 }
        speech.contextualHints = Array((fixed + projects + apps).prefix(100))
    }

    // MARK: voice & core

    func toggleListening() {
        if speech.state == .listening {
            speech.cancelCommand()
        } else {
            player.stop()
            speech.listenForCommand()
        }
    }

    func startCore() {
        let docker = URL(fileURLWithPath: "/Applications/Docker.app")
        guard FileManager.default.fileExists(atPath: docker.path) else {
            append(.system, "Docker Desktop не найден в /Applications.")
            return
        }
        NSWorkspace.shared.openApplication(at: docker, configuration: NSWorkspace.OpenConfiguration())
        append(.system, "Запускаю Docker — Jarvis будет готов примерно через минуту.")
    }
}
