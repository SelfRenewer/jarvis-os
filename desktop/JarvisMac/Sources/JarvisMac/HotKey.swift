import Carbon.HIToolbox
import Foundation

/// A system-wide shortcut (Carbon `RegisterEventHotKey`: works without Accessibility permission).
final class HotKey {
    private static var handlers: [UInt32: () -> Void] = [:]
    private static var installed = false
    private var ref: EventHotKeyRef?
    private let id: UInt32

    init(keyCode: Int, modifiers: Int, id: UInt32, handler: @escaping () -> Void) {
        self.id = id
        Self.handlers[id] = handler
        Self.installHandler()
        let hotKeyID = EventHotKeyID(signature: OSType(0x4A52_5653), id: id)  // 'JRVS'
        RegisterEventHotKey(UInt32(keyCode), UInt32(modifiers), hotKeyID, GetApplicationEventTarget(), 0, &ref)
    }

    deinit {
        if let ref { UnregisterEventHotKey(ref) }
        Self.handlers[id] = nil
    }

    private static func installHandler() {
        guard !installed else { return }
        installed = true
        var eventType = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { _, event, _ -> OSStatus in
            var hotKeyID = EventHotKeyID()
            let status = GetEventParameter(
                event, EventParamName(kEventParamDirectObject), EventParamType(typeEventHotKeyID), nil,
                MemoryLayout<EventHotKeyID>.size, nil, &hotKeyID
            )
            if status == noErr, let handler = HotKey.handlers[hotKeyID.id] {
                DispatchQueue.main.async { handler() }
            }
            return noErr
        }, 1, &eventType, nil, nil)
    }
}
