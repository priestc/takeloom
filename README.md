# Takeloom

A CLI application for musicians to record instruments over backing tracks, manage songs/albums, and track completed takes.

Takeloom handles continuous audio recording, playback mixing, session logging, and per-take file management. Record one instrument at a time — each new session plays back your previous takes alongside the backing track so you can layer parts.

## Requirements

- Python 3.9+
- PortAudio, libsndfile, ffmpeg (system libraries)
- yt-dlp (optional, system binary) — only needed to add YouTube URLs as backing tracks from the New Project dialog

See [Prerequisites](docs/prerequisites.md) for install instructions for each operating system.

## Install

```bash
pipx install takeloom
takeloom install-launcher
```

That's the latest release from PyPI. Or with Homebrew (macOS/Linux), which
also installs the system libraries takeloom needs:
`brew install priestc/takeloom/takeloom`. Or with apt (Debian/Ubuntu) or Flatpak
(any Linux): see <https://priestc.github.io/takeloom/>. To follow every commit on `main`
instead (and auto-update to it on each launch):
`pipx install git+https://github.com/priestc/takeloom.git`

`takeloom install-launcher` adds Takeloom to the Applications folder
(macOS) or to the applications menu and desktop (Linux), so the UI opens
like any other app. Updates still go through pipx, automatically, each
time the UI opens.

**macOS, drag-to-install:** `python3 scripts/build_macos_dmg.py` builds
`dist/Takeloom.dmg`. Drag Takeloom into Applications. On first open the app
installs takeloom with pipx (Homebrew's `pipx` and `python-tk` must already
be installed). An app that came from a download is blocked the first time,
since it isn't signed by Apple: right-click it and choose Open.

## Usage

### Studio Setup

Configure your studio in three steps. Each command reads and updates `~/studio_config.json` independently, so you can re-run one without redoing the others.

```bash
takeloom setup-studio              # studio name, location, musician, remote vault server
takeloom setup-recording-devices   # sample rate, buffer size, output device, input labels, camera
takeloom setup-instruments         # assign instruments to input channels
```

`setup-recording-devices` also lets you pick a camera. If one is configured, every session
records video alongside the audio (see [Recording Session](#recording-session)).

`setup-studio` also configures the **Studio Session Vault** — shared storage for everything
a project used to keep to itself: recorded sessions, backing tracks, and completed takes
(see [Project Structure](#project-structure)). A project is just a setlist file now. Three
vault modes: local only (default, under `~/Documents`), remote only (uploaded to the remote
vault server and removed locally once every file's checksum is verified), or both (uploaded
but also kept locally). Starting a session downloads whatever that project's setlist needs
from the remote first, in remote-only mode.

The remote vault is a separate small server, `takeloom-vault` (this repo's
[`vault_server/`](vault_server/README.md) folder), running on the storage machine. It also
serves a web page for browsing sessions and watching their videos. Takeloom pairs with it
the same way Remote mode pairs: click **Pair...** next to "Remote vault server" in Studio
Setup (or run `takeloom vault pair tank2`), then approve the code shown on the vault's web
page. `takeloom vault status` checks the connection. If you have projects from before the vault existed, run
`takeloom migrate-sessions-to-vault` once to move everything in — safe to re-run,
already-migrated projects are skipped.

### Graphical Interface

```bash
takeloom ui
```

Opens a desktop window (built with tkinter). Currently implements the Studio Setup screen —
the same fields as `takeloom setup-studio` — with more screens to follow.

### Creating a Project

```bash
takeloom new-project
```

Enter a project name (e.g. "My Album"). Creates `~/Takeloom Projects/My Album.json` — an
empty setlist.

### Updating the Setlist

Copy audio files (FLAC, WAV, MP3, M4A) into the vault's `backing_tracks/` directory, then:

```bash
takeloom update-setlist "My Album"
```

Adds any of the vault's backing tracks not yet in this project's setlist (omit the project
name to use the last-used project). Each track in the setlist includes a `volume` field
(default 100%) that you can edit manually to adjust backing track playback level.

### Recording Session

```bash
takeloom start-session guitar "My Album"
```

The session plays the backing track through your speakers, monitors your instrument input in real-time, and records your take. Controls:

| Key | Action | When |
|-----|--------|------|
| `r` | Start recording (plays backing track) | Waiting |
| `b` | Back to start (restart the take) | Playing |
| `e` | Mark song end (complete the take) | Playing |
| `n` | Move to next track (auto-starts recording) | Between tracks |
| `l` | Lower backing track volume by 5% | Any time |
| `u` | Raise backing track volume by 5% | Any time |
| `q` | End session | Any time |

**Completing a take:** Press `r` to start, then `e` when the song finishes. The take is saved to the vault's `completed_takes/` and set as the preferred take in the setlist.

**Restarting a take:** Press `b` to loop back to the beginning. The backing track restarts and a new take file begins.

**Volume adjustments** are saved back to the setlist file at the end of the session.

**Video:** if a camera is configured (`takeloom setup-recording-devices`), the whole session is
also recorded on video, saved as `session_video.mp4` alongside `session.flac` once the session
ends. The video has two audio tracks: a compressed (AAC) mix of the backing track + your
instrument for easy playback, and a lossless (FLAC) track of just your instrument alone.

### Multi-Instrument Layering

Start a new session with a different instrument (e.g. "bass"). The backing track plays mixed with your previously recorded preferred takes, so you hear everything together while recording the new part.

### Desktop Audio Capture

To record virtual instruments, software synths, or system audio, you need a virtual audio loopback device. See [Desktop Audio Capture Setup](docs/desktop-audio-capture.md) for instructions.

### Live Streaming

The GUI's Streaming tab can push every session live to YouTube over RTMP — just paste a
stream key from YouTube Studio and enable it. Connecting a YouTube account is optional
and only needed if you also want each session's stream auto-titled with the studio,
musician, project, and date; that requires creating your own free Google Cloud OAuth
credentials. See [YouTube Streaming Setup](docs/youtube-streaming-setup.md) for the
steps, and why it can't just be a one-click sign-in.

### Video Window

```bash
takeloom video-ui
```

Opens the video operator's window: a thumbnail of every camera along the top (the
main camera, any added on Recording Devices, and every connected Takeloom Camera
phone) and the **program** ("out") camera large in the middle. Click a thumbnail, or
press its number key, to make it the program camera. The live stream shows the
program camera and switches the moment you pick another (no restart). During a
session every switch is written to `session_log.json` as a `program_camera` event,
with its position on the session's audio timeline, so the streamed video can be
rebuilt afterwards from each camera's own recording. The program camera is the main
camera until another is picked.

Like the main window, it drives the Mac Mini's `takeloom server`: `--remote=IP`, an
"always connect" remote, or a server running on the same machine. The main window
keeps its video for now.

## Project Structure

A project is just a setlist file — `<projects_dir>/My Album.json`. Everything else lives in
the shared Studio Session Vault (see [Studio Setup](#studio-setup)) instead of belonging to
one project, so two different projects can reference the same backing track or completed
take (most commonly a song pulled in from the Inspiration library — see
[Cross-Project Take Reuse](#cross-project-take-reuse)) without either downloading or
recording it twice:

```
<projects_dir>/
├── My Album.json
└── Side Project.json

<vault>/
├── backing_tracks/
│   ├── song1.mp3
│   └── a1b2c3d4_song2.flac
├── completed_takes/
│   ├── song1 - guitar - take1 [upload:a1b2c3d4].flac
│   ├── song1 - bass - take1 [upload:a1b2c3d4].flac
│   └── song2 - guitar - take2 [youtube:dQw4w9WgXcQ].flac
├── inspiration_takes.json
└── sessions/
    └── 2025-01-15_14-30-00_guitar_My Album/
        ├── session.flac
        ├── session_video.mp4
        └── session_log.json
```

- `backing_tracks/` and `completed_takes/` are shared across every project. A locally
  uploaded file gets a short random prefix added only if its plain name would otherwise
  collide with an unrelated project's file; a file that already has a globally unique name
  (a YouTube download, or one pulled from the Inspiration library) keeps it as-is.
- A completed take's filename tags the exact backing track it was recorded against —
  `[upload:<id>]`, `[youtube:<video id>]`, or `[inspiration:<track id>]` — since backing
  tracks are shared vault-wide now and two entries can share a display name while pointing
  at different audio.
- Existing take files are never deleted. New takes increment the take number and replace the preferred take in the setlist.
- `inspiration_takes.json` — the cross-project take index (see below).
- `session.flac` — the continuous raw recording spanning the full session
- `session_log.json` — musician, studio, and event data; what post-session processing replays to find completed takes and copy them into the vault's `completed_takes/`
- `session_video.mp4` is only present when a camera is configured

### Cross-Project Take Reuse

A song pulled from the Inspiration library carries a stable ID from that library, so a take
recorded on it from *any* project is recorded into `inspiration_takes.json` too, keyed by
that ID. This is what lets an "inspiration filter" setlist slot (a standing "draw a random
song from this filter each session" entry — added from the GUI's Streaming/Setlist screens)
prefer redrawing a song other instruments already have a take on, and what lets
`ensure_setlist_files_local` (session start, in remote vault mode) pull down another
project's take on a shared song before it's needed.

## License

MIT — see [LICENSE](LICENSE). Builds and releases: [docs/releasing.md](docs/releasing.md).
