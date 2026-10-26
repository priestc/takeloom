import AVFoundation
import SwiftUI

@main
struct TakeloomCameraApp: App {
    @StateObject private var model = AppModel()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(model)
                .onAppear { model.startCamera() }
        }
    }
}

struct ContentView: View {
    @EnvironmentObject var model: AppModel
    @State private var showingSettings = false

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()
            PreviewView(layer: model.pipeline.previewLayer).ignoresSafeArea()
            VStack {
                HStack(alignment: .top) {
                    ConnectionPill(state: model.linkState)
                    Spacer()
                    if let since = model.recordingSince {
                        RecordingPill(since: since)
                    }
                }
                Spacer()
                if let error = model.cameraError {
                    Text(error).padding(8).background(.red.opacity(0.85), in: Capsule()).foregroundStyle(.white)
                }
                HStack {
                    if let status = model.uploadStatus {
                        Label(status, systemImage: "arrow.up.circle").pillStyle()
                    } else if !model.pending.isEmpty {
                        Label("\(model.pending.count) recording(s) waiting to upload", systemImage: "tray.full")
                            .pillStyle()
                    }
                    Spacer()
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
        }
        .sheet(isPresented: $showingSettings) {
            SettingsView().environmentObject(model)
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
                    TextField("This phone's name", text: $name)
                } header: {
                    Text("takeloom server")
                } footer: {
                    Text("The Mac running `takeloom server`. The first connection has to be approved there.")
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
