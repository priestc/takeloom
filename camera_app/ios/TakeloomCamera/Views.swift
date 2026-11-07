import AVFoundation
import SwiftUI

@main
struct TakeloomCameraApp: App {
    @StateObject private var model = AppModel()
    @StateObject private var mountSleep = MountSleep()
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(model)
                .environmentObject(mountSleep)
                .onAppear { model.startCamera() }
        }
        .onChange(of: scenePhase) { _, phase in
            // Only while the app's on screen; leaving it puts the user's
            // brightness straight back.
            if phase == .active { mountSleep.start() } else { mountSleep.stop() }
        }
    }
}

struct ContentView: View {
    @EnvironmentObject var model: AppModel
    @EnvironmentObject var mountSleep: MountSleep
    @State private var showingSettings = false

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()
            PreviewView(layer: model.pipeline.previewLayer).ignoresSafeArea()
            if model.guide {
                FramingGuide(lenses: model.lenses, selected: model.currentLens).ignoresSafeArea()
            }
            VStack {
                HStack(alignment: .top) {
                    ConnectionPill(state: model.linkState)
                    Spacer()
                    VStack(alignment: .trailing, spacing: 8) {
                        if let since = model.recordingSince {
                            RecordingPill(since: since)
                        }
                        if let level = model.batteryLevel {
                            BatteryPill(level: level, charging: model.charging, minutesLeft: model.batteryMinutesLeft)
                        }
                    }
                }
                Spacer()
                if let error = model.cameraError {
                    Text(error).padding(8).background(.red.opacity(0.85), in: Capsule()).foregroundStyle(.white)
                }
                HStack {
                    if model.scanning && model.serverHost.isEmpty {
                        Label("Looking for the takeloom server…", systemImage: "antenna.radiowaves.left.and.right")
                            .pillStyle()
                    } else if let status = model.uploadStatus {
                        Label(status, systemImage: "arrow.up.circle").pillStyle()
                    } else if !model.pending.isEmpty {
                        Label("\(model.pending.count) recording(s) waiting to upload", systemImage: "tray.full")
                            .pillStyle()
                    }
                    Spacer()
                    if model.lenses.count > 1 {
                        Button { model.toggleGuide() } label: {
                            Text("Guide").font(.subheadline.bold()).padding(.horizontal, 14).frame(minHeight: 36)
                                .foregroundStyle(model.guide ? .black : .white)
                                .background(model.guide ? Color.white : Color.black.opacity(0.5), in: Capsule())
                        }
                        LensButtons(lenses: model.lenses, current: model.currentLens) { model.chooseLens($0) }
                        Spacer()
                    }
                    Button {
                        showingSettings = true
                    } label: {
                        Image(systemName: "gearshape.fill").font(.title2).padding(12)
                            .background(.black.opacity(0.5), in: Circle())
                    }
                    .foregroundStyle(.white)
                }
            }
            .padding()

            if mountSleep.asleep {
                // In the mount: black over everything (the camera, live feed
                // and preview JPEGs carry on underneath). A tap only wakes it.
                Color.black.ignoresSafeArea()
                    .contentShape(Rectangle())
                    .onTapGesture { mountSleep.touched() }
            }
        }
        .simultaneousGesture(TapGesture().onEnded { mountSleep.touched() })
        .sheet(isPresented: $showingSettings) {
            SettingsView().environmentObject(model)
        }
        .alert(model.discovered.count > 1 ? "takeloom servers found" : "takeloom server found",
               isPresented: $model.discoveryPrompt) {
            ForEach(model.discovered) { server in
                Button(model.discovered.count > 1 ? "\(server.hostname) (\(server.address))" : "Connect") {
                    model.connect(to: server.host)
                }
            }
            Button("Not now", role: .cancel) {}
        } message: {
            if model.discovered.count == 1, let server = model.discovered.first {
                Text("Connect to \(server.hostname) at \(server.address)? You can change it later in Settings.")
            } else {
                Text("Which one should this camera use? You can change it later in Settings.")
            }
        }
        .alert("No takeloom server found", isPresented: $model.discoveryFoundNothing) {
            Button("Scan again") { model.scanForServer(prompt: true) }
            Button("OK", role: .cancel) {}
        } message: {
            Text("Make sure `takeloom server` is running and this phone is on the same Wi-Fi, or enter its address in Settings.")
        }
        .statusBarHidden()
        .persistentSystemOverlays(.hidden)
    }
}

struct PreviewView: UIViewRepresentable {
    let layer: AVCaptureVideoPreviewLayer

    final class HostView: UIView {
        var previewLayer: AVCaptureVideoPreviewLayer?
        override func layoutSubviews() {
            super.layoutSubviews()
            previewLayer?.frame = bounds
        }
    }

    func makeUIView(context: Context) -> HostView {
        let view = HostView()
        view.backgroundColor = .black
        view.layer.addSublayer(layer)
        view.previewLayer = layer
        return view
    }

    func updateUIView(_ uiView: HostView, context: Context) {}
}

/// The framing guide over the preview while it shows the widest lens: a
/// labelled rectangle for the frame each other lens would record, so a phone
/// can be aimed in its mount knowing what every zoom will catch. Centered:
/// the lenses sit a centimetre or so apart, which doesn't show at room
/// distances. Same as the Android app's FramingGuide.kt.
struct FramingGuide: View {
    let lenses: [Double]
    let selected: Double
    private let aspect: CGFloat = 16.0 / 9.0  // landscape video; the preview is letterboxed (.resizeAspect)

    var body: some View {
        GeometryReader { geo in
            let scale = min(geo.size.width / aspect, geo.size.height)
            let picture = CGSize(width: scale * aspect, height: scale)
            let center = CGPoint(x: geo.size.width / 2, y: geo.size.height / 2)
            if let widest = lenses.first, lenses.count > 1 {
                ForEach(lenses.dropFirst(), id: \.self) { zoom in
                    let f = widest / zoom
                    let size = CGSize(width: picture.width * f, height: picture.height * f)
                    let color: Color = zoom == selected ? .yellow : .white
                    Rectangle().stroke(color, lineWidth: zoom == selected ? 3 : 2)
                        .frame(width: size.width, height: size.height)
                        .overlay(alignment: .topLeading) {
                            Text(lensLabel(zoom)).font(.headline).foregroundStyle(color)
                                .shadow(color: .black, radius: 2).padding(6)
                        }
                        .position(center)
                }
                Text(lensLabel(widest)).font(.headline)
                    .foregroundStyle(widest == selected ? .yellow : .white).shadow(color: .black, radius: 2)
                    .position(x: center.x + picture.width / 2 - 30, y: center.y - picture.height / 2 + 20)
            }
        }
        .allowsHitTesting(false)
    }
}

func lensLabel(_ lens: Double) -> String {
    lens == lens.rounded() ? "\(Int(lens))×" : String(format: "%g×", lens)
}

/// One button per optical zoom step ("0.5×", "1×", "5×"), the current one highlighted.
struct LensButtons: View {
    let lenses: [Double]
    let current: Double
    let choose: (Double) -> Void

    var body: some View {
        HStack(spacing: 6) {
            ForEach(lenses, id: \.self) { lens in
                Button {
                    choose(lens)
                } label: {
                    Text(lensLabel(lens))
                        .font(.subheadline.bold())
                        .frame(minWidth: 36, minHeight: 36)
                        .foregroundStyle(lens == current ? .black : .white)
                        .background(lens == current ? Color.yellow : Color.black.opacity(0.5), in: Circle())
                }
            }
        }
    }
}

/// "🔋 64% · 3h 10m left", "⚡ 64% · charging", or just the level until the
/// server has learned this phone's drain rate (takeloom/phone_camera/battery.py).
struct BatteryPill: View {
    let level: Float
    let charging: Bool
    let minutesLeft: Int?

    var body: some View {
        let percent = Int((level * 100).rounded())
        let low = !charging && (percent < 20 || (minutesLeft ?? Int.max) < 30)
        // The color before pillStyle(): the one nearest the text wins.
        Text(text(percent)).foregroundStyle(low ? Color(red: 1, green: 0.42, blue: 0.42) : .white).pillStyle()
    }

    private func text(_ percent: Int) -> String {
        if charging { return "⚡ \(percent)% · charging" }
        guard let minutes = minutesLeft else { return "🔋 \(percent)%" }
        if minutes >= 60 { return "🔋 \(percent)% · \(minutes / 60)h \(String(format: "%02d", minutes % 60))m left" }
        return "🔋 \(percent)% · \(minutes)m left"
    }
}

struct ConnectionPill: View {
    let state: LinkState

    var body: some View {
        HStack(spacing: 8) {
            Circle().fill(color).frame(width: 10, height: 10)
            Text(text).lineLimit(2)
        }
        .pillStyle()
    }

    private var color: Color {
        switch state {
        case .connected: return .green
        case .connecting, .awaitingApproval: return .yellow
        case .idle: return .gray
        case .failed: return .red
        }
    }

    private var text: String {
        switch state {
        case .idle: return "Not connected — set the server in Settings"
        case .connecting: return "Connecting…"
        case .awaitingApproval: return "Approve this phone on the takeloom server"
        case .connected(let hostname): return "Connected to \(hostname)"
        case .failed(let error): return error
        }
    }
}

struct RecordingPill: View {
    let since: Date

    var body: some View {
        TimelineView(.periodic(from: since, by: 1)) { context in
            let elapsed = Int(context.date.timeIntervalSince(since))
            HStack(spacing: 8) {
                Circle().fill(.red).frame(width: 12, height: 12)
                Text(String(format: "REC %d:%02d:%02d", elapsed / 3600, elapsed / 60 % 60, elapsed % 60))
                    .monospacedDigit().bold()
            }
            .pillStyle()
        }
    }
}

struct SettingsView: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var host = ""
    @State private var name = ""
    @State private var front = false
    @State private var quality = VideoQuality.hd1080

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("Server address (e.g. 192.168.1.20)", text: $host)
                        .keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                    Button {
                        model.scanForServer(prompt: false)
                    } label: {
                        HStack {
                            Text(model.scanning ? "Scanning the network…" : "Scan for server")
                            if model.scanning { Spacer(); ProgressView() }
                        }
                    }
                    .disabled(model.scanning)
                    ForEach(model.discovered) { server in
                        Button {
                            host = server.host
                        } label: {
                            HStack {
                                Text("\(server.hostname) — \(server.address)")
                                if host == server.host { Spacer(); Image(systemName: "checkmark") }
                            }
                        }
                    }
                    TextField("This phone's name", text: $name)
                } header: {
                    Text("takeloom server")
                } footer: {
                    Text("The Mac running `takeloom server`. Scan to find it on this Wi-Fi, or type its address. "
                         + "The first connection has to be approved there.")
                }
                Section("Camera") {
                    Picker("Camera", selection: $front) {
                        Text("Back").tag(false)
                        Text("Front").tag(true)
                    }
                    Picker("Quality", selection: $quality) {
                        ForEach(VideoQuality.allCases) { Text($0.rawValue).tag($0) }
                    }
                    if model.recordingSession != nil {
                        Text("Camera settings can't change while a session is recording.").foregroundStyle(.secondary)
                    }
                }
                if !model.pending.isEmpty {
                    Section("Recordings not yet on the server") {
                        ForEach(model.pending) { recording in
                            VStack(alignment: .leading) {
                                Text(recording.sessionID).font(.callout)
                                if let rejected = recording.rejected {
                                    Text(rejected).font(.caption).foregroundStyle(.red)
                                    Button("Try again") { model.retryPending(recording) }
                                }
                            }
                            .swipeActions { Button("Delete", role: .destructive) { model.deletePending(recording) } }
                        }
                    }
                }
                Section {
                    Text("Takeloom Camera \(Settings.appVersion)").foregroundStyle(.secondary)
                }
            }
            .navigationTitle("Settings")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") {
                        Settings.deviceName = name.isEmpty ? UIDevice.current.name : name
                        if model.recordingSession == nil,
                           front != model.useFrontCamera || quality != model.quality {
                            model.applyCameraSettings(front: front, quality: quality)
                        }
                        model.connect(to: host)
                        dismiss()
                    }
                }
            }
            .onAppear {
                host = model.serverHost
                name = Settings.deviceName
                front = model.useFrontCamera
                quality = model.quality
            }
        }
    }
}

extension View {
    func pillStyle() -> some View {
        padding(.horizontal, 12).padding(.vertical, 8)
            .background(.black.opacity(0.55), in: Capsule())
            .foregroundStyle(.white)
            .font(.callout)
    }
}
