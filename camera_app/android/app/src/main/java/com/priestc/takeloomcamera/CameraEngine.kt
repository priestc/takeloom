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
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.HandlerThread
import android.os.SystemClock
import android.util.Range
import android.util.Size
import android.view.Surface
import java.io.File
import java.nio.ByteBuffer

/**
 * One optical zoom step: [zoom] its field of view against the main
 * camera's (0.6 = ultra-wide, 3 = a 3x telephoto), reached by opening
 * camera [id] and setting its zoom ratio to [ratio]. On phones whose main
 * camera is a "logical" camera over several lenses (Pixels, many others)
 * every step is the main camera at a different ratio, and the camera
 * switches lens itself; on others (Samsungs) some lenses are cameras of
 * their own, at ratio 1.
 */
data class Lens(val zoom: Double, val id: String, val ratio: Double = 1.0)

data class FinishedRecording(
    val sessionId: String,
    val file: File,
    val videoStartPtsNs: Long,
    val duration: Double,
    val width: Int,
    val height: Int,
)

/**
 * Camera2 capture into two hardware H.264 encodes: the recording's (full
 * quality, into [Mp4Recorder] during a session, which adds the mic) and the
 * live feed's ([onEncodedFrame], up to 720p, at the bitrate the server's
 * Wi-Fi budget gives this phone — [setLiveQuality]). The camera itself only
 * feeds the on-screen preview and a [FrameRotator], which draws every frame
 * into both encoders, turned 180° when the phone is mounted the other way
 * up ([setUpsideDown]). The live encoder's size is fixed while the camera
 * runs. If the live encoder can't start, the recording's encode is shared by
 * both ([liveAdaptive] false: the server then counts this phone's whole
 * recording bitrate).
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
    private var encoder: MediaCodec? = null  // the recording's
    private var encoderSurface: Surface? = null
    private var liveEncoder: MediaCodec? = null
    private var liveSurface: Surface? = null
    private var previewSurface: Surface? = null
    private var rotator: FrameRotator? = null
    /** What the phone's orientation asks for; applied whenever not recording. */
    @Volatile private var wantUpsideDown = false
    /** What the encoders are actually getting (frozen during a recording). */
    @Volatile var upsideDown = false
        private set
    private val drainThreads = mutableListOf<Thread>()
    @Volatile private var draining = false
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
    @Volatile var liveAdaptive = false
        private set
    var liveWidth = 1280
        private set
    var liveHeight = 720
        private set
    /** Until the server says otherwise: modest enough for several phones. */
    @Volatile var liveBitrate = 3_000_000
        private set
    /** Zoom/pan from the server, and what this camera can do (status). */
    @Volatile var framing = Framing()
        private set
    @Volatile var zoomCaps: ZoomCaps? = null
        private set
    /** Every camera usable as a zoom step on this side, widest first. Only
     *  the ones Android lets apps open: on many phones (Samsung's included)
     *  the telephoto is reserved for the maker's own camera app. */
    @Volatile var lenses: List<Lens> = emptyList()
        private set
    private var openId: String? = null
    private var request: CaptureRequest.Builder? = null
    private var framingPlan: FramingPlan? = null
    /** What [FrameRotator] cuts out, in the picture as the server sees it
     *  (for the preview JPEGs, which come from the uncropped TextureView). */
    @Volatile var pictureCrop = GlCrop()
        private set

    fun nowNs(): Long = if (useBootTime) SystemClock.elapsedRealtimeNanos() else System.nanoTime()

    private fun cameraId(front: Boolean): String? {
        val want = if (front) CameraMetadata.LENS_FACING_FRONT else CameraMetadata.LENS_FACING_BACK
        return manager.cameraIdList.firstOrNull {
            manager.getCameraCharacteristics(it).get(CameraCharacteristics.LENS_FACING) == want
        } ?: manager.cameraIdList.firstOrNull()
    }

    /**
     * The landscape in which the sensor's frames come out upright as they
     * are. Held the other way round (Surface.ROTATION_180 from this one), the
     * frames are turned 180° by the [FrameRotator] instead — see
     * [setUpsideDown].
     */
    fun uprightOrientation(front: Boolean): Int {
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
        val size = chooseSize(front, quality)
        width = size.width
        height = size.height
        lenses = findLenses(id, front)

        try {
            val (codec, surface) = startEncoder(width, height, quality.bitrate, live = false)
            encoder = codec
            encoderSurface = surface
        } catch (e: Exception) {
            return@post onError("Couldn't start the video encoder: ${e.message}")
        }
        val liveSize = chooseLiveSize(id)
        liveWidth = liveSize?.width ?: width
        liveHeight = liveSize?.height ?: height
        liveAdaptive = false
        if (liveSize != null) {
            runCatching { startEncoder(liveSize.width, liveSize.height, liveBitrate, live = true) }.onSuccess {
                liveEncoder = it.first
                liveSurface = it.second
                liveAdaptive = true
            }
        }
        if (!liveAdaptive) {
            liveWidth = width
            liveHeight = height
        }
        val outputs = listOfNotNull(
            Triple(encoderSurface!!, width, height),
            liveSurface?.let { Triple(it, liveWidth, liveHeight) },
        )
        try {
            rotator = FrameRotator(width, height, outputs).also { it.rotate180 = upsideDown }
        } catch (e: Exception) {
            return@post onError(e.message ?: "Couldn't set up the frame rotator.")
        }
        texture.setDefaultBufferSize(width, height)
        val preview = Surface(texture)
        previewSurface = preview

        openCamera(lensFor(framing.zoom)?.id ?: id)
    }

    /** On the camera thread. The encoders and [FrameRotator] stay as they
     *  are, so switching lenses mid-recording only pauses the picture. */
    @SuppressLint("MissingPermission")
    private fun openCamera(id: String) {
        val preview = previewSurface ?: return
        val chars = manager.getCameraCharacteristics(id)
        zoomCaps = ZoomCaps(chars)
        useBootTime = chars.get(CameraCharacteristics.SENSOR_INFO_TIMESTAMP_SOURCE) ==
            CameraMetadata.SENSOR_INFO_TIMESTAMP_SOURCE_REALTIME
        openId = id
        manager.openCamera(id, object : CameraDevice.StateCallback() {
            override fun onOpened(camera: CameraDevice) {
                if (openId != id) return camera.close()  // switched again meanwhile
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

    /** On the camera thread: close the open camera, open [id] in its place. */
    private fun switchCamera(id: String) {
        runCatching { session?.close() }
        session = null
        request = null
        runCatching { device?.close() }
        device = null
        framingPlan = null
        openCamera(id)
    }

    /**
     * Every optical step on this side: the main camera at 1x; the steps its
     * own zoom ratio reaches with a different lens (an ultra-wide below 1x,
     * the physical lenses of a logical camera); then any other camera apps
     * may open, by field of view (focal length over sensor width), if it
     * can capture at the recording's size and isn't a step already found.
     */
    private fun findLenses(mainId: String, front: Boolean): List<Lens> {
        val found = mutableListOf(Lens(1.0, mainId))
        if (front) return found
        val main = manager.getCameraCharacteristics(mainId)
        fun add(lens: Lens) {
            if (found.none { Math.abs(Math.log(it.zoom / lens.zoom)) < 0.08 }) found += lens
        }
        fun round(zoom: Double) = Math.round(zoom * 10) / 10.0

        // The main camera's own zoom ratio range (Android 11+).
        val range = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R)
            main.get(CameraCharacteristics.CONTROL_ZOOM_RATIO_RANGE) else null
        val rMin = range?.lower?.toDouble() ?: 1.0
        val rMax = range?.upper?.toDouble() ?: 1.0
        if (rMin < 0.95) add(Lens(round(rMin), mainId, rMin))
        // The 1x field of view everything else is measured against. With an
        // ultra-wide in the ratio range, the widest physical lens sits at
        // rMin; otherwise the main camera's own optics are 1x.
        val physical = main.physicalCameraIds.mapNotNull { id ->
            runCatching { fovOf(manager.getCameraCharacteristics(id)) }.getOrNull()
        }
        val refFov = if (rMin < 0.95 && physical.isNotEmpty()) physical.max() * rMin else fovOf(main)
            ?: return found.sortedBy { it.zoom }
        // A logical camera's other lenses, reached by ratio.
        if (range != null) {
            for (fov in physical) {
                val zoom = refFov / fov
                if (zoom > 1.05 && zoom <= rMax) add(Lens(round(zoom), mainId, zoom))
            }
        }
        // Separate cameras (a Samsung's ultra-wide, for one).
        val facing = main.get(CameraCharacteristics.LENS_FACING)
        for (id in manager.cameraIdList) {
            if (id == mainId) continue
            val chars = runCatching { manager.getCameraCharacteristics(id) }.getOrNull() ?: continue
            if (chars.get(CameraCharacteristics.LENS_FACING) != facing) continue
            val fov = fovOf(chars) ?: continue
            val map = chars.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP) ?: continue
            if (map.getOutputSizes(SurfaceTexture::class.java)?.any { it.width == width && it.height == height } != true) continue
            add(Lens(round(refFov / fov), id))
        }
        return found.sortedBy { it.zoom }
    }

    /** Sensor width over focal length: bigger is wider. */
    private fun fovOf(chars: CameraCharacteristics): Double? {
        val focal = chars.get(CameraCharacteristics.LENS_INFO_AVAILABLE_FOCAL_LENGTHS)?.minOrNull() ?: return null
        val sensor = chars.get(CameraCharacteristics.SENSOR_INFO_PHYSICAL_SIZE) ?: return null
        if (focal <= 0f) return null
        return sensor.width.toDouble() / focal
    }

    /** The lens nearest [zoom] (by ratio). */
    private fun lensFor(zoom: Double): Lens? =
        lenses.minByOrNull { Math.abs(Math.log(maxOf(zoom, 1e-6) / it.zoom)) }

    /** The status's "zoom_caps": optical steps only. */
    fun zoomCapsJson(): org.json.JSONObject? {
        val l = lenses.ifEmpty { return null }
        return jsonOf("min" to l.first().zoom, "max" to l.last().zoom,
            "lenses" to org.json.JSONArray(l.map { it.zoom }), "pan" to false)
    }

    /** The live feed's size: the largest of the camera's no wider than 1280 in the recording's aspect. */
    private fun chooseLiveSize(id: String): Size? {
        val map = manager.getCameraCharacteristics(id).get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP)
        return map?.getOutputSizes(MediaCodec::class.java)?.filter {
            it.width <= 1280 && it.width.toLong() * height == it.height.toLong() * width
        }?.maxByOrNull { it.width }
    }

    @Suppress("DEPRECATION")
    private fun createSession(camera: CameraDevice, preview: Surface) {
        val encoderInput = rotator?.inputSurface ?: return
        val targets = listOf(preview, encoderInput)
        camera.createCaptureSession(targets, object : CameraCaptureSession.StateCallback() {
            override fun onConfigured(s: CameraCaptureSession) {
                session = s
                val request = camera.createCaptureRequest(CameraDevice.TEMPLATE_RECORD).apply {
                    targets.forEach { addTarget(it) }
                    set(CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE, Range(fps, fps))
                    set(CaptureRequest.CONTROL_MODE, CameraMetadata.CONTROL_MODE_AUTO)
                }
                this@CameraEngine.request = request
                framingPlan = null  // a new request: everything gets set
                applyFraming()
            }

            override fun onConfigureFailed(s: CameraCaptureSession) {
                onError("Couldn't configure the camera at ${width}x$height.")
            }
        }, handler)
    }

    private fun startEncoder(width: Int, height: Int, bitrate: Int, live: Boolean): Pair<MediaCodec, Surface> {
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
        val cbr = MediaCodecInfo.EncoderCapabilities.BITRATE_MODE_CBR
        if (live && codec.codecInfo.getCapabilitiesForType(MediaFormat.MIMETYPE_VIDEO_AVC)
                .encoderCapabilities.isBitrateModeSupported(cbr)) {
            // Constant bitrate for the live feed: no bursts to flood the Wi-Fi.
            format.setInteger(MediaFormat.KEY_BITRATE_MODE, cbr)
        }
        try {
            codec.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
        } catch (e: Exception) {
            // Some encoders reject an explicit High profile; their default is fine.
            format.removeKey(MediaFormat.KEY_PROFILE)
            format.removeKey(MediaFormat.KEY_LEVEL)
            codec.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
        }
        val surface = codec.createInputSurface()
        codec.start()
        draining = true
        drainThreads += Thread({ drain(codec, live) }, if (live) "live-encoder" else "video-encoder").apply { start() }
        return codec to surface
    }

    private fun stopLiveEncoder() {
        liveAdaptive = false
        val codec = liveEncoder ?: return
        liveEncoder = null
        runCatching { codec.stop() }
        runCatching { codec.release() }
        liveSurface?.release()
        liveSurface = null
    }

    /** One encoder's output: the live encoder's to the network; the
     *  recording's to the file (and the network too, without a live one). */
    private fun drain(codec: MediaCodec, live: Boolean) {
        val info = MediaCodec.BufferInfo()
        var codecConfig: ByteArray? = null  // SPS+PPS, Annex-B
        while (draining) {
            val index = try { codec.dequeueOutputBuffer(info, 10_000) } catch (_: Exception) { break }
            when {
                index == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED -> {
                    val format = codec.outputFormat
                    if (!live) videoFormat = format
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
                        if (live || !liveAdaptive) {
                            onEncodedFrame(ptsNs, keyframe, if (keyframe && config != null) config + data else data)
                        }
                        if (!live) recorder?.writeVideo(buffer, info, keyframe)
                    }
                    codec.releaseOutputBuffer(index, false)
                }
            }
        }
    }

    /** The live feed's (the server asks, e.g. a decoder joining). */
    fun requestKeyframe() = requestSyncFrame(liveEncoder ?: encoder)

    private fun requestRecordingKeyframe() = requestSyncFrame(encoder)

    private fun requestSyncFrame(codec: MediaCodec?) {
        runCatching {
            codec?.setParameters(Bundle().apply { putInt(MediaCodec.PARAMETER_KEY_REQUEST_SYNC_FRAME, 0) })
        }
    }

    /** Zoom and pan from the server (framing message). */
    fun setFraming(framing: Framing) = handler.post {
        this.framing = framing
        applyFraming()
    }

    /** On the camera thread: the right lens, then its zoom ratio and crop
     *  region, then the request again. */
    private fun applyFraming() {
        val lens = lensFor(framing.zoom)
        if (lens != null && lens.id != openId) return switchCamera(lens.id)  // applied once it's open
        val s = session ?: return
        val r = request ?: return
        val caps = zoomCaps ?: return
        // The server's x/y are in the picture as it sees it; with the
        // frames turned 180°, that's the opposite side of the sensor.
        // The lens's own ratio on its camera: optical, no crop.
        val zoom = (lens?.ratio ?: 1.0).coerceIn(caps.min, caps.max)
        val f0 = framing.copy(zoom = zoom)
        val f = if (upsideDown) f0.copy(x = 1 - f0.x, y = 1 - f0.y) else f0
        val plan = caps.plan(f, framingPlan)
        val cameraChanged = plan.ratio != framingPlan?.ratio || plan.crop != framingPlan?.crop
        framingPlan = plan
        rotator?.crop = plan.glCrop
        val g = plan.glCrop
        pictureCrop = if (upsideDown) g.copy(x = 1 - g.x, y = 1 - g.y) else g
        if (!cameraChanged) return  // a pan inside the camera's view: the crop alone
        if (plan.ratio != null && Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            r.set(CaptureRequest.CONTROL_ZOOM_RATIO, plan.ratio)
        }
        r.set(CaptureRequest.SCALER_CROP_REGION, plan.crop)
        runCatching { s.setRepeatingRequest(r.build(), null, handler) }
    }

    /** The server's share of the Wi-Fi for this phone (live_quality). Only
     *  the bitrate: the live size is fixed while the camera runs. */
    fun setLiveQuality(bitrate: Int) {
        liveBitrate = bitrate
        runCatching {
            liveEncoder?.setParameters(Bundle().apply { putInt(MediaCodec.PARAMETER_KEY_VIDEO_BITRATE, bitrate) })
        }
    }

    /**
     * The phone is held the other way up from [uprightOrientation] (camera
     * bump at the bottom): turn the encoded frames 180°. Never mid-recording
     * (the iOS app freezes rotation the same way): a recording's frames all
     * keep the orientation it started with, and a change made meanwhile is
     * applied once it stops.
     */
    fun setUpsideDown(value: Boolean) = handler.post {
        wantUpsideDown = value
        applyUpsideDown()
    }

    private fun applyUpsideDown() {
        if (recorder != null || upsideDown == wantUpsideDown) return
        upsideDown = wantUpsideDown
        rotator?.rotate180 = upsideDown
        applyFraming()
        requestKeyframe()  // the live decoder gets a clean picture straight away
    }

    fun stop() = handler.post { stopLocked() }

    private fun stopLocked() {
        runCatching { session?.close() }
        session = null
        request = null
        runCatching { device?.close() }
        device = null
        openId = null
        rotator?.release()  // before the encoder surfaces it draws into go
        rotator = null
        draining = false
        drainThreads.forEach { it.join(500) }
        drainThreads.clear()
        runCatching { encoder?.stop() }
        runCatching { encoder?.release() }
        encoder = null
        encoderSurface?.release()
        encoderSurface = null
        stopLiveEncoder()
        previewSurface?.release()
        previewSurface = null
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
            recorder = Mp4Recorder(sessionId, file, format, useBootTime, onStarted) { requestRecordingKeyframe() }
        } catch (e: Exception) {
            onFailed("Couldn't start recording: ${e.message}")
        }
    }

    fun stopRecording(sessionId: String, done: (Result<FinishedRecording>) -> Unit) {
        val r = recorder ?: return
        if (r.sessionId != sessionId) return
        recorder = null
        handler.post { applyUpsideDown() }  // any turn made during the recording
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
