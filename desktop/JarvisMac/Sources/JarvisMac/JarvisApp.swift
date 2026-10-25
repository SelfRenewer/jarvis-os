import AppKit
import Carbon.HIToolbox
import Combine
import ServiceManagement
import SwiftUI

@main
enum JarvisMain {
    static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        app.setActivationPolicy(.accessory)  // lives in the menu bar, no Dock icon
        app.run()
    }
}

/// Hosting view that reacts to the very first click even when its window is not key (a non-activating panel
/// otherwise swallows the first click to become key — «не с первого нажатия»).
final class FirstClickHostingView<Content: View>: NSHostingView<Content> {
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
}

/// A floating panel that can take keyboard input without activating the whole app (like Spotlight).
final class JarvisPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { true }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    private var statusItem: NSStatusItem!
    private var panel: JarvisPanel!
    private let model = ChatModel()
    private var hotKey: HotKey?
    private var stateObserver: Any?
    private var outsideClicks: Any?
    private var hiddenByOutsideClick: Date?
    private var hudPanel: JarvisPanel!
    private var hudHost: FirstClickHostingView<HUDView>!
    private var hudObserver: Any?
    private var partialObserver: Any?

    func applicationDidFinishLaunching(_ notification: Notification) {
        // One Jarvis: a second copy (an old build still running, a double launch) would hear «Джарвис» as well.
        let me = NSRunningApplication.current
        if let id = Bundle.main.bundleIdentifier,
           let other = NSRunningApplication.runningApplications(withBundleIdentifier: id)
            .first(where: { $0.processIdentifier != me.processIdentifier }) {
            other.activate()
            NSApp.terminate(nil)
            return
        }
        buildPanel()
        buildHUD()
        buildStatusItem()
        model.isPanelVisible = { [weak self] in self?.panel.isVisible ?? false }
        hudObserver = model.$hud.removeDuplicates().sink { [weak self] phase in
            Task { @MainActor in self?.updateHUD(phase) }
        }
        partialObserver = model.speech.$partial.removeDuplicates().sink { [weak self] _ in
            Task { @MainActor in
                guard let self, self.hudPanel.isVisible else { return }
                self.updateHUD(self.model.hud)  // the heard text grows: resize
            }
        }
        // ⌥Space: open Jarvis and listen right away; again: hide.
        hotKey = HotKey(keyCode: kVK_Space, modifiers: optionKey, id: 1) { [weak self] in self?.hotKeyPressed() }
        model.start()
        stateObserver = model.speech.$state.sink { [weak self] state in
            Task { @MainActor in self?.updateIcon(listening: state == .listening, armed: state == .waiting) }
        }
        if CommandLine.arguments.contains("--show") {
            // After the status item is laid out (its position places the panel).
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { [weak self] in self?.showPanel(focus: true) }
        }
    }

    // MARK: status item

    private func buildStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        if let button = statusItem.button {
            button.image = MenuBarIcon.image(listening: false, armed: false)
            button.target = self
            button.action = #selector(statusClicked(_:))
            button.sendAction(on: [.leftMouseUp, .rightMouseUp])
            button.toolTip = "Jarvis (⌥Space)"
        }
    }

    private func updateIcon(listening: Bool, armed: Bool) {
        statusItem?.button?.image = MenuBarIcon.image(listening: listening, armed: armed)
    }

    @objc private func statusClicked(_ sender: NSStatusBarButton) {
        if NSApp.currentEvent?.type == .rightMouseUp {
            statusItem.menu = buildMenu()
            statusItem.button?.performClick(nil)
            statusItem.menu = nil
        } else {
            togglePanel()
        }
    }

    private func buildMenu() -> NSMenu {
        let menu = NSMenu()
        menu.addItem(withTitle: "Открыть Jarvis", action: #selector(openFromMenu), keyEquivalent: "").target = self
        menu.addItem(withTitle: "Сказать команду (⌥Space)", action: #selector(listenFromMenu), keyEquivalent: "").target = self
        menu.addItem(.separator())
        menu.addItem(withTitle: "Jarvis OS — карта", action: #selector(openMap), keyEquivalent: "").target = self
        menu.addItem(withTitle: "Сайты", action: #selector(openSites), keyEquivalent: "").target = self
        menu.addItem(withTitle: "Лаборатория", action: #selector(openLab), keyEquivalent: "").target = self
        menu.addItem(withTitle: "TLT", action: #selector(openTLT), keyEquivalent: "").target = self
        menu.addItem(.separator())
        let wake = menu.addItem(withTitle: "Слушать слово «Джарвис»", action: #selector(toggleWake), keyEquivalent: "")
        wake.target = self
        wake.state = model.wakeWord ? .on : .off
        let speak = menu.addItem(withTitle: "Отвечать голосом", action: #selector(toggleSpeak), keyEquivalent: "")
        speak.target = self
        speak.state = model.speakReplies ? .on : .off
        let login = menu.addItem(withTitle: "Запускать при входе в систему", action: #selector(toggleLogin), keyEquivalent: "")
        login.target = self
        login.state = SMAppService.mainApp.status == .enabled ? .on : .off
        menu.addItem(.separator())
        if model.status == .offline {
            menu.addItem(withTitle: "Запустить ядро Jarvis (Docker)", action: #selector(startCore), keyEquivalent: "").target = self
        }
        menu.addItem(withTitle: "Обновить приложения и проекты", action: #selector(rescan), keyEquivalent: "").target = self
        menu.addItem(.separator())
        menu.addItem(withTitle: "Выйти из Jarvis", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        return menu
    }

    @objc private func openFromMenu() {
        model.hideHUD()
        showPanel(focus: true)
    }
    @objc private func listenFromMenu() { hotKeyPressed() }
    @objc private func openMap() { model.openJarvis("/") }
    @objc private func openSites() { model.openJarvis("/sites") }
    @objc private func openLab() { model.openJarvis("/lab") }
    @objc private func openTLT() { model.openJarvis("/tlt") }

    func applicationWillTerminate(_ notification: Notification) {
        model.site.stop()
    }
    @objc private func toggleWake() { model.wakeWord.toggle() }
    @objc private func toggleSpeak() { model.speakReplies.toggle() }
    @objc private func startCore() { model.startCore() }
    @objc private func rescan() { Task { await model.sendHello(rescan: true) } }

    @objc private func toggleLogin() {
        do {
            if SMAppService.mainApp.status == .enabled {
                try SMAppService.mainApp.unregister()
            } else {
                try SMAppService.mainApp.register()
            }
        } catch {
            let alert = NSAlert()
            alert.messageText = "Не получилось изменить автозапуск"
            alert.informativeText = "\(error.localizedDescription)\nМожно добавить Jarvis вручную: Системные настройки → Основные → Объекты входа."
            alert.runModal()
        }
    }

    // MARK: panel

    private func buildPanel() {
        panel = JarvisPanel(
            contentRect: NSRect(x: 0, y: 0, width: 440, height: 600),
            styleMask: [.nonactivatingPanel, .titled, .fullSizeContentView, .resizable, .closable],
            backing: .buffered, defer: false
        )
        panel.titleVisibility = .hidden
        panel.titlebarAppearsTransparent = true
        panel.isMovableByWindowBackground = true
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.hidesOnDeactivate = false
        panel.minSize = NSSize(width: 360, height: 420)
        panel.standardWindowButton(.closeButton)?.isHidden = true
        panel.standardWindowButton(.miniaturizeButton)?.isHidden = true
        panel.standardWindowButton(.zoomButton)?.isHidden = true
        panel.delegate = self
        let view = ChatView(model: model, speech: model.speech) { [weak self] in self?.hidePanel() }
        panel.contentView = FirstClickHostingView(rootView: view)
        panel.setFrameAutosaveName("JarvisPanel")
    }

    /// One click — one action: open when hidden, close when shown (found live 2026-10-13: closing only a *key*
    /// panel made the first click after using another app merely refocus it).
    /// Found live 2026-10-14: on this macOS a click on the menu-bar icon also reaches the global «click outside»
    /// monitor, so mouse-down hid the panel and mouse-up opened it again — a click right after such a hide closes.
    private func togglePanel() {
        if let hidden = hiddenByOutsideClick, Date().timeIntervalSince(hidden) < 0.6 {
            hiddenByOutsideClick = nil
            return
        }
        if panel.isVisible {
            hidePanel()
        } else {
            model.hideHUD()
            showPanel(focus: true)
        }
    }

    // MARK: small voice window

    private func buildHUD() {
        hudPanel = JarvisPanel(
            contentRect: NSRect(x: 0, y: 0, width: 420, height: 64),
            styleMask: [.nonactivatingPanel, .borderless],
            backing: .buffered, defer: false
        )
        hudPanel.level = .statusBar
        hudPanel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
        hudPanel.isOpaque = false
        hudPanel.backgroundColor = .clear
        hudPanel.hasShadow = true
        hudPanel.hidesOnDeactivate = false
        hudHost = FirstClickHostingView(rootView: HUDView(model: model, speech: model.speech) { [weak self] in self?.openFromHUD() })
        hudPanel.contentView = hudHost
    }

    private func updateHUD(_ phase: HUDPhase) {
        guard phase != .hidden, !panel.isVisible else {
            hudPanel.orderOut(nil)
            return
        }
        // Size to the content, top-centred under the menu bar (the window grows downwards).
        DispatchQueue.main.async { [weak self] in
            guard let self, let screen = NSScreen.main else { return }
            self.hudHost.layoutSubtreeIfNeeded()
            let size = self.hudHost.fittingSize
            let origin = NSPoint(x: screen.frame.midX - size.width / 2, y: screen.visibleFrame.maxY - size.height - 10)
            self.hudPanel.setFrame(NSRect(origin: origin, size: size), display: true)
            self.hudPanel.orderFrontRegardless()
        }
    }

    private func openFromHUD() {
        model.hideHUD()
        showPanel(focus: true)
    }

    /// Found live 2026-10-14: hiding the panel while it was still capturing a command left the recognizer in command
    /// mode — the next «Джарвис, …» went into the hidden chat and the small window never showed. Hiding ends it.
    private func hidePanel() {
        if model.speech.state == .listening { model.speech.cancelCommand() }
        panel.orderOut(nil)
        statusItem?.button?.highlight(false)
        if let monitor = outsideClicks {
            NSEvent.removeMonitor(monitor)
            outsideClicks = nil
        }
    }

    private func showPanel(focus: Bool) {
        let placed = UserDefaults.standard.bool(forKey: "panelPlaced")
        if !panel.isVisible, !placed || !NSScreen.screens.contains(where: { $0.visibleFrame.intersects(panel.frame) }) {
            positionUnderStatusItem()  // first time (or the saved place is off-screen): under the menu-bar icon
            UserDefaults.standard.set(true, forKey: "panelPlaced")
        }
        if focus {
            NSApp.activate()
            panel.makeKeyAndOrderFront(nil)
            model.requestInputFocus()
        } else {
            panel.orderFrontRegardless()
        }
        statusItem?.button?.highlight(true)
        // Like Spotlight: a click in any other app or on the desktop closes the panel. Clicks on our own icon are
        // local events, so the icon keeps toggling.
        if outsideClicks == nil {
            outsideClicks = NSEvent.addGlobalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown]) { [weak self] _ in
                let point = NSEvent.mouseLocation
                Task { @MainActor in
                    guard let self else { return }
                    if let icon = self.statusItem?.button?.window?.frame, icon.insetBy(dx: -2, dy: -2).contains(point) {
                        return  // the icon itself: its own action toggles the panel
                    }
                    self.hidePanel()
                    self.hiddenByOutsideClick = Date()
                }
            }
        }
    }

    private func positionUnderStatusItem() {
        guard let screen = NSScreen.main else { return }
        let size = panel.frame.size
        var origin = NSPoint(x: screen.visibleFrame.maxX - size.width - 16, y: screen.visibleFrame.maxY - size.height - 8)
        if let buttonFrame = statusItem?.button?.window?.frame {
            origin.x = min(max(buttonFrame.midX - size.width / 2, screen.visibleFrame.minX + 8), screen.visibleFrame.maxX - size.width - 8)
        }
        panel.setFrameOrigin(origin)
    }

    /// ⌥Space: with the big window open — listen there; otherwise the small voice window.
    private func hotKeyPressed() {
        if panel.isVisible {
            if model.speech.state == .listening { model.speech.cancelCommand() } else { model.toggleListening() }
            return
        }
        model.listenCompact()
    }
}

/// The menu-bar glyph: the emblem's «A» in an orbit (a template image, so it follows the menu bar's colour).
enum MenuBarIcon {
    static func image(listening: Bool, armed: Bool) -> NSImage {
        let size = NSSize(width: 18, height: 18)
        let image = NSImage(size: size, flipped: false) { rect in
            NSColor.black.setStroke()
            NSColor.black.setFill()
            let orbit = NSBezierPath()
            orbit.appendArc(withCenter: NSPoint(x: rect.midX, y: rect.midY), radius: 7.6, startAngle: 120, endAngle: 60, clockwise: false)
            orbit.lineWidth = 1.2
            orbit.stroke()
            let a = NSBezierPath()
            a.move(to: NSPoint(x: 4.6, y: 3.6))
            a.line(to: NSPoint(x: 9, y: 14.4))
            a.line(to: NSPoint(x: 13.4, y: 3.6))
            a.move(to: NSPoint(x: 6.6, y: 3.6))
            a.line(to: NSPoint(x: 9, y: 8.4))
            a.line(to: NSPoint(x: 11.4, y: 3.6))
            a.lineWidth = listening ? 2.0 : 1.4
            a.lineCapStyle = .round
            a.lineJoinStyle = .round
            a.stroke()
            if listening || armed {
                NSBezierPath(ovalIn: NSRect(x: 14.2, y: 13.6, width: 3.2, height: 3.2)).fill()
            }
            return true
        }
        image.isTemplate = true
        return image
    }
}
