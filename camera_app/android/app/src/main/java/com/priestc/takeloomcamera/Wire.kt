package com.priestc.takeloomcamera

import org.json.JSONObject
import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.io.DataInputStream
import java.io.DataOutputStream
import java.net.InetSocketAddress
import java.net.Socket
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.atomic.AtomicLong

/**
 * The takeloom server's camera protocol — see takeloom/phone_camera/protocol.py
 * for the authoritative description. Every message is
 * [1 byte type][4 byte big-endian length][payload].
 */
object Wire {
    const val PORT = 51824
    const val JSON = 1
    const val VIDEO = 2
    const val PREVIEW = 3
    const val UPLOAD = 4
    private const val MAX_FRAME_BYTES = 32 * 1024 * 1024

    fun frame(type: Int, payload: ByteArray, offset: Int = 0, length: Int = payload.size): ByteArray {
        val out = ByteArray(length + 5)
        out[0] = type.toByte()
        putInt(out, 1, length)
        System.arraycopy(payload, offset, out, 5, length)
        return out
    }

    fun json(obj: JSONObject): ByteArray = frame(JSON, obj.toString().toByteArray(Charsets.UTF_8))

    fun video(ptsNs: Long, keyframe: Boolean, annexB: ByteArray): ByteArray {
        val payload = ByteArray(annexB.size + 9)
        for (i in 0 until 8) payload[i] = (ptsNs ushr (56 - 8 * i)).toByte()
        payload[8] = if (keyframe) 1 else 0
        System.arraycopy(annexB, 0, payload, 9, annexB.size)
        return frame(VIDEO, payload)
    }

    private fun putInt(buf: ByteArray, at: Int, value: Int) {
        buf[at] = (value ushr 24).toByte()
        buf[at + 1] = (value ushr 16).toByte()
        buf[at + 2] = (value ushr 8).toByte()
        buf[at + 3] = value.toByte()
    }

    fun readFrame(input: DataInputStream): Pair<Int, ByteArray> {
        val type = input.readUnsignedByte()
        val length = input.readInt()
        if (length < 0 || length > MAX_FRAME_BYTES) throw java.io.IOException("bad frame length $length")
        val payload = ByteArray(length)
        input.readFully(payload)
        return type to payload
    }
}

fun jsonOf(vararg pairs: Pair<String, Any?>): JSONObject {
    val obj = JSONObject()
    for ((k, v) in pairs) obj.put(k, v ?: JSONObject.NULL)
    return obj
}

/**
 * One TCP connection speaking the framed protocol, with its own reader and
 * writer threads. Sends never block the caller: they queue, and
 * [bytesQueued] is what live-video backpressure looks at.
 */
class FramedSocket(
    private val host: String,
    private val onFrame: (Int, ByteArray) -> Unit,
    private val onClosed: (String?) -> Unit,
) {
    private val socket = Socket()
    private val outbox = LinkedBlockingQueue<Pair<ByteArray, (() -> Unit)?>>()
    val bytesQueued = AtomicLong(0)
    @Volatile var closed = false
        private set

    /** Blocking: call from a background thread. */
    fun connect() {
        try {
            socket.tcpNoDelay = true
            socket.connect(InetSocketAddress(host, Wire.PORT), 5000)
        } catch (e: Exception) {
            close(e.message ?: "Couldn't connect to $host")
            return
        }
        Thread({ writeLoop() }, "socket-writer").start()
        Thread({ readLoop() }, "socket-reader").start()
    }

    fun send(data: ByteArray, onSent: (() -> Unit)? = null) {
        if (closed) return
        bytesQueued.addAndGet(data.size.toLong())
        outbox.put(data to onSent)
    }

    fun sendJson(obj: JSONObject) = send(Wire.json(obj))

    fun close(error: String? = null) {
        synchronized(this) {
            if (closed) return
            closed = true
        }
        try { socket.close() } catch (_: Exception) {}
        outbox.clear()
        outbox.put(ByteArray(0) to null)  // wake the writer so it exits
        onClosed(error)
    }

    private fun writeLoop() {
        try {
            val out = DataOutputStream(BufferedOutputStream(socket.getOutputStream(), 256 * 1024))
            while (!closed) {
                val (data, onSent) = outbox.take()
                if (closed) break
                out.write(data)
                if (outbox.isEmpty()) out.flush()
                bytesQueued.addAndGet(-data.size.toLong())
                onSent?.invoke()
            }
        } catch (e: Exception) {
            close(e.message)
        }
    }

    private fun readLoop() {
        try {
            val input = DataInputStream(BufferedInputStream(socket.getInputStream()))
            while (!closed) {
                val (type, payload) = Wire.readFrame(input)
                onFrame(type, payload)
            }
        } catch (e: Exception) {
            close(if (closed) null else (e.message ?: "Connection closed."))
        }
    }
}
