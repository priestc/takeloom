package com.priestc.takeloomcamera

import android.annotation.SuppressLint
import android.content.Context
import android.content.pm.ActivityInfo
import android.graphics.SurfaceTexture
import android.hardware.camera2.CameraCaptureSession
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraDevice
import android.hardware.camera2.CameraManager
import android.hardware.camera2.CameraMetadata
import android.hardware.camera2.CaptureRequest
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.AudioTimestamp
import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.media.MediaMuxer
import android.media.MediaRecorder
import android.os.Bundle
import android.os.Handler
import android.os.HandlerThread
import android.os.SystemClock
import android.util.Range
import android.util.Size
import android.view.Surface
import java.io.File
import java.nio.ByteBuffer

data class FinishedRecording(
    val sessionId: String,
    val file: File,
    val videoStartPtsNs: Long,
    val duration: Double,
    val width: Int,
    val height: Int,
)

/**
 * Camera2 capture into one hardware H.264 encode, whose output goes to the
 * live feed ([onEncodedFrame]) and, during a session, the recording file
 * ([Mp4Recorder], which adds the mic).
 *
 * Every timestamp here is on the camera's sensor clock — either boot time or
 * monotonic, depending on the device (SENSOR_INFO_TIMESTAMP_SOURCE). [nowNs]
 * reads that same clock for the server clock pings, and the mic's
 * timestamps are taken on it too, which is what lets the server convert a
 * frame's capture time into its own clock.
 */
class CameraEngine(context: Context) {
    var onEncodedFrame: (Long, Boolean, ByteArray) -> Unit = { _, _, _ -> }
    var onError: (String) -> Unit = {}

    private val manager = context.getSystemService(Context.CAMERA_SERVICE) as CameraManager
    private val thread = HandlerThread("camera").apply { start() }
    private val handler = Handler(thread.looper)
    private var device: CameraDevice? = null
    private var session: CameraCaptureSession? = null
    private var encoder: MediaCodec? = null
    private var encoderSurface: Surface? = null
    private var previewSurface: Surface? = null
    private var drainThread: Thread? = null
    @Volatile private var draining = false
    @Volatile private var codecConfig: ByteArray? = null  // SPS+PPS, Annex-B
    @Volatile private var videoFormat: MediaFormat? = null
    @Volatile private var recorder: Mp4Recorder? = null

    @Volatile var useBootTime = false
        private set
    val fps = 30
    var width = 1920
        private set
    var height = 1080
        private set
    var front = false
        private set

    fun nowNs(): Long = if (useBootTime) SystemClock.elapsedRealtimeNanos() else System.nanoTime()

    private fun cameraId(front: Boolean): String? {
        val want = if (front) CameraMetadata.LENS_FACING_FRONT else CameraMetadata.LENS_FACING_BACK
        return manager.cameraIdList.firstOrNull {
            manager.getCameraCharacteristics(it).get(CameraCharacteristics.LENS_FACING) == want
        } ?: manager.cameraIdList.firstOrNull()
    }

    /**
     * Which landscape the activity must lock to so the sensor's frames come
     * out upright with no rotation at all — the encoder surface gets them
     * unrotated, and the live H.264 has nowhere to carry a rotation flag.
     */
    fun requiredOrientation(front: Boolean): Int {
        val id = cameraId(front) ?: return ActivityInfo.SCREEN_ORIENTATION_LANDSCAPE
        val sensor = manager.getCameraCharacteristics(id).get(CameraCharacteristics.SENSOR_ORIENTATION) ?: 90
        val deviceDegrees = if (front) (360 - sensor) % 360 else sensor
        return if (deviceDegrees == 270) ActivityInfo.SCREEN_ORIENTATION_REVERSE_LANDSCAPE
        else ActivityInfo.SCREEN_ORIENTATION_LANDSCAPE
    }

    /** The capture size actually used for [quality] — the TextureView needs it before start(). */
    fun chooseSize(front: Boolean, quality: VideoQuality): Size {
        val id = cameraId(front) ?: return Size(quality.width, quality.height)
        val map = manager.getCameraCharacteristics(id).get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP)
        val sizes = map?.getOutputSizes(MediaCodec::class.java)?.toList().orEmpty()
        sizes.firstOrNull { it.width == quality.width && it.height == quality.height }?.let { return it }
        // Largest 16:9 no bigger than asked for, else whatever's closest.
        return sizes.filter { it.width * 9 == it.height * 16 && it.width <= quality.width }.maxByOrNull { it.width }
            ?: sizes.minByOrNull { Math.abs(it.width - quality.width) } ?: Size(quality.width, quality.height)
    }

    @SuppressLint("MissingPermission")
    fun start(texture: SurfaceTexture, front: Boolean, quality: VideoQuality) = handler.post {
        stopLocked()
        this.front = front
        val id = cameraId(front) ?: return@post onError("No camera available.")
        val chars = manager.getCameraCharacteristics(id)
        useBootTime = chars.get(CameraCharacteristics.SENSOR_INFO_TIMESTAMP_SOURCE) ==
            CameraMetadata.SENSOR_INFO_TIMESTAMP_SOURCE_REALTIME
        val size = chooseSize(front, quality)
        width = size.width
        height = size.height

        try {
            startEncoder(quality.bitrate)
        } catch (e: Exception) {
            return@post onError("Couldn't start the video encoder: ${e.message}")
        }
        texture.setDefaultBufferSize(width, height)
        val preview = Surface(texture)
        previewSurface = preview

        manager.openCamera(id, object : CameraDevice.StateCallback() {
            override fun onOpened(camera: CameraDevice) {
                device = camera
                createSession(camera, preview)
            }

            override fun onDisconnected(camera: CameraDevice) {
                camera.close()
                if (device === camera) device = null
            }

            override fun onError(camera: CameraDevice, error: Int) {
                camera.close()
                if (device === camera) device = null
                onError("Camera error $error.")
            }
        }, handler)
    }

    @Suppress("DEPRECATION")
    private fun createSession(camera: CameraDevice, preview: Surface) {
        val encoderInput = encoderSurface ?: return
        camera.createCaptureSession(listOf(preview, encoderInput), object : CameraCaptureSession.StateCallback() {
            override fun onConfigured(s: CameraCaptureSession) {
                session = s
                val request = camera.createCaptureRequest(CameraDevice.TEMPLATE_RECORD).apply {
                    addTarget(preview)
                    addTarget(encoderInput)
                    set(CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE, Range(fps, fps))
                    set(CaptureRequest.CONTROL_MODE, CameraMetadata.CONTROL_MODE_AUTO)
                }
                s.setRepeatingRequest(request.build(), null, handler)
            }

            override fun onConfigureFailed(s: CameraCaptureSession) {
                onError("Couldn't configure the camera at ${width}x$height.")
            }
        }, handler)
    }

    private fun startEncoder(bitrate: Int) {
        val format = MediaFormat.createVideoFormat(MediaFormat.MIMETYPE_VIDEO_AVC, width, height).apply {
            setInteger(MediaFormat.KEY_COLOR_FORMAT, MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface)
            setInteger(MediaFormat.KEY_BIT_RATE, bitrate)
            setInteger(MediaFormat.KEY_FRAME_RATE, fps)
            // A keyframe every 2 s: how fast the live feed recovers from a
            // dropped frame. No B-frames: decode order == presentation
            // order, which the timestamp-less live stream relies on.
            setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, 2)
            setInteger(MediaFormat.KEY_MAX_B_FRAMES, 0)
            setInteger(MediaFormat.KEY_PROFILE, MediaCodecInfo.CodecProfileLevel.AVCProfileHigh)
            setInteger(MediaFormat.KEY_LEVEL, MediaCodecInfo.CodecProfileLevel.AVCLevel42)
        }
        val codec = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_VIDEO_AVC)
        try {
            codec.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
        } catch (e: Exception) {
            // Some encoders reject an explicit High profile; their default is fine.
            format.removeKey(MediaFormat.KEY_PROFILE)
            format.removeKey(MediaFormat.KEY_LEVEL)
            codec.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
        }
        encoderSurface = codec.createInputSurface()
        codec.start()
        encoder = codec
        draining = true
        drainThread = Thread({ drain(codec) }, "video-encoder").apply { start() }
    }

    private fun drain(codec: MediaCodec) {
        val info = MediaCodec.BufferInfo()
        while (draining) {
            val index = try { codec.dequeueOutputBuffer(info, 10_000) } catch (_: Exception) { break }
            when {
                index == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED -> {
                    val format = codec.outputFormat
                    videoFormat = format
                    val sps = format.getByteBuffer("csd-0")
                    val pps = format.getByteBuffer("csd-1")
                    if (sps != null && pps != null) codecConfig = bytesOf(sps) + bytesOf(pps)
                }
                index >= 0 -> {
                    val buffer = codec.getOutputBuffer(index)
                    if (buffer != null && info.size > 0 && info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG == 0) {
                        buffer.position(info.offset)
                        buffer.limit(info.offset + info.size)
                        val keyframe = info.flags and MediaCodec.BUFFER_FLAG_KEY_FRAME != 0
                        val data = ByteArray(info.size)
                        buffer.duplicate().get(data)
                        val ptsNs = info.presentationTimeUs * 1000
                        // MediaCodec already emits Annex-B; SPS/PPS go in
                        // front of each keyframe for the live decoder.
                        val config = codecConfig
                        onEncodedFrame(ptsNs, keyframe, if (keyframe && config != null) config + data else data)
                        recorder?.writeVideo(buffer, info, keyframe)
                    }
                    codec.releaseOutputBuffer(index, false)
                }
            }
        }
    }

    fun requestKeyframe() {
        runCatching {
            encoder?.setParameters(Bundle().apply { putInt(MediaCodec.PARAMETER_KEY_REQUEST_SYNC_FRAME, 0) })
        }
    }

    fun stop() = handler.post { stopLocked() }

    private fun stopLocked() {
        runCatching { session?.close() }
        session = null
        runCatching { device?.close() }
        device = null
        draining = false
        drainThread?.join(500)
        drainThread = null
        runCatching { encoder?.stop() }
        runCatching { encoder?.release() }
        encoder = null
        encoderSurface?.release()
        encoderSurface = null
        previewSurface?.release()
        previewSurface = null
        codecConfig = null
        videoFormat = null
    }

    // --- recording ---

    val recordingSession: String? get() = recorder?.sessionId

    /** Starts at the next keyframe; [onStarted] gets that frame's capture time. */
    fun startRecording(sessionId: String, file: File, onStarted: (Long) -> Unit, onFailed: (String) -> Unit) {
        val format = videoFormat
        if (recorder != null) return onFailed("Already recording another session.")
        if (format == null) return onFailed("The camera isn't running.")
        try {
            recorder = Mp4Recorder(sessionId, file, format, useBootTime, onStarted) { requestKeyframe() }
        } catch (e: Exception) {
            onFailed("Couldn't start recording: ${e.message}")
        }
    }

    fun stopRecording(sessionId: String, done: (Result<FinishedRecording>) -> Unit) {
        val r = recorder ?: return
        if (r.sessionId != sessionId) return
        recorder = null
        Thread {
            val result = runCatching {
                r.finish()
                FinishedRecording(sessionId, r.file, r.videoStartNs ?: 0, r.duration, width, height)
            }
            done(result)
        }.start()
    }

    private fun bytesOf(buffer: ByteBuffer): ByteArray {
        val dup = buffer.duplicate()
        dup.position(0)
        return ByteArray(dup.remaining()).also { dup.get(it) }
    }
}

/**
 * One session's file: the encoder's H.264 samples as-is (no second encode)
 * plus the mic as AAC. The mic track is what the server cross-correlates
 * against the studio audio to place the video exactly (takeloom/
 * phone_camera/sync.py), so the mic's samples are timestamped on the same
 * clock as the camera's frames, from AudioRecord's own capture timestamps.
 */
class Mp4Recorder(
    val sessionId: String,
    val file: File,
    videoFormat: MediaFormat,
    private val bootTime: Boolean,
    private val onStarted: (Long) -> Unit,
    private val requestKeyframe: () -> Unit,
) {
    private val lock = Object()
    private val muxer = MediaMuxer(file.path, MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4)
    private val videoTrack = muxer.addTrack(videoFormat)
    private var audioTrack = -1
    private var muxing = false
    var videoStartNs: Long? = null
        private set
    private var lastVideoNs: Long? = null
    private val audio = AudioCapture(bootTime) { format -> onAudioFormat(format) }

    val duration: Double
        get() {
            val start = videoStartNs ?: return 0.0
            return ((lastVideoNs ?: start) - start) / 1e9
        }

    init {
        audio.onSample = { buffer, info -> writeAudio(buffer, info) }
        audio.start()
    }

    /** The muxer can only start once both tracks are known, and the AAC
     *  encoder's format only arrives with its first output. */
    private fun onAudioFormat(format: MediaFormat) {
        synchronized(lock) {
            audioTrack = muxer.addTrack(format)
            muxer.setOrientationHint(0)
            muxer.start()
            muxing = true
        }
        requestKeyframe()
    }

    fun writeVideo(buffer: ByteBuffer, info: MediaCodec.BufferInfo, keyframe: Boolean) {
        var started: Long? = null
        synchronized(lock) {
            if (!muxing) return
            if (videoStartNs == null) {
                if (!keyframe) return
                videoStartNs = info.presentationTimeUs * 1000
                started = videoStartNs
            }
            muxer.writeSampleData(videoTrack, buffer, info)
            lastVideoNs = info.presentationTimeUs * 1000
        }
        started?.let(onStarted)
    }

    private fun writeAudio(buffer: ByteBuffer, info: MediaCodec.BufferInfo) {
        synchronized(lock) {
            val start = videoStartNs ?: return
            if (!muxing || info.presentationTimeUs * 1000 < start) return
            muxer.writeSampleData(audioTrack, buffer, info)
        }
    }

    fun finish() {
        audio.stop()
        synchronized(lock) {
            try {
                if (muxing) muxer.stop()
            } finally {
                muxer.release()
                muxing = false
            }
        }
        if (videoStartNs == null) throw IllegalStateException("No video was captured.")
    }
}

/** Mic -> AAC, each buffer stamped with when its first sample was captured. */
class AudioCapture(private val bootTime: Boolean, private val onFormat: (MediaFormat) -> Unit) {
    var onSample: (ByteBuffer, MediaCodec.BufferInfo) -> Unit = { _, _ -> }
    private val sampleRate = 48_000
    @Volatile private var running = false
    private var thread: Thread? = null

    fun start() {
        running = true
        thread = Thread({ run() }, "audio-capture").apply { start() }
    }

    fun stop() {
        running = false
        thread?.join(2000)
    }

    @SuppressLint("MissingPermission")
    private fun run() {
        val minBuffer = AudioRecord.getMinBufferSize(sampleRate, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        val record = AudioRecord(
            MediaRecorder.AudioSource.CAMCORDER, sampleRate, AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT, maxOf(minBuffer, 8192) * 4,
        )
        val codec = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_AUDIO_AAC)
        codec.configure(MediaFormat.createAudioFormat(MediaFormat.MIMETYPE_AUDIO_AAC, sampleRate, 1).apply {
            setInteger(MediaFormat.KEY_AAC_PROFILE, MediaCodecInfo.CodecProfileLevel.AACObjectLC)
            setInteger(MediaFormat.KEY_BIT_RATE, 128_000)
            setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, 16384)
        }, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
        codec.start()
        record.startRecording()

        val timebase = if (bootTime) AudioTimestamp.TIMEBASE_BOOTTIME else AudioTimestamp.TIMEBASE_MONOTONIC
        val stamp = AudioTimestamp()
        val pcm = ByteArray(2048 * 2)
        var framesRead = 0L
        val info = MediaCodec.BufferInfo()
        val now = { if (bootTime) SystemClock.elapsedRealtimeNanos() else System.nanoTime() }
        try {
            while (running) {
                val n = record.read(pcm, 0, pcm.size)
                if (n <= 0) continue
                val frames = n / 2
                // Capture time of this chunk's first sample, from the
                // device's own (frame position, time) pairing when it has
                // one; otherwise "now, minus how long the chunk is".
                val ptsNs = if (record.getTimestamp(stamp, timebase) == AudioRecord.SUCCESS) {
                    stamp.nanoTime + (framesRead - stamp.framePosition) * 1_000_000_000L / sampleRate
                } else {
                    now() - frames * 1_000_000_000L / sampleRate
                }
                framesRead += frames
                val input = codec.dequeueInputBuffer(10_000)
                if (input >= 0) {
                    val buf = codec.getInputBuffer(input)!!
                    buf.clear()
                    buf.put(pcm, 0, minOf(n, buf.remaining()))
                    codec.queueInputBuffer(input, 0, minOf(n, buf.capacity()), ptsNs / 1000, 0)
                }
                drain(codec, info)
            }
        } finally {
            runCatching { record.stop() }
            record.release()
            runCatching { codec.stop() }
            codec.release()
        }
    }

    private fun drain(codec: MediaCodec, info: MediaCodec.BufferInfo) {
        while (true) {
            val index = codec.dequeueOutputBuffer(info, 0)
            when {
                index == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED -> onFormat(codec.outputFormat)
                index >= 0 -> {
                    val buffer = codec.getOutputBuffer(index)
                    if (buffer != null && info.size > 0 && info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG == 0) {
                        buffer.position(info.offset)
                        buffer.limit(info.offset + info.size)
                        onSample(buffer, info)
                    }
                    codec.releaseOutputBuffer(index, false)
                }
                else -> return
            }
        }
    }
}
