# Takeloom Camera

An iOS / Android app that turns a phone into the session camera. The phone
records the session at full quality, streams a live feed to the server (for
YouTube streaming and the Record tab's preview), and uploads its own
recording once the session ends. The server then lines that video up with
the session's audio automatically, with no manual latency test.

- Server side: `takeloom/phone_camera/` (hub, live recorder, sync)
- iOS app: `camera_app/ios/` (Swift, AVFoundation + VideoToolbox)
- Android app: `camera_app/android/` (Kotlin, Camera2 + MediaCodec)
- Wire protocol: `takeloom/phone_camera/protocol.py`

## Using it

1. Run `takeloom server` on the Mac Mini as usual. It also listens for
   phones on port **51824**.
2. Open Takeloom Camera. The first time, it scans the phone's Wi-Fi network
   for the server: every address on port 51824, keeping those that answer
   the protocol's `discover` as takeloom. Then it asks "Connect to Mac.local
   at 192.168.1.200:51824?" (or lists them, if it finds several). Say yes and
   it saves that address. To change it later, tap ⚙: type an address, or
   use **Scan for server**. The first connection shows a pairing request on
   the Mac (the same approval flow as a Remote client). Approve it.
3. The phone shows up on the Record tab by itself, as one of the camera
   previews. There's nothing to add: every connected phone is recorded.
4. Record as normal. Starting a session tells every connected phone to
   record, and ending it tells them to stop and upload. Processing waits
   for the uploads, showing progress in the status line, then builds the
   session's videos from the phones' files.

### More than one camera

Every session records every connected camera at once, local webcams and
Takeloom Camera phones alike; nothing needs adding. Recording Devices →
Camera lists every camera ever seen. There you can:

- give each one a **name**, its label everywhere in the UI;
- pick the **main camera**, which gives each take its video and becomes
  `session_video.mp4` (if it isn't connected, the first connected camera
  stands in);
- **hide** anything that isn't really a camera. Screen captures are hidden
  when first seen, and macOS's Continuity Camera from a nearby iPhone is a
  likely candidate too.

What's streamed live is the program camera, picked in `takeloom video-ui`.

Each camera besides the main one is saved whole as its own
`cameraN_<name>.mp4` in the session, with the same audio tracks and
watermark as `session_video.mp4`. A phone's copy gets its own measured
sync offset (below); a local camera's uses the hand-tuned camera offset.
These are whole-session videos only: per-take videos still come from the
main camera.

Mount the phone in landscape and keep it on a charger. The app keeps the
screen awake. Camera and quality (720p / 1080p / 4K) are in ⚙ and can't
change mid-session.

## How the sync works

See `takeloom/phone_camera/sync.py` for the details. In short:

1. **Clock sync.** The phone pings the server NTP-style (lowest-RTT sample
   wins), so every frame's capture timestamp converts to the server's
   clock. The server stamps the moment mix recording starts on that same
   clock. This gives an estimate within a few tens of ms.
2. **Audio cross-correlation.** The phone records its mic into the same
   file, on the same clock as its video. GCC-PHAT between that mic track
   and the session's mix and dry instrument, searched ±1 s around the
   clock estimate, places the video to well under a frame.
3. **Drift.** Step 2 runs on up to 24 windows across the session, and a
   fitted line removes phone-vs-audio-interface clock drift (tens of ppm,
   so tens of ms per hour) by rescaling the video timestamps (stream copy,
   no re-encode).

The measured offset goes into `session_log.json` as `video_offset_ms`. The
`video_sync` record there shows how it was measured: `method: audio` or
`clock`, matched windows, residual, drift. That value replaces the global
`video_latency_compensation_ms` for this session. If the mic heard nothing
usable, the clock estimate is used instead.

If the upload never arrives (phone died, app killed), the session uses the
live feed the server recorded, aligned by clock sync alone. That video may
have gaps wherever Wi-Fi dropped frames. Recordings that haven't uploaded
stay on the phone and resume uploading on the next connection, including
after a relaunch.

## Building

**iOS** (needs [XcodeGen](https://github.com/yonaskolb/XcodeGen)):

    cd camera_app/ios
    xcodegen generate
    open TakeloomCamera.xcodeproj   # set your signing team, run on a device

**Android**:

    cd camera_app/android
    ./gradlew assembleDebug         # app/build/outputs/apk/debug/app-debug.apk
    adb install -r app/build/outputs/apk/debug/app-debug.apk

## Limitations

- Only `takeloom server` hosts phones. A local (non-server) takeloom UI
  doesn't list them.
- The Latency tab doesn't apply (it refuses a phone camera), and Video
  Check runs audio-only with a phone selected.
- The live feed's timestamps are taken on arrival, so its lip-sync on a
  YouTube stream is approximate. The server delays the stream's audio by
  the measured live latency at start.
- On Android, a recording killed mid-session isn't playable (MediaMuxer
  only finalizes on stop). The live-feed fallback covers that case. iOS
  writes fragmented MP4 and stays playable.
