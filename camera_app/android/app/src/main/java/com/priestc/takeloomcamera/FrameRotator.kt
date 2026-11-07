package com.priestc.takeloomcamera

import android.graphics.SurfaceTexture
import android.opengl.EGL14
import android.opengl.EGLConfig
import android.opengl.EGLContext
import android.opengl.EGLDisplay
import android.opengl.EGLExt
import android.opengl.EGLSurface
import android.opengl.GLES11Ext
import android.opengl.GLES20
import android.os.Handler
import android.os.HandlerThread
import android.view.Surface
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.FloatBuffer
import java.util.concurrent.CountDownLatch

/**
 * Sits between the camera and the hardware encoders: the camera renders into
 * [inputSurface] (an OES texture), and each frame is drawn into every encoder
 * input surface, turned 180° when [rotate180] is set.
 *
 * The sensor's frames only come out upright in one of the two landscapes,
 * and the live H.264 has nowhere to carry a rotation flag, so a phone held
 * the other way up (camera bump at the bottom, as the Takeloom mount allows)
 * has to have its pixels turned here, the way the iOS app turns them in
 * AVFoundation (CameraPipeline.applyRotation).
 *
 * Each frame keeps the camera's own sensor timestamp
 * (eglPresentationTimeANDROID), so the encoders' presentation times stay on
 * the sensor clock that clock sync and the recording's start time rely on.
 */
class FrameRotator(width: Int, height: Int, outputs: List<Triple<Surface, Int, Int>>) {
    @Volatile var rotate180 = false
    /** Zoom/pan beyond what the camera does itself (ZoomCaps.plan). */
    @Volatile var crop = GlCrop()
    private val coords: FloatBuffer =
        ByteBuffer.allocateDirect(8 * 4).order(ByteOrder.nativeOrder()).asFloatBuffer()

    private val thread = HandlerThread("frame-rotator").apply { start() }
    private val handler = Handler(thread.looper)
    private var display: EGLDisplay = EGL14.EGL_NO_DISPLAY
    private var context: EGLContext = EGL14.EGL_NO_CONTEXT
    private var pbuffer: EGLSurface = EGL14.EGL_NO_SURFACE
    private val targets = mutableListOf<Target>()
    private var program = 0
    private var texture = 0
    private lateinit var surfaceTexture: SurfaceTexture
    lateinit var inputSurface: Surface
        private set
    @Volatile private var released = false

    private class Target(val egl: EGLSurface, val width: Int, val height: Int)

    init {
        val ready = CountDownLatch(1)
        var failure: Throwable? = null
        handler.post {
            try {
                setUp(width, height, outputs)
            } catch (e: Throwable) {
                failure = e
            }
            ready.countDown()
        }
        ready.await()
        failure?.let {
            release()
            throw RuntimeException("Couldn't set up the frame rotator: ${it.message}", it)
        }
    }

    private fun setUp(width: Int, height: Int, outputs: List<Triple<Surface, Int, Int>>) {
        display = EGL14.eglGetDisplay(EGL14.EGL_DEFAULT_DISPLAY)
        val version = IntArray(2)
        check(EGL14.eglInitialize(display, version, 0, version, 1)) { "eglInitialize failed" }
        val attribs = intArrayOf(
            // Alpha too: the preview ImageReader's buffers are RGBA_8888, and
            // an RGBX surface would be refused there as the wrong format.
            EGL14.EGL_RED_SIZE, 8, EGL14.EGL_GREEN_SIZE, 8, EGL14.EGL_BLUE_SIZE, 8, EGL14.EGL_ALPHA_SIZE, 8,
            EGL14.EGL_RENDERABLE_TYPE, EGL14.EGL_OPENGL_ES2_BIT,
            EGLExt.EGL_RECORDABLE_ANDROID, 1,
            EGL14.EGL_NONE,
        )
        val configs = arrayOfNulls<EGLConfig>(1)
        val count = IntArray(1)
        check(EGL14.eglChooseConfig(display, attribs, 0, configs, 0, 1, count, 0) && count[0] > 0) {
            "no recordable EGL config"
        }
        val config = configs[0]!!
        context = EGL14.eglCreateContext(display, config, EGL14.EGL_NO_CONTEXT,
            intArrayOf(EGL14.EGL_CONTEXT_CLIENT_VERSION, 2, EGL14.EGL_NONE), 0)
        check(context != EGL14.EGL_NO_CONTEXT) { "eglCreateContext failed" }
        pbuffer = EGL14.eglCreatePbufferSurface(display, config,
            intArrayOf(EGL14.EGL_WIDTH, 1, EGL14.EGL_HEIGHT, 1, EGL14.EGL_NONE), 0)
        for ((surface, w, h) in outputs) {
            val egl = EGL14.eglCreateWindowSurface(display, config, surface, intArrayOf(EGL14.EGL_NONE), 0)
            check(egl != EGL14.EGL_NO_SURFACE) { "eglCreateWindowSurface failed" }
            targets += Target(egl, w, h)
        }
        EGL14.eglMakeCurrent(display, pbuffer, pbuffer, context)

        program = buildProgram()
        val tex = IntArray(1)
        GLES20.glGenTextures(1, tex, 0)
        texture = tex[0]
        GLES20.glBindTexture(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, texture)
        GLES20.glTexParameteri(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, GLES20.GL_TEXTURE_MIN_FILTER, GLES20.GL_LINEAR)
        GLES20.glTexParameteri(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, GLES20.GL_TEXTURE_MAG_FILTER, GLES20.GL_LINEAR)
        GLES20.glTexParameteri(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, GLES20.GL_TEXTURE_WRAP_S, GLES20.GL_CLAMP_TO_EDGE)
        GLES20.glTexParameteri(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, GLES20.GL_TEXTURE_WRAP_T, GLES20.GL_CLAMP_TO_EDGE)

        surfaceTexture = SurfaceTexture(texture).apply {
            setDefaultBufferSize(width, height)
            setOnFrameAvailableListener({ drawFrame() }, handler)
        }
        inputSurface = Surface(surfaceTexture)
    }

    /** On the rotator thread, once per camera frame. */
    private fun drawFrame() {
        if (released) return
        EGL14.eglMakeCurrent(display, pbuffer, pbuffer, context)
        surfaceTexture.updateTexImage()
        // Not surfaceTexture.getTransformMatrix(): for a SurfaceTexture the
        // camera adds a turn by the sensor orientation (so a preview comes
        // out upright in the phone's natural portrait), which would put the
        // video 90° off. The encoders always got the sensor's frames as
        // they are, so all that's needed is GL's bottom-up rows flipped.
        val timestampNs = surfaceTexture.timestamp
        fillCoords()
        for (t in targets) {
            if (!EGL14.eglMakeCurrent(display, t.egl, t.egl, context)) continue
            GLES20.glViewport(0, 0, t.width, t.height)
            GLES20.glUseProgram(program)
            GLES20.glActiveTexture(GLES20.GL_TEXTURE0)
            GLES20.glBindTexture(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, texture)
            GLES20.glUniformMatrix4fv(GLES20.glGetUniformLocation(program, "uTexMatrix"), 1, false, FLIP_ROWS, 0)
            val pos = GLES20.glGetAttribLocation(program, "aPos")
            val tex = GLES20.glGetAttribLocation(program, "aTex")
            GLES20.glEnableVertexAttribArray(pos)
            GLES20.glVertexAttribPointer(pos, 2, GLES20.GL_FLOAT, false, 0, QUAD)
            GLES20.glEnableVertexAttribArray(tex)
            GLES20.glVertexAttribPointer(tex, 2, GLES20.GL_FLOAT, false, 0, coords)
            GLES20.glDrawArrays(GLES20.GL_TRIANGLE_STRIP, 0, 4)
            EGLExt.eglPresentationTimeANDROID(display, t.egl, timestampNs)
            EGL14.eglSwapBuffers(display, t.egl)
        }
    }

    /**
     * Texture coordinates for the quad's corners (bottom-left, bottom-right,
     * top-left, top-right of the picture sent): each corner's place in the
     * picture, turned 180° into the sensor's orientation if need be, then
     * into the crop. The t written is 1 - y because FLIP_ROWS turns it
     * back round into the buffer's top-down rows.
     */
    private fun fillCoords() {
        val c = crop
        val turn = rotate180
        coords.position(0)
        for ((px, py) in CORNERS) {
            val rx = if (turn) 1 - px else px
            val ry = if (turn) 1 - py else py
            val sx = c.x + (rx - 0.5) * c.size
            val sy = c.y + (ry - 0.5) * c.size
            coords.put(sx.toFloat()).put((1 - sy).toFloat())
        }
        coords.position(0)
    }

    /** Blocks until the GL side is torn down. Call before the encoders' surfaces go. */
    fun release() {
        if (released) return
        released = true
        val done = CountDownLatch(1)
        handler.post {
            runCatching {
                if (display != EGL14.EGL_NO_DISPLAY) {
                    EGL14.eglMakeCurrent(display, EGL14.EGL_NO_SURFACE, EGL14.EGL_NO_SURFACE, EGL14.EGL_NO_CONTEXT)
                    targets.forEach { EGL14.eglDestroySurface(display, it.egl) }
                    if (pbuffer != EGL14.EGL_NO_SURFACE) EGL14.eglDestroySurface(display, pbuffer)
                    if (context != EGL14.EGL_NO_CONTEXT) EGL14.eglDestroyContext(display, context)
                    EGL14.eglReleaseThread()
                    EGL14.eglTerminate(display)
                }
            }
            targets.clear()
            if (::inputSurface.isInitialized) inputSurface.release()
            if (::surfaceTexture.isInitialized) surfaceTexture.release()
            done.countDown()
        }
        done.await()
        thread.quitSafely()
    }

    private fun buildProgram(): Int {
        val vs = compile(GLES20.GL_VERTEX_SHADER, """
            attribute vec4 aPos;
            attribute vec4 aTex;
            uniform mat4 uTexMatrix;
            varying vec2 vTex;
            void main() {
                gl_Position = aPos;
                vTex = (uTexMatrix * aTex).xy;
            }
        """)
        val fs = compile(GLES20.GL_FRAGMENT_SHADER, """
            #extension GL_OES_EGL_image_external : require
            precision mediump float;
            varying vec2 vTex;
            uniform samplerExternalOES sTex;
            void main() {
                gl_FragColor = texture2D(sTex, vTex);
            }
        """)
        val p = GLES20.glCreateProgram()
        GLES20.glAttachShader(p, vs)
        GLES20.glAttachShader(p, fs)
        GLES20.glLinkProgram(p)
        val ok = IntArray(1)
        GLES20.glGetProgramiv(p, GLES20.GL_LINK_STATUS, ok, 0)
        check(ok[0] != 0) { "shader link failed: ${GLES20.glGetProgramInfoLog(p)}" }
        return p
    }

    private fun compile(type: Int, source: String): Int {
        val s = GLES20.glCreateShader(type)
        GLES20.glShaderSource(s, source.trimIndent())
        GLES20.glCompileShader(s)
        val ok = IntArray(1)
        GLES20.glGetShaderiv(s, GLES20.GL_COMPILE_STATUS, ok, 0)
        check(ok[0] != 0) { "shader compile failed: ${GLES20.glGetShaderInfoLog(s)}" }
        return s
    }

    private companion object {
        fun floats(vararg v: Float): FloatBuffer =
            ByteBuffer.allocateDirect(v.size * 4).order(ByteOrder.nativeOrder()).asFloatBuffer().apply {
                put(v)
                position(0)
            }

        /** t -> 1 - t (column-major): the buffer's first row at the top. */
        val FLIP_ROWS = floatArrayOf(1f, 0f, 0f, 0f, 0f, -1f, 0f, 0f, 0f, 0f, 1f, 0f, 0f, 1f, 0f, 1f)

        // Full-screen triangle strip: bottom-left, bottom-right, top-left, top-right.
        val QUAD = floats(-1f, -1f, 1f, -1f, -1f, 1f, 1f, 1f)
        /** The same corners in the picture: x from the left, y from the top. */
        val CORNERS = listOf(0.0 to 1.0, 1.0 to 1.0, 0.0 to 0.0, 1.0 to 0.0)
    }
}
