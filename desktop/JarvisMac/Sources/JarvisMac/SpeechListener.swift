import AVFoundation
import Foundation
import Speech

/// Russian speech on this Mac (Apple's on-device recognizer — audio never leaves the Mac).
///
/// Two modes: a *command* (after the hotkey or the mic button: listens until ~1.5 s of silence) and the *wake word*
/// («Джарвис, открой телеграм»: always listening, only the words after «Джарвис» become a command).
@MainActor
final class SpeechListener: ObservableObject {
    enum State: Equatable {
        case off
        case waiting  // wake word armed
        case listening  // capturing a command
    }

    @Published private(set) var state: State = .off
    @Published private(set) var partial = ""
    @Published private(set) var problem: String?

    /// A finished command (text, said after the wake word or the hotkey).
    var onCommand: ((String) -> Void)?
    /// «Джарвис» was heard: show the panel.
    var onWake: (() -> Void)?

    private let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "ru-RU"))
    private let engine = AVAudioEngine()
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?
    private let requestLock = NSLock()
    private var engineRunning = false
    private var wakeEnabled = false
    private var paused = false
    private var heardWake = false
    private var explicitCommand = false
    private var commandText = ""
    private var silenceTimer: Timer?
    private var wakeQuietTimer: Timer?
    private var lastWakeText = ""
    private var lastWakeChange = Date.distantPast
    private var restartTimer: Timer?
    private var generation = 0

    /// Only the vocative as a separate word: «Джарвис, …» — not «уменьшим Джарвиса» (found live 2026-10-13).
    private static let wakePattern = try! NSRegularExpression(
        pattern: "(?<![\\p{L}])(джарвис|джервис|жарвис|джарвиз|jarvis)(?![\\p{L}])", options: [.caseInsensitive]
    )

    var onDevice: Bool { recognizer?.supportsOnDeviceRecognition ?? false }
    /// Phrases to favour (app and project names, «Claude»): passed to every recognition request.
    var contextualHints: [String] = []

    // MARK: permissions

    func ensurePermissions() async -> Bool {
        let speech: Bool = await withCheckedContinuation { cont in
            SFSpeechRecognizer.requestAuthorization { status in cont.resume(returning: status == .authorized) }
        }
        guard speech else {
            problem = "Нет доступа к распознаванию речи: Системные настройки → Конфиденциальность → Распознавание речи → Jarvis."
            return false
        }
        let mic = await AVCaptureDevice.requestAccess(for: .audio)
        guard mic else {
            problem = "Нет доступа к микрофону: Системные настройки → Конфиденциальность → Микрофон → Jarvis."
            return false
        }
        problem = nil
        return true
    }

    // MARK: public controls

    func setWakeWord(_ on: Bool) {
        wakeEnabled = on
        if on {
            guard onDevice else {
                problem = "Слово «Джарвис» работает только с распознаванием на устройстве — для русского оно недоступно."
                wakeEnabled = false
                return
            }
            if state == .off { Task { await self.arm() } }
        } else if state == .waiting {
            stopEverything()
        }
    }

    /// Hotkey / mic button: capture one command now.
    func listenForCommand() {
        Task {
            guard await ensurePermissions() else { return }
            beginTask(command: true)
        }
    }

    /// Stop capturing (e.g. the user started typing); the wake word stays armed if enabled.
    func cancelCommand() {
        guard state == .listening else { return }
        commandText = ""
        partial = ""
        returnToIdle()
    }

    /// Our own voice reply must not trigger «Джарвис».
    func pause(_ on: Bool) {
        paused = on
        if on {
            if state == .waiting { endTask() }
        } else if wakeEnabled, state != .listening {
            beginTask(command: false)
        }
    }

    // MARK: internals

    private func arm() async {
        guard await ensurePermissions() else { wakeEnabled = false; return }
        beginTask(command: false)
    }

    private func startEngine() -> Bool {
        if engineRunning { return true }
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0 else {
            problem = "Микрофон недоступен."
            return false
        }
        input.removeTap(onBus: 0)
        input.installTap(onBus: 0, bufferSize: 2048, format: format) { [weak self] buffer, _ in
            guard let self else { return }
            self.requestLock.lock()
            self.request?.append(buffer)
            self.requestLock.unlock()
        }
        engine.prepare()
        do {
            try engine.start()
            engineRunning = true
            return true
        } catch {
            problem = "Не удалось включить микрофон: \(error.localizedDescription)"
            return false
        }
    }

    private func stopEngine() {
        guard engineRunning else { return }
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        engineRunning = false
    }

    private func beginTask(command: Bool) {
        guard let recognizer, recognizer.isAvailable else {
            problem = "Распознавание речи сейчас недоступно."
            return
        }
        endTask()
        guard startEngine() else { return }
        let request = SFSpeechAudioBufferRecognitionRequest()
        request.shouldReportPartialResults = true
        if recognizer.supportsOnDeviceRecognition { request.requiresOnDeviceRecognition = true }
        request.addsPunctuation = command
        request.contextualStrings = contextualHints
        request.taskHint = command ? .dictation : .search
        requestLock.lock()
        self.request = request
        requestLock.unlock()
        generation += 1
        let current = generation
        heardWake = command
        explicitCommand = command
        lastWakeText = ""
        lastWakeChange = Date()
        commandText = ""
        partial = ""
        state = command ? .listening : .waiting
        task = recognizer.recognitionTask(with: request) { [weak self] result, error in
            Task { @MainActor in self?.handle(result: result, error: error, generation: current) }
        }
        restartTimer?.invalidate()
        if command {
            // Nothing said at all → give up after 8 s; a long command ends after 30 s.
            restartTimer = Timer.scheduledTimer(withTimeInterval: 8, repeats: false) { [weak self] _ in
                Task { @MainActor in
                    guard let self, self.generation == current, self.commandText.isEmpty else { return }
                    self.returnToIdle()
                }
            }
        } else {
            // The wake-word task is renewed every 50 s (long tasks degrade).
            restartTimer = Timer.scheduledTimer(withTimeInterval: 50, repeats: false) { [weak self] _ in
                Task { @MainActor in
                    guard let self, self.generation == current, self.state == .waiting, !self.heardWake else { return }
                    self.beginTask(command: false)
                }
            }
        }
    }

    private func handle(result: SFSpeechRecognitionResult?, error: Error?, generation current: Int) {
        guard current == generation else { return }
        if let result {
            let text = result.bestTranscription.formattedString
            if heardWake {
                // A hotkey command is the whole phrase (minus a leading «Джарвис»); after the wake word, its tail.
                commandText = afterWake(text) ?? (explicitCommand ? text : commandText)
                partial = commandText
                scheduleSilence(final: result.isFinal)
            } else if let tail = afterWake(text), addressed(text) {
                heardWake = true
                wakeQuietTimer?.invalidate()
                state = .listening
                commandText = tail
                partial = tail
                onWake?()
                scheduleSilence(final: result.isFinal)
            } else if result.isFinal {
                beginTask(command: false)  // a phrase without «Джарвис» ended: listen again
            } else {
                noteWakeSpeech(text)
            }
        } else if error != nil {
            if state == .listening, !commandText.isEmpty {
                finishCommand()
            } else if wakeEnabled, !paused {
                Task { @MainActor in
                    try? await Task.sleep(nanoseconds: 800_000_000)
                    if self.generation == current { self.beginTask(command: false) }
                }
            } else {
                returnToIdle()
            }
        }
    }

    /// «Джарвис» counts only as an address: the first word of the phrase, or said after a pause — not in the middle
    /// of talking to someone else («…и Джарвис тоже…» flowing on without a break).
    private func addressed(_ text: String) -> Bool {
        let range = NSRange(text.startIndex..., in: text)
        guard let match = Self.wakePattern.firstMatch(in: text, range: range),
              let swiftRange = Range(match.range, in: text) else { return false }
        let before = text[..<swiftRange.lowerBound].trimmingCharacters(in: CharacterSet.letters.inverted)
        if before.isEmpty { return true }
        let isNew = Self.wakePattern.firstMatch(in: lastWakeText, range: NSRange(lastWakeText.startIndex..., in: lastWakeText)) == nil
        return isNew && Date().timeIntervalSince(lastWakeChange) >= 0.8
    }

    /// Speech without «Джарвис»: remember when it last changed; after 2 s of quiet start a fresh transcript, so the
    /// next «Джарвис, …» is the first word again.
    private func noteWakeSpeech(_ text: String) {
        guard text != lastWakeText else { return }
        lastWakeText = text
        lastWakeChange = Date()
        wakeQuietTimer?.invalidate()
        let current = generation
        wakeQuietTimer = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: false) { [weak self] _ in
            Task { @MainActor in
                guard let self, self.generation == current, self.state == .waiting, !self.heardWake else { return }
                self.beginTask(command: false)
            }
        }
    }

    /// «…Джарвис, открой телеграм» → «открой телеграм» (nil when the wake word was not said).
    private func afterWake(_ text: String) -> String? {
        let range = NSRange(text.startIndex..., in: text)
        guard let match = Self.wakePattern.matches(in: text, range: range).last,
              let swiftRange = Range(match.range, in: text) else { return nil }
        return String(text[swiftRange.upperBound...]).trimmingCharacters(in: CharacterSet(charactersIn: " ,.!?—-:").union(.whitespaces))
    }

    private func scheduleSilence(final: Bool) {
        silenceTimer?.invalidate()
        if final {
            finishCommand()
            return
        }
        let current = generation
        let wait: TimeInterval = commandText.isEmpty ? 4.0 : 1.4
        silenceTimer = Timer.scheduledTimer(withTimeInterval: wait, repeats: false) { [weak self] _ in
            Task { @MainActor in
                guard let self, self.generation == current else { return }
                self.finishCommand()
            }
        }
    }

    private func finishCommand() {
        silenceTimer?.invalidate()
        let text = commandText.trimmingCharacters(in: .whitespacesAndNewlines)
        commandText = ""
        partial = ""
        returnToIdle()
        if !text.isEmpty { onCommand?(text) }
    }

    private func returnToIdle() {
        endTask()
        heardWake = false
        if wakeEnabled, !paused {
            beginTask(command: false)
        } else {
            stopEngine()
            state = .off
        }
    }

    private func endTask() {
        silenceTimer?.invalidate()
        wakeQuietTimer?.invalidate()
        restartTimer?.invalidate()
        task?.cancel()
        task = nil
        requestLock.lock()
        request?.endAudio()
        request = nil
        requestLock.unlock()
        generation += 1
        if !wakeEnabled || paused { state = .off }
    }

    private func stopEverything() {
        endTask()
        stopEngine()
        state = .off
        partial = ""
    }
}
