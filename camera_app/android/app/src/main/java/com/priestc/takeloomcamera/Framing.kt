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

/** The part of the camera's own (centered) view to show: middle and size as
 *  fractions of that view, in the sensor's orientation. Cut by [FrameRotator]. */
data class GlCrop(val x: Double = 0.5, val y: Double = 0.5, val size: Double = 1.0)

/**
 * How a framing is carried out: the camera's zoom ratio (null: don't set
 * one), a sensor crop region (null: none), and the rest as a [GlCrop].
 * [cameraView] is how much of the widest view the camera itself shows.
 */
data class FramingPlan(val ratio: Float?, val crop: Rect?, val glCrop: GlCrop, val cameraView: Double, val zoom: Double)

/**
 * What this camera can do. Zoom is Camera2's zoom ratio (Android 11+), which
 * on a multi-lens camera switches lenses by itself (optical zoom); before
 * that, a centered sensor crop. Panning: a camera with FREEFORM cropping
 * moves its sensor crop region off-center; the rest (most phones, which only
 * crop centered) get a centered camera zoom plus a crop of the frames on the
 * GPU, so every phone can pan.
 */
class ZoomCaps(chars: CameraCharacteristics) {
    private val activeArray: Rect = chars.get(CameraCharacteristics.SENSOR_INFO_ACTIVE_ARRAY_SIZE) ?: Rect(0, 0, 1, 1)
    private val useRatio = Build.VERSION.SDK_INT >= Build.VERSION_CODES.R
    val min: Double
    val max: Double
    private val sensorPan = chars.get(CameraCharacteristics.SCALER_CROPPING_TYPE) == CameraCharacteristics.SCALER_CROPPING_TYPE_FREEFORM

    init {
        val maxDigital = (chars.get(CameraCharacteristics.SCALER_AVAILABLE_MAX_DIGITAL_ZOOM) ?: 1f).toDouble()
        val range = if (useRatio) chars.get(CameraCharacteristics.CONTROL_ZOOM_RATIO_RANGE) else null
        min = range?.lower?.toDouble() ?: 1.0
        max = min(range?.upper?.toDouble() ?: maxDigital, 10.0)
    }

    fun json() = jsonOf("min" to min, "max" to max,
        "lenses" to JSONArray(if (min < 0.99) listOf(min, 1.0) else listOf(1.0)), "pan" to true)

    /**
     * Split a framing (x/y already in the sensor's orientation) into what
     * the camera does and what's cropped. [previous] is the plan in force:
     * a pan alone keeps the camera's zoom whenever the new region still fits
     * in its view, so panning is a crop of the frames only, instant and
     * smooth, instead of the camera re-zooming (a few frames late, so the
     * picture jumps) on every step. When it no longer fits, the camera zooms
     * out with room to spare for the next steps. A zoom change, or a
     * centered shot, sets the camera's zoom to fit exactly: all optical.
     */
    fun plan(framing: Framing, previous: FramingPlan?): FramingPlan {
        val zoom = min(max(framing.zoom, min), max)
        val visible = min / zoom
        val limit = (1 - visible) / 2
        val dx = min(max(framing.x - 0.5, -limit), limit)
        val dy = min(max(framing.y - 0.5, -limit), limit)
        val need = min(1.0, visible + 2 * max(abs(dx), abs(dy)))
        if (sensorPan) {
            // The sensor crops anywhere, atomically with the zoom ratio.
            if (!useRatio) return FramingPlan(null, region(visible, 0.5 + dx, 0.5 + dy), GlCrop(), 1.0, zoom)
            val size = visible / need
            val crop = if (size > 0.999) null else region(size, 0.5 + dx / need, 0.5 + dy / need)
            return FramingPlan((min / need).toFloat(), crop, GlCrop(), need, zoom)
        }
        val centered = abs(dx) < 1e-6 && abs(dy) < 1e-6
        val cameraView = when {
            previous == null || abs(previous.zoom - zoom) > 1e-9 || centered -> need
            need <= previous.cameraView + 1e-9 -> previous.cameraView
            else -> min(1.0, need + visible)
        }
        val glCrop = GlCrop(0.5 + dx / cameraView, 0.5 + dy / cameraView, visible / cameraView)
        return if (useRatio) {
            FramingPlan((min / cameraView).toFloat(), null, glCrop, cameraView, zoom)
        } else {
            val crop = if (cameraView > 0.999) null else region(cameraView, 0.5, 0.5)
            FramingPlan(null, crop, glCrop, cameraView, zoom)
        }
    }

    private fun region(size: Double, x: Double, y: Double): Rect {
        val w = activeArray.width() * size
        val h = activeArray.height() * size
        val left = (activeArray.left + activeArray.width() * x - w / 2).toInt()
        val top = (activeArray.top + activeArray.height() * y - h / 2).toInt()
        return Rect(left, top, left + w.toInt(), top + h.toInt())
    }
}
