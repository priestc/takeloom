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

1. In takeloom's Studio Setup tab, enter the vault's address (e.g. `tank2`) next to
   "Remote vault server" and click **Pair...** (or run `takeloom vault pair tank2`). Takeloom
   shows a 6-digit code.
2. Open `http://tank2:51824` in a browser. The pending request is listed at the top with the
   same code. Click **Approve**.
3. Takeloom receives its token and stores it in `~/studio_config.json`. Requests expire after
   5 minutes.

Paired devices are listed at `/clients` in the web UI, where they can be revoked. You can also
use `takeloom-vault clients` / `takeloom-vault revoke ID`. Only a SHA-256 of each token is
stored (in `~/.config/takeloom-vault/clients.json`).

## Web UI

- `/`: every session in the vault, newest first, plus any pending pairing requests.
- `/sessions/<name>`: the session's video (`session_video.mp4`), with a jump button for
  each song's start; the takes it filed (linked to their audio/video in `completed_takes/`);
  and its raw files (FLAC with an inline player, MIDI, the log).

No login: anyone on the local network can browse. The API needs a paired token.

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

The footer (musician, instrument, the song, when it was played, and the camera on screen) is the
mixer's too (`takeloom_vault/footer.py`): takeloom no longer burns it into the camera videos, so
the player draws it over the picture, changing with each song and cut, and rendering draws the
same into `edit.mp4` (needs ffmpeg's `drawtext`, which Ubuntu's has). Sessions recorded before
this already have it in their videos, so it isn't added again. Click any stretch to switch it to the next camera; the player above jumps
there showing that camera. **Render final video** saves the plan (`edit.json` in the session's
folder) and makes `edit.mp4` in the background: each stretch of picture from its camera, cut
frame-accurately, and the sound unbroken from the main camera from start to finish, so a cut
never breaks the audio. Rendering needs the same ffmpeg (with `libx264`) as encoding below; it
takes about as long as encoding one camera's session video. See `takeloom_vault/edit.py`.

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

A job file is a description (file names, offsets, watermark timings), never a command line:
this server builds the ffmpeg command itself and only accepts plain file names inside the
session's own folder.

## API

See the docstring at the top of `takeloom_vault/server.py`. Uploads are `PUT
/api/files/<vault path>` with an `X-Content-SHA256` header. The file is written beside its
destination and only renamed into place once the hash matches. Takeloom uploads a
session's `session_log.json` last, and a session is only listed once that file exists, so an
interrupted upload never looks complete.
