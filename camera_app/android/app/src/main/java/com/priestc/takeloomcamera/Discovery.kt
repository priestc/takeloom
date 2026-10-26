package com.priestc.takeloomcamera

import android.content.Context
import android.net.ConnectivityManager
import org.json.JSONObject
import java.io.DataInputStream
import java.net.Inet4Address
import java.net.InetSocketAddress
import java.net.Socket
import java.util.concurrent.Callable
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/** A takeloom server found on the local network. */
data class DiscoveredServer(val host: String, val hostname: String) {
    val address get() = "$host:${Wire.PORT}"
}

/**
 * Finding the takeloom server without typing its address: try every
 * address on the phone's own network on the camera port, and keep the ones
 * that answer the protocol's "discover" with "discover_ack" (see
 * takeloom/phone_camera/protocol.py), so another service that happens to use
 * the port isn't mistaken for takeloom. Same as the iOS app's Discovery.swift.
 */
object ServerDiscovery {
    private const val MAX_CONCURRENT = 64
    private const val CONNECT_TIMEOUT_MS = 1000
    private const val READ_TIMEOUT_MS = 1500

    /** Blocking (a few seconds): call from a background thread. */
    fun scan(context: Context): List<DiscoveredServer> {
        val hosts = candidateHosts(context)
        if (hosts.isEmpty()) return emptyList()
        val pool = Executors.newFixedThreadPool(MAX_CONCURRENT)
        try {
            val futures = pool.invokeAll(hosts.map { host -> Callable { probe(host) } }, 60, TimeUnit.SECONDS)
            return futures.mapNotNull { runCatching { it.get() }.getOrNull() }
                .sortedBy { it.host.substringAfterLast('.').toIntOrNull() ?: 0 }
        } finally {
            pool.shutdownNow()
        }
    }

    /**
     * The other addresses on the phone's network: its /24 (a wider network
     * is narrowed to the /24 the phone is in, to keep a scan to seconds).
     */
    fun candidateHosts(context: Context): List<String> {
        val cm = context.getSystemService(ConnectivityManager::class.java) ?: return emptyList()
        val link = cm.getLinkProperties(cm.activeNetwork) ?: return emptyList()
        val v4 = link.linkAddresses.firstOrNull { it.address is Inet4Address } ?: return emptyList()
        val bytes = v4.address.address
        val ip = bytes.fold(0L) { acc, b -> (acc shl 8) or (b.toLong() and 0xFF) }
        val prefix = maxOf(v4.prefixLength, 24)
        val mask = (0xFFFFFFFFL shl (32 - prefix)) and 0xFFFFFFFFL
        val network = ip and mask
        val size = (mask.inv() and 0xFFFFFFFFL)
        return (1 until size).map { network or it }.filter { it != ip }.map { a ->
            "${a shr 24 and 0xFF}.${a shr 16 and 0xFF}.${a shr 8 and 0xFF}.${a and 0xFF}"
        }
    }

    /** Ask `host` "discover"; its DiscoveredServer if it answers as takeloom. */
    fun probe(host: String): DiscoveredServer? = try {
        Socket().use { socket ->
            socket.connect(InetSocketAddress(host, Wire.PORT), CONNECT_TIMEOUT_MS)
            socket.soTimeout = READ_TIMEOUT_MS
            socket.getOutputStream().write(Wire.json(jsonOf("kind" to "discover")))
            val (type, payload) = Wire.readFrame(DataInputStream(socket.getInputStream()))
            val message = if (type == Wire.JSON) JSONObject(String(payload, Charsets.UTF_8)) else null
            if (message?.optString("kind") == "discover_ack") {
                DiscoveredServer(host, message.optString("hostname").ifEmpty { host })
            } else {
                null
            }
        }
    } catch (_: Exception) {
        null
    }
}
