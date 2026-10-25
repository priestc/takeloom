import AVFoundation
import Foundation

/// Writes one session's full-quality file: the encoder's H.264 samples as-is
/// (passthrough, no second encode) plus the mic as AAC. The mic track is what
/// the server cross-correlates against the studio audio to place the video
/// exactly (takeloom/phone_camera/sync.py), so both tracks keep their capture
/// timestamps on the one capture clock.
///
/// Only ever used on CameraPipeline's writer queue.
final class SessionRecorder {
    let sessionID: String
    let url: URL
    private(set) var videoStartPts: CMTime?
    private(set) var lastVideoPts: CMTime?
    private(set) var droppedFrames = 0
    /// Called (on the writer queue) once the first keyframe is in.
    var onStarted: ((CMTime) -> Void)?

    private var writer: AVAssetWriter?
    private var videoInput: AVAssetWriterInput?
    private var audioInput: AVAssetWriterInput?
    private var failed: Error?

    init(sessionID: String, url: URL) {
        self.sessionID = sessionID
        self.url = url
    }

    func appendVideo(_ sampleBuffer: CMSampleBuffer, keyframe: Bool) {
        guard failed == nil else { return }
        if writer == nil {
            // The file has to open on a keyframe; CameraPipeline asked the
            // encoder for one when recording was requested.
            guard keyframe, let format = CMSampleBufferGetFormatDescription(sampleBuffer) else { return }
            do {
                try start(format: format, at: CMSampleBufferGetPresentationTimeStamp(sampleBuffer))
            } catch {
                failed = error
                return
            }
        }
        guard let videoInput else { return }
        if videoInput.isReadyForMoreMediaData {
            videoInput.append(sampleBuffer)
            lastVideoPts = CMSampleBufferGetPresentationTimeStamp(sampleBuffer)
        } else {
            droppedFrames += 1
        }
    }

    func appendAudio(_ sampleBuffer: CMSampleBuffer) {
        guard failed == nil, let start = videoStartPts, let audioInput else { return }
        guard CMSampleBufferGetPresentationTimeStamp(sampleBuffer) >= start else { return }
        if audioInput.isReadyForMoreMediaData {
            audioInput.append(sampleBuffer)
        }
    }

    private func start(format: CMFormatDescription, at pts: CMTime) throws {
        try? FileManager.default.removeItem(at: url)
        let writer = try AVAssetWriter(outputURL: url, fileType: .mp4)
        // Fragmented: everything up to the last fragment stays playable if
        // the app is killed or the phone dies mid-session.
        writer.movieFragmentInterval = CMTime(seconds: 2, preferredTimescale: 600)

        let video = AVAssetWriterInput(mediaType: .video, outputSettings: nil, sourceFormatHint: format)
        video.expectsMediaDataInRealTime = true
        let audio = AVAssetWriterInput(mediaType: .audio, outputSettings: [
            AVFormatIDKey: kAudioFormatMPEG4AAC,
            AVSampleRateKey: 48_000,
            AVNumberOfChannelsKey: 1,
            AVEncoderBitRateKey: 128_000,
        ])
        audio.expectsMediaDataInRealTime = true
        guard writer.canAdd(video), writer.canAdd(audio) else {
            throw NSError(domain: "TakeloomCamera", code: 1, userInfo: [NSLocalizedDescriptionKey: "Can't set up the recording file."])
        }
        writer.add(video)
        writer.add(audio)
        guard writer.startWriting() else {
            throw writer.error ?? NSError(domain: "TakeloomCamera", code: 2)
        }
        writer.startSession(atSourceTime: pts)
        self.writer = writer
        videoInput = video
        audioInput = audio
        videoStartPts = pts
        onStarted?(pts)
    }

    func finish(completion: @escaping (Error?) -> Void) {
        if let failed {
            completion(failed)
            return
        }
        guard let writer else {
            completion(NSError(domain: "TakeloomCamera", code: 3,
                               userInfo: [NSLocalizedDescriptionKey: "No video was captured."]))
            return
        }
        videoInput?.markAsFinished()
        audioInput?.markAsFinished()
        writer.finishWriting {
            completion(writer.status == .completed ? nil : writer.error)
        }
    }

    var duration: Double {
        guard let start = videoStartPts, let end = lastVideoPts else { return 0 }
        return (end - start).seconds
    }
}
