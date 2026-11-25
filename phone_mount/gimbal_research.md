# Motorized pan/tilt for the phone mount: research notes

Status: research only, nothing built yet (2026-11-25).

Goal: let a phone on the mount tilt up/down and pan side to side, and later
follow the musician automatically (AI tracking) so the shot stays zoomed in
on them as they move around the stage a bit.

## 1. Start without a motor: follow by cropping

Takeloom Camera can already zoom and pan by cropping the image. The `framing`
message (`{"kind": "framing", "zoom", "x", "y"}`, see
`takeloom/phone_camera/protocol.py`) sets a zoom and a position, and
`camera_app/ios/TakeloomCamera/CameraPipeline.swift` crops to it.

So the first version of AI follow can be software only: the phone finds the
musician and moves the crop window to keep them centered. A 48 MP sensor
recording 1080p leaves a lot of room to crop. This has no motor noise and
nothing to buy, and it's enough for a subject moving "a bit". A motor is only
needed when the subject leaves the widest lens's view.

## 2. Buying one

- **Apple DockKit stands**, for example the
  [Belkin Auto-Tracking Stand Pro](https://www.belkin.com/p/auto-tracking-stand-pro-with-dockkit/MMA008dq05BKAPL.html)
  (about $150, 360° pan, 90° tilt, iPhone 12+ on iOS 17+). The best ready-made
  option on iPhone: DockKit lets your own app steer the stand and feed it your
  own tracking ([WWDC23 session](https://developer.apple.com/videos/play/wwdc2023/10304/)),
  so Takeloom Camera stays in charge of the camera.
  - iPhone only: Belkin says it doesn't support Android, so the Galaxy S20 FE
    can't use it.
  - A desk stand, so it doesn't fit the wall mount.
  - It's the only DockKit stand the search found, so there's little choice.
- **Generic gimbals (AliExpress, DJI Osmo Mobile and similar)**: not
  recommended.
  - Their Bluetooth control is closed. DJI forum users
    [have asked](https://forum.dji.com/thread-281229-1-1.html) for
    documentation without an answer; the only way in is
    [reverse engineering](https://aaadityaart.notion.site/Reverse-Engineering-the-DJI-Osmo-37b18cae2ab08098acefd85694a070bd).
  - Their tracking runs in the maker's app, which would fight Takeloom Camera
    for the camera.
  - They're handheld 3-axis designs on batteries that go to sleep. A wall
    mount needs no roll axis and no stabilization.

## 3. Building one (recommended if a motor is needed)

The mount is already a manual pan/tilt joint (v1–v3), so a motorized v4 is a
natural next step, and it works with iPhone and Android alike.

- **Two axes, no stabilization.** The wall doesn't move: the motors only need
  to aim, slowly, smoothly and quietly. Much easier than a real gimbal.
- **Motor noise is the main constraint** in a recording room. Hobby servos are
  out: they whine and jitter. Two quiet options:
  - Small steppers with TMC2209 drivers in StealthChop mode: nearly silent,
    simple, and they hold position well.
  - Brushless gimbal motors (for example the 2804 size) with an AS5600
    position sensor and the SimpleFOC library: silent and very smooth, but
    more work to tune.
- **Controller:** an ESP32 on Wi-Fi taking commands from the vault, the same
  way the phones and nodes already talk to it
  (`vault_server/takeloom_vault/camera_hub.py`). The protocol stays ours.
- **Power:** a wire along the wall. The phone probably needs one anyway for
  long sessions.

## 4. Where the tracking runs

On the phone: Vision on iOS, ML Kit on Android. Running it on the vault from
the live feed would add network delay to every correction.

Combine the two: the crop window follows small, quick movements, and the motor
slowly re-centers when the subject drifts toward the edge of the frame. The
motor then rarely moves, which keeps both the noise and the visible camera
motion down.

## Suggested order

1. Software follow by cropping (subject detection in the app driving `framing`).
2. A motorized v4 mount (steppers + ESP32).
3. Tie the motor into the follow.
