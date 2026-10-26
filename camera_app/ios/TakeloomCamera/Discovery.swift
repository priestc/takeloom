import Foundation
import Network

/// A takeloom server found on the local network.
struct DiscoveredServer: Identifiable, Hashable {
    let host: String      // its IP address
    let hostname: String  // what it calls itself ("Mac.local")
    var id: String { host }
    var address: String { "\(host):\(Wire.port)" }
}

/// Finding the takeloom server without typing its address: try every
/// address on the phone's own Wi-Fi network on the camera port, and keep
/// the ones that answer the protocol's "discover" with "discover_ack" (see
/// takeloom/phone_camera/protocol.py), so another service that happens to
/// use the port isn't mistaken for takeloom.
enum ServerDiscovery {
    static let maxConcurrent = 64
    static let probeTimeout: TimeInterval = 1.5

    /// Every takeloom server answering on this network, by address.
    static func scan() async -> [DiscoveredServer] {
        let hosts = candidateHosts()
        var found: [DiscoveredServer] = []
        await withTaskGroup(of: DiscoveredServer?.self) { group in
            var next = hosts.makeIterator()
            for _ in 0..<maxConcurrent {
                guard let host = next.next() else { break }
                group.addTask { await probe(host) }
            }
            while let result = await group.next() {
                if let result { found.append(result) }
                if let host = next.next() { group.addTask { await probe(host) } }
            }
        }
        return found.sorted { $0.host.compare($1.host, options: .numeric) == .orderedAscending }
    }

    /// The other addresses on the Wi-Fi network: the phone's /24 (a wider
    /// network is narrowed to the /24 the phone is in, to keep a scan to a
    /// few seconds).
    static func candidateHosts() -> [String] {
        guard let (address, netmask) = wifiIPv4() else { return [] }
        let prefix = (0..<32).filter { netmask & (UInt32(1) << (31 - $0)) != 0 }.count
        let mask: UInt32 = prefix >= 24 ? netmask : 0xFFFF_FF00
        let network = address & mask
        let size = ~mask
        guard size > 1 else { return [] }
        return (1..<size).map { network | $0 }.filter { $0 != address }.map { ip in
            "\(ip >> 24 & 0xFF).\(ip >> 16 & 0xFF).\(ip >> 8 & 0xFF).\(ip & 0xFF)"
        }
    }

    /// The local network's IPv4 address and netmask (host order): Wi-Fi
    /// (en0) if it has one, else the first other interface on a private
    /// network (e.g. an iPad on a USB Ethernet adapter).
    private static func wifiIPv4() -> (UInt32, UInt32)? {
        var list: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&list) == 0, let first = list else { return nil }
        defer { freeifaddrs(list) }
        var candidates: [(name: String, ip: UInt32, mask: UInt32)] = []
        for pointer in sequence(first: first, next: { $0.pointee.ifa_next }) {
            let entry = pointer.pointee
            guard let addr = entry.ifa_addr, addr.pointee.sa_family == UInt8(AF_INET),
                  let mask = entry.ifa_netmask else { continue }
            let ip = addr.withMemoryRebound(to: sockaddr_in.self, capacity: 1) { UInt32(bigEndian: $0.pointee.sin_addr.s_addr) }
            let netmask = mask.withMemoryRebound(to: sockaddr_in.self, capacity: 1) { UInt32(bigEndian: $0.pointee.sin_addr.s_addr) }
            let isPrivate = ip >> 24 == 10 || ip >> 20 == 0xAC1 || ip >> 16 == 0xC0A8  // 10/8, 172.16/12, 192.168/16
            if isPrivate { candidates.append((String(cString: entry.ifa_name), ip, netmask)) }
        }
        let chosen = candidates.first { $0.name == "en0" } ?? candidates.first
        return chosen.map { ($0.ip, $0.mask) }
    }

    /// Ask `host` "discover"; its DiscoveredServer if it answers as takeloom.
    static func probe(_ host: String) async -> DiscoveredServer? {
        await withCheckedContinuation { continuation in
            let queue = DispatchQueue(label: "discovery.\(host)")
            let connection = FramedConnection(host: host, queue: queue, connectTimeout: 1)
            var finished = false
            func finish(_ result: DiscoveredServer?) {  // always on `queue`
                guard !finished else { return }
                finished = true
                connection.onClose = nil
                connection.close()
                continuation.resume(returning: result)
            }
            connection.onFrame = { type, payload in
                guard type == .json,
                      let message = try? JSONSerialization.jsonObject(with: payload) as? [String: Any],
                      message["kind"] as? String == "discover_ack" else { return finish(nil) }
                finish(DiscoveredServer(host: host, hostname: message["hostname"] as? String ?? host))
            }
            connection.onClose = { _ in finish(nil) }
            connection.start { connection.sendJSON(["kind": "discover"]) }
            queue.asyncAfter(deadline: .now() + probeTimeout) { finish(nil) }
        }
    }
}
