package com.priestc.takeloomcamera

import org.json.JSONObject
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledFuture
import java.util.concurrent.TimeUnit

sealed class LinkState {
    object Idle : LinkState()
    object Connecting : LinkState()
    object AwaitingApproval : LinkState()
    data class Connected(val hostname: String) : LinkState()
    data class Failed(val error: String) : LinkState()
}

/**
 * NTP-style offset between this phone's capture clock and the server's
 * clock, from the lowest-round-trip of recent pings (the one least
 * distorted by Wi-Fi queuing).
 */
class ClockSync {
    private val samples = ArrayDeque<Pair<Long, Long>>()  // (rtt, offset)

    fun add(t0: Long, serverTime: Long, t1: Long) {
        val rtt = t1 - t0
        if (rtt < 0) return
        samples.addLast(rtt to serverTime - (t0 + t1) / 2)
        while (samples.size > 40) samples.removeFirst()
    }

    val best: Pair<Long, Long>? get() = samples.minByOrNull { it.first }
    fun reset() = samples.clear()
}

/**
 * The persistent "camera" connection to the takeloom server: pairing,
 * clock sync, status reports, the live feed, and start/stop commands.
 * Reconnects on its own until [disconnect]. All state lives on [executor].
 */
class ServerLink(private val settings: Settings) {
    var onState: (LinkState) -> Unit = {}
    var onCommand: (JSONObject) -> Unit = {}
    var statusProvider: () -> JSONObject = { JSONObject() }
    var nowNs: () -> Long = { System.nanoTime() }
    var onNeedKeyframe: () -> Unit = {}

    private val executor = Executors.newSingleThreadScheduledExecutor { Thread(it, "server-link") }
    private var socket: FramedSocket? = null
    private var host = ""
    private var wanted = false
    @Volatile private var helloDone = false
    private val clock = ClockSync()
    private var pingId = 0
    private val pings = HashMap<Int, Long>()
    private var timer: ScheduledFuture<*>? = null
    private var ticks = 0
    private var reconnect: ScheduledFuture<*>? = null
    @Volatile private var droppingUntilKeyframe = false

    /** Live video gives way (dropped until the next keyframe) once this much
     *  is still queued for the network. The recording file is never affected. */
    private val maxVideoBacklog = 3L * 1024 * 1024

    @Volatile var clockOffset: Pair<Long, Long>? = null  // (offset, rtt)
        private set

    fun connect(host: String) = executor.execute {
        this.host = host
        wanted = true
        open()
    }

    fun disconnect() = executor.execute {
        wanted = false
        reconnect?.cancel(false)
        socket?.close()
        socket = null
        onState(LinkState.Idle)
    }

    // --- sending, any thread ---

    fun sendJson(obj: JSONObject) {
        if (helloDone) socket?.sendJson(obj)
    }

    fun sendVideo(ptsNs: Long, keyframe: Boolean, annexB: ByteArray) {
        val socket = socket ?: return
        if (!helloDone) return
        val queued = socket.bytesQueued.get()
        if (queued > maxVideoBacklog) droppingUntilKeyframe = true
        if (droppingUntilKeyframe) {
            if (!keyframe || queued > maxVideoBacklog / 2) {
                if (queued < maxVideoBacklog / 2) onNeedKeyframe()
                return
            }
            droppingUntilKeyframe = false
        }
        socket.send(Wire.video(ptsNs, keyframe, annexB))
    }

    fun sendPreview(jpeg: ByteArray) {
        val socket = socket ?: return
        if (helloDone && socket.bytesQueued.get() < maxVideoBacklog / 4) socket.send(Wire.frame(Wire.PREVIEW, jpeg))
    }

    fun sendStatus() = executor.execute {
        if (!helloDone) return@execute
        val status = statusProvider()
        status.put("kind", "status")
        socket?.sendJson(status)
    }

    // --- lifecycle, on executor ---

    private fun open() {
        socket?.close()
        helloDone = false
        clock.reset()
        clockOffset = null
        if (!wanted || host.isEmpty()) return
        onState(LinkState.Connecting)
        lateinit var s: FramedSocket
        s = FramedSocket(
            host,
            onFrame = { type, payload -> executor.execute { if (s === socket) handle(type, payload) } },
            onClosed = { error ->
                executor.execute {
                    if (s !== socket) return@execute
                    socket = null
                    helloDone = false
                    timer?.cancel(false)
                    if (wanted) {
                        onState(LinkState.Failed(error ?: "Connection closed."))
                        reconnect = executor.schedule({ open() }, 2, TimeUnit.SECONDS)
                    }
                }
            },
        )
        socket = s
        val hostNow = host
        Thread {
            s.connect()
            s.sendJson(jsonOf(
                "kind" to "hello", "role" to "camera", "token" to settings.token(hostNow),
                "client_name" to settings.deviceName, "device_id" to settings.deviceId,
                "platform" to "android", "app_version" to settings.appVersion,
            ))
        }.start()
    }

    private fun handle(type: Int, payload: ByteArray) {
        if (type != Wire.JSON) return
        val msg = try { JSONObject(String(payload, Charsets.UTF_8)) } catch (_: Exception) { return }
        when (msg.optString("kind")) {
            "hello_pending" -> onState(LinkState.AwaitingApproval)
            "hello_ack" -> {
                if (msg.optBoolean("ok")) {
                    msg.optString("token").takeIf { it.isNotEmpty() }?.let { settings.setToken(host, it) }
                    helloDone = true
                    onState(LinkState.Connected(msg.optString("hostname", host)))
                    startTimer()
                    sendStatus()
                    onNeedKeyframe()
                } else {
                    val error = msg.optString("error", "Refused.")
                    if (error.contains("denied")) settings.setToken(host, "")
                    wanted = false  // don't keep re-requesting pairing; the user retries
                    onState(LinkState.Failed(error))
                    socket?.close()
                }
            }
            "clock_pong" -> {
                val t1 = nowNs()
                val t0 = pings.remove(msg.optInt("id")) ?: return
                clock.add(t0, msg.getLong("ts"), t1)
                val best = clock.best ?: return
                if (best != clockOffset?.let { it.second to it.first }) {
                    clockOffset = best.second to best.first
                    socket?.sendJson(jsonOf("kind" to "clock_sync", "offset_ns" to best.second, "rtt_ns" to best.first))
                }
            }
            "request_keyframe" -> onNeedKeyframe()
            else -> onCommand(msg)
        }
    }

    private fun sendPing() {
        pingId += 1
        val t0 = nowNs()
        pings[pingId] = t0
        if (pings.size > 50) pings.clear()
        socket?.sendJson(jsonOf("kind" to "clock_ping", "id" to pingId, "t0" to t0))
    }

    /** 10 Hz: a burst of pings for the first few seconds (a good clock
     *  estimate fast), then one a second to track drift; status every 5 s. */
    private fun startTimer() {
        timer?.cancel(false)
        ticks = 0
        timer = executor.scheduleAtFixedRate({
            if (!helloDone) return@scheduleAtFixedRate
            ticks += 1
            if (ticks <= 30 || ticks % 10 == 0) sendPing()
            if (ticks % 50 == 0) sendStatus()
        }, 0, 100, TimeUnit.MILLISECONDS)
    }
}
