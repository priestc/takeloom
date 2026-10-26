package com.priestc.takeloomcamera

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.Color
import android.graphics.Matrix
import android.graphics.RectF
import android.graphics.SurfaceTexture
import android.graphics.drawable.GradientDrawable
import android.os.BatteryManager
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.text.InputType
import android.view.Gravity
import android.view.Surface
import android.view.TextureView
import android.view.View
import android.view.WindowManager
import android.widget.Button
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.RadioButton
import android.widget.RadioGroup
import android.widget.TextView
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.util.concurrent.Executors

class MainActivity : Activity() {
    private lateinit var settings: Settings
    private lateinit var store: RecordingStore
    private lateinit var engine: CameraEngine
    private lateinit var link: ServerLink
    private lateinit var uploader: Uploader
    private lateinit var textureView: TextureView
    private lateinit var connectionText: TextView
    private lateinit var recordingText: TextView
    private lateinit var uploadText: TextView
    private val main = Handler(Looper.getMainLooper())
    private val previewExecutor = Executors.newSingleThreadExecutor()
    @Volatile private var previewBusy = false

    private var recordingSession: String? = null
    private var recordingSince = 0L
    private var uploadStatus: String? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        settings = Settings(this)
        store = RecordingStore(getExternalFilesDir(null) ?: filesDir)
        engine = CameraEngine(this)
        link = ServerLink(settings)
        uploader = Uploader(store, settings)
        requestedOrientation = engine.requiredOrientation(settings.useFrontCamera)
        buildUi()
        wire()

        val needed = arrayOf(Manifest.permission.CAMERA, Manifest.permission.RECORD_AUDIO)
            .filter { checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }
        if (needed.isNotEmpty()) requestPermissions(needed.toTypedArray(), 1) else startEverything()
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        if (checkSelfPermission(Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED &&
            checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED
        ) {
            startEverything()
        } else {
            connectionText.text = "Camera and microphone access are needed — allow them in Settings › Apps."
        }
    }

    private fun startEverything() {
        if (textureView.isAvailable) startCamera()
        if (settings.serverHost.isNotEmpty()) {
            link.connect(settings.serverHost)
        } else {
            findServerAtFirstLaunch()  // look for it instead of asking for its address
        }
        uploader.kick()
    }

    // --- finding the server (ServerDiscovery) ---

    private var scanning = false

    /** Scan the network on a background thread; `onDone` on the UI thread. */
    private fun scanForServer(onDone: (List<DiscoveredServer>) -> Unit) {
        if (scanning) return
        scanning = true
        Thread({
            val found = ServerDiscovery.scan(this)
            runOnUiThread {
                scanning = false
                if (!isFinishing) onDone(found)
            }
        }, "server-discovery").start()
    }

    private fun findServerAtFirstLaunch() {
        connectionText.text = "◌ Looking for the takeloom server…"
        scanForServer { found ->
            when {
                found.isEmpty() -> AlertDialog.Builder(this)
                    .setTitle("No takeloom server found")
                    .setMessage("Make sure `takeloom server` is running and this phone is on the same Wi-Fi, " +
                        "or enter its address in Settings.")
                    .setPositiveButton("Scan again") { _, _ -> findServerAtFirstLaunch() }
                    .setNegativeButton("OK") { _, _ -> showState(LinkState.Idle) }
                    .show()
                found.size == 1 -> AlertDialog.Builder(this)
                    .setTitle("takeloom server found")
                    .setMessage("Connect to ${found[0].hostname} at ${found[0].address}? " +
                        "You can change it later in Settings.")
                    .setPositiveButton("Connect") { _, _ -> useServer(found[0].host) }
                    .setNegativeButton("Not now") { _, _ -> showState(LinkState.Idle) }
                    .show()
                else -> AlertDialog.Builder(this)
                    .setTitle("Which takeloom server?")
                    .setItems(found.map { "${it.hostname} (${it.address})" }.toTypedArray()) { _, i ->
                        useServer(found[i].host)
                    }
                    .setNegativeButton("Not now") { _, _ -> showState(LinkState.Idle) }
                    .show()
            }
        }
    }

    private fun useServer(host: String) {
        settings.serverHost = host
        link.connect(host)
    }

    override fun onDestroy() {
        engine.stop()
        link.disconnect()
        super.onDestroy()
    }

    // --- wiring ---

    private fun wire() {
        link.nowNs = { engine.nowNs() }
        link.onNeedKeyframe = { engine.requestKeyframe() }
        link.statusProvider = { statusFields() }
        link.onState = { state -> main.post { showState(state); if (state is LinkState.Connected) uploader.kick() } }
        link.onCommand = { msg -> main.post { handleCommand(msg) } }
        engine.onEncodedFrame = { pts, key, data -> link.sendVideo(pts, key, data) }
        engine.onError = { error -> main.post { connectionText.text = error } }
        uploader.onProgress = { text -> main.post { uploadStatus = text; refreshUploadText() } }
        uploader.onChange = { main.post { refreshUploadText(); link.sendStatus() } }
        refreshUploadText()
        main.post(object : Runnable {
            override fun run() {
                tick()
                main.postDelayed(this, 200)
            }
        })
    }

    /** 5 Hz: the recording clock, and a preview JPEG for the server. */
    private fun tick() {
        recordingText.visibility = if (recordingSession != null) View.VISIBLE else View.GONE
        if (recordingSession != null) {
            val s = (SystemClock.elapsedRealtime() - recordingSince) / 1000
            recordingText.text = String.format("● REC %d:%02d:%02d", s / 3600, s / 60 % 60, s % 60)
        }
        if (!previewBusy && textureView.isAvailable) {
            val bitmap = textureView.getBitmap(320, 320 * engine.height / maxOf(engine.width, 1)) ?: return
            previewBusy = true
            previewExecutor.execute {
                val out = ByteArrayOutputStream()
                bitmap.compress(Bitmap.CompressFormat.JPEG, 60, out)
                bitmap.recycle()
                link.sendPreview(out.toByteArray())
                previewBusy = false
            }
        }
    }

    private fun startCamera() {
        val texture = textureView.surfaceTexture ?: return
        val quality = settings.quality
        val size = engine.chooseSize(settings.useFrontCamera, quality)
        configureTransform(size.width, size.height)
        engine.start(texture, settings.useFrontCamera, quality)
    }

    /** Camera2Basic's preview transform: undo the display rotation, then
     *  letterbox (not crop) so the preview shows exactly what's recorded. */
    private fun configureTransform(bufferWidth: Int, bufferHeight: Int) {
        val viewWidth = textureView.width.toFloat()
        val viewHeight = textureView.height.toFloat()
        if (viewWidth == 0f || viewHeight == 0f) return
        @Suppress("DEPRECATION")
        val rotation = windowManager.defaultDisplay.rotation
        val matrix = Matrix()
        val viewRect = RectF(0f, 0f, viewWidth, viewHeight)
        val bufferRect = RectF(0f, 0f, bufferHeight.toFloat(), bufferWidth.toFloat())
        val cx = viewRect.centerX()
        val cy = viewRect.centerY()
        if (rotation == Surface.ROTATION_90 || rotation == Surface.ROTATION_270) {
            bufferRect.offset(cx - bufferRect.centerX(), cy - bufferRect.centerY())
            matrix.setRectToRect(viewRect, bufferRect, Matrix.ScaleToFit.FILL)
            val scale = minOf(viewHeight / bufferHeight, viewWidth / bufferWidth)
            matrix.postScale(scale, scale, cx, cy)
            matrix.postRotate(90f * (rotation - 2), cx, cy)
        } else if (rotation == Surface.ROTATION_180) {
            matrix.postRotate(180f, cx, cy)
        }
        textureView.setTransform(matrix)
    }

    // --- server commands ---

    private fun handleCommand(msg: JSONObject) {
        val sessionId = msg.optString("session_id").ifEmpty { return }
        when (msg.optString("kind")) {
            "start_recording" -> startRecording(sessionId)
            "stop_recording" -> stopRecording(sessionId)
        }
    }

    private fun startRecording(sessionId: String) {
        val current = recordingSession
        if (current != null) {
            if (current != sessionId) {
                link.sendJson(jsonOf("kind" to "recording_failed", "session_id" to sessionId,
                    "error" to "Already recording session $current."))
            }
            return
        }
        recordingSession = sessionId
        recordingSince = SystemClock.elapsedRealtime()
        engine.startRecording(
            sessionId, store.videoFile(sessionId),
            onStarted = { pts ->
                link.sendJson(jsonOf("kind" to "recording_started", "session_id" to sessionId, "video_start_pts_ns" to pts))
            },
            onFailed = { error ->
                main.post { if (recordingSession == sessionId) recordingSession = null }
                link.sendJson(jsonOf("kind" to "recording_failed", "session_id" to sessionId, "error" to error))
            },
        )
        link.sendStatus()
    }

    private fun stopRecording(sessionId: String) {
        if (recordingSession != sessionId) return
        recordingSession = null
        val host = settings.serverHost
        val clock = link.clockOffset
        engine.stopRecording(sessionId) { result ->
            result.onSuccess { r ->
                store.save(PendingRecording(
                    sessionId, host, r.videoStartPtsNs, clock?.first, clock?.second,
                    r.width, r.height, engine.fps, r.duration,
                ))
                link.sendJson(jsonOf(
                    "kind" to "recording_stopped", "session_id" to sessionId, "file_size" to r.file.length(),
                    "video_start_pts_ns" to r.videoStartPtsNs, "duration_s" to r.duration,
                ))
            }.onFailure { e ->
                link.sendJson(jsonOf("kind" to "recording_failed", "session_id" to sessionId,
                    "error" to (e.message ?: "Recording failed.")))
            }
            main.post { refreshUploadText() }
            uploader.kick()
        }
        link.sendStatus()
    }

    private fun statusFields(): JSONObject {
        val battery = registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        val level = battery?.getIntExtra(BatteryManager.EXTRA_LEVEL, -1) ?: -1
        val scale = battery?.getIntExtra(BatteryManager.EXTRA_SCALE, -1) ?: -1
        val plugged = (battery?.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0) ?: 0) != 0
        return jsonOf(
            "width" to engine.width, "height" to engine.height, "fps" to engine.fps,
            "camera" to if (engine.front) "front" else "back",
            "battery" to if (level >= 0 && scale > 0) level.toDouble() / scale else null,
            "charging" to plugged,
            "free_bytes" to store.directory.usableSpace,
            "recording_session" to engine.recordingSession,
            "pending_uploads" to store.all().count { it.rejected == null },
        )
    }

    // --- UI ---

    private fun showState(state: LinkState) {
        connectionText.text = when (state) {
            LinkState.Idle -> "○ Not connected — set the server in Settings"
            LinkState.Connecting -> "◌ Connecting…"
            LinkState.AwaitingApproval -> "◌ Approve this phone on the takeloom server"
            is LinkState.Connected -> "● Connected to ${state.hostname}"
            is LinkState.Failed -> "✕ ${state.error}"
        }
        connectionText.setTextColor(when (state) {
            is LinkState.Connected -> Color.rgb(120, 230, 120)
            is LinkState.Failed -> Color.rgb(255, 120, 120)
            else -> Color.WHITE
        })
    }

    private fun refreshUploadText() {
        val pending = store.all()
        val text = uploadStatus ?: when {
            pending.any { it.rejected != null } -> "${pending.count { it.rejected != null }} recording(s) refused by the server — see Settings"
            pending.isNotEmpty() -> "${pending.size} recording(s) waiting to upload"
            else -> null
        }
        uploadText.text = text ?: ""
        uploadText.visibility = if (text == null) View.GONE else View.VISIBLE
    }

    private fun pill(view: TextView) = view.apply {
        setTextColor(Color.WHITE)
        textSize = 15f
        setPadding(dp(12), dp(8), dp(12), dp(8))
        background = GradientDrawable().apply {
            cornerRadius = dp(20).toFloat()
            setColor(Color.argb(140, 0, 0, 0))
        }
    }

    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()

    private fun buildUi() {
        val root = FrameLayout(this).apply { setBackgroundColor(Color.BLACK) }
        textureView = TextureView(this)
        textureView.surfaceTextureListener = object : TextureView.SurfaceTextureListener {
            override fun onSurfaceTextureAvailable(surface: SurfaceTexture, width: Int, height: Int) {
                if (checkSelfPermission(Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) startCamera()
            }
            override fun onSurfaceTextureSizeChanged(surface: SurfaceTexture, width: Int, height: Int) =
                configureTransform(engine.width, engine.height)
            override fun onSurfaceTextureDestroyed(surface: SurfaceTexture) = true
            override fun onSurfaceTextureUpdated(surface: SurfaceTexture) {}
        }
        root.addView(textureView, FrameLayout.LayoutParams(-1, -1))

        connectionText = pill(TextView(this))
        root.addView(connectionText, FrameLayout.LayoutParams(-2, -2, Gravity.TOP or Gravity.START).apply {
            setMargins(dp(16), dp(16), dp(16), 0)
        })
        recordingText = pill(TextView(this)).apply { setTextColor(Color.rgb(255, 80, 80)); visibility = View.GONE }
        root.addView(recordingText, FrameLayout.LayoutParams(-2, -2, Gravity.TOP or Gravity.END).apply {
            setMargins(0, dp(16), dp(16), 0)
        })
        uploadText = pill(TextView(this)).apply { visibility = View.GONE }
        root.addView(uploadText, FrameLayout.LayoutParams(-2, -2, Gravity.BOTTOM or Gravity.START).apply {
            setMargins(dp(16), 0, 0, dp(16))
        })
        val settingsButton = Button(this).apply {
            text = "⚙"
            textSize = 22f
            setTextColor(Color.WHITE)
            background = GradientDrawable().apply { shape = GradientDrawable.OVAL; setColor(Color.argb(140, 0, 0, 0)) }
            setOnClickListener { showSettings() }
        }
        root.addView(settingsButton, FrameLayout.LayoutParams(dp(56), dp(56), Gravity.BOTTOM or Gravity.END).apply {
            setMargins(0, 0, dp(16), dp(16))
        })
        setContentView(root)
        showState(LinkState.Idle)
    }

    private fun showSettings() {
        val pad = dp(20)
        val form = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad / 2, pad, 0)
        }
        val hostField = EditText(this).apply {
            hint = "Server address (e.g. 192.168.1.20)"
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
            setText(settings.serverHost)
        }
        val nameField = EditText(this).apply {
            hint = "This phone's name"
            setText(settings.deviceName)
        }
        val cameraGroup = RadioGroup(this).apply { orientation = RadioGroup.HORIZONTAL }
        val back = RadioButton(this).apply { text = "Back camera"; id = View.generateViewId() }
        val front = RadioButton(this).apply { text = "Front camera"; id = View.generateViewId() }
        cameraGroup.addView(back)
        cameraGroup.addView(front)
        cameraGroup.check(if (settings.useFrontCamera) front.id else back.id)
        val qualityGroup = RadioGroup(this).apply { orientation = RadioGroup.HORIZONTAL }
        val qualityButtons = VideoQuality.values().map { q ->
            RadioButton(this).apply { text = q.label; id = View.generateViewId() }.also { qualityGroup.addView(it) }
        }
        qualityGroup.check(qualityButtons[settings.quality.ordinal].id)
        form.addView(hostField)
        form.addView(Button(this).apply {
            text = "Scan for server"
            setOnClickListener { button ->
                button.isEnabled = false
                text = "Scanning the network…"
                scanForServer { found ->
                    button.isEnabled = true
                    text = if (found.isEmpty()) "No server found — scan again" else "Scan for server"
                    when (found.size) {
                        0 -> {}
                        1 -> hostField.setText(found[0].host)
                        else -> AlertDialog.Builder(this@MainActivity)
                            .setTitle("Which takeloom server?")
                            .setItems(found.map { "${it.hostname} (${it.address})" }.toTypedArray()) { _, i ->
                                hostField.setText(found[i].host)
                            }
                            .show()
                    }
                }
            }
        })
        form.addView(nameField)
        form.addView(cameraGroup)
        form.addView(qualityGroup)
        val recording = recordingSession != null
        if (recording) {
            form.addView(TextView(this).apply { text = "Camera settings can't change while a session is recording." })
            cameraGroup.isEnabled = false
            qualityGroup.isEnabled = false
        }
        for (r in store.all()) {
            form.addView(TextView(this).apply {
                text = "Not yet on the server: ${r.sessionId}" + (r.rejected?.let { "\n  Refused: $it" } ?: "")
                setPadding(0, dp(8), 0, 0)
            })
            if (r.rejected != null) {
                form.addView(LinearLayout(this).apply {
                    addView(Button(this@MainActivity).apply {
                        text = "Try again"
                        setOnClickListener { store.save(r.copy(rejected = null)); uploader.kick(); refreshUploadText() }
                    })
                    addView(Button(this@MainActivity).apply {
                        text = "Delete"
                        setOnClickListener { store.remove(r.sessionId); refreshUploadText(); isEnabled = false }
                    })
                })
            }
        }

        form.addView(TextView(this).apply {
            text = "Takeloom Camera ${settings.appVersion}"
            setPadding(0, 32, 0, 0)
        })

        AlertDialog.Builder(this)
            .setTitle("Settings")
            .setView(android.widget.ScrollView(this).apply { addView(form) })
            .setNegativeButton("Cancel", null)
            .setPositiveButton("Save") { _, _ ->
                settings.deviceName = nameField.text.toString().trim().ifEmpty { settings.deviceName }
                val host = hostField.text.toString().trim()
                settings.serverHost = host
                val wantFront = cameraGroup.checkedRadioButtonId == front.id
                val wantQuality = VideoQuality.values()[qualityButtons.indexOfFirst { it.id == qualityGroup.checkedRadioButtonId }.coerceAtLeast(0)]
                if (!recording && (wantFront != settings.useFrontCamera || wantQuality != settings.quality)) {
                    settings.useFrontCamera = wantFront
                    settings.quality = wantQuality
                    // The activity's landscape lock depends on the camera
                    // (see CameraEngine.requiredOrientation); recreating it
                    // reopens the camera with the new settings.
                    engine.stop()
                    recreate()
                    return@setPositiveButton
                }
                if (host.isEmpty()) link.disconnect() else link.connect(host)
            }
            .show()
    }
}
