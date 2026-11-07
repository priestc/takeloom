package com.priestc.takeloomcamera

import android.content.Context
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.os.SystemClock
import kotlin.math.sqrt

/**
 * Blanks the screen once the phone is in the mount rather than in a hand.
 *
 * A phone on the wall mount keeps exactly the same angle to gravity; one in
 * a hand drifts by more than a couple of degrees within seconds, however
 * steady the hand. So: once the accelerometer's gravity vector has stayed
 * within [MOVE_G] of where it settled for [STILL_MS], [onSleep]; any
 * movement past that again (the phone taken down), or a [touched], [onWake].
 *
 * "Sleep" is the caller's black screen at minimum brightness, not the real
 * display sleep: Android stops the camera when the activity leaves the
 * foreground, and the camera has to keep running for the live feed and any
 * recording.
 */
class MountSleep(context: Context, private val onSleep: () -> Unit, private val onWake: () -> Unit) : SensorEventListener {
    private val sensors = context.getSystemService(Context.SENSOR_SERVICE) as SensorManager
    private val accelerometer: Sensor? = sensors.getDefaultSensor(Sensor.TYPE_ACCELEROMETER)
    private var ref: FloatArray? = null
    private val smoothed = FloatArray(3)
    private var haveSample = false
    private var stillSince = 0L
    private var lastTouch = 0L
    var asleep = false
        private set

    fun start() {
        accelerometer?.let { sensors.registerListener(this, it, SensorManager.SENSOR_DELAY_UI) }
        stillSince = SystemClock.elapsedRealtime()
    }

    fun stop() {
        sensors.unregisterListener(this)
        wake()
    }

    /** The screen was touched: wake, and stay awake at least [STILL_MS]. */
    fun touched() {
        lastTouch = SystemClock.elapsedRealtime()
        wake()
    }

    override fun onSensorChanged(event: SensorEvent) {
        // A light low-pass, in g: sensor noise out, a real tilt kept.
        for (i in 0..2) {
            val g = event.values[i] / SensorManager.GRAVITY_EARTH
            smoothed[i] = if (haveSample) smoothed[i] * 0.8f + g * 0.2f else g
        }
        haveSample = true
        val now = SystemClock.elapsedRealtime()
        val r = ref
        if (r == null || distance(r, smoothed) > MOVE_G) {
            ref = smoothed.copyOf()
            stillSince = now
            wake()
            return
        }
        if (!asleep && now - stillSince >= STILL_MS && now - lastTouch >= STILL_MS) {
            asleep = true
            onSleep()
        }
    }

    override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) {}

    private fun wake() {
        if (!asleep) return
        asleep = false
        onWake()
    }

    private fun distance(a: FloatArray, b: FloatArray): Float {
        val dx = a[0] - b[0]
        val dy = a[1] - b[1]
        val dz = a[2] - b[2]
        return sqrt(dx * dx + dy * dy + dz * dz)
    }

    companion object {
        /** About 2° of tilt (sin 2° ≈ 0.035 g). */
        const val MOVE_G = 0.035f
        const val STILL_MS = 20_000L
    }
}
