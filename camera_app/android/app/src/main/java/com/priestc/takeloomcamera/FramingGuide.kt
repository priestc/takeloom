package com.priestc.takeloomcamera

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.RectF
import android.view.View

/**
 * The framing guide drawn over the preview while it shows the widest lens:
 * a labelled rectangle for the frame each other lens would record, so a
 * phone can be aimed in its mount knowing what every zoom will catch.
 * Every rectangle is centered: the lenses sit a centimetre or so apart,
 * which doesn't show at room distances.
 */
class FramingGuide(context: Context) : View(context) {
    /** Widest first; [selected] is drawn highlighted. */
    var lenses: List<Double> = emptyList()
        set(value) { field = value; invalidate() }
    var selected: Double = 1.0
        set(value) { field = value; invalidate() }
    /** The video's width/height, for where the picture sits in the view. */
    var aspect: Float = 16f / 9f
        set(value) { field = value; invalidate() }

    private val density = resources.displayMetrics.density
    private val line = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = 2 * density
    }
    private val label = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        textSize = 16 * density
        isFakeBoldText = true
        setShadowLayer(3 * density, 0f, 0f, Color.BLACK)
    }

    override fun onDraw(canvas: Canvas) {
        if (lenses.size < 2) return
        // The picture is letterboxed into the view (MainActivity.configureTransform).
        val scale = minOf(width / aspect, height.toFloat())
        val pictureW = scale * aspect
        val pictureH = scale
        val cx = width / 2f
        val cy = height / 2f
        val widest = lenses.first()
        for (zoom in lenses.drop(1)) {
            val f = (widest / zoom).toFloat()
            val r = RectF(cx - pictureW * f / 2, cy - pictureH * f / 2, cx + pictureW * f / 2, cy + pictureH * f / 2)
            val color = if (zoom == selected) Color.rgb(255, 214, 10) else Color.WHITE
            line.color = color
            line.strokeWidth = (if (zoom == selected) 3 else 2) * density
            canvas.drawRect(r, line)
            label.color = color
            canvas.drawText(lensLabel(zoom), r.left + 6 * density, r.top + 20 * density, label)
        }
        val widestColor = if (widest == selected) Color.rgb(255, 214, 10) else Color.WHITE
        label.color = widestColor
        // Top right: the connection pill has the top left.
        val text = lensLabel(widest)
        canvas.drawText(text, cx + pictureW / 2 - label.measureText(text) - 10 * density,
            cy - pictureH / 2 + 24 * density, label)
    }

    companion object {
        fun lensLabel(zoom: Double) = if (zoom == Math.floor(zoom)) "${zoom.toInt()}×" else "$zoom×"
    }
}
