import Foundation
import UIKit

/// Persisted app settings (UserDefaults).
enum Settings {
    private static let defaults = UserDefaults.standard

    static var serverHost: String {
        get { defaults.string(forKey: "serverHost") ?? "" }
        set { defaults.set(newValue, forKey: "serverHost") }
    }

    /// Name shown on the server (pairing prompt, camera picker).
    static var deviceName: String {
        get { defaults.string(forKey: "deviceName") ?? UIDevice.current.name }
        set { defaults.set(newValue, forKey: "deviceName") }
    }

    /// Stable per-install identity; the server keys a camera on it.
    static var deviceID: String {
        if let id = defaults.string(forKey: "deviceID") { return id }
        let id = UUID().uuidString.lowercased()
        defaults.set(id, forKey: "deviceID")
        return id
    }

    static func token(for host: String) -> String {
        defaults.string(forKey: "token.\(host)") ?? ""
    }

    static func setToken(_ token: String, for host: String) {
        defaults.set(token, forKey: "token.\(host)")
    }

    static var quality: VideoQuality {
        get { VideoQuality(rawValue: defaults.string(forKey: "quality") ?? "") ?? .hd1080 }
        set { defaults.set(newValue.rawValue, forKey: "quality") }
    }

    static var useFrontCamera: Bool {
        get { defaults.bool(forKey: "useFrontCamera") }
        set { defaults.set(newValue, forKey: "useFrontCamera") }
    }

    static var appVersion: String {
        Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "?"
    }
}

enum VideoQuality: String, CaseIterable, Identifiable {
    case hd720 = "720p"
    case hd1080 = "1080p"
    case uhd4k = "4K"

    var id: String { rawValue }

    var dimensions: (width: Int32, height: Int32) {
        switch self {
        case .hd720: return (1280, 720)
        case .hd1080: return (1920, 1080)
        case .uhd4k: return (3840, 2160)
        }
    }

    /// Bits/s for the one encode that feeds both the live stream and the
    /// recorded file — high enough to be a keeper, low enough for Wi-Fi.
    var bitrate: Int {
        switch self {
        case .hd720: return 6_000_000
        case .hd1080: return 12_000_000
        case .uhd4k: return 35_000_000
        }
    }
}
