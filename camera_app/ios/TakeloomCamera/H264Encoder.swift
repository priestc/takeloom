import CoreMedia
import Foundation
import VideoToolbox

/// A hardware H.264 encode. CameraPipeline runs two: the recording's (full
/// size and bitrate, straight into SessionRecorder) and the live feed's
/// (Annex-B over the network, at whatever size and bitrate the server's
/// Wi-Fi budget gives this phone — `maxWidth`, `setBitrate`).
final class H264Encoder {
    var bitrate: Int
    var fps: Int
    /// Encode at most this wide, scaled by VideoToolbox (aspect kept); nil:
    /// the camera's own size. A change starts a new session (and keyframe).
    var maxWidth: Int?
    /// Cap bursts as well as the average — for the live feed, so a keyframe
    /// never floods the Wi-Fi.
    let limitBursts: Bool
    /// Called on VideoToolbox's thread with each encoded frame.
    var onOutput: ((CMSampleBuffer) -> Void)?

    private var session: VTCompressionSession?
    private var width = 0
    private var height = 0
    private let lock = NSLock()
    private var forceKeyframe = false

    init(bitrate: Int, fps: Int, maxWidth: Int? = nil, limitBursts: Bool = false) {
        self.bitrate = bitrate
        self.fps = fps
        self.maxWidth = maxWidth
        self.limitBursts = limitBursts
    }

    /// The size frames come out at, for a `sourceWidth` x `sourceHeight` camera.
    func outputSize(sourceWidth: Int, sourceHeight: Int) -> (width: Int, height: Int) {
        guard let maxWidth, sourceWidth > maxWidth, sourceWidth > 0 else { return (sourceWidth, sourceHeight) }
        let height = Int((Double(maxWidth) * Double(sourceHeight) / Double(sourceWidth)).rounded()) & ~1
        return (maxWidth & ~1, max(2, height))
    }

    /// Takes effect on the running session, no new keyframe needed.
    func setBitrate(_ bitrate: Int) {
        self.bitrate = bitrate
        if let session { applyBitrate(session) }
    }

    private func applyBitrate(_ session: VTCompressionSession) {
        VTSessionSetProperty(session, key: kVTCompressionPropertyKey_AverageBitRate, value: bitrate as CFNumber)
        if limitBursts {
            // At most 1.5x the average in any one second, in bytes.
            let limits = [Double(bitrate) * 1.5 / 8, 1.0] as CFArray
            VTSessionSetProperty(session, key: kVTCompressionPropertyKey_DataRateLimits, value: limits)
        }
    }

    deinit {
        invalidate()
    }

    func invalidate() {
        if let session {
            VTCompressionSessionCompleteFrames(session, untilPresentationTimeStamp: .invalid)
            VTCompressionSessionInvalidate(session)
        }
        session = nil
    }

    func requestKeyframe() {
        lock.lock()
        forceKeyframe = true
        lock.unlock()
    }

    func encode(_ pixelBuffer: CVPixelBuffer, pts: CMTime, duration: CMTime) {
        let (w, h) = outputSize(sourceWidth: CVPixelBufferGetWidth(pixelBuffer),
                                sourceHeight: CVPixelBufferGetHeight(pixelBuffer))
        if session == nil || w != width || h != height {
            invalidate()
            guard makeSession(width: w, height: h) else { return }
            requestKeyframe()
        }
        guard let session else { return }

        lock.lock()
        let key = forceKeyframe
        forceKeyframe = false
        lock.unlock()
        let props: CFDictionary? = key ? [kVTEncodeFrameOptionKey_ForceKeyFrame: kCFBooleanTrue] as CFDictionary : nil

        VTCompressionSessionEncodeFrame(
            session, imageBuffer: pixelBuffer, presentationTimeStamp: pts, duration: duration,
            frameProperties: props, infoFlagsOut: nil
        ) { [weak self] status, _, sampleBuffer in
            guard status == noErr, let sampleBuffer, CMSampleBufferDataIsReady(sampleBuffer) else { return }
            self?.onOutput?(sampleBuffer)
        }
    }

    private func makeSession(width: Int, height: Int) -> Bool {
        var created: VTCompressionSession?
        let status = VTCompressionSessionCreate(
            allocator: nil, width: Int32(width), height: Int32(height), codecType: kCMVideoCodecType_H264,
            encoderSpecification: nil, imageBufferAttributes: nil, compressedDataAllocator: nil,
            outputCallback: nil, refcon: nil, compressionSessionOut: &created
        )
        guard status == noErr, let created else { return false }
        let set = { (key: CFString, value: CFTypeRef) in
            _ = VTSessionSetProperty(created, key: key, value: value)
        }
        set(kVTCompressionPropertyKey_RealTime, kCFBooleanTrue)
        set(kVTCompressionPropertyKey_ProfileLevel, kVTProfileLevel_H264_High_AutoLevel)
        // No B-frames: decode order == presentation order, which keeps the
        // raw live stream (no timestamps on the wire into ffmpeg) simple.
        set(kVTCompressionPropertyKey_AllowFrameReordering, kCFBooleanFalse)
        applyBitrate(created)
        set(kVTCompressionPropertyKey_ExpectedFrameRate, fps as CFNumber)
        // A keyframe every 2 s: how fast the live feed recovers from a
        // dropped frame, and matches the server's own fragment cadence.
        set(kVTCompressionPropertyKey_MaxKeyFrameIntervalDuration, 2 as CFNumber)
        set(kVTCompressionPropertyKey_MaxKeyFrameInterval, (fps * 2) as CFNumber)
        VTCompressionSessionPrepareToEncodeFrames(created)
        session = created
        self.width = width
        self.height = height
        return true
    }

    // MARK: - Sample buffer helpers

    static func isKeyframe(_ sampleBuffer: CMSampleBuffer) -> Bool {
        guard let attachments = CMSampleBufferGetSampleAttachmentsArray(sampleBuffer, createIfNecessary: false)
                as? [[CFString: Any]], let first = attachments.first else { return true }
        return !((first[kCMSampleAttachmentKey_NotSync] as? Bool) ?? false)
    }

    /// The encoder's AVCC (length-prefixed) output as Annex-B (start-code
    /// delimited), SPS/PPS in front of every keyframe — what the server's
    /// ffmpeg `-f h264` input reads.
    static func annexB(_ sampleBuffer: CMSampleBuffer, keyframe: Bool) -> Data? {
        guard let block = CMSampleBufferGetDataBuffer(sampleBuffer) else { return nil }
        let startCode: [UInt8] = [0, 0, 0, 1]
        var out = Data()

        if keyframe, let format = CMSampleBufferGetFormatDescription(sampleBuffer) {
            var count = 0
            CMVideoFormatDescriptionGetH264ParameterSetAtIndex(
                format, parameterSetIndex: 0, parameterSetPointerOut: nil, parameterSetSizeOut: nil,
                parameterSetCountOut: &count, nalUnitHeaderLengthOut: nil
            )
            for i in 0..<count {
                var pointer: UnsafePointer<UInt8>?
                var size = 0
                CMVideoFormatDescriptionGetH264ParameterSetAtIndex(
                    format, parameterSetIndex: i, parameterSetPointerOut: &pointer, parameterSetSizeOut: &size,
                    parameterSetCountOut: nil, nalUnitHeaderLengthOut: nil
                )
                if let pointer {
                    out.append(contentsOf: startCode)
                    out.append(pointer, count: size)
                }
            }
        }

        let length = CMBlockBufferGetDataLength(block)
        var avcc = [UInt8](repeating: 0, count: length)
        guard CMBlockBufferCopyDataBytes(block, atOffset: 0, dataLength: length, destination: &avcc) == noErr else {
            return nil
        }
        var i = 0
        while i + 4 <= length {
            let naluLength = Int(avcc[i]) << 24 | Int(avcc[i + 1]) << 16 | Int(avcc[i + 2]) << 8 | Int(avcc[i + 3])
            i += 4
            guard naluLength > 0, i + naluLength <= length else { break }
            out.append(contentsOf: startCode)
            out.append(contentsOf: avcc[i..<(i + naluLength)])
            i += naluLength
        }
        return out
    }
}

extension CMTime {
    var nanoseconds: Int64 {
        CMTimeConvertScale(self, timescale: 1_000_000_000, method: .roundHalfAwayFromZero).value
    }
}
