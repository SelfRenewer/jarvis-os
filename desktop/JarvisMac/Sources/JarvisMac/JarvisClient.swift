import Foundation

struct ReplyButton: Decodable, Hashable {
    let text: String
    let data: String
}

struct ServerMessage: Decodable {
    let id: Int
    let text: String
    let format: String
    let buttons: [[ReplyButton]]
    let audio: String?
}

struct ServerEdit: Decodable {
    let id: Int
    let text: String
    let format: String
    let buttons: [[ReplyButton]]
}

struct ServerReply: Decodable {
    let messages: [ServerMessage]
    let edits: [ServerEdit]
    let toast: String?
    let voiceMac: Bool?  // the Mac's own voice setting (the server is the source of truth)

    enum CodingKeys: String, CodingKey {
        case messages, edits, toast
        case voiceMac = "voice_mac"
    }
}

struct MacAction: Decodable {
    let id: String
    let kind: String
    let payload: [String: String?]
    let source: String

    func value(_ key: String) -> String? { payload[key] ?? nil }
}

struct ProjectInfo: Encodable, Hashable {
    let name: String
    let path: String
}

enum ClientError: LocalizedError {
    case notConfigured
    case http(Int)

    var errorDescription: String? {
        switch self {
        case .notConfigured: return "Jarvis для Mac не настроен: выполните ./desktop/install.sh"
        case .http(let code): return code == 401 ? "Неверный ключ доступа (выполните ./desktop/install.sh)" : "Ошибка ядра Jarvis (HTTP \(code))"
        }
    }
}

/// The local Jarvis API (`/api/desktop/v1/*`), loopback only, bearer token from the config file.
final class JarvisClient {
    private let config: JarvisConfig?
    private let session: URLSession

    init(config: JarvisConfig?) {
        self.config = config
        let cfg = URLSessionConfiguration.ephemeral
        cfg.timeoutIntervalForRequest = 60
        cfg.timeoutIntervalForResource = 120
        self.session = URLSession(configuration: cfg)
    }

    var isConfigured: Bool { config != nil }

    private func request(_ path: String, method: String = "GET", body: Encodable? = nil, timeout: TimeInterval = 45) throws -> URLRequest {
        guard let config, let url = URL(string: config.base_url + path) else { throw ClientError.notConfigured }
        var request = URLRequest(url: url, timeoutInterval: timeout)
        request.httpMethod = method
        request.setValue("Bearer \(config.token)", forHTTPHeaderField: "Authorization")
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONEncoder().encode(AnyEncodable(body))
        }
        return request
    }

    private func call<T: Decodable>(_ request: URLRequest, as type: T.Type) async throws -> T {
        let (data, response) = try await session.data(for: request)
        let code = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(code) else { throw ClientError.http(code) }
        return try JSONDecoder().decode(T.self, from: data)
    }

    /// Is the core (Docker) up? No token needed.
    func health() async -> Bool {
        guard let config, let url = URL(string: config.base_url + "/health") else { return false }
        var request = URLRequest(url: url, timeoutInterval: 3)
        request.httpMethod = "GET"
        guard let (_, response) = try? await session.data(for: request) else { return false }
        return (response as? HTTPURLResponse)?.statusCode == 200
    }

    struct Hello: Encodable {
        let apps: [String]
        let projects: [ProjectInfo]
        let version: String
    }

    struct HelloReply: Decodable {
        let status: String
        let address: String?
        let voiceMac: Bool?

        enum CodingKeys: String, CodingKey {
            case status, address
            case voiceMac = "voice_mac"
        }
    }

    struct VoiceBody: Encodable { let enabled: Bool }
    struct VoiceReply: Decodable {
        let voiceMac: Bool
        enum CodingKeys: String, CodingKey { case voiceMac = "voice_mac" }
    }

    /// «Отвечать голосом» on this Mac (Telegram's /voice is separate).
    func setVoice(_ enabled: Bool) async throws -> Bool {
        let req = try request("/api/desktop/v1/voice", method: "POST", body: VoiceBody(enabled: enabled))
        return try await call(req, as: VoiceReply.self).voiceMac
    }

    func hello(apps: [String], projects: [ProjectInfo]) async throws -> HelloReply {
        let body = Hello(apps: apps, projects: projects, version: AppInfo.version)
        return try await call(try request("/api/desktop/v1/hello", method: "POST", body: body), as: HelloReply.self)
    }

    struct MessageBody: Encodable { let text: String; let spoken: Bool }

    func send(_ text: String, spoken: Bool) async throws -> ServerReply {
        let req = try request("/api/desktop/v1/message", method: "POST", body: MessageBody(text: text, spoken: spoken), timeout: 90)
        return try await call(req, as: ServerReply.self)
    }

    struct CallbackBody: Encodable { let data: String; let message_id: Int? }

    func tap(_ data: String, messageId: Int?) async throws -> ServerReply {
        let req = try request("/api/desktop/v1/callback", method: "POST", body: CallbackBody(data: data, message_id: messageId))
        return try await call(req, as: ServerReply.self)
    }

    struct ActionsReply: Decodable { let actions: [MacAction] }

    func pollActions(wait: Int) async throws -> [MacAction] {
        let req = try request("/api/desktop/v1/actions?wait=\(wait)", timeout: TimeInterval(wait + 15))
        return try await call(req, as: ActionsReply.self).actions
    }

    struct ResultBody: Encodable { let ok: Bool; let detail: String? }
    struct ResultReply: Decodable { let status: String }

    func report(_ id: String, ok: Bool, detail: String?) async {
        guard let req = try? request("/api/desktop/v1/actions/\(id)/result", method: "POST", body: ResultBody(ok: ok, detail: detail)) else { return }
        _ = try? await call(req, as: ResultReply.self)
    }
}

/// Type-erased Encodable for request bodies.
struct AnyEncodable: Encodable {
    private let encodeFunc: (Encoder) throws -> Void
    init(_ value: Encodable) { encodeFunc = value.encode }
    func encode(to encoder: Encoder) throws { try encodeFunc(encoder) }
}

enum AppInfo {
    static let version = (Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String) ?? "dev"
}
