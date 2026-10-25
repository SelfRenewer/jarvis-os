import AVFoundation
import Foundation
import SwiftUI

/// A reply as the bot formats it for Telegram: plain text, or Telegram HTML (b/strong, i/em, u/ins, s/strike/del,
/// code, pre, a href, blockquote [expandable], tg-spoiler). Unknown tags are dropped, their text kept.
enum TextSegment: Hashable {
    case text(AttributedString)
    case code(String)  // <pre>: a monospaced block (aligned tables)
    case quote(AttributedString)  // <blockquote>: shown expanded, with a bar on the left
}

enum RichText {
    static func segments(_ text: String, format: String) -> [TextSegment] {
        guard format == "html" else { return [.text(AttributedString(text))] }
        var parser = HTMLReader(source: text)
        return parser.read()
    }

    /// The entities Telegram HTML uses: &lt; &gt; &amp; &quot;, &#39; / &#x27; and any numeric one.
    static func unescape(_ text: String) -> String {
        guard text.contains("&") else { return text }
        var out = ""
        var rest = Substring(text)
        while let amp = rest.firstIndex(of: "&") {
            out += rest[..<amp]
            let tail = rest[amp...]
            guard let semi = tail.prefix(12).firstIndex(of: ";") else {
                out += "&"
                rest = rest[rest.index(after: amp)...]
                continue
            }
            let name = String(tail[tail.index(after: amp)..<semi])
            if let decoded = entity(name) {
                out += decoded
                rest = rest[rest.index(after: semi)...]
            } else {
                out += "&"
                rest = rest[rest.index(after: amp)...]
            }
        }
        return out + rest
    }

    private static func entity(_ name: String) -> String? {
        switch name {
        case "lt": return "<"
        case "gt": return ">"
        case "amp": return "&"
        case "quot": return "\""
        case "apos": return "'"
        default: break
        }
        guard name.hasPrefix("#") else { return nil }
        let digits = name.dropFirst()
        let value = digits.lowercased().hasPrefix("x") ? UInt32(digits.dropFirst(), radix: 16) : UInt32(digits)
        return value.flatMap(Unicode.Scalar.init).map { String(Character($0)) }
    }

    /// Text without tags (the small window, notifications).
    static func plain(_ html: String) -> String {
        unescape(html.replacingOccurrences(of: "<[^>]+>", with: "", options: .regularExpression))
    }
}

/// A small reader for the tags Telegram supports; the bot escapes all text, so `<` always starts a tag.
private struct HTMLReader {
    private struct Style {
        var tag: String
        var intent: InlinePresentationIntent = []
        var underline = false
        var link: URL?
    }

    let source: String
    private var segments: [TextSegment] = []
    private var current = AttributedString()
    private var styles: [Style] = []
    private var inQuote = false
    private var preText: String?

    init(source: String) {
        self.source = source
    }

    mutating func read() -> [TextSegment] {
        var index = source.startIndex
        var textStart = index
        while index < source.endIndex {
            if source[index] == "<", let close = source[index...].firstIndex(of: ">") {
                append(String(source[textStart..<index]))
                tag(String(source[source.index(after: index)..<close]))
                index = source.index(after: close)
                textStart = index
            } else {
                index = source.index(after: index)
            }
        }
        append(String(source[textStart...]))
        if let pre = preText {  // an unclosed <pre>
            segments.append(.code(RichText.unescape(pre)))
            preText = nil
        }
        flush()
        return segments
    }

    private mutating func tag(_ raw: String) {
        let closing = raw.hasPrefix("/")
        let body = closing ? String(raw.dropFirst()) : raw
        let name = body.split(whereSeparator: { $0 == " " || $0 == "\n" }).first.map { $0.lowercased() } ?? ""
        if name == "pre" {
            if closing {
                var code = RichText.unescape(preText ?? "")
                while code.hasSuffix("\n") { code.removeLast() }
                segments.append(.code(code))
                preText = nil
            } else {
                flush()
                preText = ""
            }
            return
        }
        if preText != nil { return }  // <code> inside <pre>: the block is monospaced already
        if name == "blockquote" {
            flush()
            inQuote = !closing
            return
        }
        if closing {
            if let index = styles.lastIndex(where: { $0.tag == name }) { styles.remove(at: index) }
            return
        }
        var style = Style(tag: name)
        switch name {
        case "b", "strong": style.intent = .stronglyEmphasized
        case "i", "em": style.intent = .emphasized
        case "u", "ins": style.underline = true
        case "s", "strike", "del": style.intent = .strikethrough
        case "code": style.intent = .code
        case "a": style.link = Self.href(body)
        default: break  // tg-spoiler, span, tg-emoji: the text is shown as is
        }
        styles.append(style)
    }

    private static func href(_ tag: String) -> URL? {
        guard let range = tag.range(of: "href=\"") else { return nil }
        let rest = tag[range.upperBound...]
        guard let end = rest.firstIndex(of: "\"") else { return nil }
        let url = RichText.unescape(String(rest[..<end]))
        guard url.lowercased().hasPrefix("http://") || url.lowercased().hasPrefix("https://") else { return nil }
        return URL(string: url)
    }

    private mutating func append(_ raw: String) {
        guard !raw.isEmpty else { return }
        if preText != nil {
            preText! += raw
            return
        }
        var piece = AttributedString(RichText.unescape(raw))
        var intent: InlinePresentationIntent = []
        for style in styles { intent.formUnion(style.intent) }
        if !intent.isEmpty { piece.inlinePresentationIntent = intent }
        if styles.contains(where: { $0.underline }) { piece.underlineStyle = .single }
        if let link = styles.last(where: { $0.link != nil })?.link { piece.link = link }
        current += piece
    }

    /// The text collected so far becomes a segment (newlines at its ends trimmed; blank text dropped).
    private mutating func flush() {
        defer { current = AttributedString() }
        while current.characters.first == "\n" {
            current.removeSubrange(current.startIndex..<current.characters.index(after: current.startIndex))
        }
        while current.characters.last == "\n" {
            current.removeSubrange(current.characters.index(before: current.endIndex)..<current.endIndex)
        }
        guard !String(current.characters).trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        segments.append(inQuote ? .quote(current) : .text(current))
    }
}

/// Plays Jarvis's voice replies (MP3 from the core); tells the listener to pause so it does not hear itself.
@MainActor
final class VoicePlayer: NSObject, AVAudioPlayerDelegate {
    private var player: AVAudioPlayer?
    var onPlaying: ((Bool) -> Void)?

    func play(base64: String) {
        guard let data = Data(base64Encoded: base64), let player = try? AVAudioPlayer(data: data) else { return }
        self.player?.stop()
        self.player = player
        player.delegate = self
        onPlaying?(true)
        if !player.play() { onPlaying?(false) }
    }

    func stop() {
        player?.stop()
        player = nil
        onPlaying?(false)
    }

    nonisolated func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        Task { @MainActor in self.onPlaying?(false) }
    }
}
