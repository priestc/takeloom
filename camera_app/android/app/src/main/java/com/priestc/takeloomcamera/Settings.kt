package com.priestc.takeloomcamera

import android.content.Context
import android.os.Build
import java.util.UUID

enum class VideoQuality(val label: String, val width: Int, val height: Int, val bitrate: Int) {
    HD720("720p", 1280, 720, 6_000_000),
    HD1080("1080p", 1920, 1080, 12_000_000),
    UHD4K("4K", 3840, 2160, 35_000_000),
}

/** Persisted app settings (SharedPreferences). */
class Settings(context: Context) {
    private val prefs = context.getSharedPreferences("takeloom_camera", Context.MODE_PRIVATE)
    private val resolver = context.contentResolver

    var serverHost: String
        get() = prefs.getString("serverHost", "") ?: ""
        set(value) = prefs.edit().putString("serverHost", value).apply()

    /** Name shown on the server (pairing prompt, camera picker). */
    var deviceName: String
        get() = prefs.getString("deviceName", null) ?: deviceModel
        set(value) = prefs.edit().putString("deviceName", value).apply()

    /**
     * The phone's model, readable: the device's own marketing name where
     * Android has one ("Galaxy S23 Ultra"), else manufacturer + model
     * ("Google Pixel 8 Pro"). Sent as the hello's device_model.
     */
    val deviceModel: String
        get() {
            val marketing = android.provider.Settings.Global.getString(resolver, "device_name")
            if (!marketing.isNullOrBlank()) return marketing
            val manufacturer = Build.MANUFACTURER.replaceFirstChar { it.uppercase() }
            return if (Build.MODEL.startsWith(Build.MANUFACTURER, ignoreCase = true)) Build.MODEL
            else "$manufacturer ${Build.MODEL}"
        }

    /** Stable per-install identity; the server keys a camera on it. */
    val deviceId: String
        get() = prefs.getString("deviceId", null) ?: UUID.randomUUID().toString().also {
            prefs.edit().putString("deviceId", it).apply()
        }

    fun token(host: String): String = prefs.getString("token.$host", "") ?: ""
    fun setToken(host: String, token: String) = prefs.edit().putString("token.$host", token).apply()

    var quality: VideoQuality
        get() = VideoQuality.values().firstOrNull { it.name == prefs.getString("quality", null) } ?: VideoQuality.HD1080
        set(value) = prefs.edit().putString("quality", value.name).apply()

    var useFrontCamera: Boolean
        get() = prefs.getBoolean("useFrontCamera", false)
        set(value) = prefs.edit().putBoolean("useFrontCamera", value).apply()

    // camera_app/VERSION, via the build (app/build.gradle.kts).
    val appVersion: String = BuildConfig.VERSION_NAME
}
