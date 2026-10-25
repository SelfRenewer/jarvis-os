import AppKit
import SwiftUI

enum Theme {
    static let navy = Color(red: 0.020, green: 0.043, blue: 0.094)
    static let deep = Color(red: 0.035, green: 0.075, blue: 0.160)
    static let accent = Color(red: 0.31, green: 0.76, blue: 1.0)
    static let accentSoft = Color(red: 0.31, green: 0.76, blue: 1.0).opacity(0.16)
    static let text = Color.white.opacity(0.92)
    static let muted = Color.white.opacity(0.48)
}

struct ChatView: View {
    @ObservedObject var model: ChatModel
    @ObservedObject var speech: SpeechListener
    @FocusState private var inputFocused: Bool
    var close: () -> Void

    var body: some View {
        ZStack {
            VisualEffectBackground()
            LinearGradient(colors: [Theme.navy.opacity(0.88), Theme.deep.opacity(0.94)], startPoint: .top, endPoint: .bottom)
            VStack(spacing: 0) {
                header
                if model.status == .offline || model.status == .notConfigured { coreBanner }
                Divider().overlay(Color.white.opacity(0.06))
                if model.messages.isEmpty { emptyState } else { conversation }
                inputBar
            }
        }
        .clipShape(RoundedRectangle(cornerRadius: 18, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 18, style: .continuous).strokeBorder(Color.white.opacity(0.08)))
        .preferredColorScheme(.dark)
        .onAppear { inputFocused = true }
        .onChange(of: model.focusTick) { _, _ in inputFocused = true }
        .onExitCommand(perform: close)
    }

    // MARK: header

    private var header: some View {
        HStack(spacing: 12) {
            JarvisEmblem(active: speech.state == .listening)
                .frame(width: 34, height: 34)
            VStack(alignment: .leading, spacing: 2) {
                Text("JARVIS").font(.system(size: 15, weight: .semibold, design: .rounded)).tracking(4)
                    .foregroundStyle(Theme.text)
                Text(statusLine).font(.system(size: 11)).foregroundStyle(Theme.muted)
            }
            Spacer()
            Button { model.openJarvis("/") } label: {
                Image(systemName: "globe").foregroundStyle(Theme.muted)
            }
            .buttonStyle(.plain)
            .help("Открыть веб-интерфейс Jarvis OS в Safari")
            Button { model.wakeWord.toggle() } label: {
                Image(systemName: model.wakeWord ? "ear.fill" : "ear")
                    .foregroundStyle(model.wakeWord ? Theme.accent : Theme.muted)
            }
            .buttonStyle(.plain)
            .help(model.wakeWord ? "Слушаю слово «Джарвис» (на этом Mac) — выключить" : "Включить слово «Джарвис»")
            Menu {
                Button("Открыть веб-интерфейс") { model.openJarvis("/") }
                Button("Сайты") { model.openJarvis("/sites") }
                Button("Лаборатория") { model.openJarvis("/lab") }
                Button("TLT") { model.openJarvis("/tlt") }
                Divider()
                Toggle("Отвечать голосом", isOn: $model.speakReplies)
                Button("Обновить приложения и проекты") { Task { await model.sendHello(rescan: true) } }
                Button("Очистить окно") { model.clear() }
            } label: {
                Image(systemName: "ellipsis.circle").foregroundStyle(Theme.muted)
            }
            .menuStyle(.borderlessButton)
            .menuIndicator(.hidden)
            .frame(width: 22)
        }
        .padding(.horizontal, 16)
        .padding(.top, 14)
        .padding(.bottom, 10)
    }

    private var statusLine: String {
        switch model.status {
        case .online:
            switch speech.state {
            case .listening: return "Слушаю…"
            case .waiting: return "Онлайн · скажите «Джарвис»"
            case .off: return "Онлайн · ⌥Space — сказать"
            }
        case .offline: return "Ядро выключено"
        case .notConfigured: return "Не настроен"
        case .unknown: return "Подключаюсь…"
        }
    }

    private var coreBanner: some View {
        HStack(spacing: 10) {
            Image(systemName: "bolt.horizontal.circle").foregroundStyle(.orange)
            Text(model.status == .offline ? "Ядро Jarvis не запущено (Docker выключен)" : "Выполните ./desktop/install.sh")
                .font(.system(size: 12)).foregroundStyle(Theme.text)
            Spacer()
            if model.status == .offline {
                Button("Запустить") { model.startCore() }
                    .buttonStyle(PillButtonStyle(prominent: true))
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 8)
        .background(Color.orange.opacity(0.10))
    }

    // MARK: conversation

    private var conversation: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 10) {
                    ForEach(model.messages) { message in
                        MessageRow(message: message) { button in model.tap(button, in: message) }
                            .id(message.id)
                    }
                    if model.busy {
                        TypingDots().id("typing")
                    }
                }
                .padding(16)
            }
            .onChange(of: model.messages.count) { _, _ in
                withAnimation(.easeOut(duration: 0.2)) { proxy.scrollTo(model.messages.last?.id, anchor: .bottom) }
            }
            .onChange(of: model.busy) { _, busy in
                if busy { withAnimation { proxy.scrollTo("typing", anchor: .bottom) } }
            }
        }
    }

    private var emptyState: some View {
        VStack(spacing: 18) {
            Spacer(minLength: 10)
            JarvisEmblem(active: speech.state == .listening).frame(width: 92, height: 92)
            VStack(spacing: 6) {
                Text("Чем могу помочь, сэр?").font(.system(size: 17, weight: .medium)).foregroundStyle(Theme.text)
                Text("Скажите «Джарвис, …», нажмите ⌥Space или напишите").font(.system(size: 12))
                    .foregroundStyle(Theme.muted)
            }
            FlowLayout(spacing: 8) {
                ForEach(ChatModel.suggestions, id: \.self) { text in
                    Button(text) { model.submit(text) }.buttonStyle(PillButtonStyle(prominent: false))
                }
            }
            .padding(.horizontal, 24)
            Spacer(minLength: 10)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    // MARK: input

    private var inputBar: some View {
        VStack(spacing: 0) {
            if speech.state == .listening {
                HStack(spacing: 10) {
                    Waveform().frame(width: 34, height: 18)
                    Text(speech.partial.isEmpty ? "Говорите…" : speech.partial)
                        .font(.system(size: 13)).foregroundStyle(Theme.text).lineLimit(2)
                    Spacer()
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 8)
                .background(Theme.accentSoft)
                .transition(.move(edge: .bottom).combined(with: .opacity))
            }
            if let problem = speech.problem {
                Text(problem).font(.system(size: 11)).foregroundStyle(.orange).padding(.horizontal, 16).padding(.top, 6)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            HStack(spacing: 10) {
                TextField("Скажите или напишите…", text: $model.input, axis: .vertical)
                    .textFieldStyle(.plain)
                    .font(.system(size: 14))
                    .foregroundStyle(Theme.text)
                    .lineLimit(1...5)
                    .focused($inputFocused)
                    .onSubmit { model.submit() }
                    .onChange(of: model.input) { _, value in if !value.isEmpty { speech.cancelCommand() } }
                Button { model.toggleListening() } label: {
                    Image(systemName: speech.state == .listening ? "stop.circle.fill" : "mic.circle.fill")
                        .font(.system(size: 22))
                        .foregroundStyle(speech.state == .listening ? Color.red.opacity(0.85) : Theme.accent)
                }
                .buttonStyle(.plain)
                .help("Сказать (⌥Space)")
                Button { model.submit() } label: {
                    Image(systemName: "arrow.up.circle.fill").font(.system(size: 22))
                        .foregroundStyle(model.input.isEmpty ? Theme.muted : Theme.accent)
                }
                .buttonStyle(.plain)
                .disabled(model.input.isEmpty)
                .keyboardShortcut(.return, modifiers: [])
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 12)
            .background(Color.white.opacity(0.04))
        }
        .animation(.easeOut(duration: 0.18), value: speech.state)
    }
}

// MARK: - pieces

struct MessageRow: View {
    let message: ChatMessage
    let onTap: (ReplyButton) -> Void

    var body: some View {
        switch message.role {
        case .system:
            HStack {
                Spacer()
                content.font(.system(size: 11)).foregroundStyle(Theme.muted).multilineTextAlignment(.center)
                Spacer()
            }
        case .user:
            HStack {
                Spacer(minLength: 50)
                HStack(alignment: .top, spacing: 6) {
                    if message.spoken { Image(systemName: "waveform").font(.system(size: 10)).padding(.top, 3) }
                    content
                }
                .font(.system(size: 13.5))
                .foregroundStyle(Theme.navy)
                .padding(.horizontal, 12).padding(.vertical, 8)
                .background(LinearGradient(colors: [Theme.accent, Theme.accent.opacity(0.78)], startPoint: .topLeading, endPoint: .bottomTrailing))
                .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
            }
        case .jarvis:
            VStack(alignment: .leading, spacing: 8) {
                HStack(alignment: .top, spacing: 6) {
                    if message.spoken {
                        Image(systemName: "speaker.wave.2.fill").font(.system(size: 10)).foregroundStyle(Theme.accent)
                            .padding(.top, 3)
                    }
                    content.font(.system(size: 13.5)).foregroundStyle(Theme.text).textSelection(.enabled)
                }
                if !message.buttons.isEmpty {
                    FlowLayout(spacing: 6) {
                        ForEach(Array(message.buttons.joined().enumerated()), id: \.offset) { _, button in
                            Button(button.text) { onTap(button) }.buttonStyle(PillButtonStyle(prominent: false))
                        }
                    }
                }
            }
            .padding(.horizontal, 12).padding(.vertical, 9)
            .background(Color.white.opacity(0.055))
            .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous).strokeBorder(Color.white.opacity(0.07)))
            .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.trailing, 30)
        }
    }

    @ViewBuilder private var content: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(Array(message.segments.enumerated()), id: \.offset) { _, segment in
                switch segment {
                case .text(let text):
                    Text(text).fixedSize(horizontal: false, vertical: true)
                case .code(let code):
                    Text(code).font(.system(size: 11.5, design: .monospaced)).foregroundStyle(Theme.text)
                        .padding(8)
                        .background(Color.black.opacity(0.28))
                        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
                        .fixedSize(horizontal: false, vertical: true)
                case .quote(let text):
                    HStack(alignment: .top, spacing: 8) {
                        RoundedRectangle(cornerRadius: 1.5, style: .continuous).fill(Theme.accent.opacity(0.55))
                            .frame(width: 3)
                        Text(text).foregroundStyle(Theme.text.opacity(0.85)).fixedSize(horizontal: false, vertical: true)
                    }
                    .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }
}

struct PillButtonStyle: ButtonStyle {
    var prominent: Bool

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 12, weight: .medium))
            .foregroundStyle(prominent ? Theme.navy : Theme.text)
            .padding(.horizontal, 11).padding(.vertical, 6)
            .background(prominent ? Theme.accent : Theme.accentSoft)
            .overlay(Capsule().strokeBorder(Theme.accent.opacity(prominent ? 0 : 0.35)))
            .clipShape(Capsule())
            .opacity(configuration.isPressed ? 0.7 : 1)
            .scaleEffect(configuration.isPressed ? 0.97 : 1)
    }
}

/// The logo's emblem: an «A» inside an orbit, glowing (brighter while listening).
struct JarvisEmblem: View {
    var active: Bool
    @State private var pulse = false

    var body: some View {
        GeometryReader { geo in
            let s = min(geo.size.width, geo.size.height)
            ZStack {
                Circle().fill(Theme.accent.opacity(active ? 0.22 : 0.10)).blur(radius: s * 0.18)
                    .scaleEffect(pulse && active ? 1.15 : 0.95)
                Circle().trim(from: 0.07, to: 0.93).stroke(Theme.accent.opacity(0.85), lineWidth: max(1, s * 0.03))
                    .rotationEffect(.degrees(-90))
                EmblemA().stroke(Color.white.opacity(0.92), style: StrokeStyle(lineWidth: max(1.2, s * 0.045), lineCap: .round, lineJoin: .round))
                    .padding(s * 0.2)
                Circle().fill(Color.white).frame(width: s * 0.09, height: s * 0.09)
                    .shadow(color: Theme.accent, radius: s * 0.12)
                    .offset(y: s * 0.04)
                Circle().fill(Theme.accent).frame(width: s * 0.08, height: s * 0.08)
                    .offset(x: s * 0.33, y: -s * 0.33)
            }
            .frame(width: geo.size.width, height: geo.size.height)
        }
        .onAppear {
            withAnimation(.easeInOut(duration: 1.1).repeatForever(autoreverses: true)) { pulse = true }
        }
    }
}

struct EmblemA: Shape {
    func path(in rect: CGRect) -> Path {
        var path = Path()
        let top = CGPoint(x: rect.midX, y: rect.minY)
        path.move(to: CGPoint(x: rect.minX + rect.width * 0.08, y: rect.maxY))
        path.addLine(to: top)
        path.addLine(to: CGPoint(x: rect.maxX - rect.width * 0.08, y: rect.maxY))
        path.move(to: CGPoint(x: rect.minX + rect.width * 0.28, y: rect.maxY))
        path.addLine(to: CGPoint(x: rect.midX, y: rect.midY + rect.height * 0.06))
        path.addLine(to: CGPoint(x: rect.maxX - rect.width * 0.28, y: rect.maxY))
        return path
    }
}

struct Waveform: View {
    @State private var phase = false

    var body: some View {
        HStack(spacing: 3) {
            ForEach(0..<5) { index in
                Capsule().fill(Theme.accent)
                    .frame(width: 3)
                    .scaleEffect(y: phase ? [0.4, 1.0, 0.6, 0.9, 0.5][index] : [0.9, 0.5, 1.0, 0.4, 0.8][index], anchor: .center)
            }
        }
        .onAppear {
            withAnimation(.easeInOut(duration: 0.45).repeatForever(autoreverses: true)) { phase = true }
        }
    }
}

struct TypingDots: View {
    @State private var on = false

    var body: some View {
        HStack(spacing: 5) {
            ForEach(0..<3) { index in
                Circle().fill(Theme.accent).frame(width: 6, height: 6)
                    .opacity(on ? 1 : 0.25)
                    .animation(.easeInOut(duration: 0.5).repeatForever().delay(Double(index) * 0.15), value: on)
            }
        }
        .padding(.horizontal, 12).padding(.vertical, 10)
        .background(Color.white.opacity(0.05))
        .clipShape(Capsule())
        .onAppear { on = true }
    }
}

/// Wraps its children into rows (buttons under a message, suggestion chips).
struct FlowLayout: Layout {
    var spacing: CGFloat = 8

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let width = proposal.width ?? 360
        var x: CGFloat = 0, y: CGFloat = 0, rowHeight: CGFloat = 0, maxX: CGFloat = 0
        for view in subviews {
            let size = view.sizeThatFits(.unspecified)
            if x > 0, x + size.width > width {
                x = 0
                y += rowHeight + spacing
                rowHeight = 0
            }
            x += size.width + spacing
            maxX = max(maxX, x - spacing)
            rowHeight = max(rowHeight, size.height)
        }
        return CGSize(width: min(maxX, width), height: y + rowHeight)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var x = bounds.minX, y = bounds.minY, rowHeight: CGFloat = 0
        for view in subviews {
            let size = view.sizeThatFits(.unspecified)
            if x > bounds.minX, x + size.width > bounds.maxX {
                x = bounds.minX
                y += rowHeight + spacing
                rowHeight = 0
            }
            view.place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(size))
            x += size.width + spacing
            rowHeight = max(rowHeight, size.height)
        }
    }
}

struct VisualEffectBackground: NSViewRepresentable {
    func makeNSView(context: Context) -> NSVisualEffectView {
        let view = NSVisualEffectView()
        view.material = .hudWindow
        view.blendingMode = .behindWindow
        view.state = .active
        return view
    }

    func updateNSView(_ nsView: NSVisualEffectView, context: Context) {}
}
