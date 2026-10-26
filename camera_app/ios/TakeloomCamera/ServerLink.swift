import Foundation

/// NTP-style offset between this phone's capture clock and the server's
/// clock, from the lowest-round-trip of recent pings (the one least
/// distorted by Wi-Fi queuing).
struct ClockSync {
    private var samples: [(rtt: Int64, offset: Int64)] = []

    mutating func add(t0: Int64, serverTime: Int64, t1: Int64) {
        let rtt = t1 - t0
        guard rtt >= 0 else { return }
        samples.append((rtt, serverTime - (t0 + t1) / 2))
        if samples.count > 40 { samples.removeFirst(samples.count - 40) }
    }

    var best: (rtt: Int64, offset: Int64)? {
        samples.min { $0.rtt < $1.rtt }
    }

    var sampleCount: Int { samples.count }

    mutating func reset() { samples.removeAll() }
}

enum LinkState: Equatable {
    case idle
    case connecting
    case awaitingApproval
    case connected(hostname: String)
    case failed(String)
}

/// The persistent "camera" connection to the takeloom server: pairing,
/// clock sync, status reports, the live feed, and start/stop commands.
/// Reconnects on its own until disconnect() is called.
final class ServerLink {
    let queue = DispatchQueue(label: "server.link")
    var onState: ((LinkState) -> Void)?
    /// start_recording / stop_recording / ... from the server, on `queue`.
    var onCommand: (([String: Any]) -> Void)?
    /// Supplies the fields of the periodic status message.
    var statusProvider: (() -> [String: Any])?
    var nowNs: () -> Int64 = { Int64(DispatchTime.now().uptimeNanoseconds) }
    var onNeedKeyframe: (() -> Void)?

    private var connection: FramedConnection?
    private var host = ""
    private var wanted = false
    private var helloDone = false
    private var clock = ClockSync()
    private var pingID = 0
    private var pings: [Int: Int64] = [:]
    private var timer: DispatchSourceTimer?
    private var ticks = 0
    private var reconnectWork: DispatchWorkItem?
    private var droppingUntilKeyframe = false

    /// Live video gives way (frames dropped until the next keyframe) once
    /// this much is still queued for the network: a couple of seconds at
    /// 1080p. The recording file is never affected.
    private let maxVideoBacklog = 3 * 1024 * 1024

    private(set) var clockOffset: (offset: Int64, rtt: Int64)?

    func connect(host: String) {
        queue.async {
            self.host = host
            self.wanted = true
            self.open()
        }
    }

    func disconnect() {
        queue.async {
            self.wanted = false
            self.reconnectWork?.cancel()
            self.connection?.close()
            self.connection = nil
            self.onState?(.idle)
        }
    }

    // MARK: - Sending (any thread)

    func sendJSON(_ object: [String: Any]) {
        queue.async {
            guard self.helloDone else { return }
            self.connection?.sendJSON(object)
        }
    }

    func sendVideo(ptsNs: Int64, keyframe: Bool, annexB: Data) {
        queue.async {
            guard self.helloDone, let connection = self.connection else { return }
            if connection.bytesInFlight > self.maxVideoBacklog {
                self.droppingUntilKeyframe = true
            }
            if self.droppingUntilKeyframe {
                guard keyframe, connection.bytesInFlight < self.maxVideoBacklog / 2 else {
                    if connection.bytesInFlight < self.maxVideoBacklog / 2 { self.onNeedKeyframe?() }
                    return
                }
                self.droppingUntilKeyframe = false
            }
            connection.send(Wire.video(ptsNs: ptsNs, keyframe: keyframe, annexB: annexB))
        }
    }

    func sendPreview(_ jpeg: Data) {
        queue.async {
            guard self.helloDone, let connection = self.connection,
                  connection.bytesInFlight < self.maxVideoBacklog / 4 else { return }
            connection.send(Wire.frame(.preview, jpeg))
        }
    }

    // MARK: - Connection lifecycle (on queue)

    private func open() {
        connection?.close()
        helloDone = false
        clock.reset()
        clockOffset = nil
        guard wanted, !host.isEmpty else { return }
        onState?(.connecting)
        let connection = FramedConnection(host: host, queue: queue)
        self.connection = connection
        connection.onFrame = { [weak self, weak connection] type, data in
            guard let self, let connection, connection === self.connection else { return }
            self.handle(type: type, data: data)
        }
        connection.onClose = { [weak self, weak connection] error in
            guard let self, connection === self.connection else { return }
            self.connection = nil
            self.helloDone = false
            self.stopTimer()
            if self.wanted {
                self.onState?(.failed(error.map { "\($0.localizedDescription)" } ?? "Connection closed."))
                self.scheduleReconnect()
            }
        }
        connection.start {
            connection.sendJSON([
                "kind": "hello", "role": "camera", "token": Settings.token(for: self.host),
                "client_name": Settings.deviceName, "device_id": Settings.deviceID,
                "platform": "ios", "app_version": Settings.appVersion, "device_model": Settings.deviceModel,
            ])
        }
    }

    private func scheduleReconnect() {
        reconnectWork?.cancel()
        let work = DispatchWorkItem { [weak self] in self?.open() }
        reconnectWork = work
        queue.asyncAfter(deadline: .now() + 2, execute: work)
    }

    private func handle(type: FrameType, data: Data) {
        guard type == .json, let message = decodeJSON(data), let kind = message["kind"] as? String else { return }
        switch kind {
        case "hello_pending":
            onState?(.awaitingApproval)
        case "hello_ack":
            if message["ok"] as? Bool == true {
                if let token = message["token"] as? String { Settings.setToken(token, for: host) }
                helloDone = true
                onState?(.connected(hostname: message["hostname"] as? String ?? host))
                startTimer()
                sendStatus()
                onNeedKeyframe?()
            } else {
                let error = message["error"] as? String ?? "Refused."
                if error.contains("denied") { Settings.setToken("", for: host) }
                wanted = false  // don't hammer the server with re-requests; the user retries
                onState?(.failed(error))
                connection?.close()
            }
        case "clock_pong":
            let t1 = nowNs()
            guard let id = (message["id"] as? NSNumber)?.intValue, let t0 = pings.removeValue(forKey: id),
                  let serverTime = int64(message["ts"]) else { return }
            clock.add(t0: t0, serverTime: serverTime, t1: t1)
            if let best = clock.best, best.offset != clockOffset?.offset || best.rtt != clockOffset?.rtt {
                clockOffset = (best.offset, best.rtt)
                connection?.sendJSON(["kind": "clock_sync", "offset_ns": best.offset, "rtt_ns": best.rtt])
            }
        case "request_keyframe":
            onNeedKeyframe?()
        default:
            onCommand?(message)
        }
    }

    private func sendPing() {
        pingID += 1
        let t0 = nowNs()
        pings[pingID] = t0
        if pings.count > 50 { pings.removeAll() }
        connection?.sendJSON(["kind": "clock_ping", "id": pingID, "t0": t0])
    }

    func sendStatus() {
        queue.async {
            guard self.helloDone, var status = self.statusProvider?() else { return }
            status["kind"] = "status"
            self.connection?.sendJSON(status)
        }
    }

    /// 10 Hz: a burst of pings for the first few seconds (a good clock
    /// estimate fast), then one a second to track drift; status every 5 s.
    private func startTimer() {
        stopTimer()
        ticks = 0
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now(), repeating: .milliseconds(100))
        timer.setEventHandler { [weak self] in
            guard let self, self.helloDone else { return }
            self.ticks += 1
            if self.ticks <= 30 || self.ticks % 10 == 0 { self.sendPing() }
            if self.ticks % 50 == 0 { self.sendStatus() }
        }
        timer.resume()
        self.timer = timer
    }

    private func stopTimer() {
        timer?.cancel()
        timer = nil
    }
}
