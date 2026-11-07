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
import android.content.pm.ActivityInfo
import android.hardware.display.DisplayManager
import android.os.BatteryManager
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.text.InputType
import android.view.Gravity
import android.view.MotionEvent
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
    /** Battery, and how long it'll last by the server's learned drain rate. */
    private lateinit var batteryText: TextView
    @Volatile private var batteryMinutesLeft: Int? = null
    private var batteryTicks = 0
    private val main = Handler(Looper.getMainLooper())
    private val previewExecutor = Executors.newSingleThreadExecutor()
    @Volatile private var previewBusy = false

    private var recordingSession: String? = null
    private var recordingSince = 0L
    private var uploadStatus: String? = null
    private var lastRotation = -1
    private lateinit var mountSleep: MountSleep
    /** Black over everything while [mountSleep] has the screen "off". */
    private lateinit var sleepCover: View
    /** The lens buttons along the bottom (one per [CameraEngine.lenses]). */
    private lateinit var lensBar: LinearLayout
    private var lensBarShows: Triple<List<Double>, Double, Boolean>? = null
    private lateinit var framingGuide: FramingGuide

    /** A turn from one landscape to the other is a 180° display change with
     *  no configuration change, so it's only seen here. */
    private val displayListener = object : DisplayManager.DisplayListener {
        override fun onDisplayAdded(displayId: Int) {}
        override fun onDisplayRemoved(displayId: Int) {}
        override fun onDisplayChanged(displayId: Int) = onRotation()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        settings = Settings(this)
        store = RecordingStore(getExternalFilesDir(null) ?: filesDir)
        engine = CameraEngine(this)
        link = ServerLink(settings)
        uploader = Uploader(store, settings)
        // Either landscape: the mount holds the phone with the camera bump
        // at the top or the bottom, and the frames are turned to match.
        requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_SENSOR_LANDSCAPE
        buildUi()
        mountSleep = MountSleep(this, onSleep = { setScreenAsleep(true) }, onWake = { setScreenAsleep(false) })
        wire()
        getSystemService(DisplayManager::class.java).registerDisplayListener(displayListener, main)
        onRotation()

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

    /** Which landscape the phone is in now: re-fit the preview, and tell the
     *  engine whether the sensor's frames need turning 180° to be upright. */
    private fun onRotation() {
        @Suppress("DEPRECATION")
        val rotation = windowManager.defaultDisplay.rotation
        if (rotation == lastRotation) return
        lastRotation = rotation
        val uprightRotation =
            if (engine.uprightOrientation(settings.useFrontCamera) == ActivityInfo.SCREEN_ORIENTATION_REVERSE_LANDSCAPE)
                Surface.ROTATION_270 else Surface.ROTATION_90
        engine.setUpsideDown(rotation != uprightRotation)
        configureTransform(engine.width, engine.height)
    }

    override fun onResume() {
        super.onResume()
        mountSleep.start()
    }

    override fun onPause() {
        mountSleep.stop()
        super.onPause()
    }

    /** A tap while blanked only wakes the screen; it doesn't reach a button. */
    override fun dispatchTouchEvent(ev: MotionEvent): Boolean {
        if (ev.actionMasked == MotionEvent.ACTION_DOWN) {
            val wasAsleep = mountSleep.asleep
            mountSleep.touched()
            if (wasAsleep) {
                swallowGesture = true
                return true
            }
            swallowGesture = false
        }
        if (swallowGesture) return true
        return super.dispatchTouchEvent(ev)
    }

    private var swallowGesture = false

    /** In the mount: black screen at the lowest backlight. The camera, live
     *  feed and server preview JPEGs (taken from the TextureView underneath)
     *  carry on regardless. */
    private fun setScreenAsleep(asleep: Boolean) {
        sleepCover.visibility = if (asleep) View.VISIBLE else View.GONE
        window.attributes = window.attributes.apply {
            screenBrightness = if (asleep) WindowManager.LayoutParams.BRIGHTNESS_OVERRIDE_OFF
            else WindowManager.LayoutParams.BRIGHTNESS_OVERRIDE_NONE
        }
    }

    override fun onDestroy() {
        getSystemService(DisplayManager::class.java).unregisterDisplayListener(displayListener)
        engine.stop()
        link.disconnect()
        super.onDestroy()
    }

    // --- wiring ---

    private fun wire() {
        link.nowNs = { engine.nowNs() }
        link.onNeedKeyframe = { engine.requestKeyframe() }
        link.onLiveQuality = { _, bitrate -> if (bitrate > 0) engine.setLiveQuality(bitrate) }
        link.onFraming = { framing -> engine.setFraming(framing) }
        link.onBatteryEstimate = { minutes -> main.post { batteryMinutesLeft = minutes; refreshBattery() } }
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
        refreshLensBar()
        if (batteryTicks++ % 25 == 0) refreshBattery()  // every 5 s
        recordingText.visibility = if (recordingSession != null) View.VISIBLE else View.GONE
        if (recordingSession != null) {
            val s = (SystemClock.elapsedRealtime() - recordingSince) / 1000
            recordingText.text = String.format("● REC %d:%02d:%02d", s / 3600, s / 60 % 60, s % 60)
        }
        if (!previewBusy) {
            // From the video pipeline, not the TextureView: getBitmap()
            // leaves out the view's rotation, so those came out sideways.
            val bitmap = engine.takePreview() ?: return
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
        engine.setGuide(false)  // a recording is always through the chosen lens
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

    /** "🔋 64% · 3h 10m left", "⚡ 64% · charging", or just the level
     *  until the server has learned this phone's drain rate. */
    private fun refreshBattery() {
        val battery = registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        val level = battery?.getIntExtra(BatteryManager.EXTRA_LEVEL, -1) ?: -1
        val scale = battery?.getIntExtra(BatteryManager.EXTRA_SCALE, -1) ?: -1
        val plugged = (battery?.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0) ?: 0) != 0
        if (level < 0 || scale <= 0) {
            batteryText.visibility = View.GONE
            return
        }
        val percent = level * 100 / scale
        val minutes = batteryMinutesLeft
        batteryText.text = when {
            plugged -> "⚡ $percent% · charging"
            minutes != null && minutes >= 60 -> "🔋 $percent% · ${minutes / 60}h ${String.format("%02d", minutes % 60)}m left"
            minutes != null -> "🔋 $percent% · ${minutes}m left"
            else -> "🔋 $percent%"
        }
        val low = !plugged && (percent < 20 || (minutes != null && minutes < 30))
        batteryText.setTextColor(if (low) Color.rgb(255, 107, 107) else Color.WHITE)
        batteryText.visibility = View.VISIBLE
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
            // The live feed, apart from the recording (bandwidth.py on the server).
            "live_adaptive" to engine.liveAdaptive,
            "live_width" to engine.liveWidth, "live_height" to engine.liveHeight,
            "live_bitrate" to engine.liveBitrate,
            "framing" to engine.framing.json(),
            "zoom_caps" to engine.zoomCapsJson(),
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

    /** Rebuilt only when the lenses or the current one change (tick, 5 Hz). */
    private fun refreshLensBar() {
        val zooms = engine.lenses.map { it.zoom }
        val current = zooms.minByOrNull { Math.abs(Math.log(maxOf(engine.framing.zoom, 1e-6) / it)) } ?: 1.0
        val guideOn = engine.guide
        val shows = Triple(zooms, current, guideOn)
        if (shows == lensBarShows) return
        lensBarShows = shows
        framingGuide.lenses = zooms
        framingGuide.selected = current
        framingGuide.aspect = engine.width.toFloat() / maxOf(engine.height, 1)
        framingGuide.visibility = if (guideOn) View.VISIBLE else View.GONE
        lensBar.removeAllViews()
        if (zooms.size < 2) return  // one lens: nothing to choose
        val guideButton = pill(TextView(this)).apply {
            text = "Guide"
            if (guideOn) {
                setTextColor(Color.BLACK)
                (background as GradientDrawable).setColor(Color.WHITE)
            }
            setOnClickListener { engine.setGuide(!engine.guide) }
        }
        lensBar.addView(guideButton, LinearLayout.LayoutParams(-2, -2).apply { setMargins(dp(4), 0, dp(12), 0) })
        for (zoom in zooms) {
            val button = pill(TextView(this)).apply {
                text = FramingGuide.lensLabel(zoom)
                if (zoom == current) {
                    setTextColor(Color.BLACK)
                    (background as GradientDrawable).setColor(Color.argb(230, 255, 214, 10))
                }
                setOnClickListener { chooseLens(zoom) }
            }
            lensBar.addView(button, LinearLayout.LayoutParams(-2, -2).apply { setMargins(dp(4), 0, dp(4), 0) })
        }
    }

    /** A lens tapped on screen: switch now, and tell the server, which
     *  remembers it and shows it in video-ui (and answers with the same). */
    private fun chooseLens(zoom: Double) {
        engine.setFraming(Framing(zoom))
        link.sendJson(jsonOf("kind" to "framing", "zoom" to zoom, "x" to 0.5, "y" to 0.5))
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
        // Over the preview, under the controls: drawn while the guide's on.
        framingGuide = FramingGuide(this).apply { visibility = View.GONE }
        root.addView(framingGuide, FrameLayout.LayoutParams(-1, -1))

        connectionText = pill(TextView(this))
        root.addView(connectionText, FrameLayout.LayoutParams(-2, -2, Gravity.TOP or Gravity.START).apply {
            setMargins(dp(16), dp(16), dp(16), 0)
        })
        recordingText = pill(TextView(this)).apply { setTextColor(Color.rgb(255, 80, 80)); visibility = View.GONE }
        batteryText = pill(TextView(this)).apply { visibility = View.GONE }
        val topRight = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.END
            addView(recordingText, LinearLayout.LayoutParams(-2, -2).apply { setMargins(0, 0, 0, dp(8)) })
            addView(batteryText, LinearLayout.LayoutParams(-2, -2))
        }
        root.addView(topRight, FrameLayout.LayoutParams(-2, -2, Gravity.TOP or Gravity.END).apply {
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
        lensBar = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        root.addView(lensBar, FrameLayout.LayoutParams(-2, -2, Gravity.BOTTOM or Gravity.CENTER_HORIZONTAL).apply {
            setMargins(dp(16), dp(16), dp(16), dp(16))
        })
        sleepCover = View(this).apply {
            setBackgroundColor(Color.BLACK)
            visibility = View.GONE
            // Buttons are raised (elevation), so they'd show through a flat cover.
            elevation = dp(100).toFloat()
        }
        root.addView(sleepCover, FrameLayout.LayoutParams(-1, -1))
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
                    // Which landscape is upright depends on the camera
                    // (see CameraEngine.uprightOrientation); recreating the
                    // activity reopens the camera with the new settings.
                    engine.stop()
                    recreate()
                    return@setPositiveButton
                }
                if (host.isEmpty()) link.disconnect() else link.connect(host)
            }
            .show()
    }
}
