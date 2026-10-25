plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.priestc.takeloomcamera"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.priestc.takeloomcamera"
        // 29: MediaFormat.KEY_MAX_B_FRAMES, and AudioRecord timestamps on
        // the boot-time clock camera sensors often use.
        minSdk = 29
        targetSdk = 34
        versionCode = 1
        versionName = "1.0"
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
