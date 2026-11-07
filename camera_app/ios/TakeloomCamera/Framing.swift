import AVFoundation
import CoreImage

/// Zoom and pan, as the server sends them (takeloom/phone_camera/framing.py):
/// `zoom` like the Camera app (1 = main lens, 0.5 = ultra-wide), `x`/`y` the
/// middle of the picture as fractions of the widest view.
struct Framing: Equatable {
    var zoom: Double = 1
    var x: Double = 0.5
    var y: Double = 0.5

    var dictionary: [String: Any] { ["zoom": zoom, "x": x, "y": y] }
}

/// What this camera can do, in Framing's units — the status's "zoom_caps".
struct ZoomCaps {
    /// The device's videoZoomFactor for the main (1x) lens: the ultra-wide
    /// is the virtual device's 1.0, so the main lens is its first switch-over.
    let mainFactor: Double
    let min: Double
    let max: Double
    let lenses: [Double]

    init(device: AVCaptureDevice) {
        let switchOvers = device.virtualDeviceSwitchOverVideoZoomFactors.map { $0.doubleValue }
        let hasUltraWide = device.constituentDevices.contains { $0.deviceType == .builtInUltraWideCamera }
        let main = hasUltraWide ? (switchOvers.first ?? 1) : 1
        let widest = Double(device.minAvailableVideoZoomFactor) / main
        var lenses = hasUltraWide ? [widest, 1] : [1]
        lenses += switchOvers.dropFirst(hasUltraWide ? 1 : 0).map { $0 / main }
        mainFactor = main
        min = widest
        self.lenses = lenses
        // Past twice the longest lens it's all the camera's own digital zoom.
        max = Swift.min(Double(device.maxAvailableVideoZoomFactor) / main, Swift.max(10, (lenses.last ?? 1) * 2))
    }

    var dictionary: [String: Any] { ["min": min, "max": max, "lenses": lenses, "pan": true] }

    /// Split a framing into what the camera does (a centered zoom, optical
    /// where a lens allows) and a crop of its frames for the rest.
    /// `previous` is the plan in force: a pan alone keeps the camera's zoom
    /// whenever the new region still fits in its view, so panning is a crop
    /// only, instant and smooth, instead of the camera re-zooming (a few
    /// frames late, so the picture jumps) on every step. When it no longer
    /// fits, the camera zooms out with room to spare for the next steps. A
    /// zoom change, or a centered shot, sets the camera's zoom to fit
    /// exactly: all optical. Same rule as the Android app's ZoomCaps.plan.
    func plan(_ framing: Framing, previous: FramingPlan?) -> FramingPlan {
        let zoom = Swift.min(Swift.max(framing.zoom, min), max)
        let visible = min / zoom  // of the widest view
        let limit = (1 - visible) / 2
        let dx = Swift.min(Swift.max(framing.x - 0.5, -limit), limit)
        let dy = Swift.min(Swift.max(framing.y - 0.5, -limit), limit)
        let need = Swift.min(1, visible + 2 * Swift.max(abs(dx), abs(dy)))
        let centered = abs(dx) < 1e-6 && abs(dy) < 1e-6
        let cameraView: Double
        if let previous, abs(previous.zoom - zoom) < 1e-9, !centered {
            cameraView = need <= previous.cameraView + 1e-9 ? previous.cameraView : Swift.min(1, need + visible)
        } else {
            cameraView = need
        }
        let factor = CGFloat(min / cameraView * mainFactor)
        let crop = CropRect(size: visible / cameraView, x: 0.5 + dx / cameraView, y: 0.5 + dy / cameraView)
        return FramingPlan(factor: factor, crop: crop, cameraView: cameraView, zoom: zoom)
    }
}

/// How a framing is carried out (ZoomCaps.plan): the camera's
/// videoZoomFactor, the crop of its frames, and how much of the widest view
/// the camera itself shows.
struct FramingPlan: Equatable {
    var factor: CGFloat
    var crop: CropRect
    var cameraView: Double
    var zoom: Double
}

struct CropRect: Equatable {
    var size: Double = 1  // of the frame's width and height
    var x: Double = 0.5   // middle, from the left
    var y: Double = 0.5   // middle, from the top

    var isFull: Bool { size > 0.999 }
}

/// Crops and scales frames back up to their own size on the GPU, for a pan
/// (CameraPipeline feeds the result to both encoders and the preview).
final class FrameCropper {
    // No color management: a YUV buffer in, the same values out.
    private let context = CIContext(options: [.workingColorSpace: NSNull(), .outputColorSpace: NSNull()])
    private var pool: CVPixelBufferPool?
    private var poolSize = (0, 0)

    func crop(_ source: CVPixelBuffer, to rect: CropRect) -> CVPixelBuffer? {
        let width = CVPixelBufferGetWidth(source), height = CVPixelBufferGetHeight(source)
        guard let out = makeBuffer(width: width, height: height,
                                   format: CVPixelBufferGetPixelFormatType(source)) else { return nil }
        let w = Double(width), h = Double(height)
        // CoreImage's origin is the bottom left.
        let region = CGRect(x: (rect.x - rect.size / 2) * w, y: (1 - rect.y - rect.size / 2) * h,
                            width: rect.size * w, height: rect.size * h)
        let image = CIImage(cvPixelBuffer: source)
            .cropped(to: region)
            .transformed(by: CGAffineTransform(translationX: -region.minX, y: -region.minY))
            .transformed(by: CGAffineTransform(scaleX: w / region.width, y: h / region.height))
        context.render(image, to: out, bounds: CGRect(x: 0, y: 0, width: w, height: h), colorSpace: nil)
        CVBufferPropagateAttachments(source, out)
        return out
    }

    private func makeBuffer(width: Int, height: Int, format: OSType) -> CVPixelBuffer? {
        if pool == nil || poolSize != (width, height) {
            let attributes: [String: Any] = [
                kCVPixelBufferPixelFormatTypeKey as String: format,
                kCVPixelBufferWidthKey as String: width,
                kCVPixelBufferHeightKey as String: height,
                kCVPixelBufferIOSurfacePropertiesKey as String: [:] as [String: Any],
            ]
            pool = nil
            CVPixelBufferPoolCreate(nil, nil, attributes as CFDictionary, &pool)
            poolSize = (width, height)
        }
        guard let pool else { return nil }
        var buffer: CVPixelBuffer?
        CVPixelBufferPoolCreatePixelBuffer(nil, pool, &buffer)
        return buffer
    }
}
