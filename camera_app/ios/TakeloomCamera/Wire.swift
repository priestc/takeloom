import Foundation
import Network

/// The takeloom server's camera protocol — see takeloom/phone_camera/protocol.py
/// for the authoritative description. Every message is
/// [1 byte type][4 byte big-endian length][payload].
enum FrameType: UInt8 {
    case json = 1
    case video = 2
    case preview = 3
    case upload = 4
}

enum Wire {
    static let port: UInt16 = 51824
    static let maxFrameBytes = 32 * 1024 * 1024

    static func frame(_ type: FrameType, _ payload: Data) -> Data {
        var data = Data(capacity: payload.count + 5)
        data.append(type.rawValue)
        appendBigEndian(&data, UInt32(payload.count))
        data.append(payload)
        return data
    }

    static func json(_ object: [String: Any]) -> Data {
        let payload = (try? JSONSerialization.data(withJSONObject: object)) ?? Data("{}".utf8)
        return frame(.json, payload)
    }

    static func video(ptsNs: Int64, keyframe: Bool, annexB: Data) -> Data {
        var payload = Data(capacity: annexB.count + 9)
        appendBigEndian(&payload, UInt64(bitPattern: ptsNs))
        payload.append(keyframe ? 1 : 0)
        payload.append(annexB)
        return frame(.video, payload)
    }

    private static func appendBigEndian<T: FixedWidthInteger>(_ data: inout Data, _ value: T) {
        var be = value.bigEndian
        withUnsafeBytes(of: &be) { data.append(contentsOf: $0) }
    }
}

/// One TCP connection speaking the framed protocol. Everything — sends,
/// receives, callbacks — runs on `queue`.
final class FramedConnection {
    private let connection: NWConnection
    let queue: DispatchQueue
    var onFrame: ((FrameType, Data) -> Void)?
    var onClose: ((Error?) -> Void)?
    private var closed = false
    /// Bytes handed to the network stack but not yet sent — what live video
    /// backpressure looks at. Only touched on `queue`.
    private(set) var bytesInFlight = 0

    init(host: String, port: UInt16 = Wire.port, queue: DispatchQueue) {
        let tcp = NWProtocolTCP.Options()
        tcp.noDelay = true
        tcp.connectionTimeout = 5
        let params = NWParameters(tls: nil, tcp: tcp)
        connection = NWConnection(host: NWEndpoint.Host(host), port: NWEndpoint.Port(rawValue: port)!, using: params)
        self.queue = queue
    }

    func start(onReady: @escaping () -> Void) {
        connection.stateUpdateHandler = { [weak self] state in
            guard let self else { return }
            switch state {
            case .ready:
                onReady()
                self.readHeader()
            case .failed(let error), .waiting(let error):
                // .waiting = refused/unreachable; give up and let the
                // owner's retry loop decide when to try again.
                self.close(error)
            case .cancelled:
                self.close(nil)
            default:
                break
            }
        }
        connection.start(queue: queue)
    }

    /// Thread-safe; `completion` runs on `queue` once the bytes are sent.
    func send(_ data: Data, completion: (() -> Void)? = nil) {
        queue.async {
            guard !self.closed else { return }
            self.bytesInFlight += data.count
            self.connection.send(content: data, completion: .contentProcessed { error in
                self.bytesInFlight -= data.count
                if let error {
                    self.close(error)
                } else {
                    completion?()
                }
            })
        }
    }

    func sendJSON(_ object: [String: Any]) {
        send(Wire.json(object))
    }

    func close(_ error: Error? = nil) {
        guard !closed else { return }
        closed = true
        connection.cancel()
        onClose?(error)
        onClose = nil
        onFrame = nil
    }

    private func readHeader() {
        connection.receive(minimumIncompleteLength: 5, maximumLength: 5) { [weak self] data, _, isComplete, error in
            guard let self, !self.closed else { return }
            guard let data, data.count == 5, error == nil else {
                self.close(error)
                return
            }
            let bytes = [UInt8](data)
            let length = Int(bytes[1]) << 24 | Int(bytes[2]) << 16 | Int(bytes[3]) << 8 | Int(bytes[4])
            guard let type = FrameType(rawValue: bytes[0]), length <= Wire.maxFrameBytes else {
                self.close(nil)
                return
            }
            if length == 0 {
                self.onFrame?(type, Data())
                if isComplete { self.close(nil) } else { self.readHeader() }
                return
            }
            self.readPayload(type: type, length: length)
        }
    }

    private func readPayload(type: FrameType, length: Int) {
        connection.receive(minimumIncompleteLength: length, maximumLength: length) { [weak self] data, _, _, error in
            guard let self, !self.closed else { return }
            guard let data, data.count == length, error == nil else {
                self.close(error)
                return
            }
            self.onFrame?(type, data)
            if !self.closed { self.readHeader() }
        }
    }
}

func decodeJSON(_ data: Data) -> [String: Any]? {
    (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
}

/// Int64 from a JSON number (NSNumber) or nil.
func int64(_ value: Any?) -> Int64? {
    (value as? NSNumber)?.int64Value
}
