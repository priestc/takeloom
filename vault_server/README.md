# takeloom-vault

The remote Studio Vault server. Takeloom uploads each recorded session here once it's
been processed, and uses it for every other remote-vault access too: Sessions-tab listing,
`session_log.json` corrections, deletes, and downloading backing tracks and completed takes
that were pruned locally. It also serves a web UI for browsing sessions and watching
their videos.

Stdlib-only Python (3.9+), no dependencies, so it installs anywhere with plain `pipx`.

## Install (on the storage machine, e.g. tank2)

```bash
pipx install "git+https://github.com/priestc/takeloom.git#subdirectory=vault_server"
takeloom-vault install-service --vault "/path/to/Takeloom Studio Vault"
loginctl enable-linger "$USER"   # keep it running while logged out
```

`install-service` writes and starts a systemd **user** unit (`takeloom-vault.service`), so
the server runs as the user who owns the vault files. Logs:
`journalctl --user -u takeloom-vault -f`.

To upgrade: `pipx install --force "git+https://github.com/priestc/takeloom.git#subdirectory=vault_server"`,
then `systemctl --user restart takeloom-vault`.

It listens on port **51824** (takeloom assumes that port when given a bare hostname). Only
local-network peers are allowed (private, loopback, link-local and Tailscale addresses); there's
no TLS. It's LAN-trust, the same as takeloom's own Remote mode.

## Pairing

1. In takeloom's Studio Setup tab, enter the vault's address (e.g. `tank2.lan`) next to
   "Remote vault server" and click **Pair...** (or run `takeloom vault pair tank2.lan`). Takeloom
   shows a 6-digit code.
2. Open `http://tank2.lan:51824` in a browser. The pending request is listed at the top with the
   same code. Click **Approve**.
3. Takeloom receives its token and stores it in `~/studio_config.json`. Requests expire after
   5 minutes.

Paired devices are listed at `/clients` in the web UI, where they can be revoked. You can also
use `takeloom-vault clients` / `takeloom-vault revoke ID`. Only a SHA-256 of each token is
stored (in `~/.config/takeloom-vault/clients.json`).

Only the studio machine pairs here. Everything paired with *its* `takeloom server` (Takeloom
Camera phones, remote-control laptops) gets a token of its own from this vault through it
(`POST /api/clients`), listed under it on the Paired devices page. A delegated token works for
as long as the studio machine stays paired: revoking it revokes them all. The studio machine
revokes one itself once its device is no longer authorized there.

## Web UI

- `/`: every session in the vault, newest first, plus any pending pairing requests.
- `/sessions/<name>`: the session's video (`session_video.mp4`), with a jump button for
  each song's start; the takes it filed (linked to their audio/video in `completed_takes/`);
  and its raw files (FLAC with an inline player, MIDI, the log).

No login: anyone on the local network can browse. The API needs a paired token.

## What it stores

Everything takeloom keeps in its vault: `sessions/`, `completed_takes/`, `backing_tracks/`,
`mixes/` (the Completed Takes mixer's saved mixes), `final_mixes/` (exported song mixes),
`projects/` (setlists), and the root files `inspiration_takes.json` and `backing_tuning.json`.
Takeloom keeps them in sync after each session (`takeloom vault sync`).

Only a session's raw recording is lossless. A completed take's file is an MP3 (LAME V2), so
`/api/takes/<take filename>/original` cuts the take's lossless original back out of its
session for an exported mix. It cuts from `session.flac` by sample when the session still has
it, and otherwise from a camera video's Instrument Only (FLAC) track. A legacy `.flac` take
is served as it is. The result is cached with the take videos. Take video clips and edits get
an MP3 Instrument Only track, so this server's ffmpeg needs `libmp3lame`. Debian's and
Ubuntu's ffmpeg packages include it.

## Take videos

Completed takes don't get their own video file. Cameras are a set: every camera records its
own whole-session video (`camera1_<name>.mp4`, `camera2_<name>.mp4`, ...), and each session
log's `takes` snapshot records where each take sits in every one of them, plus which camera
was on the program feed when the take started (see `takeloom/take_video.py`). The first
request for a take's video (`/takes/<take filename>/video` in the browser,
`/api/takes/<take filename>/video` for takeloom) cuts it out with ffmpeg and caches it:
from the camera named by `?camera=<label>`, or by default the program camera. The session
page has a button per camera to switch the session video, and a link per camera for each
take. Sessions from before this have a single `session_video.mp4`, which works as before. An
hourly sweep evicts clips nobody has requested for `--cache-max-age-days` (default 14), and
the least recently used ones once the cache passes `--cache-max-gb` (default 20). The cache
lives in `~/.cache/takeloom-vault/take-videos` unless `--cache-dir` says otherwise.
Evicting is safe, since any clip can be cut again. The first cut of a long take takes a
little while; after that it's served straight from the cache.

Takes recorded before this have their own `.mp4`. Running `takeloom vault migrate-take-videos`
on the studio machine records the refs for them, and adding `--delete` removes those `.mp4`
duplicates.

## Editing a session into one video

A session page with more than one camera has an **Edit** section. Set the **camera priority**
(▲/▼): the first camera is the main one, on screen most of the time and the source of the sound;
the others are cut in every N seconds for M seconds, shared by priority (with three others,
3 : 2 : 1, interleaved evenly), so the lowest is cut to least. The cuts are worked out here on
the vault (`edit.auto_cuts`), which asks `edit.usable` whether a camera is worth cutting to for
each stretch: always, for now, and the place to plug in a check that the performer is in
frame. The page shows them as a timeline strip, one colour per camera. The player above previews the
result: every camera's video plays together, in step, and with **Preview edit** on it cuts
between them as the timeline says, with the priority-1 camera's sound, just as the render will
(a camera's button turns preview off and holds that camera).

The footer (musician, instrument, the song, when it was played, and the camera on screen) only
ever goes on YouTube (`takeloom_vault/footer.py`). `edit.mp4` and the player are clean.
**Publish to YouTube** first renders the same plan again from the camera videos with the footer
drawn in, changing with each song and cut, then uploads that (`edit_youtube.mp4`, deleted after).
That needs ffmpeg's `drawtext`, which Ubuntu's has. takeloom's live stream draws the same footer.
Sessions recorded before November 2026 already have it in their videos, so it isn't added
again. Click any stretch to switch it to the next camera; the player above jumps
there showing that camera. **Render final video** saves the plan (`edit.json` in the session's
folder) and makes `edit.mp4` in the background: each stretch of picture from its camera, cut
frame-accurately, and the sound unbroken from the main camera from start to finish, so a cut
never breaks the audio. Rendering needs the same ffmpeg (with `libx264`) as encoding below; it
takes about as long as encoding one camera's session video. See `takeloom_vault/edit.py`.

A session whose phones' mics were archived (takeloom saves each as `<camera>_mic.flac`, on the
session's timeline) also gets an **Audio** switch: **Processed** is the audio interface's sound
(the main camera's tracks), **Atmospheric** is the phones' mics played together, the room as the
phones heard it. The player previews either, and the render uses the one chosen: for Atmospheric,
the mics summed and limited as the soundtrack, with the processed mix kept as a second track.

## Publishing to YouTube

Beside **Render final video**, **Publish to YouTube** uploads the session's `edit.mp4` straight
from the vault, with the title shown (prefilled from takeloom's YouTube title template) and the
chosen visibility, then links to the video. It uses the YouTube account connected in takeloom
(video-ui › Settings › Streaming): takeloom gives it to the vault when it's connected and when
`takeloom server` starts (`PUT /api/youtube`), and the vault keeps it in
`~/.config/takeloom-vault/youtube.json`, readable only by its user. Uploads resume after a failed
chunk. YouTube makes every upload from an unverified Google API project private whatever
visibility was asked for; the page says when that happened. Each upload costs 1,600 of the
project's 10,000 daily API quota units, so about six a day.

## Encoding session videos here

Encoding a session's videos (every camera's whole-session video, re-encoded to burn in its
watermark) is the heavy part of processing a session. If this vault runs on a capable
computer, takeloom can leave that to it: in takeloom's Studio Setup, set **Encode session
videos on** to **Vault server** (it only applies with **Remote only** vault storage).

The studio machine then files the takes as usual but uploads the raw videos and audio with a
`render_jobs.json` describing each encode, and this server makes the videos in the background,
one session at a time, with ffmpeg at low priority. When they're done it deletes the raw
inputs. The session's page says so while it's working, and a take's video can't be cut from
it until it's finished. Sessions still pending when the server stops are picked up again at
startup.

It needs `ffmpeg` with the `libx264` encoder on this machine (`apt install ffmpeg` has it).
`GET /api/ping` reports `"encode": true` when it's available; takeloom checks that, and
encodes on the studio machine instead when it isn't (a NAS without ffmpeg, an older vault, or
the vault being unreachable).

### Phone recordings, straight from the phones (0.11+)

With vault encoding on, the Takeloom Camera phones (app 1.6+) upload their recordings here,
not to the studio machine: into `camera_uploads/<session>/`, in resumable chunks
(`/api/camera-uploads/...`). The studio machine waits for them, then lines each one up with the
session's audio from a small extract of its sound this server sends, and its
`render_jobs.json` gets two more kinds of job ahead of the encodes: `normalize` (the video,
timestamps rescaled for the phone's clock drift, as a stream copy) and `mic` (the phone's mic
track on the session's timeline). The recording is moved into the session first and kept there
untouched, as the archive master; so is `session.flac`.

A job file is a description (file names, offsets, watermark timings), never a command line:
this server builds the ffmpeg command itself and only accepts plain file names inside the
session's own folder.

## API

See the docstring at the top of `takeloom_vault/server.py`. Uploads are `PUT
/api/files/<vault path>` with an `X-Content-SHA256` header. The file is written beside its
destination and only renamed into place once the hash matches. Takeloom uploads a
session's `session_log.json` last, and a session is only listed once that file exists, so an
interrupted upload never looks complete.
