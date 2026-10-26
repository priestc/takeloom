plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// The camera app's one version number, shared with iOS — see camera_app/VERSION
// and `takeloom make-release --camera`. versionCode must only ever increase:
// 1.2.3 -> 10203.
val cameraAppVersion = rootProject.file("../VERSION").readText().trim()
val cameraAppVersionCode = cameraAppVersion.split(".").map { it.toInt() }
    .let { (major, minor, patch) -> major * 10000 + minor * 100 + patch }

android {
    namespace = "com.priestc.takeloomcamera"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.priestc.takeloomcamera"
        // 29: MediaFormat.KEY_MAX_B_FRAMES, and AudioRecord timestamps on
        // the boot-time clock camera sensors often use.
        minSdk = 29
        targetSdk = 34
        versionCode = cameraAppVersionCode
        versionName = cameraAppVersion
    }

    buildFeatures {
        buildConfig = true
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
}
