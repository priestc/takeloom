# Design: phone cameras, lined up whenever their recordings arrive

Status: stage 1 built (behind `config.camera_sources`, off until stage 3);
stages 2-5 not yet. Replaces the "process the session, wait for
the phones' uploads, repair whatever arrives afterwards" model with one
where a camera recording is lined up whenever it arrives, every derived
file is made on demand, and the vault simply shows what each session has.

## Why

Today a session's takes and its camera videos are processed together, in
one step, right after the session ends (`Backend._process_session_steps`,
`processing/splicer.py`'s `process_session`). Processing needs every
phone's recording to make the camera videos, so it waits — but not
indefinitely, because it also files the takes. Then it uses the phone's
live feed instead, and a recording that turns up later goes down a separate
"late" path (`recover_late_camera_uploads`, `realign_late_upload`) that
downloads the finished video, lines the recording up against it, and swaps
it in.

That split has cost real data and time (2026-11-23, session 11-16-09):

- processing stopped waiting for a phone during a one-second reconnect;
- a recording that arrived between processing and the vault upload was
  sent where nothing would ever use it, and the vault's staging cleanup
  would have deleted it;
- the late path lines up against the finished video's audio, so it can't
  use the sync clicks, and the Samsung ended up clock-synced only;
- a failed upload led to a local encode, which left stale render jobs on
  the vault.

And most of what processing does with a phone's recording only makes
derived files. With the originals and `session.flac` now archived (commit
c0fd7d4), everything in those files can be recomputed from an original
plus two numbers.

## What a phone recording actually needs

| Step today | Needs the recording? | Output | Kept? |
|---|---|---|---|
| Line it up (offset, drift; `sync.py`) | its sound only | two numbers | yes, in the session log |
| Normalize (drift-rescaled video, stream copy) | yes | `<cam>_raw.mp4` | no (intermediate) |
| Mic track (`extract_mic`) | yes | `<cam>_mic.flac` | yes |
| Camera video (video + Mix + Instrument, stream copy) | yes | `<cam>.mp4` | yes |
| Take video refs (per take, per camera) | the camera video's layout | refs in the log and `.video.json` | yes |

Only the first row is essential. The rest are conveniences that can be
made when someone needs them, and cached the way take clips already are
(`vault_server/takeloom_vault/takes.py`'s `TakeVideoCache`).

## The new model

### 1. Takes never wait for video

Processing a session files its takes from `session.flac` straight away,
whatever the cameras are doing. A take already records its exact span
(`TakeInfo.source`: session, `start_frame`, `end_frame`, `sample_rate`).
The take's video info shrinks to which cameras recorded the session and
which one was on the program feed when the take started:

    "video": {"session": "<session dir>", "default": "<camera label>"}

Per-camera positions are computed when a clip is cut, from the take's
frames and that camera's alignment. No refs are stored per camera.

### 2. Each camera is a set of sources, lined up independently

A session log's camera entry lists what's been recorded for it, best
first, each with its own alignment once known:

    {"label": "Samsung", "device": "takeloom-camera:…", "phone_camera": {…},
     "sources": [
       {"file": "camera2_samsung_phone.mp4", "kind": "phone",
        "state": "waiting" | "aligned" | "failed",
        "align": {"file_zero_s": 0.8002, "drift": 4.99e-05, "method": "audio"|"clicks"|"clock",
                  "windows_matched": 24, "residual_ms": 0.03, "aligned_at": "…"}},
       {"file": "camera2_samsung_live.mp4", "kind": "live",
        "state": "aligned", "align": {"file_zero_s": …, "drift": 0, "method": "clock"}}]}

`file_zero_s` is where the file's time 0 sits on the session's (mix's)
timeline, and `drift` is its clock's rate against it: session time =
`file_zero_s + file_time × (1 + drift)`. That's the whole alignment.

- **phone**: the phone's own recording, archived as-is. Best when aligned.
- **live**: the stream-quality feed the server recorded during the session,
  aligned by clock sync the moment the session ends. It's a placeholder,
  deleted as soon as the phone's own recording has arrived in full.
- **local** (webcams, legacy): as before.

A camera shows its best aligned source: the live feed until the phone's
recording arrives, then the phone's recording once it's lined up (a minute
or so, in which the camera shows as "Lining up").

### 3. Uploads are accepted any time

The hub accepts a phone's recording for any session the server knows,
whether it's processed or not, here or in the vault. "Late" stops being a
category. The upload goes straight to the vault into the session's own
folder (`sessions/<s>/<cam>_phone.mp4`, not a staging folder) when the
vault is the archive, else into the local session folder. Nothing waits on
it and nothing gives up on it: a recording that hasn't arrived simply
hasn't arrived yet. Recordings still on the phone stay there until a
server or vault says it has all of it (unchanged).

### 3a. The vault shows what's there

The session page lists each camera's current state, straight from the
session log and the files in the session's folder, with nothing to act on:

- **Phone recording**: lined up (by audio / sync clicks / clock), with its
  resolution and how well it matched; or "Arrived, lining up"; or
  "Uploading (N%)", from the partial file's size.
- **Live feed only** (stream quality): the phone's recording isn't there.
- **No video**: neither is.

### 4. The vault lines sources up when they arrive

Lining up runs on the vault, where everything it needs already is: the
phone's recording (it uploads straight there), `session.flac` (always kept
now), the Mix reference, and the sync-click times in the session log. It
makes no network traffic and never touches the Mac Mini, so it can run the
moment a recording lands, even while another session is being recorded.

A worker thread in takeloom-vault (beside the render and edit workers)
picks up every source in the "waiting" state whose file is complete, at
startup and whenever an upload finishes, and lines each up:

- the recording's sound, decoded at 8 kHz exactly as `sync.py` does today
  (the decode on tank2's ffmpeg was checked bit-identical to takeloom's,
  edit lists included);
- against `session.flac`, the Mix reference, and the sync clicks rebuilt
  from the log, so the clicks help every recording however late it is;
- the result goes into that source's `align` in the session log.

(The camera's live feed is already gone by then: it's deleted the moment
the phone's recording has arrived in full, when the upload completes.)

Measured cost per camera: about 0.6 s and 120 MB for a 5½-minute session;
per hour of recording, about 5 s of CPU (mostly reading through the phone
file) and 350 MB of memory, freed as soon as it's done. It runs at low
priority (nice), like the vault's encodes.

This adds numpy to takeloom-vault (a prebuilt wheel on tank2, no compiler).
The alignment code (`measure_alignment`, `gcc_phat_lag`, the decode and the
click reference) lives in the vault package. takeloom keeps a copy for a
vault-less setup ("Local" storage), where the Mac Mini lines up its own
sessions the same way, between sessions. A test runs both copies on the
same synthetic session and requires identical results.

### 5. Everything else is made on demand, from originals

| Need | Made from | Where | Cached? |
|---|---|---|---|
| Vault player (session page) | original video, plus `session.flac` and the Mix reference as the sound | browser | — |
| Take clip | original + `session.flac`/Mix, cut at the take's frames | vault | yes (`TakeVideoCache`) |
| Final edit / YouTube | originals, each with its alignment applied | vault (`edit.py`) | `edit.mp4` |
| Atmospheric audio | each original's mic, aligned | vault, first time it's asked for | yes |
| Exported song mix | `session.flac` (unchanged) | — | — |

- **ffmpeg users** (clips, edit, mic): an input placed by its alignment is
  `-itsscale (1+drift)` plus a start offset of `file_zero_s` (a seek when
  negative, `-itsoffset` when positive). These all re-encode already, so
  there's no extra generation.
- **The browser player**: video elements play the originals, muted. An
  `<audio>` element plays the session's sound and is the clock everything
  follows (today one camera's video is). Each video is kept at
  `(T − file_zero_s) / (1 + drift)` for session time T. It's corrected by
  nudging `playbackRate` (±0.1%, inaudible on a muted video) rather than
  by seeking, so drift of tens of ppm never shows. Seeks only happen past
  a 0.25 s error, as now.
- **The sound**: `session.flac` (the instrument; browsers play FLAC) or the
  Mix reference, which becomes its own file, `session_mix.m4a` (AAC,
  reference only, as decided: never lossless). It's written once by
  processing instead of being muxed into every camera video.

### 5a. No main camera

Cameras are a set of devices that may or may not be there for any given
session; none is ever singled out. Everything that leans on one today
stops doing so:

- **Sound** never comes from a camera: the player, take clips and the edit
  all use the session's own audio (`session.flac`, the Mix reference, or
  the phones' mics together as Atmospheric).
- **The editor's camera ranking** ("priority", whose first entry is
  treated as the main camera) becomes a share of screen time per camera,
  equal by default; the automatic cuts spread the picture by those shares.
- **The edit's frame size** is the largest of the session's cameras (or a
  setting), not one camera's.
- **A stretch whose camera has no picture** (it started late or stopped
  early) goes to whichever camera has picture there, by share, not to a
  fixed one.
- **A take clip's camera**: whichever the person asks for. When they
  don't, the one that was on the program feed when the take started. That
  is a fact about the session, not a ranking.
- **The player's clock**: the session's audio (5), never a camera.
- The words "main camera", "primary", "priority-1" go from the code, the
  vault's pages, and the docs (`docs/takeloom-camera.md`,
  `vault_server/README.md`). Saved editor plans with a "priority" list are
  read as equal shares.

### 6. What goes away

- muxed camera videos (`<cam>.mp4`) and raw intermediates (`<cam>_raw.mp4`)
  for new sessions: about 1.3 GB per camera per session;
- render jobs for phone cameras: `normalize`, `mic` and the per-camera
  `mux` (vault `render.py` keeps running old sessions' pending jobs);
- the late path: `_late_upload_target`, `recover_late_camera_uploads`,
  `realign_late_upload`, `late_camera_uploads/`;
- `camera_uploads/` staging on the vault, and `staged_inputs`;
- session processing waiting for phone uploads at all (`wait_for_upload`;
  the hub still tracks progress, for the status line);
- per-camera take video refs and the `.video.json` sidecars' per-camera
  fields (kept readable for old takes).

### 7. Old sessions

Sessions recorded before this keep their camera videos, refs and mic
files. Every reader (player, take clips, edit, take originals) checks
for `sources` in the camera entry and falls back to today's code when it
isn't there. Nothing is converted. Old sessions keep working exactly as
they do now.

## Order of work

1. **Session log and processing.** Add `sources`/`align` and a
   `session_mix.m4a` reference. Processing files takes immediately, records
   the live feeds as aligned placeholders, and stops muxing phone cameras.
   Takes get the slim video info.
2. **Alignment worker** on the vault (numpy), using the clicks for every
   source. The live feed is deleted when the phone's upload completes.
   Make the hub accept uploads for any known session, straight into the
   vault session folder.
3. **Vault readers**: player (session audio as the clock, `playbackRate` drift
   correction), take clips, edit render and Atmospheric mics, all from
   originals plus alignment, with the legacy fallback.
4. **Remove** the late path, staging, phone render jobs and processing's
   wait for uploads. The vault page lists each camera's current state (3a).
5. **Tests**: the synthetic sessions used so far (drift, clicks-only rooms,
   late arrival), a browser check of the player keeping three cameras in
   step for 10+ minutes, and old sessions from the real vault rendering
   unchanged.

## Decided

- A live feed is deleted as soon as its phone's recording has arrived in full.
- Nothing waits for an upload or gives up on one; the vault page shows each
  camera's current state.
- No camera is ever "main": the player follows the session's audio, and
  the editor gives cameras shares of screen time (5a).
- Lining up runs on the vault (4), which takes numpy for it: no network
  traffic, nothing on the Mac Mini, so it never gets in a recording's way.
- The edit's frame size is the largest of the session's cameras.
