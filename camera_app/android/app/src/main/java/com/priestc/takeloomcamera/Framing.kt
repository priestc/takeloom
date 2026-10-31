package com.priestc.takeloomcamera

import android.graphics.Rect
import android.hardware.camera2.CameraCharacteristics
import android.os.Build
import org.json.JSONArray
import kotlin.math.abs
import kotlin.math.max
import kotlin.math.min

/**
 * Zoom and pan, as the server sends them (takeloom/phone_camera/framing.py):
 * [zoom] like the Camera app (1 = main lens, 0.6 or 0.5 = ultra-wide), [x]/[y]
 * the middle of the picture as fractions of the widest view.
 */
data class Framing(val zoom: Double = 1.0, val x: Double = 0.5, val y: Double = 0.5) {
    fun json() = jsonOf("zoom" to zoom, "x" to x, "y" to y)
}

/**
 * What this camera can do. Zoom is Camera2's zoom ratio (Android 11+), which
 * on a multi-lens camera switches lenses by itself (optical zoom); before
 * that, only a digital crop. Panning moves the sensor crop region off-center,
 * which only cameras with FREEFORM cropping allow — the rest zoom centered.
 */
class ZoomCaps(chars: CameraCharacteristics) {
    private val activeArray: Rect = chars.get(CameraCharacteristics.SENSOR_INFO_ACTIVE_ARRAY_SIZE) ?: Rect(0, 0, 1, 1)
    private val useRatio = Build.VERSION.SDK_INT >= Build.VERSION_CODES.R
    val min: Double
    val max: Double
    val pan = chars.get(CameraCharacteristics.SCALER_CROPPING_TYPE) == CameraCharacteristics.SCALER_CROPPING_TYPE_FREEFORM

    init {
        val maxDigital = (chars.get(CameraCharacteristics.SCALER_AVAILABLE_MAX_DIGITAL_ZOOM) ?: 1f).toDouble()
        val range = if (useRatio) chars.get(CameraCharacteristics.CONTROL_ZOOM_RATIO_RANGE) else null
        min = range?.lower?.toDouble() ?: 1.0
        max = min(range?.upper?.toDouble() ?: maxDigital, 10.0)
    }

    fun json() = jsonOf("min" to min, "max" to max,
        "lenses" to JSONArray(if (min < 0.99) listOf(min, 1.0) else listOf(1.0)), "pan" to pan)

    /**
     * Split a framing into the camera's centered zoom ratio (as much as
     * keeps the requested region in view) and a crop region in active-array
     * coordinates for the rest (null: none). Without zoom ratio support the
     * crop region does everything.
     */
    fun plan(framing: Framing): Pair<Float?, Rect?> {
        val zoom = min(max(framing.zoom, min), max)
        val visible = min / zoom
        val limit = (1 - visible) / 2
        val dx = if (pan) min(max(framing.x - 0.5, -limit), limit) else 0.0
        val dy = if (pan) min(max(framing.y - 0.5, -limit), limit) else 0.0
        if (!useRatio) return null to region(visible, 0.5 + dx, 0.5 + dy)
        val cameraView = min(1.0, visible + 2 * max(abs(dx), abs(dy)))
        val size = visible / cameraView
        val crop = if (size > 0.999) null else region(size, 0.5 + dx / cameraView, 0.5 + dy / cameraView)
        return (min / cameraView).toFloat() to crop
    }

    private fun region(size: Double, x: Double, y: Double): Rect {
        val w = activeArray.width() * size
        val h = activeArray.height() * size
        val left = (activeArray.left + activeArray.width() * x - w / 2).toInt()
        val top = (activeArray.top + activeArray.height() * y - h / 2).toInt()
        return Rect(left, top, left + w.toInt(), top + h.toInt())
    }
}
