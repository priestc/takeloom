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

/// Camera + mic capture feeding one H.264 encoder, whose output goes to the
/// live feed (`onEncodedFrame`) and, during a session, the recording file.
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
    private let encoder: H264Encoder
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
        encoder = H264Encoder(bitrate: Settings.quality.bitrate, fps: 30)
        super.init()
        encoder.onOutput = { [weak self] sampleBuffer in self?.handleEncoded(sampleBuffer) }
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
            self.encoder.bitrate = quality.bitrate
            self.encoder.invalidate()
            let error = self.configure()
            DispatchQueue.main.async { completion(error) }
        }
    }

    private func configure() -> String? {
        session.beginConfiguration()
        defer { session.commitConfiguration() }

        if let videoInput { session.removeInput(videoInput) }
        guard let device = AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: position),
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

    func requestKeyframe() {
        encoder.requestKeyframe()
    }

    // MARK: - Capture callbacks

    func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer, from connection: AVCaptureConnection) {
        if output === audioOutput {
            recorder?.appendAudio(sampleBuffer)  // already on writerQueue
            return
        }
        guard let pixelBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        encoder.encode(pixelBuffer, pts: CMSampleBufferGetPresentationTimeStamp(sampleBuffer),
                       duration: CMSampleBufferGetDuration(sampleBuffer))
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

    private func handleEncoded(_ sampleBuffer: CMSampleBuffer) {
        let keyframe = H264Encoder.isKeyframe(sampleBuffer)
        if let onEncodedFrame, let annexB = H264Encoder.annexB(sampleBuffer, keyframe: keyframe) {
            onEncodedFrame(CMSampleBufferGetPresentationTimeStamp(sampleBuffer).nanoseconds, keyframe, annexB)
        }
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
            self.encoder.requestKeyframe()
        }
    }

    func stopRecording(sessionID: String, completion: @escaping (Result<FinishedRecording, Error>) -> Void) {
        writerQueue.async {
            guard let recorder = self.recorder, recorder.sessionID == sessionID else { return }
            self.recorder = nil
            self.stoppingRecorder = true
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
