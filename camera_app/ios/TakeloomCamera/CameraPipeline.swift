import AVFoundation
import CoreImage
import UIKit

struct FinishedRecording {
    let sessionID: String
    let url: URL
    let videoStartPtsNs: Int64
    let duration: Double
    let width: Int
    let height: Int
}

/// Camera + mic capture feeding two H.264 encoders: the live feed's
/// (`onEncodedFrame`, sized and paced by the server's Wi-Fi budget —
/// `setLiveQuality`) and, during a session only, the recording's (full
/// quality, into the file, which is uploaded afterwards).
///
/// Every timestamp here is on the capture session's synchronization clock —
/// the same clock `nowNs()` reads for the server clock pings, which is what
/// lets the server convert a frame's capture time into its own clock.
final class CameraPipeline: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate,
    AVCaptureAudioDataOutputSampleBufferDelegate {
    let session = AVCaptureSession()
    let previewLayer: AVCaptureVideoPreviewLayer

    /// (capture pts ns, keyframe, Annex-B access unit), on the encoder's thread.
    var onEncodedFrame: ((Int64, Bool, Data) -> Void)?
    /// A ~320px JPEG a few times a second, for the server's preview panel.
    var onPreviewJPEG: ((Data) -> Void)?

    private let sessionQueue = DispatchQueue(label: "camera.session")
    private let videoQueue = DispatchQueue(label: "camera.video", qos: .userInteractive)
    private let writerQueue = DispatchQueue(label: "camera.writer", qos: .userInitiated)
    private let previewQueue = DispatchQueue(label: "camera.preview", qos: .utility)
    private let videoOutput = AVCaptureVideoDataOutput()
    private let audioOutput = AVCaptureAudioDataOutput()
    private var videoInput: AVCaptureDeviceInput?
    private var rotationCoordinator: AVCaptureDevice.RotationCoordinator?
    private var rotationObservation: NSKeyValueObservation?
    private let recordEncoder: H264Encoder
    private let liveEncoder: H264Encoder
    /// videoQueue: whether frames go to recordEncoder (a recording's on).
    private var recordEncoding = false
    /// Zoom/pan from the server, and what this camera can do (status).
    private(set) var framing = Framing()
    private(set) var zoomCaps: ZoomCaps?
    /// sessionQueue: the plan in force (ZoomCaps.plan keeps pans crop-only).
    private var framingPlan: FramingPlan?
    /// The framing guide's on: the camera shows its widest lens, whatever
    /// `framing` says, until it's turned off (or a recording starts).
    private(set) var guide = false
    /// videoQueue: the crop that framing needs beyond the camera's own zoom.
    private var crop = CropRect()
    private let cropper = FrameCropper()
    /// What the live feed is running at, for the status message.
    private(set) var liveBitrate = CameraPipeline.defaultLiveBitrate
    private(set) var liveMaxWidth = CameraPipeline.defaultLiveMaxWidth
    /// Until the server says otherwise: modest enough for several phones.
    static let defaultLiveBitrate = 3_000_000
    static let defaultLiveMaxWidth = 1280
    private let ciContext = CIContext()
    private var lastPreviewTime = CFAbsoluteTimeGetCurrent()
    private var previewBusy = false

    // Writer-queue state.
    private var recorder: SessionRecorder?
    private var stoppingRecorder = false

    private(set) var fps = 30
    private(set) var quality: VideoQuality = Settings.quality
    private(set) var position: AVCaptureDevice.Position = Settings.useFrontCamera ? .front : .back
    private(set) var frameWidth = 0
    private(set) var frameHeight = 0

    /// Rotation is frozen while recording — a mid-session change would
    /// swap the frame dimensions under the encoder and the file.
    var isRecording: Bool {
        writerQueue.sync { recorder != nil }
    }

    override init() {
        previewLayer = AVCaptureVideoPreviewLayer(session: session)
        previewLayer.videoGravity = .resizeAspect
        recordEncoder = H264Encoder(bitrate: Settings.quality.bitrate, fps: 30)
        liveEncoder = H264Encoder(bitrate: CameraPipeline.defaultLiveBitrate, fps: 30,
                                  maxWidth: CameraPipeline.defaultLiveMaxWidth, limitBursts: true)
        super.init()
        recordEncoder.onOutput = { [weak self] sampleBuffer in self?.handleRecorded(sampleBuffer) }
        liveEncoder.onOutput = { [weak self] sampleBuffer in self?.handleLive(sampleBuffer) }
    }

    var clock: CMClock {
        session.synchronizationClock ?? CMClockGetHostTimeClock()
    }

    func nowNs() -> Int64 {
        CMClockGetTime(clock).nanoseconds
    }

    // MARK: - Setup

    func start(completion: @escaping (String?) -> Void) {
        sessionQueue.async {
            let error = self.configure()
            if error == nil { self.session.startRunning() }
            DispatchQueue.main.async { completion(error) }
        }
    }

    func reconfigure(position: AVCaptureDevice.Position, quality: VideoQuality, completion: @escaping (String?) -> Void) {
        sessionQueue.async {
            guard !self.isRecording else {
                DispatchQueue.main.async { completion("Can't change the camera while recording.") }
                return
            }
            self.position = position
            self.quality = quality
            self.videoQueue.sync {
                self.recordEncoder.bitrate = quality.bitrate
                self.recordEncoder.invalidate()
                self.liveEncoder.invalidate()
            }
            let error = self.configure()
            DispatchQueue.main.async { completion(error) }
        }
    }

    private func configure() -> String? {
        session.beginConfiguration()
        defer { session.commitConfiguration() }

        if let videoInput { session.removeInput(videoInput) }
        guard let device = Self.camera(position: position),
              let input = try? AVCaptureDeviceInput(device: device), session.canAddInput(input) else {
            return "No camera available."
        }
        session.addInput(input)
        videoInput = input

        let dims = quality.dimensions
        let preset: AVCaptureSession.Preset
        switch quality {
        case .hd720: preset = .hd1280x720
        case .hd1080: preset = .hd1920x1080
        case .uhd4k: preset = .hd4K3840x2160
        }
        session.sessionPreset = session.canSetSessionPreset(preset) ? preset : .high
        frameWidth = Int(dims.width)
        frameHeight = Int(dims.height)

        if (try? device.lockForConfiguration()) != nil {
            let duration = CMTime(value: 1, timescale: CMTimeScale(fps))
            if device.activeFormat.videoSupportedFrameRateRanges.contains(where: { $0.maxFrameRate >= Double(fps) }) {
                device.activeVideoMinFrameDuration = duration
                device.activeVideoMaxFrameDuration = duration
            }
            device.unlockForConfiguration()
        }

        if !session.outputs.contains(videoOutput) {
            videoOutput.videoSettings = [
                kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange,
            ]
            videoOutput.alwaysDiscardsLateVideoFrames = true
            videoOutput.setSampleBufferDelegate(self, queue: videoQueue)
            guard session.canAddOutput(videoOutput) else { return "Can't capture video." }
            session.addOutput(videoOutput)
        }

        if session.inputs.count < 2, let mic = AVCaptureDevice.default(for: .audio),
           let micInput = try? AVCaptureDeviceInput(device: mic), session.canAddInput(micInput) {
            // The mic track is archived as the room's "atmospheric" sound
            // (takeloom/phone_camera/sync.py's extract_mic), not just used to
            // line the video up: measurement mode turns off the voice
            // processing (automatic gain, noise reduction) iOS otherwise
            // applies. If the session won't take it, the default stays.
            session.automaticallyConfiguresApplicationAudioSession = false
            let audio = AVAudioSession.sharedInstance()
            if (try? audio.setCategory(.playAndRecord, mode: .measurement, options: [.mixWithOthers, .defaultToSpeaker])) == nil
                || (try? audio.setActive(true)) == nil {
                session.automaticallyConfiguresApplicationAudioSession = true
            }
            session.addInput(micInput)
        }
        if !session.outputs.contains(audioOutput), session.canAddOutput(audioOutput) {
            audioOutput.setSampleBufferDelegate(self, queue: writerQueue)
            session.addOutput(audioOutput)
        }

        let coordinator = AVCaptureDevice.RotationCoordinator(device: device, previewLayer: previewLayer)
        rotationCoordinator = coordinator
        rotationObservation = coordinator.observe(\.videoRotationAngleForHorizonLevelCapture, options: [.initial, .new]) {
            [weak self] _, _ in
            self?.sessionQueue.async { self?.applyRotation() }
        }
        applyRotation()
        zoomCaps = ZoomCaps(device: device)
        framingPlan = nil  // a new camera: its zoom gets set
        applyFraming()
        return nil
    }

    /// Keep the encoded frames upright for however the phone is held in
    /// landscape — snapped to 0/180, since a portrait angle would change the
    /// frame size (and the app's UI is landscape-only anyway).
    private func applyRotation() {
        guard let coordinator = rotationCoordinator, !isRecording else { return }
        let captureAngle: CGFloat = abs(coordinator.videoRotationAngleForHorizonLevelCapture - 180) < 90 ? 180 : 0
        if let connection = videoOutput.connection(with: .video), connection.isVideoRotationAngleSupported(captureAngle) {
            connection.videoRotationAngle = captureAngle
            connection.isVideoMirrored = false
        }
        let previewAngle = coordinator.videoRotationAngleForHorizonLevelPreview
        DispatchQueue.main.async {
            if let connection = self.previewLayer.connection, connection.isVideoRotationAngleSupported(previewAngle) {
                connection.videoRotationAngle = previewAngle
            }
        }
    }

    /// The live feed's (the server asks, e.g. a decoder joining).
    func requestKeyframe() {
        liveEncoder.requestKeyframe()
    }

    /// The back camera with the most lenses (iOS switches between them as
    /// the zoom changes: optical zoom), else the plain wide-angle one.
    private static func camera(position: AVCaptureDevice.Position) -> AVCaptureDevice? {
        let types: [AVCaptureDevice.DeviceType] = position == .back
            ? [.builtInTripleCamera, .builtInDualWideCamera, .builtInDualCamera, .builtInWideAngleCamera]
            : [.builtInWideAngleCamera]
        for type in types {
            if let device = AVCaptureDevice.default(type, for: .video, position: position) { return device }
        }
        return nil
    }

    /// The framing guide on or off (see `guide`).
    func setGuide(_ on: Bool) {
        sessionQueue.async {
            guard self.guide != on else { return }
            self.guide = on
            self.applyFraming()
        }
    }

    /// The mic's current level in dB (nil: no mic), for the screen's meter.
    var micLevelDb: Float? {
        audioOutput.connection(with: .audio)?.audioChannels.first?.averagePowerLevel
    }

    /// Zoom and pan from the server (framing message).
    func setFraming(_ framing: Framing) {
        sessionQueue.async {
            self.framing = framing
            self.applyFraming()
        }
    }

    /// sessionQueue: the camera's zoom, and the crop for the rest.
    private func applyFraming() {
        guard let device = videoInput?.device, let caps = zoomCaps else { return }
        let wanted = guide ? Framing(zoom: caps.min) : framing
        let plan = caps.plan(wanted, previous: framingPlan)
        // A pan inside the camera's view leaves its zoom alone: the crop only.
        if plan.factor != framingPlan?.factor, (try? device.lockForConfiguration()) != nil {
            device.videoZoomFactor = min(max(plan.factor, device.minAvailableVideoZoomFactor),
                                         device.maxAvailableVideoZoomFactor)
            device.unlockForConfiguration()
        }
        framingPlan = plan
        videoQueue.async { self.crop = plan.crop }
    }

    /// The server's share of the Wi-Fi for this phone (live_quality). The
    /// bitrate applies at once; a new size waits while recording, since the
    /// server keeps the live feed as the session's fallback video, and one
    /// file can't change size partway.
    func setLiveQuality(maxWidth: Int, bitrate: Int) {
        videoQueue.async {
            self.liveBitrate = bitrate
            self.liveMaxWidth = maxWidth
            self.liveEncoder.setBitrate(bitrate)
            if !self.recordEncoding { self.applyLiveWidth() }
        }
    }

    /// videoQueue.
    private func applyLiveWidth() {
        guard liveEncoder.maxWidth != liveMaxWidth else { return }
        liveEncoder.maxWidth = liveMaxWidth
        liveEncoder.invalidate()  // a new size: a new session (and keyframe) on the next frame
    }

    /// The live encoder's frames out so far and last error (status message).
    var liveEncoderStats: (frames: Int, error: Int32) {
        (liveEncoder.framesOut, liveEncoder.lastError)
    }

    /// The live feed's frame size (for the status message).
    var liveSize: (width: Int, height: Int) {
        liveEncoder.outputSize(sourceWidth: frameWidth, sourceHeight: frameHeight)
    }

    // MARK: - Capture callbacks

    func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer, from connection: AVCaptureConnection) {
        if output === audioOutput {
            recorder?.appendAudio(sampleBuffer)  // already on writerQueue
            return
        }
        guard let captured = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        // Panned: the frame's crop, at the frame's own size, for everything.
        let pixelBuffer = crop.isFull ? captured : (cropper.crop(captured, to: crop) ?? captured)
        let pts = CMSampleBufferGetPresentationTimeStamp(sampleBuffer)
        let duration = CMSampleBufferGetDuration(sampleBuffer)
        liveEncoder.encode(pixelBuffer, pts: pts, duration: duration)
        if recordEncoding {
            recordEncoder.encode(pixelBuffer, pts: pts, duration: duration)
        }
        maybeSendPreview(pixelBuffer)
    }

    private func maybeSendPreview(_ pixelBuffer: CVPixelBuffer) {
        let now = CFAbsoluteTimeGetCurrent()
        guard onPreviewJPEG != nil, !previewBusy, now - lastPreviewTime >= 0.2 else { return }
        lastPreviewTime = now
        previewBusy = true
        previewQueue.async {
            defer { self.videoQueue.async { self.previewBusy = false } }
            let image = CIImage(cvPixelBuffer: pixelBuffer)
            let scale = 320.0 / max(1, image.extent.width)
            let scaled = image.transformed(by: CGAffineTransform(scaleX: scale, y: scale))
            guard let colorSpace = CGColorSpace(name: CGColorSpace.sRGB),
                  let jpeg = self.ciContext.jpegRepresentation(
                    of: scaled, colorSpace: colorSpace,
                    options: [kCGImageDestinationLossyCompressionQuality as CIImageRepresentationOption: 0.6]
                  ) else { return }
            self.onPreviewJPEG?(jpeg)
        }
    }

    private func handleLive(_ sampleBuffer: CMSampleBuffer) {
        let keyframe = H264Encoder.isKeyframe(sampleBuffer)
        if let onEncodedFrame, let annexB = H264Encoder.annexB(sampleBuffer, keyframe: keyframe) {
            onEncodedFrame(CMSampleBufferGetPresentationTimeStamp(sampleBuffer).nanoseconds, keyframe, annexB)
        }
    }

    private func handleRecorded(_ sampleBuffer: CMSampleBuffer) {
        let keyframe = H264Encoder.isKeyframe(sampleBuffer)
        writerQueue.async {
            self.recorder?.appendVideo(sampleBuffer, keyframe: keyframe)
        }
    }

    // MARK: - Recording

    /// Starts at the next keyframe (requested right away); `onStarted` gets
    /// that first frame's capture time.
    func startRecording(sessionID: String, url: URL, onStarted: @escaping (Int64) -> Void,
                        onError: @escaping (String) -> Void) {
        writerQueue.async {
            guard self.recorder == nil, !self.stoppingRecorder else {
                onError("Already recording another session.")
                return
            }
            let recorder = SessionRecorder(sessionID: sessionID, url: url)
            recorder.onStarted = { pts in onStarted(pts.nanoseconds) }
            self.recorder = recorder
            self.videoQueue.async {
                self.recordEncoding = true
                self.recordEncoder.requestKeyframe()
            }
        }
    }

    func stopRecording(sessionID: String, completion: @escaping (Result<FinishedRecording, Error>) -> Void) {
        writerQueue.async {
            guard let recorder = self.recorder, recorder.sessionID == sessionID else { return }
            self.recorder = nil
            self.stoppingRecorder = true
            self.videoQueue.async {
                // Idle between sessions: only the live feed costs battery.
                self.recordEncoding = false
                self.recordEncoder.invalidate()
                self.applyLiveWidth()  // any size the server asked for meanwhile
            }
            let width = self.frameWidth, height = self.frameHeight
            recorder.finish { error in
                self.writerQueue.async { self.stoppingRecorder = false }
                if let error {
                    completion(.failure(error))
                } else {
                    completion(.success(FinishedRecording(
                        sessionID: sessionID, url: recorder.url,
                        videoStartPtsNs: recorder.videoStartPts?.nanoseconds ?? 0,
                        duration: recorder.duration, width: width, height: height
                    )))
                }
            }
        }
    }

    var currentRecordingSession: String? {
        writerQueue.sync { recorder?.sessionID }
    }
}
