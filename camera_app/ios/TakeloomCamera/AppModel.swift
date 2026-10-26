import AVFoundation
import Combine
import UIKit

/// Glue between the camera, the server link and the uploader, plus the
/// state the UI shows.
@MainActor
final class AppModel: ObservableObject {
    @Published var linkState: LinkState = .idle
    @Published var recordingSession: String?
    @Published var recordingSince: Date?
    @Published var uploadStatus: String?
    @Published var pending: [PendingRecording] = []
    @Published var cameraError: String?
    @Published var serverHost = Settings.serverHost
    /// Network scan for the server (ServerDiscovery): running, and what it
    /// found last. `discoveryPrompt` asks whether to use what was found.
    @Published var scanning = false
    @Published var discovered: [DiscoveredServer] = []
    @Published var discoveryPrompt = false
    @Published var discoveryFoundNothing = false
    @Published var useFrontCamera = Settings.useFrontCamera
    @Published var quality = Settings.quality

    nonisolated let pipeline = CameraPipeline()
    private let link = ServerLink()
    private let uploader = Uploader()

    init() {
        UIDevice.current.isBatteryMonitoringEnabled = true
        UIApplication.shared.isIdleTimerDisabled = true

        link.nowNs = { [pipeline] in pipeline.nowNs() }
        link.onNeedKeyframe = { [pipeline] in pipeline.requestKeyframe() }
        link.statusProvider = { [weak self] in self?.statusFields() ?? [:] }
        link.onState = { [weak self] state in
            Task { @MainActor in
                self?.linkState = state
                if case .connected = state { self?.uploader.kick() }
            }
        }
        link.onCommand = { [weak self] message in
            Task { @MainActor in self?.handle(message) }
        }
        pipeline.onEncodedFrame = { [link] pts, keyframe, data in
            link.sendVideo(ptsNs: pts, keyframe: keyframe, annexB: data)
        }
        pipeline.onPreviewJPEG = { [link] jpeg in link.sendPreview(jpeg) }
        uploader.onProgress = { [weak self] text in Task { @MainActor in self?.uploadStatus = text } }
        uploader.onChange = { [weak self] in Task { @MainActor in self?.refreshPending() } }
        refreshPending()
    }

    func startCamera() {
        Task {
            let video = await AVCaptureDevice.requestAccess(for: .video)
            _ = await AVCaptureDevice.requestAccess(for: .audio)
            guard video else {
                cameraError = "Camera access is off — turn it on in Settings › Takeloom Camera."
                return
            }
            pipeline.start { error in
                self.cameraError = error
                if self.serverHost.isEmpty {
                    // First launch: look for the server instead of asking for its address.
                    self.scanForServer(prompt: true)
                } else {
                    self.link.connect(host: self.serverHost)
                }
                self.uploader.kick()
            }
        }
    }

    /// Scan the network for takeloom servers. With `prompt`, ask (an alert)
    /// whether to connect to what's found; otherwise just list it (Settings).
    func scanForServer(prompt: Bool) {
        guard !scanning else { return }
        scanning = true
        discovered = []
        Task {
            let found = await ServerDiscovery.scan()
            scanning = false
            discovered = found
            if prompt {
                discoveryPrompt = !found.isEmpty
                discoveryFoundNothing = found.isEmpty
            }
        }
    }

    func connect(to host: String) {
        let host = host.trimmingCharacters(in: .whitespacesAndNewlines)
        serverHost = host
        Settings.serverHost = host
        if host.isEmpty {
            link.disconnect()
        } else {
            link.connect(host: host)
        }
    }

    func applyCameraSettings(front: Bool, quality: VideoQuality) {
        useFrontCamera = front
        self.quality = quality
        Settings.useFrontCamera = front
        Settings.quality = quality
        pipeline.reconfigure(position: front ? .front : .back, quality: quality) { [weak self] error in
            self?.cameraError = error
            self?.link.sendStatus()
        }
    }

    func deletePending(_ recording: PendingRecording) {
        RecordingStore.remove(recording.sessionID)
        refreshPending()
    }

    func retryPending(_ recording: PendingRecording) {
        var updated = recording
        updated.rejected = nil
        RecordingStore.save(updated)
        refreshPending()
        uploader.kick()
    }

    private func refreshPending() {
        pending = RecordingStore.all()
        link.sendStatus()
    }

    // MARK: - Server commands

    private func handle(_ message: [String: Any]) {
        guard let kind = message["kind"] as? String, let sessionID = message["session_id"] as? String else { return }
        switch kind {
        case "start_recording":
            startRecording(sessionID)
        case "stop_recording":
            stopRecording(sessionID)
        default:
            break
        }
    }

    private func startRecording(_ sessionID: String) {
        guard recordingSession == nil else {
            if recordingSession != sessionID {
                link.sendJSON(["kind": "recording_failed", "session_id": sessionID,
                               "error": "Already recording session \(recordingSession ?? "")."])
            }
            return
        }
        recordingSession = sessionID
        recordingSince = Date()
        pipeline.startRecording(
            sessionID: sessionID, url: RecordingStore.videoURL(for: sessionID),
            onStarted: { [link] pts in
                link.sendJSON(["kind": "recording_started", "session_id": sessionID, "video_start_pts_ns": pts])
            },
            onError: { [link] error in
                link.sendJSON(["kind": "recording_failed", "session_id": sessionID, "error": error])
            }
        )
        link.sendStatus()
    }

    private func stopRecording(_ sessionID: String) {
        guard recordingSession == sessionID else { return }
        recordingSession = nil
        recordingSince = nil
        let host = serverHost
        let clock = link.clockOffset
        let fps = pipeline.fps
        pipeline.stopRecording(sessionID: sessionID) { [weak self, link] result in
            switch result {
            case .success(let recording):
                let size = (try? FileManager.default.attributesOfItem(atPath: recording.url.path)[.size] as? NSNumber)?
                    .int64Value ?? 0
                RecordingStore.save(PendingRecording(
                    sessionID: sessionID, fileName: recording.url.lastPathComponent, host: host,
                    videoStartPtsNs: recording.videoStartPtsNs, clockOffsetNs: clock?.offset, clockRttNs: clock?.rtt,
                    width: recording.width, height: recording.height, fps: fps, duration: recording.duration
                ))
                link.sendJSON(["kind": "recording_stopped", "session_id": sessionID, "file_size": size,
                               "video_start_pts_ns": recording.videoStartPtsNs, "duration_s": recording.duration])
            case .failure(let error):
                link.sendJSON(["kind": "recording_failed", "session_id": sessionID,
                               "error": error.localizedDescription])
            }
            Task { @MainActor in
                self?.refreshPending()
                self?.uploader.kick()
            }
        }
        link.sendStatus()
    }

    nonisolated private func statusFields() -> [String: Any] {
        let free = (try? URL(fileURLWithPath: NSHomeDirectory())
            .resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey])
            .volumeAvailableCapacityForImportantUsage) ?? nil
        let pipeline = self.pipeline
        let device = DispatchQueue.main.sync { () -> (Float, UIDevice.BatteryState) in
            (UIDevice.current.batteryLevel, UIDevice.current.batteryState)
        }
        var fields: [String: Any] = [
            "width": pipeline.frameWidth, "height": pipeline.frameHeight, "fps": pipeline.fps,
            "camera": pipeline.position == .front ? "front" : "back",
            "charging": device.1 == .charging || device.1 == .full,
            "free_bytes": free ?? 0,
            "recording_session": pipeline.currentRecordingSession ?? NSNull(),
            "pending_uploads": RecordingStore.all().filter { $0.rejected == nil }.count,
        ]
        fields["battery"] = device.0 >= 0 ? device.0 : NSNull()
        return fields
    }
}
