import SwiftUI

/// The small voice window at the top of the screen. A click opens the big chat.
struct HUDView: View {
    @ObservedObject var model: ChatModel
    @ObservedObject var speech: SpeechListener
    var open: () -> Void

    var body: some View {
        HStack(alignment: .center, spacing: 12) {
            JarvisEmblem(active: model.hud == .listening).frame(width: 30, height: 30)
            VStack(alignment: .leading, spacing: 4) { content }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
        .frame(width: 420, alignment: .leading)
        .background(
            ZStack {
                VisualEffectBackground()
                Theme.navy.opacity(0.9)
            }
        )
        .clipShape(RoundedRectangle(cornerRadius: 22, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 22, style: .continuous).strokeBorder(Color.white.opacity(0.10)))
        .contentShape(Rectangle())
        .onTapGesture(perform: open)
        .preferredColorScheme(.dark)
        .help("Открыть окно Jarvis")
    }

    @ViewBuilder private var content: some View {
        switch model.hud {
        case .hidden:
            EmptyView()
        case .listening:
            HStack(spacing: 10) {
                Waveform().frame(width: 28, height: 16)
                Text(speech.partial.isEmpty ? "Слушаю…" : speech.partial)
                    .font(.system(size: 14)).foregroundStyle(Theme.text).lineLimit(2)
            }
        case .thinking(let heard):
            Text("«\(heard)»").font(.system(size: 12)).foregroundStyle(Theme.muted).lineLimit(2)
            TypingDots()
        case .reply(let text, let more):
            Text(text).font(.system(size: 14)).foregroundStyle(Theme.text).lineLimit(3)
                .fixedSize(horizontal: false, vertical: true)
            if more {
                Text("Открыть ›").font(.system(size: 12, weight: .medium)).foregroundStyle(Theme.accent)
            }
        }
    }
}
