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

The phones connect to the **vault** (`takeloom-vault` on tank2, 0.18+), not
to a recording node: the vault is wired to the router, so the phones' live
feeds and uploads never cross a node's Wi-Fi. The vault speaks the same
protocol on its own port (51824, shared with its web pages and API) and
has the phone hub (`vault_server/takeloom_vault/camera_hub.py`). A
recording node's `takeloom server` (the Mac Mini, the laptop) controls the
phones through it (`takeloom/phone_camera/vault_hub.py`): it starts and
stops their recordings with each session, sets their live quality and
framing, and shows their previews. The vault records each phone's live
feed into the session's folder and takes its upload there. A node with no
remote vault (or one older than 0.18) still listens for phones itself, as
below.

1. Run `takeloom server` on the recording node as usual.
2. Open Takeloom Camera. The first time, it scans the phone's Wi-Fi network
   for the server: every address on port 51824, keeping those that answer
   the protocol's `discover` as takeloom. Then it asks "Connect to tank2
   at 192.168.1.190:51824?" (or lists them, if it finds several). Say yes and
   it saves that address. To change it later, tap ⚙: type an address, or
   use **Scan for server**. The first connection asks to pair: approve it
   at the top of any page of the vault's web UI. The app keeps a token per
   server, so a phone that was paired with the Mac Mini pairs once more
   with the vault. The vault's **Cameras** page lists the connected phones
   with their previews and the recording nodes.
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

### Sharing the Wi-Fi

Each phone encodes twice: its recording at the quality picked in ⚙ (kept on
the phone, uploaded after the session) and a lighter **live feed** for the
stream, the previews and the fallback video. The server splits a Wi-Fi
budget between the phones' live feeds: `camera_wifi_budget_mbps` in the
config, 20 Mbps by default. The program camera gets three shares to every
other phone's one, so adding phones lowers the others' quality instead of
flooding the network. With three phones that's 8 Mbps at 1080p for the
program camera and 3 Mbps at 720p for each of the others. The split is
re-done when a phone connects or leaves and when the program camera
changes, and the server log shows it ("Live camera feeds: …").

- iOS changes size and bitrate as told, but holds its size while recording
  (the fallback video can't change size partway) and switches when it ends.
- Android's live feed is fixed at up to 720p; only its bitrate follows the
  budget. A phone whose camera can't feed a second encoder falls back to a
  single encode for both, at the full recording bitrate.
- A phone on an older app version can't adapt either. Its full bitrate
  (12 Mbps at 1080p, 35 at 4K) comes off the budget first, and the log
  says to update it.

Each camera besides the main one is saved whole as its own
`cameraN_<name>.mp4` in the session, with the same audio tracks and
watermark as `session_video.mp4`. A phone's copy gets its own measured
sync offset (below); a local camera's uses the hand-tuned camera offset.
These are whole-session videos only: per-take videos still come from the
main camera.

Hold the phone in the official [Takeloom phone mount](../phone_mount/), a
3D-printable wall mount that keeps it in landscape. The software assumes
every recording phone is in one. Keep the phone on a charger. The app keeps the
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

### Uploading straight to the vault

When the remote vault makes the session videos (Studio Setup: "Remote
only" storage, "Encode session videos on: Vault server", takeloom-vault
0.11+), the phones' recordings never pass through the Mac Mini. A phone
asks the server where to upload, as always, and is pointed at the vault
(`upload_redirect` in protocol.py), with a vault token the server got for
it. The upload goes there over HTTP in resumable 8 MB chunks. The server
follows its progress on the vault, then lines it up from a small extract
of its sound the vault sends (mono 8 kHz, about 115 MB an hour), just as it
would a local file. It leaves the file work to the vault as render jobs
that run before the encodes: rescaling the video for drift, and cutting
the mic track. Older apps (before 1.6) still upload to the server.

The phone pairs only with the server. Everything paired with the server,
phones and remote-control laptops alike, gets its own vault token through
the server's own pairing with the vault, shown under it on the vault's
Paired devices page. Revoking the server there revokes them all, and a
client no longer authorized on the server loses its token within minutes.
A remote laptop uses its token to fetch take audio and videos straight
from the vault, when it can reach it.

### Speakers, headphones, and sync clicks

Step 2 only works if the phones hear something that's also in the session's
audio. A MIDI keyboard or a DI'd bass heard only in headphones leaves the
room silent, and the video falls back to clock sync: tens of ms off, with
drift uncorrected (most sessions before this did). Two settings in
Recording Devices fix that:

- **Speakers play only the instrument.** The audio stream gets 4 outputs:
  Playback 1-2 (the speakers) carries just the instrument, and Playback 3-4
  (the headphones) the full mix with the backing track. The phones then
  always hear the instrument, which matches `session.flac` exactly, and the
  room's sound (the Atmospheric track) never has the copyrighted backing
  track in it. On the Scarlett 4i4, set Focusrite Control 2's Routing to
  Output 1-2 = Mix A and Headphones = Mix B. In the Mixer, put Playback 1-2
  only in Mix A and Playback 3-4 only in Mix B. takeloom's automatic direct
  monitor then sets the input's fader in both mixes. Close Focusrite Control 2
  afterwards, since takeloom can't reach the device while it's open.
- **Sync clicks.** With the split on, whenever the speakers have been quiet
  for 20 seconds (and 2 seconds into the session), they play a 60 ms chirp.
  Each one's position is logged (`sync_clicks` in `session_log.json`), and
  alignment matches the phones' mics against the same chirps rebuilt from
  the log. A session nobody could hear still lines up to well under a
  millisecond, drift included. Clicks only happen in silences, never over
  the instrument.

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

**Android**: with the phone plugged in (USB debugging on), run

    takeloom camera-android

from a takeloom checkout (an editable install). It builds the app from
camera_app/android and installs it over the existing one, keeping its data,
using Android Studio's bundled Java and SDK so neither needs to be on PATH.
By hand, that's:

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
