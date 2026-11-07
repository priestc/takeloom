import CoreMotion
import UIKit

/// Blanks the screen once the phone is in the mount rather than in a hand.
///
/// A phone on the wall mount keeps exactly the same angle to gravity; one in
/// a hand drifts by more than a couple of degrees within seconds, however
/// steady the hand. So: once the accelerometer's gravity vector has stayed
/// within `moveG` of where it settled for `stillSeconds`, `asleep` turns on;
/// any movement past that again (the phone taken down), or a `touched()`,
/// turns it off.
///
/// "Asleep" is a black screen at the lowest brightness, not the real display
/// sleep: iOS stops the camera when the screen locks, and the camera has to
/// keep running for the live feed and any recording. Same logic as the
/// Android app's MountSleep.kt.
@MainActor
final class MountSleep: ObservableObject {
    @Published private(set) var asleep = false

    private static let moveG = 0.035  // about 2° of tilt (sin 2° ≈ 0.035 g)
    private static let stillSeconds: TimeInterval = 20

    private let motion = CMMotionManager()
    private var ref: (Double, Double, Double)?
    private var smoothed: (Double, Double, Double)?
    private var stillSince = Date()
    private var lastTouch = Date.distantPast
    /// The user's own brightness, put back on waking.
    private var savedBrightness: CGFloat?

    func start() {
        guard motion.isAccelerometerAvailable, !motion.isAccelerometerActive else { return }
        stillSince = Date()
        motion.accelerometerUpdateInterval = 0.1
        motion.startAccelerometerUpdates(to: .main) { [weak self] data, _ in
            guard let a = data?.acceleration else { return }
            MainActor.assumeIsolated { self?.sample(a) }
        }
    }

    func stop() {
        motion.stopAccelerometerUpdates()
        ref = nil
        smoothed = nil
        wake()
    }

    /// The screen was touched: wake, and stay awake at least `stillSeconds`.
    func touched() {
        lastTouch = Date()
        wake()
    }

    private func sample(_ a: CMAcceleration) {
        // A light low-pass: sensor noise out, a real tilt kept.
        let s: (Double, Double, Double)
        if let p = smoothed {
            s = (p.0 * 0.8 + a.x * 0.2, p.1 * 0.8 + a.y * 0.2, p.2 * 0.8 + a.z * 0.2)
        } else {
            s = (a.x, a.y, a.z)
        }
        smoothed = s
        let now = Date()
        guard let r = ref, distance(r, s) <= Self.moveG else {
            ref = s
            stillSince = now
            wake()
            return
        }
        if !asleep, now.timeIntervalSince(stillSince) >= Self.stillSeconds,
           now.timeIntervalSince(lastTouch) >= Self.stillSeconds {
            asleep = true
            savedBrightness = UIScreen.main.brightness
            UIScreen.main.brightness = 0
        }
    }

    private func wake() {
        guard asleep else { return }
        asleep = false
        if let b = savedBrightness {
            UIScreen.main.brightness = b
            savedBrightness = nil
        }
    }

    private func distance(_ a: (Double, Double, Double), _ b: (Double, Double, Double)) -> Double {
        let dx = a.0 - b.0, dy = a.1 - b.1, dz = a.2 - b.2
        return (dx * dx + dy * dy + dz * dz).squareRoot()
    }
}
