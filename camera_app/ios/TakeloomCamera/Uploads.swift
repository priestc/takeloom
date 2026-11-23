import Foundation

/// A finished session recording waiting to go to the server.
struct PendingRecording: Codable, Identifiable {
    var sessionID: String
    var fileName: String
    var host: String
    var videoStartPtsNs: Int64
    var clockOffsetNs: Int64?
    var clockRttNs: Int64?
    var width: Int
    var height: Int
    var fps: Int
    var duration: Double
    var rejected: String?

    var id: String { sessionID }
}

/// Recordings live in Documents/Recordings as <session>.mp4 + <session>.json
/// until the server confirms it has the whole file — the app can be killed,
/// lose Wi-Fi, or be relaunched days later and still finish the upload.
enum RecordingStore {
    static var directory: URL {
        let url = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("Recordings", isDirectory: true)
        try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }

    static func safeName(_ sessionID: String) -> String {
        String(sessionID.map { $0.isLetter || $0.isNumber || "-_.".contains($0) ? $0 : "_" })
    }

    static func videoURL(for sessionID: String) -> URL {
        directory.appendingPathComponent(safeName(sessionID) + ".mp4")
    }

    static func save(_ recording: PendingRecording) {
        let url = directory.appendingPathComponent(safeName(recording.sessionID) + ".json")
        if let data = try? JSONEncoder().encode(recording) { try? data.write(to: url, options: .atomic) }
    }

    static func all() -> [PendingRecording] {
        let files = (try? FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)) ?? []
        return files.filter { $0.pathExtension == "json" }
            .compactMap { try? JSONDecoder().decode(PendingRecording.self, from: Data(contentsOf: $0)) }
            .sorted { $0.sessionID < $1.sessionID }
    }

    static func remove(_ sessionID: String) {
        let name = safeName(sessionID)
        try? FileManager.default.removeItem(at: directory.appendingPathComponent(name + ".json"))
        try? FileManager.default.removeItem(at: directory.appendingPathComponent(name + ".mp4"))
    }
}

/// Sends pending recordings one at a time over their own connection (so the
/// live feed and clock pings never queue behind gigabytes), resuming from
/// whatever the server already has after a drop.
///
/// The server may instead point a recording at its remote vault
/// ("upload_redirect": a URL and a vault token it got for this phone, so
/// pairing with the server is all this ever needs): it then goes there over
/// HTTP, a chunk per PUT, resuming from whatever the vault already has.
/// Every attempt asks the server first, so it always decides where.
final class Uploader {
    private let queue = DispatchQueue(label: "uploader")
    var onProgress: ((String?) -> Void)?
    var onChange: (() -> Void)?
    private var busy = false
    private var connection: FramedConnection?
    private let chunkSize = 512 * 1024

    func kick() {
        queue.async { self.next() }
    }

    private func next() {
        guard !busy else { return }
        guard let recording = RecordingStore.all().first(where: { $0.rejected == nil && !$0.host.isEmpty }) else {
            onProgress?(nil)
            return
        }
        busy = true
        upload(recording) { [weak self] retryLater in
            guard let self else { return }
            self.busy = false
            self.connection = nil
            self.onChange?()
            if retryLater {
                self.queue.asyncAfter(deadline: .now() + 5) { self.next() }
            } else {
                self.next()
            }
        }
    }

    /// done(true) = transient failure, try again later.
    private func upload(_ recording: PendingRecording, done: @escaping (Bool) -> Void) {
        let url = RecordingStore.videoURL(for: recording.sessionID)
        guard let size = (try? FileManager.default.attributesOfItem(atPath: url.path)[.size] as? NSNumber)?.int64Value,
              let file = try? FileHandle(forReadingFrom: url) else {
            RecordingStore.remove(recording.sessionID)
            done(false)
            return
        }
        let connection = FramedConnection(host: recording.host, queue: queue)
        self.connection = connection
        var finished = false
        let finish = { (retry: Bool) in
            guard !finished else { return }
            finished = true
            try? file.close()
            connection.close()
            done(retry)
        }
        var redirected = false
        var sent: Int64 = 0

        func sendChunks() {
            // One chunk in flight at a time: memory stays flat however big
            // the file is, and progress tracks what actually left.
            guard let chunk = try? file.read(upToCount: chunkSize), !chunk.isEmpty else {
                connection.sendJSON(["kind": "upload_end"])
                return
            }
            connection.send(Wire.frame(.upload, chunk)) {
                sent += Int64(chunk.count)
                let pct = Int(100 * Double(sent) / Double(max(size, 1)))
                self.onProgress?("Uploading \(recording.sessionID): \(pct)%")
                sendChunks()
            }
        }

        connection.onClose = { _ in if !redirected { finish(true) } }
        connection.onFrame = { type, data in
            guard type == .json, let message = decodeJSON(data), let kind = message["kind"] as? String else { return }
            switch kind {
            case "hello_ack":
                guard message["ok"] as? Bool == true else { finish(true); return }
                var metadata: [String: Any] = [
                    "video_start_pts_ns": recording.videoStartPtsNs,
                    "width": recording.width, "height": recording.height, "fps": recording.fps,
                    "duration_s": recording.duration, "platform": "ios",
                ]
                if let offset = recording.clockOffsetNs { metadata["clock_offset_ns"] = offset }
                if let rtt = recording.clockRttNs { metadata["clock_rtt_ns"] = rtt }
                connection.sendJSON([
                    "kind": "upload_begin", "session_id": recording.sessionID, "size": size, "metadata": metadata,
                ])
            case "upload_resume":
                let offset = int64(message["offset"]) ?? 0
                sent = offset
                do { try file.seek(toOffset: UInt64(offset)) } catch { finish(true); return }
                sendChunks()
            case "upload_redirect":
                guard let target = (message["url"] as? String).flatMap(URL.init(string:)),
                      let token = message["token"] as? String else { finish(true); return }
                redirected = true
                connection.close()
                let chunk = Int(int64(message["chunk_size"]) ?? 8 * 1024 * 1024)
                DispatchQueue.global(qos: .utility).async {
                    let retry = self.uploadToVault(recording, file: file, size: size, url: target,
                                                   token: token, chunkSize: chunk)
                    self.queue.async {
                        guard !finished else { return }
                        finished = true
                        try? file.close()
                        done(retry)
                    }
                }
            case "upload_rejected":
                var updated = recording
                updated.rejected = message["error"] as? String ?? "Rejected by the server."
                RecordingStore.save(updated)
                finish(false)
            case "upload_done":
                if message["ok"] as? Bool == true {
                    RecordingStore.remove(recording.sessionID)
                    finish(false)
                } else {
                    finish(true)
                }
            default:
                break
            }
        }
        connection.start {
            connection.sendJSON([
                "kind": "hello", "role": "upload", "token": Settings.token(for: recording.host),
                "client_name": Settings.deviceName, "device_id": Settings.deviceID,
                "platform": "ios", "app_version": Settings.appVersion, "device_model": Settings.deviceModel,
                "http_upload": true,
            ])
        }
    }

    /// The recording to the vault at `url` (see the class comment), from
    /// wherever the vault's copy is up to. Blocking — runs off the uploader
    /// queue. Returns true to try again later (through the server), false
    /// once the vault has all of it (and it's deleted here).
    private func uploadToVault(_ recording: PendingRecording, file: FileHandle, size: Int64, url: URL,
                               token: String, chunkSize: Int) -> Bool {
        guard let status = vaultRequest(url, method: "GET", token: token, body: nil) else { return true }
        var offset = int64(status.json["received"]) ?? 0
        if let theirs = int64(status.json["size"]), theirs != size { offset = 0 }
        if status.json["done"] as? Bool == true {
            RecordingStore.remove(recording.sessionID)
            return false
        }
        while true {
            do { try file.seek(toOffset: UInt64(offset)) } catch { return true }
            guard let chunk = try? file.read(upToCount: chunkSize), !chunk.isEmpty else { return true }
            var parts = URLComponents(url: url, resolvingAgainstBaseURL: false)!
            parts.queryItems = [URLQueryItem(name: "offset", value: String(offset)),
                                URLQueryItem(name: "size", value: String(size))]
            guard let reply = vaultRequest(parts.url!, method: "PUT", token: token, body: chunk) else { return true }
            if reply.code == 409 {
                offset = int64(reply.json["received"]) ?? 0  // the vault says where it's up to
                continue
            }
            guard reply.code == 200 else { return true }
            if reply.json["done"] as? Bool == true {
                onProgress?("Uploaded \(recording.sessionID) to the vault")
                RecordingStore.remove(recording.sessionID)
                return false
            }
            offset = int64(reply.json["received"]) ?? (offset + Int64(chunk.count))
            let pct = Int(100 * Double(offset) / Double(max(size, 1)))
            onProgress?("Uploading \(recording.sessionID) to the vault: \(pct)%")
        }
    }

    /// One HTTP request, waited for: (status code, JSON body), or nil if it
    /// never got an answer (or a 401/5xx: try again later, via the server).
    private func vaultRequest(_ url: URL, method: String, token: String, body: Data?) -> (code: Int, json: [String: Any])? {
        var request = URLRequest(url: url, timeoutInterval: 120)
        request.httpMethod = method
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        if let body {
            request.setValue("application/octet-stream", forHTTPHeaderField: "Content-Type")
            request.httpBody = body
        }
        let done = DispatchSemaphore(value: 0)
        var result: (code: Int, json: [String: Any])?
        URLSession.shared.dataTask(with: request) { data, response, error in
            defer { done.signal() }
            guard error == nil, let http = response as? HTTPURLResponse else { return }
            let json = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] } ?? [:]
            if http.statusCode == 200 || http.statusCode == 409 { result = (http.statusCode, json) }
        }.resume()
        done.wait()
        return result
    }
}
