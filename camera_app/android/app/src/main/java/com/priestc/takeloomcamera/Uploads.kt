package com.priestc.takeloomcamera

import org.json.JSONObject
import java.io.File
import java.io.RandomAccessFile
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/** A finished session recording waiting to go to the server. */
data class PendingRecording(
    val sessionId: String,
    val host: String,
    val videoStartPtsNs: Long,
    val clockOffsetNs: Long?,
    val clockRttNs: Long?,
    val width: Int,
    val height: Int,
    val fps: Int,
    val duration: Double,
    val rejected: String? = null,
) {
    fun toJson(): JSONObject = jsonOf(
        "sessionId" to sessionId, "host" to host, "videoStartPtsNs" to videoStartPtsNs,
        "clockOffsetNs" to clockOffsetNs, "clockRttNs" to clockRttNs, "width" to width, "height" to height,
        "fps" to fps, "duration" to duration, "rejected" to rejected,
    )

    companion object {
        fun fromJson(o: JSONObject) = PendingRecording(
            sessionId = o.getString("sessionId"), host = o.getString("host"),
            videoStartPtsNs = o.getLong("videoStartPtsNs"),
            clockOffsetNs = if (o.isNull("clockOffsetNs")) null else o.getLong("clockOffsetNs"),
            clockRttNs = if (o.isNull("clockRttNs")) null else o.getLong("clockRttNs"),
            width = o.getInt("width"), height = o.getInt("height"), fps = o.getInt("fps"),
            duration = o.getDouble("duration"),
            rejected = if (o.isNull("rejected")) null else o.getString("rejected"),
        )
    }
}

/**
 * Recordings live in <app files>/Recordings as <session>.mp4 + <session>.json
 * until the server confirms it has the whole file — the app can be killed,
 * lose Wi-Fi, or be relaunched days later and still finish the upload.
 */
class RecordingStore(root: File) {
    val directory = File(root, "Recordings").apply { mkdirs() }

    private fun safe(sessionId: String) =
        sessionId.map { if (it.isLetterOrDigit() || it in "-_.") it else '_' }.joinToString("")

    fun videoFile(sessionId: String) = File(directory, safe(sessionId) + ".mp4")

    @Synchronized fun save(r: PendingRecording) {
        val tmp = File(directory, safe(r.sessionId) + ".json.tmp")
        tmp.writeText(r.toJson().toString())
        tmp.renameTo(File(directory, safe(r.sessionId) + ".json"))
    }

    @Synchronized fun all(): List<PendingRecording> =
        (directory.listFiles { f -> f.name.endsWith(".json") } ?: emptyArray())
            .mapNotNull { runCatching { PendingRecording.fromJson(JSONObject(it.readText())) }.getOrNull() }
            .sortedBy { it.sessionId }

    @Synchronized fun remove(sessionId: String) {
        File(directory, safe(sessionId) + ".json").delete()
        videoFile(sessionId).delete()
    }
}

/**
 * Sends pending recordings one at a time over their own connection (so the
 * live feed and clock pings never queue behind gigabytes), resuming from
 * whatever the server already has after a drop.
 */
class Uploader(private val store: RecordingStore, private val settings: Settings) {
    var onProgress: (String?) -> Unit = {}
    var onChange: () -> Unit = {}
    private val executor = Executors.newSingleThreadScheduledExecutor { Thread(it, "uploader") }
    private var busy = false
    private val chunkSize = 512 * 1024

    fun kick() = executor.execute { next() }

    private fun next() {
        if (busy) return
        val recording = store.all().firstOrNull { it.rejected == null && it.host.isNotEmpty() }
        if (recording == null) {
            onProgress(null)
            return
        }
        busy = true
        upload(recording) { retryLater ->
            executor.execute {
                busy = false
                onChange()
                if (retryLater) executor.schedule({ next() }, 5, TimeUnit.SECONDS) else next()
            }
        }
    }

    /** done(true) = transient failure, try again later. */
    private fun upload(recording: PendingRecording, done: (Boolean) -> Unit) {
        val file = store.videoFile(recording.sessionId)
        if (!file.exists()) {
            store.remove(recording.sessionId)
            done(false)
            return
        }
        val size = file.length()
        val input = RandomAccessFile(file, "r")
        var finished = false
        lateinit var socket: FramedSocket
        val finish = { retry: Boolean ->
            val first = synchronized(this) { (!finished).also { finished = true } }
            if (first) {
                runCatching { input.close() }
                socket.close()
                done(retry)
            }
        }
        var sent = 0L
        val buffer = ByteArray(chunkSize)

        fun sendChunk() {
            // One chunk queued at a time: memory stays flat however big the
            // file is, and progress tracks what actually left.
            val n = runCatching { input.read(buffer) }.getOrDefault(-1)
            if (n <= 0) {
                socket.sendJson(jsonOf("kind" to "upload_end"))
                return
            }
            socket.send(Wire.frame(Wire.UPLOAD, buffer, 0, n)) {
                sent += n
                onProgress("Uploading ${recording.sessionId}: ${100 * sent / maxOf(size, 1)}%")
                sendChunk()
            }
        }

        socket = FramedSocket(
            recording.host,
            onFrame = { type, payload ->
                if (type == Wire.JSON) {
                    val msg = JSONObject(String(payload, Charsets.UTF_8))
                    when (msg.optString("kind")) {
                        "hello_ack" -> if (!msg.optBoolean("ok")) finish(true) else {
                            val metadata = jsonOf(
                                "video_start_pts_ns" to recording.videoStartPtsNs,
                                "width" to recording.width, "height" to recording.height, "fps" to recording.fps,
                                "duration_s" to recording.duration, "platform" to "android",
                            )
                            recording.clockOffsetNs?.let { metadata.put("clock_offset_ns", it) }
                            recording.clockRttNs?.let { metadata.put("clock_rtt_ns", it) }
                            socket.sendJson(jsonOf(
                                "kind" to "upload_begin", "session_id" to recording.sessionId,
                                "size" to size, "metadata" to metadata,
                            ))
                        }
                        "upload_resume" -> {
                            sent = msg.optLong("offset")
                            input.seek(sent)
                            sendChunk()
                        }
                        "upload_rejected" -> {
                            store.save(recording.copy(rejected = msg.optString("error", "Rejected by the server.")))
                            finish(false)
                        }
                        "upload_done" -> {
                            if (msg.optBoolean("ok")) {
                                store.remove(recording.sessionId)
                                finish(false)
                            } else {
                                finish(true)
                            }
                        }
                    }
                }
            },
            onClosed = { finish(true) },
        )
        Thread {
            socket.connect()
            socket.sendJson(jsonOf(
                "kind" to "hello", "role" to "upload", "token" to settings.token(recording.host),
                "client_name" to settings.deviceName, "device_id" to settings.deviceId,
                "platform" to "android", "app_version" to settings.appVersion,
                "device_model" to settings.deviceModel,
            ))
        }.start()
    }
}
